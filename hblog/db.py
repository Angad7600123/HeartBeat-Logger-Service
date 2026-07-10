"""SQLite storage layer.

Tuned for a Raspberry Pi SD card: WAL journalling, batched commits, incremental
auto-vacuum, and age + size-capped retention. A single writer (the daemon) and
many concurrent readers (the CLI) are supported because WAL allows readers while a
writer is active.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .models import Event, Incident, ServiceStatus

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    unit       TEXT,
    priority   INTEGER NOT NULL,
    pid        INTEGER,
    boot_id    TEXT,
    message_id TEXT,
    kind       TEXT,
    exit_code  INTEGER,
    signal     INTEGER,
    source     TEXT,
    message    TEXT NOT NULL,
    extra      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts       ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_unit_ts  ON events(unit, ts);
CREATE INDEX IF NOT EXISTS idx_events_priority ON events(priority);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts  ON events(kind, ts);

CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    unit            TEXT NOT NULL,          -- '' for non-unit/global problems
    kind            TEXT NOT NULL,
    signature       TEXT NOT NULL,
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    count           INTEGER NOT NULL DEFAULT 1,
    exit_code       INTEGER,
    signal          INTEGER,
    sample_event_id INTEGER,
    status          TEXT NOT NULL DEFAULT 'open'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_incident_group
    ON incidents(unit, kind, signature);
CREATE INDEX IF NOT EXISTS idx_incident_last_seen ON incidents(last_seen);

CREATE TABLE IF NOT EXISTS service_status (
    unit           TEXT PRIMARY KEY,
    state          TEXT,
    sub_state      TEXT,
    result         TEXT,
    restart_count  INTEGER DEFAULT 0,
    last_heartbeat REAL DEFAULT 0,
    updated_at     REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    """Owns a SQLite connection. Use `read_only=True` for the CLI."""

    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = str(path)
        self.read_only = read_only
        if not read_only:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, timeout=30.0)
            self._apply_pragmas()
            self._migrate()
        else:
            # Fail cleanly if the DB does not exist yet rather than creating one.
            if not Path(self.path).exists():
                raise FileNotFoundError(f"database not found: {self.path}")
            uri = f"file:{Path(self.path).as_posix()}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        self.conn.row_factory = sqlite3.Row

    # -- setup ---------------------------------------------------------------

    def _apply_pragmas(self) -> None:
        cur = self.conn.cursor()
        # auto_vacuum must be set before the first table is created to take effect.
        cur.execute("PRAGMA auto_vacuum=INCREMENTAL")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        self.conn.commit()

    def _migrate(self) -> None:
        self.conn.executescript(_SCHEMA)
        cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- meta (cursor persistence etc.) --------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- writes (daemon only) ------------------------------------------------

    def write_batch(self, events: list[Event]) -> None:
        """Insert a batch of events and upsert incidents for problem events.

        All in one transaction so a crash leaves the DB consistent and we pay a
        single flush per batch instead of one per line.
        """
        if not events:
            return
        cur = self.conn.cursor()
        for ev in events:
            cur.execute(
                "INSERT INTO events"
                "(ts, unit, priority, pid, boot_id, message_id, kind,"
                " exit_code, signal, source, message, extra) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ev.ts, ev.unit, ev.priority, ev.pid, ev.boot_id,
                    ev.message_id, ev.kind, ev.exit_code, ev.signal, ev.source,
                    ev.message, json.dumps(ev.extra) if ev.extra else None,
                ),
            )
            if ev.is_problem:
                self._upsert_incident(cur, ev, cur.lastrowid)
        self.conn.commit()

    def _upsert_incident(self, cur: sqlite3.Cursor, ev: Event, event_id: int) -> None:
        signature = ev.extra.get("signature", ev.kind or "")
        unit = ev.unit or ""
        cur.execute(
            "INSERT INTO incidents"
            "(unit, kind, signature, first_seen, last_seen, count,"
            " exit_code, signal, sample_event_id, status) "
            "VALUES(?,?,?,?,?,1,?,?,?,'open') "
            "ON CONFLICT(unit, kind, signature) DO UPDATE SET "
            "  last_seen=MAX(last_seen, excluded.last_seen), "
            "  count=count+1, "
            "  exit_code=COALESCE(excluded.exit_code, exit_code), "
            "  signal=COALESCE(excluded.signal, signal), "
            "  status='open'",
            (unit, ev.kind, signature, ev.ts, ev.ts,
             ev.exit_code, ev.signal, event_id),
        )

    def upsert_service_status(self, s: ServiceStatus) -> None:
        self.conn.execute(
            "INSERT INTO service_status"
            "(unit, state, sub_state, result, restart_count, last_heartbeat, updated_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(unit) DO UPDATE SET "
            "  state=excluded.state, sub_state=excluded.sub_state, "
            "  result=excluded.result, restart_count=excluded.restart_count, "
            "  last_heartbeat=excluded.last_heartbeat, updated_at=excluded.updated_at",
            (s.unit, s.state, s.sub_state, s.result, s.restart_count,
             s.last_heartbeat, s.updated_at),
        )
        self.conn.commit()

    def prune_service_status(self, keep_units: list[str]) -> int:
        """Delete service_status rows for units no longer being watched, so a
        pruned watch list drops stale services. An empty keep list is treated as
        'watch everything' and prunes nothing."""
        if not keep_units:
            return 0
        placeholders = ",".join("?" for _ in keep_units)
        cur = self.conn.execute(
            f"DELETE FROM service_status WHERE unit NOT IN ({placeholders})",
            tuple(keep_units),
        )
        self.conn.commit()
        return cur.rowcount

    def resolve_incidents_for_unit(self, unit: str, kinds: tuple[str, ...]) -> None:
        """Mark open incidents of the given kinds resolved (e.g. when a unit is
        healthy again). Keeps history but drops them from the 'open' view."""
        q = ",".join("?" for _ in kinds)
        self.conn.execute(
            f"UPDATE incidents SET status='resolved' "
            f"WHERE unit=? AND status='open' AND kind IN ({q})",
            (unit or "", *kinds),
        )
        self.conn.commit()

    # -- retention -----------------------------------------------------------

    def prune(self, retention_days: float, incident_retention_days: float,
              max_db_mb: float) -> dict:
        """Prune old events (by age, then by size cap) and old incidents, then run
        an incremental vacuum. Returns a small report dict for logging/tests."""
        now = time.time()
        cur = self.conn.cursor()

        cutoff = now - retention_days * 86400
        cur.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        by_age = cur.rowcount

        cur.execute(
            "DELETE FROM incidents WHERE last_seen < ? AND status='resolved'",
            (now - incident_retention_days * 86400,),
        )
        incidents_pruned = cur.rowcount
        self.conn.commit()

        by_size = 0
        max_bytes = max_db_mb * 1024 * 1024
        while self._db_size_bytes() > max_bytes:
            # Delete the oldest slice of events until under the cap.
            cur.execute(
                "DELETE FROM events WHERE id IN "
                "(SELECT id FROM events ORDER BY ts ASC LIMIT 1000)"
            )
            if cur.rowcount == 0:
                break  # nothing left to delete
            by_size += cur.rowcount
            self.conn.commit()
            self.conn.execute("PRAGMA incremental_vacuum")
            self.conn.commit()

        self.conn.execute("PRAGMA incremental_vacuum")
        self.conn.commit()
        return {
            "events_pruned_age": by_age,
            "events_pruned_size": by_size,
            "incidents_pruned": incidents_pruned,
        }

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")
        self.conn.commit()

    def _db_size_bytes(self) -> int:
        page_count = self.conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        return page_count * page_size

    # -- reads (CLI) ---------------------------------------------------------

    def query_events(self, *, unit: str | None = None, since: float | None = None,
                     max_priority: int | None = None, kind: str | None = None,
                     grep: str | None = None, limit: int = 200,
                     ascending: bool = False) -> list[sqlite3.Row]:
        clauses, params = [], []
        if unit:
            clauses.append("unit = ?"); params.append(unit)
        if since is not None:
            clauses.append("ts >= ?"); params.append(since)
        if max_priority is not None:
            clauses.append("priority <= ?"); params.append(max_priority)
        if kind is not None:
            clauses.append("kind = ?"); params.append(kind)
        if grep:
            clauses.append("message LIKE ?"); params.append(f"%{grep}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "ASC" if ascending else "DESC"
        params.append(limit)
        return self.conn.execute(
            f"SELECT * FROM events {where} ORDER BY ts {order} LIMIT ?", params
        ).fetchall()

    def list_crashes(self, *, unit: str | None = None, since: float | None = None,
                     limit: int = 100) -> list[sqlite3.Row]:
        crash_kinds = ("crash", "oom", "failed")
        q = ",".join("?" for _ in crash_kinds)
        clauses = [f"kind IN ({q})"]
        params: list = list(crash_kinds)
        if unit:
            clauses.append("unit = ?"); params.append(unit)
        if since is not None:
            clauses.append("ts >= ?"); params.append(since)
        params.append(limit)
        return self.conn.execute(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} "
            f"ORDER BY ts DESC LIMIT ?", params
        ).fetchall()

    def list_incidents(self, *, unit: str | None = None, open_only: bool = False,
                       limit: int = 100) -> list[sqlite3.Row]:
        clauses, params = [], []
        if unit:
            clauses.append("unit = ?"); params.append(unit)
        if open_only:
            clauses.append("status = 'open'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self.conn.execute(
            f"SELECT * FROM incidents {where} ORDER BY last_seen DESC LIMIT ?", params
        ).fetchall()

    def service_statuses(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM service_status ORDER BY unit"
        ).fetchall()

    def distinct_units(self) -> list[str]:
        """Units seen in events or incidents (used by `status` before the unit
        monitor has populated service_status)."""
        rows = self.conn.execute(
            "SELECT DISTINCT unit FROM events WHERE unit IS NOT NULL AND unit != '' "
            "UNION SELECT DISTINCT unit FROM incidents WHERE unit != '' "
            "ORDER BY unit"
        ).fetchall()
        return [r[0] for r in rows]

    def error_counts(self, since: float, max_priority: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT unit, COUNT(*) AS n FROM events "
            "WHERE ts >= ? AND priority <= ? GROUP BY unit ORDER BY n DESC",
            (since, max_priority),
        ).fetchall()

    def unit_last_crash(self, unit: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM events WHERE unit=? AND kind IN ('crash','oom','failed') "
            "ORDER BY ts DESC LIMIT 1",
            (unit,),
        ).fetchone()

    def unit_error_count(self, unit: str, since: float, max_priority: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE unit=? AND ts>=? AND priority<=?",
            (unit, since, max_priority),
        ).fetchone()[0]
