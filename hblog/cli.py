"""hblog — command-line interface (read-only queries + maintenance).

Designed for use over SSH: plain aligned tables, no third-party deps. Query
commands open the DB read-only so they are safe to run while the daemon writes.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config, load as load_config
from .db import Database
from .models import Severity

DEFAULT_CONFIG_PATHS = [
    os.environ.get("HBLOG_CONFIG", ""),
    "/etc/heartbeat-logger/config.toml",
]

_SINCE_RE = re.compile(r"^(\d+)([smhd])$")
_SECS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since(text: str | None) -> float | None:
    """'24h' / '30m' / '7d' -> epoch cutoff. None -> None (no lower bound)."""
    if not text:
        return None
    m = _SINCE_RE.match(text.strip())
    if not m:
        raise SystemExit(f"invalid --since '{text}' (use e.g. 30m, 24h, 7d)")
    return time.time() - int(m.group(1)) * _SECS[m.group(2)]


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def fmt_age(ts: float | None) -> str:
    if not ts:
        return "-"
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("(none)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


def resolve_config(args) -> Config:
    if getattr(args, "config", None):
        return load_config(args.config)
    for p in DEFAULT_CONFIG_PATHS:
        if p and Path(p).exists():
            return load_config(p)
    return Config()


def resolve_db_path(args) -> str:
    if getattr(args, "db", None):
        return args.db
    return resolve_config(args).db_path


def resolve_config_path(args) -> str:
    """The path `scan` writes to: explicit --config, else the first default that
    exists, else the canonical /etc location."""
    if getattr(args, "config", None):
        return args.config
    for p in DEFAULT_CONFIG_PATHS:
        if p and Path(p).exists():
            return p
    return "/etc/heartbeat-logger/config.toml"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_status(args) -> int:
    cfg = resolve_config(args)
    db = Database(resolve_db_path(args), read_only=True)
    since = time.time() - 86400
    statuses = {r["unit"]: r for r in db.service_statuses()}
    units = sorted(set(statuses) | set(db.distinct_units()))
    # Only show currently-watched services. Pruning watch_units leaves stale rows
    # in the DB; honor the live config so status reflects what is actually tracked.
    units = [u for u in units if cfg.unit_is_watched(u)]
    rows = []
    for unit in units:
        st = statuses.get(unit)
        state = st["state"] if st else "-"
        restarts = st["restart_count"] if st else 0
        hb = fmt_age(st["last_heartbeat"]) if st else "-"
        crash = db.unit_last_crash(unit)
        errs = db.unit_error_count(unit, since, cfg.error_priority)
        health = _health_marker(state, crash, errs)
        rows.append([
            health, unit, state, hb, str(restarts),
            fmt_age(crash["ts"]) if crash else "-", str(errs),
        ])
    print_table(
        ["", "UNIT", "STATE", "HEARTBEAT", "RESTARTS", "LAST CRASH", "ERR/24h"],
        rows,
    )
    return 0


def _health_marker(state: str, crash, errs: int) -> str:
    if state == "failed" or (crash and time.time() - crash["ts"] < 3600):
        return "X"     # actively bad
    if errs > 0:
        return "!"     # errors but running
    return "OK"


def cmd_crashes(args) -> int:
    db = Database(resolve_db_path(args), read_only=True)
    since = parse_since(args.since)
    rows = []
    for r in db.list_crashes(unit=args.unit, since=since, limit=args.limit):
        detail = _crash_detail(r)
        rows.append([fmt_ts(r["ts"]), r["unit"] or "-", r["kind"], detail,
                     _truncate(r["message"], 60)])
    print_table(["TIME", "UNIT", "KIND", "DETAIL", "MESSAGE"], rows)
    return 0


def _crash_detail(r) -> str:
    if r["signal"] is not None:
        return f"signal {r['signal']}"
    if r["exit_code"] is not None:
        return f"exit {r['exit_code']}"
    return "-"


def cmd_incidents(args) -> int:
    db = Database(resolve_db_path(args), read_only=True)
    rows = []
    for r in db.list_incidents(unit=args.unit, open_only=args.open, limit=args.limit):
        rows.append([
            r["status"], r["unit"] or "-", r["kind"], str(r["count"]),
            fmt_ts(r["first_seen"]), fmt_age(r["last_seen"]),
            _truncate(r["signature"], 50),
        ])
    print_table(
        ["STATUS", "UNIT", "KIND", "COUNT", "FIRST SEEN", "LAST", "SIGNATURE"], rows
    )
    return 0


def cmd_logs(args) -> int:
    cfg = resolve_config(args)
    db = Database(resolve_db_path(args), read_only=True)
    since = parse_since(args.since)
    max_priority = _priority_from_name(args.priority) if args.priority else None
    if args.follow:
        return _follow_logs(db, args, since, max_priority)
    rows = db.query_events(
        unit=args.unit, since=since, max_priority=max_priority,
        grep=args.grep, limit=args.limit, ascending=True,
    )
    _print_log_rows(rows)
    return 0


def _follow_logs(db, args, since, max_priority) -> int:
    seen_since = since or (time.time() - 60)
    try:
        while True:
            rows = db.query_events(
                unit=args.unit, since=seen_since, max_priority=max_priority,
                grep=args.grep, limit=500, ascending=True,
            )
            if rows:
                _print_log_rows(rows)
                seen_since = rows[-1]["ts"] + 1e-6
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0


def _print_log_rows(rows) -> None:
    for r in rows:
        sev = Severity.label(r["priority"])
        unit = r["unit"] or "-"
        tag = f" [{r['kind']}]" if r["kind"] else ""
        print(f"{fmt_ts(r['ts'])}  {sev:7}  {unit}{tag}: {r['message']}")


def cmd_stats(args) -> int:
    cfg = resolve_config(args)
    db = Database(resolve_db_path(args), read_only=True)
    since = parse_since(args.since) or (time.time() - 86400)
    rows = [[r["unit"] or "-", str(r["n"])]
            for r in db.error_counts(since, cfg.error_priority)]
    print(f"Errors since {fmt_ts(since)} (priority <= {cfg.error_priority}):")
    print_table(["UNIT", "ERRORS"], rows)
    return 0


def cmd_prune(args) -> int:
    cfg = resolve_config(args)
    db = Database(resolve_db_path(args))
    report = db.prune(cfg.retention_days, cfg.incident_retention_days, cfg.max_db_mb)
    print("Pruned:", ", ".join(f"{k}={v}" for k, v in report.items()))
    return 0


def cmd_vacuum(args) -> int:
    db = Database(resolve_db_path(args))
    db.vacuum()
    print("VACUUM complete.")
    return 0


def cmd_scan(args) -> int:
    """Discover all services on the system and write them into the config's
    watch_units list, so the user can prune the ones they don't want tracked."""
    from .config import Config, load, write_watch_units
    from .sources.units import discover_services

    path = resolve_config_path(args)

    # Apply the existing exclude_units (or the defaults) so obvious noise
    # (systemd-*, etc.) isn't dumped into the list. watch_units itself is ignored
    # here — scan always writes the full current service set.
    try:
        excludes = load(path).exclude_units if Path(path).exists() else Config().exclude_units
    except Exception:
        excludes = Config().exclude_units
    exclude_filter = Config(exclude_units=excludes)

    try:
        discovered = discover_services()
    except FileNotFoundError:
        raise SystemExit("`scan` must be run on the Pi (systemctl was not found)")

    services = [s for s in discovered if exclude_filter.unit_is_watched(s)]
    if not services:
        raise SystemExit("no services discovered (is this a systemd system?)")

    try:
        cfg_path, created = write_watch_units(path, services)
    except PermissionError:
        raise SystemExit(
            f"permission denied writing {path}\n"
            "scan writes the system config, so run it as root, e.g.:\n"
            "  sudo /opt/heartbeat-logger/venv/bin/hblog scan"
        )

    verb = "Created" if created else "Updated"
    print(f"{verb} {cfg_path}: wrote {len(services)} services to watch_units.")
    print("Edit that file to remove any services you don't want tracked, then apply:")
    print("  sudo systemctl restart heartbeat-logger")
    return 0


