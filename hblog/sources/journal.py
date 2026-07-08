"""journald reader.

`parse_journal_entry` converts a journal entry dict (as produced by
`journalctl -o json` or the `systemd.journal.Reader`) into an Event. It needs no
systemd import, so it is used both by the live reader here and by MockSource for
off-Pi replay.

`JournalSource` is the live collector; it imports `systemd.journal` lazily so the
module imports fine on Windows (the import only happens when you actually start
the reader on the Pi).
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from ..models import Event, Severity
from .base import Source


def _decode_message(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, list):  # journald byte array
        try:
            return bytes(value).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return str(value)
    return "" if value is None else str(value)


def _pick_unit(data: dict) -> str | None:
    # For systemd's own messages about a unit (e.g. "Main process exited"), the
    # affected unit is in UNIT, while _SYSTEMD_UNIT is systemd's own scope.
    ident = data.get("SYSLOG_IDENTIFIER")
    if ident == "systemd" and data.get("UNIT"):
        return data["UNIT"]
    return data.get("_SYSTEMD_UNIT") or data.get("UNIT") or data.get("USER_UNIT")


def parse_journal_entry(data: dict, *, source: str = "journal") -> Event:
    ts_us = data.get("__REALTIME_TIMESTAMP")
    if ts_us is not None:
        try:
            ts = int(ts_us) / 1_000_000
        except (ValueError, TypeError):
            ts = time.time()
    else:
        ts = time.time()

    try:
        priority = int(data.get("PRIORITY", Severity.INFO))
    except (ValueError, TypeError):
        priority = int(Severity.INFO)

    pid = data.get("_PID")
    try:
        pid = int(pid) if pid is not None else None
    except (ValueError, TypeError):
        pid = None

    return Event(
        ts=ts,
        message=_decode_message(data.get("MESSAGE", "")),
        priority=priority,
        unit=_pick_unit(data),
        pid=pid,
        boot_id=data.get("_BOOT_ID"),
        message_id=data.get("MESSAGE_ID"),
        source=source,
    )


class JournalSource(Source):
    """Follows the systemd journal from a persisted cursor (Pi only)."""

    name = "journal"

    def __init__(self, cursor: str | None = None, poll_ms: int = 1000):
        self.cursor = cursor
        self.poll_ms = poll_ms
        self.last_cursor = cursor
        self._reader = None

    def _open(self):
        from systemd import journal  # lazy: only available on the Pi

        reader = journal.Reader()
        reader.this_boot()
        if self.cursor:
            try:
                reader.seek_cursor(self.cursor)
                reader.get_next()  # skip the entry we already processed
            except Exception:
                reader.seek_tail()
                reader.get_previous()
        else:
            reader.seek_tail()
            reader.get_previous()
        self._reader = reader
        return reader

    def events(self) -> Iterator[Event]:
        from systemd import journal

        reader = self._open()
        while True:
            for entry in reader:
                self.last_cursor = entry.get("__CURSOR")
                ev = parse_journal_entry(_normalize_native(entry))
                if self.last_cursor:
                    ev.extra["cursor"] = self.last_cursor
                yield ev
            # No new entries: wait for more.
            reader.wait(self.poll_ms / 1000)

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()


def _normalize_native(entry) -> dict:
    """The native Reader returns datetimes/ints for some fields; coerce the few we
    read into the string-ish shapes `parse_journal_entry` expects."""
    data = dict(entry)
    rt = data.get("__REALTIME_TIMESTAMP")
    if rt is not None and hasattr(rt, "timestamp"):
        data["__REALTIME_TIMESTAMP"] = int(rt.timestamp() * 1_000_000)
    return data
