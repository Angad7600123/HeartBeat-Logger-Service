"""Pipeline: Source -> classify -> batched DB writes.

Owns the batching/commit policy (flush on batch size or elapsed time) and journal
cursor persistence. The daemon drives this from a queue fed by one thread per
source; tests and the `demo` command drive it directly with `run_source`.
"""

from __future__ import annotations

import time

from .classify import classify
from .config import Config
from .db import Database
from .models import Event
from .sources.base import Source


class Pipeline:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self._buffer: list[Event] = []
        self._last_flush = time.monotonic()
        self._pending_cursor: str | None = None

    def submit(self, ev: Event) -> None:
        """Classify and buffer one event (dropping unwatched units)."""
        if not self.config.unit_is_watched(ev.unit):
            return
        classify(ev, error_priority=self.config.error_priority)
        self._buffer.append(ev)
        cursor = ev.extra.get("cursor")
        if cursor:
            self._pending_cursor = cursor
        if len(self._buffer) >= self.config.batch_size:
            self.flush()

    def maybe_flush(self) -> None:
        """Flush if the flush interval has elapsed (call periodically when idle)."""
        if self._buffer and (
            time.monotonic() - self._last_flush >= self.config.flush_interval_sec
        ):
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        self.db.write_batch(self._buffer)
        if self._pending_cursor:
            self.db.set_meta("journal_cursor", self._pending_cursor)
            self.db.conn.commit()
            self._pending_cursor = None
        self._buffer.clear()
        self._last_flush = time.monotonic()

    def run_source(self, source: Source) -> int:
        """Consume a finite source to completion, flushing at the end. Returns the
        number of events submitted. Intended for tests/demo, not the live daemon."""
        n = 0
        for ev in source.events():
            self.submit(ev)
            n += 1
        self.flush()
        return n
