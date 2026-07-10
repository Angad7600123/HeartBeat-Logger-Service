"""hblogd — the collector daemon.

Runs on the Pi as a systemd service. It:
  * follows the journal in a background thread (cursor-resumable),
  * polls systemd unit state on an interval (crash/oom/failed/restart-loop + heartbeat),
  * classifies + batches everything into SQLite,
  * prunes/vacuums on a schedule,
  * pings the systemd watchdog (sd_notify) so systemd restarts it if it hangs.

Designed to degrade gracefully: if the journal binding is missing it keeps running
with just the unit monitor, and vice-versa.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import threading
import time
from pathlib import Path

from . import __version__
from .config import Config, load as load_config
from .db import Database
from .models import (
    KIND_CRASH,
    KIND_FAILED,
    KIND_OOM,
    KIND_RESTART_LOOP,
    Event,
)
from .pipeline import Pipeline
from .sources.units import UnitMonitor

log = logging.getLogger("hblogd")

DEFAULT_CONFIG_PATHS = [
    os.environ.get("HBLOG_CONFIG", ""),
    "/etc/heartbeat-logger/config.toml",
]
_RESOLVE_KINDS = (KIND_CRASH, KIND_FAILED, KIND_OOM, KIND_RESTART_LOOP)


class Daemon:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.db_path)
        self.pipeline = Pipeline(self.db, config)
        self.monitor = UnitMonitor(config)
        self._q: queue.Queue[Event] = queue.Queue(maxsize=10000)
        self._stop = threading.Event()
        self._notifier = _Notifier(enabled=config.watchdog)
        self._last_poll = 0.0
        self._last_maint = time.monotonic()

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> int:
        log.info("hblogd %s starting (db=%s)", __version__, self.config.db_path)
        self._install_signals()

        # Read the persisted cursor here, on the main thread. The SQLite
        # connection is single-threaded (check_same_thread), so the journal
        # worker must never touch self.db — it resumes from this cursor and
        # tracks its own progress across reconnects. Cursor persistence happens
        # back on the main thread via the pipeline flush.
        initial_cursor = self.db.get_meta("journal_cursor")
        jt = threading.Thread(
            target=self._journal_worker, args=(initial_cursor,),
            name="journal", daemon=True,
        )
        jt.start()

        self._notifier.ready()
        try:
            self._main_loop()
        finally:
            self.pipeline.flush()
            self.db.close()
            log.info("hblogd stopped")
        return 0

    def _install_signals(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self._stop.set())
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass

    def _main_loop(self) -> None:
        while not self._stop.is_set():
            self._drain_queue()
            self._maybe_poll_units()
            self.pipeline.maybe_flush()
            self._maybe_maintain()
            self._notifier.ping()

    # -- journal -------------------------------------------------------------

    def _journal_worker(self, cursor: str | None) -> None:
        try:
            from .sources.journal import JournalSource
        except Exception as e:  # pragma: no cover - Pi-only path
            log.warning("journal source unavailable (%s); running monitor-only", e)
            return
        log.info("journal reader started (%s)", "resuming" if cursor else "from tail")
        while not self._stop.is_set():
            try:
                src = JournalSource(cursor=cursor)
                for ev in src.events():
                    if self._stop.is_set():
                        break
                    self._q.put(ev)
                    # Remember progress locally so a reconnect resumes cleanly
                    # without re-reading from the tail (no DB access from here).
                    cursor = ev.extra.get("cursor", cursor)
            except Exception:  # pragma: no cover - Pi-only path
                log.exception("journal reader crashed; retrying in 5s")
                self._stop.wait(5)

    def _drain_queue(self) -> None:
        deadline = time.monotonic() + 1.0
        try:
            ev = self._q.get(timeout=1.0)
        except queue.Empty:
            return
        self.pipeline.submit(ev)
        # Opportunistically drain a bit more without blocking, so bursts batch well.
        while time.monotonic() < deadline:
            try:
                self.pipeline.submit(self._q.get_nowait())
            except queue.Empty:
                break

    # -- unit monitor --------------------------------------------------------

    def _maybe_poll_units(self) -> None:
        if time.monotonic() - self._last_poll < self.config.poll_interval_sec:
            return
        self._last_poll = time.monotonic()
        try:
            events, statuses = self.monitor.poll()
        except Exception:
            log.exception("unit monitor poll failed")
            return
        for s in statuses:
            self.db.upsert_service_status(s)
        for ev in events:
            if ev.extra.get("recovery"):
                self.db.resolve_incidents_for_unit(ev.unit, _RESOLVE_KINDS)
            self.pipeline.submit(ev)
        if events:
            self.pipeline.flush()

    # -- maintenance ---------------------------------------------------------

    def _maybe_maintain(self) -> None:
        if time.monotonic() - self._last_maint < self.config.maintenance_interval_sec:
            return
        self._last_maint = time.monotonic()
        try:
            report = self.db.prune(
                self.config.retention_days,
                self.config.incident_retention_days,
                self.config.max_db_mb,
            )
            if any(report.values()):
                log.info("maintenance: %s", report)
        except Exception:
            log.exception("maintenance failed")


class _Notifier:
    """Thin wrapper over sd_notify; a no-op when systemd isn't present."""

    def __init__(self, enabled: bool):
        self._notify = None
        self._interval = 0.0
        self._last = 0.0
        if not enabled:
            return
        try:
            from systemd import daemon as sd  # type: ignore
            self._notify = sd.notify
            usec = os.environ.get("WATCHDOG_USEC")
            if usec:
                self._interval = int(usec) / 1_000_000 / 2  # ping at half the timeout
        except Exception:
            self._notify = None

    def ready(self) -> None:
        if self._notify:
            self._notify("READY=1")

    def ping(self) -> None:
        if not self._notify or self._interval <= 0:
            return
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._notify("WATCHDOG=1")
            self._last = now


def _resolve_config(path: str | None) -> Config:
    if path:
        return load_config(path)
    for p in DEFAULT_CONFIG_PATHS:
        if p and Path(p).exists():
            return load_config(p)
    return Config()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hblogd", description="HeartBeat Logger daemon")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--db", help="override db path")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = _resolve_config(args.config)
    if args.db:
        config.db_path = args.db
    config.validate()
    return Daemon(config).run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
