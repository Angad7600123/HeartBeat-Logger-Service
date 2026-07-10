import time

import pytest

from hblog.classify import classify
from hblog.db import Database
from hblog.models import Event, Severity, ServiceStatus


def mk(msg, ts, priority=Severity.INFO, unit=None, **kw):
    return classify(Event(ts=ts, message=msg, priority=int(priority), unit=unit, **kw))


def test_schema_and_wal(tmp_path):
    db = Database(tmp_path / "h.db")
    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    assert db.get_meta("schema_version") == "1"
    db.close()


def test_write_and_query(tmp_path):
    db = Database(tmp_path / "h.db")
    now = time.time()
    db.write_batch([
        mk("hello", now, unit="a.service"),
        mk("disk failed", now, Severity.ERR, unit="a.service"),
    ])
    rows = db.query_events(unit="a.service")
    assert len(rows) == 2
    errs = db.query_events(max_priority=int(Severity.ERR))
    assert len(errs) == 1
    db.close()


def test_incident_grouping(tmp_path):
    db = Database(tmp_path / "h.db")
    now = time.time()
    batch = [mk(f"connection to 10.0.0.{i} failed", now + i, Severity.ERR,
               unit="app.service") for i in range(5)]
    db.write_batch(batch)
    incidents = db.list_incidents()
    assert len(incidents) == 1
    assert incidents[0]["count"] == 5
    assert incidents[0]["unit"] == "app.service"
    db.close()


def test_crash_incident_keyed_on_exit_code(tmp_path):
    db = Database(tmp_path / "h.db")
    now = time.time()
    db.write_batch([
        mk("app.service: Main process exited, code=exited, status=1/FAILURE",
           now + i, Severity.WARNING, unit="app.service") for i in range(3)
    ])
    inc = db.list_incidents()
    assert len(inc) == 1
    assert inc[0]["kind"] == "crash"
    assert inc[0]["count"] == 3
    assert inc[0]["exit_code"] == 1
    db.close()


def test_retention_by_age(tmp_path):
    db = Database(tmp_path / "h.db")
    now = time.time()
    old = now - 40 * 86400
    db.write_batch([mk("old", old, unit="a"), mk("new", now, unit="a")])
    report = db.prune(retention_days=14, incident_retention_days=90, max_db_mb=1024)
    assert report["events_pruned_age"] == 1
    assert len(db.query_events()) == 1
    db.close()


def test_retention_by_size_cap(tmp_path):
    db = Database(tmp_path / "h.db")
    now = time.time()
    # Insert enough rows to exceed a tiny size cap.
    big = "x" * 500
    db.write_batch([mk(big, now + i, unit="a") for i in range(4000)])
    before = len(db.query_events(limit=100000))
    report = db.prune(retention_days=999, incident_retention_days=999, max_db_mb=0.2)
    after = len(db.query_events(limit=100000))
    assert report["events_pruned_size"] > 0
    assert after < before
    db.close()


def test_cursor_meta_roundtrip(tmp_path):
    db = Database(tmp_path / "h.db")
    db.set_meta("journal_cursor", "s=abc;i=1")
    db.conn.commit()
    db.close()
    db2 = Database(tmp_path / "h.db")
    assert db2.get_meta("journal_cursor") == "s=abc;i=1"
    db2.close()


def test_service_status_upsert(tmp_path):
    db = Database(tmp_path / "h.db")
    now = time.time()
    db.upsert_service_status(ServiceStatus("a.service", state="active",
                                           sub_state="running", last_heartbeat=now,
                                           updated_at=now))
    db.upsert_service_status(ServiceStatus("a.service", state="failed",
                                           restart_count=3, updated_at=now))
    rows = db.service_statuses()
    assert len(rows) == 1
    assert rows[0]["state"] == "failed"
    assert rows[0]["restart_count"] == 3
    db.close()


def test_prune_service_status(tmp_path):
    db = Database(tmp_path / "h.db")
    now = time.time()
    for u in ("a.service", "b.service", "c.service"):
        db.upsert_service_status(ServiceStatus(u, state="active", updated_at=now))
    removed = db.prune_service_status(["a.service", "c.service"])
    assert removed == 1
    remaining = {r["unit"] for r in db.service_statuses()}
    assert remaining == {"a.service", "c.service"}
    # empty keep list = "watch all" => prunes nothing
    assert db.prune_service_status([]) == 0
    assert len(db.service_statuses()) == 2
    db.close()


def test_readonly_refuses_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        Database(tmp_path / "nope.db", read_only=True)


def test_readonly_can_read(tmp_path):
    p = tmp_path / "h.db"
    db = Database(p)
    db.write_batch([mk("hi", time.time(), unit="a")])
    db.close()
    ro = Database(p, read_only=True)
    assert len(ro.query_events()) == 1
    ro.close()
