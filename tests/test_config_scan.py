from hblog.config import (
    Config,
    format_watch_units,
    load,
    write_watch_units,
)
from hblog.sources.units import discover_services


def test_format_watch_units():
    assert format_watch_units([]) == "watch_units = []"
    out = format_watch_units(["a.service", "b.service"])
    assert out == 'watch_units = [\n    "a.service",\n    "b.service",\n]'


def test_write_creates_new_file_from_template(tmp_path):
    p = tmp_path / "etc" / "config.toml"
    path, created = write_watch_units(str(p), ["b.service", "a.service"])
    assert created is True
    assert p.exists()
    cfg = load(str(p))
    # sorted + de-duplicated
    assert cfg.watch_units == ["a.service", "b.service"]
    # template defaults are present and valid
    assert cfg.db_path == "/var/lib/heartbeat-logger/hblog.db"
    assert "systemd-*" in cfg.exclude_units
    assert cfg.retention_days == 14.0


def test_surgical_replace_preserves_other_keys_and_comments(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "# my hand-written config\n"
        'db_path = "/custom/hblog.db"\n'
        "watch_units = []\n"
        'exclude_units = ["systemd-*"]\n'
        "retention_days = 30.0  # keep a month\n",
        encoding="utf-8",
    )
    path, created = write_watch_units(str(p), ["x.service"])
    assert created is False
    text = p.read_text(encoding="utf-8")
    assert '"x.service"' in text
    assert "# my hand-written config" in text     # top comment preserved
    assert "# keep a month" in text               # inline comment preserved
    cfg = load(str(p))
    assert cfg.watch_units == ["x.service"]
    assert cfg.db_path == "/custom/hblog.db"       # other keys untouched
    assert cfg.retention_days == 30.0


def test_replace_multiline_watch_units(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'watch_units = [\n    "old1.service",\n    "old2.service",\n]\n'
        "batch_size = 50\n",
        encoding="utf-8",
    )
    write_watch_units(str(p), ["new.service"])
    cfg = load(str(p))
    assert cfg.watch_units == ["new.service"]
    assert cfg.batch_size == 50                    # trailing key survives replace


def test_write_appends_when_key_absent(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('db_path = "/x/hblog.db"\n', encoding="utf-8")
    write_watch_units(str(p), ["a.service"])
    cfg = load(str(p))
    assert cfg.watch_units == ["a.service"]
    assert cfg.db_path == "/x/hblog.db"


def test_discover_services_filters_sorts_dedupes():
    def fake_list():
        return (
            "b.service loaded active running\n"
            "a.service loaded active exited\n"
            "foo.timer  loaded active\n"
            "a.service loaded active exited\n"
            "wifibroadcast@drone.service loaded active running\n"
        )
    assert discover_services(list_runner=fake_list) == [
        "a.service", "b.service", "wifibroadcast@drone.service",
    ]
