"""Configuration loading and defaults.

Config is a plain dataclass so tests can build one directly. `load()` reads a TOML
file (stdlib tomllib) and overlays it on the defaults, validating as it goes.
"""

from __future__ import annotations

import re
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


# ---------------------------------------------------------------------------
# Writing the watch list (used only by `hblog scan`).
#
# `write_watch_units` is the single writer of the config file. It rewrites *only*
# the `watch_units` array, leaving every other key and comment untouched, so the
# rest of the file stays fully user-owned. The daemon never writes the config.
# ---------------------------------------------------------------------------

# Template used only when no config file exists yet. Kept in sync with
# config/config.example.toml. The __WATCH_UNITS__ marker is replaced with the
# discovered service list.
DEFAULT_CONFIG_TEMPLATE = '''# HeartBeat Logger configuration.
# The watch_units list below was written by `hblog scan`. All keys are optional;
# omitted keys fall back to built-in defaults.

# --- storage ---------------------------------------------------------------
db_path = "/var/lib/heartbeat-logger/hblog.db"

# --- which units to watch --------------------------------------------------
# watch_units was populated by `hblog scan` with the services found on this
# system. Remove the ones you do NOT want tracked. Re-running `hblog scan`
# rewrites this list; nothing else ever modifies it.
# exclude_units is always applied, even to services listed above (prefix globs
# ending in '*' are supported).
__WATCH_UNITS__
exclude_units = [
    "systemd-*",
    "user@*",
    "session-*",
]

# --- unit-state monitor ----------------------------------------------------
poll_interval_sec = 15.0

# --- write batching (fewer SD-card flushes) --------------------------------
batch_size = 200
flush_interval_sec = 5.0

# --- problem detection -----------------------------------------------------
error_priority = 3
restart_loop_threshold = 5
restart_loop_window_sec = 120.0

# --- retention (SD-card-wear aware) ----------------------------------------
retention_days = 14.0
incident_retention_days = 90.0
max_db_mb = 256.0
maintenance_interval_sec = 3600.0

# --- daemon ----------------------------------------------------------------
watchdog = true
'''

_WATCH_START = re.compile(r"(?m)^[ \t]*watch_units[ \t]*=[ \t]*\[")


def format_watch_units(units: list[str]) -> str:
    """Render a `watch_units = [...]` TOML assignment."""
    if not units:
        return "watch_units = []"
    body = "".join(f'    "{u}",\n' for u in units)
    return f"watch_units = [\n{body}]"


def _replace_watch_units(text: str, assignment: str) -> tuple[str, bool]:
    """Replace the existing `watch_units = [ ... ]` span (which may span multiple
    lines) with `assignment`, preserving everything else. Returns (text, replaced)."""
    m = _WATCH_START.search(text)
    if not m:
        return text, False
    i = m.end() - 1  # index of the opening '['
    depth = 0
    j = i
    while j < len(text):
        if text[j] == "[":
            depth += 1
        elif text[j] == "]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    end = j + 1  # include the closing ']'
    return text[: m.start()] + assignment + text[end:], True


def write_watch_units(path: str | Path, units: list[str]) -> tuple[str, bool]:
    """Write `units` into the config's watch_units list, preserving the rest of the
    file. Creates the file from the template if it does not exist. Returns
    (path, created)."""
    p = Path(path)
    assignment = format_watch_units(sorted(set(units)))
    if p.exists():
        text = p.read_text(encoding="utf-8")
        new_text, replaced = _replace_watch_units(text, assignment)
        if not replaced:
            new_text = text.rstrip("\n") + "\n\n" + assignment + "\n"
        p.write_text(new_text, encoding="utf-8")
        return str(p), False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(DEFAULT_CONFIG_TEMPLATE.replace("__WATCH_UNITS__", assignment),
                 encoding="utf-8")
    return str(p), True
