"""Configuration loading and defaults.

Config is a plain dataclass so tests can build one directly. `load()` reads a TOML
file (stdlib tomllib) and overlays it on the defaults, validating as it goes.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .models import Severity

DEFAULT_DB_PATH = "/var/lib/heartbeat-logger/hblog.db"


@dataclass
class Config:
    # Storage
    db_path: str = DEFAULT_DB_PATH

    # Which units to watch. Empty watch_units means "all units seen in the journal";
    # exclude_units is always applied (supports simple '*' suffix globs).
    watch_units: list[str] = field(default_factory=list)
    exclude_units: list[str] = field(default_factory=list)

    # Unit-state monitor
    poll_interval_sec: float = 15.0

    # Write batching (fewer SD-card flushes)
    batch_size: int = 200
    flush_interval_sec: float = 5.0

    # Problem detection
    error_priority: int = int(Severity.ERR)   # priority <= this counts as an error
    restart_loop_threshold: int = 5           # restarts...
    restart_loop_window_sec: float = 120.0    # ...within this window => restart_loop

    # Retention (SD-card-wear aware)
    retention_days: float = 14.0              # raw events pruned after this
    incident_retention_days: float = 90.0     # incidents kept longer
    max_db_mb: float = 256.0                  # hard cap; oldest events pruned first
    maintenance_interval_sec: float = 3600.0  # how often the daemon prunes/vacuums

    # Daemon
    watchdog: bool = True                     # enable sd_notify watchdog pings

    def validate(self) -> None:
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if not (0 <= self.error_priority <= 7):
            raise ValueError("error_priority must be a syslog priority 0-7")
        if self.retention_days <= 0 or self.max_db_mb <= 0:
            raise ValueError("retention_days and max_db_mb must be > 0")
        if self.poll_interval_sec <= 0 or self.flush_interval_sec <= 0:
            raise ValueError("interval settings must be > 0")

    def unit_is_watched(self, unit: str | None) -> bool:
        """Apply watch/exclude rules to a unit name."""
        if unit is None:
            return True  # non-unit events (e.g. kernel) still flow through
        for pat in self.exclude_units:
            if _match(pat, unit):
                return False
        if not self.watch_units:
            return True
        return any(_match(pat, unit) for pat in self.watch_units)


def _match(pattern: str, unit: str) -> bool:
    if pattern.endswith("*"):
        return unit.startswith(pattern[:-1])
    return pattern == unit


_KNOWN = {f.name for f in fields(Config)}


def load(path: str | Path) -> Config:
    """Load config from a TOML file, overlaying defaults. Unknown keys are ignored
    with the parsed data restricted to known fields to avoid silent typos crashing."""
    data = {}
    p = Path(path)
    if p.exists():
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    cfg = Config(**{k: v for k, v in data.items() if k in _KNOWN})
    cfg.validate()
    return cfg
