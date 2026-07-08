"""Mock / replay source for off-Pi development and tests.

Feed it a list of Events, or a list of journal-entry dicts (the same shape
`journalctl -o json` emits), or load such a list from a JSON file. This is what
makes the DB, classifier, pipeline and CLI fully exercisable on Windows.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

from ..models import Event, Severity
from .base import Source
from .journal import parse_journal_entry


class MockSource(Source):
    name = "mock"

    def __init__(self, events: list[Event] | None = None,
                 entries: list[dict] | None = None,
                 loop: bool = False, delay: float = 0.0):
        self._events = list(events or [])
        if entries:
            self._events.extend(parse_journal_entry(e, source="mock") for e in entries)
        self.loop = loop
        self.delay = delay

    @classmethod
    def from_json_file(cls, path: str | Path, **kw) -> "MockSource":
        entries = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(entries=entries, **kw)

    def events(self) -> Iterator[Event]:
        while True:
            for ev in self._events:
                if self.delay:
                    time.sleep(self.delay)
                yield ev
            if not self.loop:
                return


def sample_events(base_ts: float | None = None) -> list[Event]:
    """A realistic mixed stream: normal logs, an error burst, a crash-loop, an OOM.

    Used by tests and by `hblog demo` to populate a DB without a Pi.
    """
    t = base_ts if base_ts is not None else time.time() - 3600
    ev = []

    def add(dt, msg, priority=Severity.INFO, unit=None, ident=None, mid=None):
        d = {
            "__REALTIME_TIMESTAMP": int((t + dt) * 1_000_000),
            "MESSAGE": msg,
            "PRIORITY": str(int(priority)),
            "_BOOT_ID": "b0011223344556677",
        }
        if unit:
            d["_SYSTEMD_UNIT"] = unit
        if ident:
            d["SYSLOG_IDENTIFIER"] = ident
        if mid:
            d["MESSAGE_ID"] = mid
        ev.append(parse_journal_entry(d, source="mock"))

    add(0, "Started Nginx web server.", unit="nginx.service", ident="systemd")
    add(5, "Accepted connection from 10.0.0.4", unit="nginx.service")
    add(10, "GET /health 200", unit="nginx.service")

    # An error burst from an app (should group into one 'error' incident).
    for i in range(4):
        add(20 + i, f"Database connection to 10.0.0.{5 + i} failed: timeout",
            Severity.ERR, unit="myapp.service")

    # A crash loop: myapp exits non-zero and systemd reports it repeatedly.
    for i in range(3):
        add(40 + i * 3,
            "myapp.service: Main process exited, code=exited, status=1/FAILURE",
            Severity.WARNING, unit="myapp.service", ident="systemd")
        add(41 + i * 3, "myapp.service: Failed with result 'exit-code'.",
            Severity.WARNING, unit="myapp.service", ident="systemd")

    # An OOM kill of a hungry worker.
    add(70, "Out of memory: Killed process 2345 (worker) total-vm:...",
        Severity.ERR, ident="kernel")
    add(71, "worker.service: A process of this unit has been killed by the OOM killer.",
        Severity.WARNING, unit="worker.service", ident="systemd")

    # A segfault.
    add(80, "worker[2400]: segfault at 0 ip 00007f... error 4 in libc.so",
        Severity.ERR, ident="kernel")

    return ev