def cmd_demo(args) -> int:
    """Populate a DB with synthetic events (no Pi needed) to try the CLI."""
    from .pipeline import Pipeline
    from .sources.mock import MockSource, sample_events

    cfg = resolve_config(args)
    db = Database(resolve_db_path(args))
    n = Pipeline(db, cfg).run_source(MockSource(events=sample_events()))
    print(f"Inserted {n} synthetic events into {db.path}")
    print("Try:  hblog --db <path> status   |   crashes   |   incidents")
    return 0


# --------------------------------------------------------------------------
# helpers + arg parsing
# --------------------------------------------------------------------------

def _truncate(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[: n - 3] + "..."


def _priority_from_name(name: str) -> int:
    name = name.strip().lower()
    if name.isdigit():
        return int(name)
    try:
        return int(Severity[name.upper()])
    except KeyError:
        raise SystemExit(f"unknown priority '{name}'")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hblog", description="HeartBeat Logger CLI")
    p.add_argument("--version", action="version", version=f"hblog {__version__}")
    p.add_argument("--db", help="path to hblog.db (overrides config)")
    p.add_argument("--config", help="path to config.toml")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="service health overview")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("crashes", help="recent crashes / failures / OOM kills")
    s.add_argument("--unit")
    s.add_argument("--since", help="e.g. 30m, 24h, 7d")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_crashes)

    s = sub.add_parser("incidents", help="grouped problems")
    s.add_argument("--unit")
    s.add_argument("--open", action="store_true", help="only open incidents")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_incidents)

    s = sub.add_parser("logs", help="search / follow stored logs")
    s.add_argument("--unit")
    s.add_argument("--since", help="e.g. 30m, 24h, 7d")
    s.add_argument("--priority", help="max severity: err/warning/... or 0-7")
    s.add_argument("--grep", help="substring match on message")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("-f", "--follow", action="store_true")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("stats", help="error counts per service")
    s.add_argument("--since", help="e.g. 24h, 7d")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser(
        "scan",
        help="discover all services and write them to watch_units in the config",
    )
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("prune", help="apply retention (age + size cap)")
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("vacuum", help="reclaim space (full VACUUM)")
    s.set_defaults(func=cmd_vacuum)

    s = sub.add_parser("demo", help="insert synthetic events (no Pi needed)")
    s.set_defaults(func=cmd_demo)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
