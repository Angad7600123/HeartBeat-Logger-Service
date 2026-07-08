import time

from hblog.config import Config
from hblog.db import Database
from hblog.pipeline import Pipeline
from hblog.sources.mock import MockSource, sample_events


def test_pipeline_end_to_end(tmp_path):
    db = Database(tmp_path / "h.db")
    cfg = Config(db_path=str(tmp_path / "h.db"), batch_size=10)
    pipe = Pipeline(db, cfg)
    n = pipe.run_source(MockSource(events=sample_events()))
    assert n > 0

    # Events landed.
    assert len(db.query_events(limit=1000)) == n

    # Distinct problem kinds were detected: error burst, crash loop, oom, segfault.
    incidents = db.list_incidents()
    kinds = {r["kind"] for r in incidents}
    assert "error" in kinds
    assert "crash" in kinds
    assert "oom" in kinds

    # The 3x crash loop (exit code 1) collapsed to a single incident, count 3.
    exit1 = [r for r in incidents
             if r["kind"] == "crash" and r["signature"] == "crash:exit=1"]
    assert len(exit1) == 1
    assert exit1[0]["count"] == 3
    assert exit1[0]["exit_code"] == 1
    db.close()


def test_pipeline_respects_exclude(tmp_path):
    db = Database(tmp_path / "h.db")
    cfg = Config(db_path=str(tmp_path / "h.db"), exclude_units=["nginx.service"])
    pipe = Pipeline(db, cfg)
    pipe.run_source(MockSource(events=sample_events()))
    assert db.query_events(unit="nginx.service") == []
    db.close()


def test_batching_flushes_remainder(tmp_path):
    db = Database(tmp_path / "h.db")
    cfg = Config(db_path=str(tmp_path / "h.db"), batch_size=1000)  # bigger than input
    pipe = Pipeline(db, cfg)
    events = sample_events()
    for e in events:
        pipe.submit(e)
    # Nothing forced a flush yet (buffer < batch_size); final flush persists all.
    pipe.flush()
    assert len(db.query_events(limit=1000)) == len(events)
    db.close()


def test_cursor_persisted(tmp_path):
    db = Database(tmp_path / "h.db")
    cfg = Config(db_path=str(tmp_path / "h.db"))
    pipe = Pipeline(db, cfg)
    events = sample_events()
    events[-1].extra["cursor"] = "s=deadbeef;i=99"
    for e in events:
        pipe.submit(e)
    pipe.flush()
    assert db.get_meta("journal_cursor") == "s=deadbeef;i=99"
    db.close()
