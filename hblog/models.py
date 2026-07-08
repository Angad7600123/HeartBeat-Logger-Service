"""Core data structures shared across collectors, classifier, DB and CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Severity(IntEnum):
    """syslog severities (a journal entry's PRIORITY field), low = more severe."""

    EMERG = 0
    ALERT = 1
    CRIT = 2
    ERR = 3
    WARNING = 4
    NOTICE = 5
    INFO = 6
    DEBUG = 7

    @classmethod
    def label(cls, priority: int) -> str:
        try:
            return cls(priority).name.lower()
        except ValueError:
            return str(priority)


# Problem "kinds" — the classifier tags events with one of these; None means an
# ordinary log line. Incidents are grouped per (unit, kind, signature).
KIND_ERROR = "error"          # priority <= ERR log line
KIND_CRASH = "crash"          # main process exited non-zero / abnormal
KIND_OOM = "oom"              # killed by the kernel OOM killer
KIND_FAILED = "failed"        # unit entered the systemd "failed" state
KIND_RESTART_LOOP = "restart_loop"  # repeated restarts in a short window

PROBLEM_KINDS = (KIND_ERROR, KIND_CRASH, KIND_OOM, KIND_FAILED, KIND_RESTART_LOOP)


@dataclass
class Event:
    """A single normalized event from any source.

    `kind` is None for ordinary log lines and is set to one of the KIND_* values
    by the classifier when the event represents a problem.
    """

    ts: float                       # epoch seconds (UTC)
    message: str
    priority: int = Severity.INFO   # syslog priority 0-7
    unit: str | None = None
    pid: int | None = None
    boot_id: str | None = None
    message_id: str | None = None   # systemd catalog MESSAGE_ID (hex GUID), if any
    source: str = "unknown"         # which collector produced it (journal/units/mock)

    # Classification output / crash metadata (filled by classify.py)
    kind: str | None = None
    exit_code: int | None = None
    signal: int | None = None

    # Extra structured fields, persisted as JSON for later inspection.
    extra: dict = field(default_factory=dict)

    @property
    def is_problem(self) -> bool:
        return self.kind is not None


@dataclass
class Incident:
    """A grouped, deduplicated problem (many events collapse into one incident)."""

    unit: str | None
    kind: str
    signature: str          # stable grouping key within (unit, kind)
    first_seen: float
    last_seen: float
    count: int = 1
    exit_code: int | None = None
    signal: int | None = None
    sample_event_id: int | None = None
    status: str = "open"    # "open" | "resolved"
    id: int | None = None


@dataclass
class ServiceStatus:
    """Latest known health snapshot for a watched unit (from the unit monitor)."""

    unit: str
    state: str = "unknown"          # ActiveState: active/inactive/failed/...
    sub_state: str = "unknown"      # SubState: running/dead/exited/...
    result: str = "success"         # Result: success/exit-code/oom-kill/signal/...
    restart_count: int = 0          # NRestarts
    last_heartbeat: float = 0.0     # epoch of the last successful liveness poll
    updated_at: float = 0.0
