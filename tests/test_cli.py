import time

from hblog.cli import main, parse_since
from hblog.config import Config
from hblog.db import Database
from hblog.pipeline import Pipeline
from hblog.sources.mock import MockSource, sample_events


def seed(tmp_path):
    p = tmp_path / "h.db"
    db = Database(p)
    Pipeline(db, Config(db_path=str(p))).run_source(MockSource(events=sample_events()))
    db.close()
    return str(p)


def test_parse_since():
    now = time.time()
    assert parse_since(None) is None
    assert abs(parse_since("1h") - (now - 3600)) < 2
    assert abs(parse_since("2d") - (now - 2 * 86400)) < 2


def test_cli_status(tmp_path, capsys):
    db = seed(tmp_path)
    rc = main(["--db", db, "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "UNIT" in out
    assert "myapp.service" in out


def test_cli_status_respects_watch_units(tmp_path, capsys):
    from hblog.db import Database
    from hblog.models import ServiceStatus

    p = tmp_path / "h.db"
    now = time.time()
    db = Database(p)
    db.upsert_service_status(ServiceStatus("a.service", state="active",
                                           last_heartbeat=now, updated_at=now))
    db.upsert_service_status(ServiceStatus("b.service", state="active",
                                           last_heartbeat=now, updated_at=now))
    db.close()

    cfg = tmp_path / "config.toml"
    cfg.write_text('watch_units = ["a.service"]\n', encoding="utf-8")

    rc = main(["--db", str(p), "--config", str(cfg), "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "a.service" in out
    assert "b.service" not in out       # pruned unit no longer shown


def test_cli_crashes(tmp_path, capsys):
    db = seed(tmp_path)
    rc = main(["--db", db, "crashes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "crash" in out or "oom" in out


def test_cli_incidents(tmp_path, capsys):
    db = seed(tmp_path)
    rc = main(["--db", db, "incidents"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "SIGNATURE" in out


def test_cli_logs_filter(tmp_path, capsys):
    db = seed(tmp_path)
    rc = main(["--db", db, "logs", "--priority", "err", "--grep", "Database"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Database connection" in out


def test_cli_stats(tmp_path, capsys):
    db = seed(tmp_path)
    rc = main(["--db", db, "stats"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ERRORS" in out


def test_cli_prune(tmp_path, capsys):
    db = seed(tmp_path)
    rc = main(["--db", db, "prune"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Pruned" in out


def test_cli_demo(tmp_path, capsys):
    p = str(tmp_path / "demo.db")
    rc = main(["--db", p, "demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "synthetic events" in out
