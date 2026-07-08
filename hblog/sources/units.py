"""systemd unit-state monitor.

Polls `systemctl show` for each watched service to get an authoritative health
snapshot — this catches a service that died silently or is crash-looping even when
it logged nothing. Each poll also records a per-service liveness heartbeat.

It is not a streaming `Source`: the daemon calls `poll()` on an interval, which
returns (problem events, status snapshots). The daemon feeds the events through
the normal classify->DB pipeline and writes the statuses.

The `systemctl` calls are injected (`show_runner` / `list_runner`) so the whole
monitor is unit-testable on Windows with canned output.
"""

from __future__ import annotations

import subprocess
import time
from collections import defaultdict, deque

from ..config import Config
from ..models import (
    KIND_CRASH,
    KIND_FAILED,
    KIND_OOM,
    KIND_RESTART_LOOP,
    Event,
    Severity,
    ServiceStatus,
)

_SHOW_PROPS = (
    "Id", "ActiveState", "SubState", "Result",
    "ExecMainCode", "ExecMainStatus", "NRestarts",
)

# siginfo si_code values reported by ExecMainCode
_CLD_EXITED = 1
_CLD_KILLED = 2
_CLD_DUMPED = 3


def _run_show(units: list[str]) -> str:  # pragma: no cover - needs systemd
    if not units:
        return ""
    cmd = ["systemctl", "show", "--no-pager",
           f"--property={','.join(_SHOW_PROPS)}", *units]
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def _run_list() -> str:  # pragma: no cover - needs systemd
    cmd = ["systemctl", "list-units", "--type=service", "--all",
           "--no-legend", "--plain", "--no-pager"]
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def parse_show(output: str) -> dict[str, dict]:
    """Parse `systemctl show` output (one or more Key=Value blocks separated by
    blank lines) into {unit_id: {prop: value}}."""
    blocks: dict[str, dict] = {}
    current: dict = {}
    for line in output.splitlines():
        if not line.strip():
            _commit(blocks, current)
            current = {}
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            current[k] = v
    _commit(blocks, current)
    return blocks


def _commit(blocks: dict, current: dict) -> None:
    if current and current.get("Id"):
        blocks[current["Id"]] = current


def parse_list(output: str) -> list[str]:
    units = []
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0].endswith(".service"):
            units.append(parts[0])
    return units


def _int(v: str | None) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class UnitMonitor:
    def __init__(self, config: Config, show_runner=_run_show, list_runner=_run_list,
                 now=time.time):
        self.config = config
        self._show = show_runner
        self._list = list_runner
        self._now = now
        self._prev: dict[str, dict] = {}        # unit -> last snapshot
        self._heartbeat: dict[str, float] = {}  # unit -> last seen active
        self._restarts: dict[str, deque] = defaultdict(deque)
        self._loop_active: dict[str, bool] = defaultdict(bool)

    def list_units(self) -> list[str]:
        units = parse_list(self._list())
        return [u for u in units if self.config.unit_is_watched(u)]

    def poll(self) -> tuple[list[Event], list[ServiceStatus]]:
        now = self._now()
        units = self.list_units()
        props = parse_show(self._show(units)) if units else {}
        events: list[Event] = []
        statuses: list[ServiceStatus] = []

        for unit in units:
            p = props.get(unit)
            if not p:
                continue
            state = p.get("ActiveState", "unknown")
            if state == "active":
                self._heartbeat[unit] = now

            status = ServiceStatus(
                unit=unit,
                state=state,
                sub_state=p.get("SubState", "unknown"),
                result=p.get("Result", "success"),
                restart_count=_int(p.get("NRestarts")) or 0,
                last_heartbeat=self._heartbeat.get(unit, 0.0),
                updated_at=now,
            )
            statuses.append(status)
            events.extend(self._detect(unit, p, state, now))
            self._prev[unit] = p

        return events, statuses

    # -- detection -----------------------------------------------------------

    def _detect(self, unit: str, p: dict, state: str, now: float) -> list[Event]:
        prev = self._prev.get(unit)
        prev_state = prev.get("ActiveState") if prev else None
        out: list[Event] = []

        # Recovery: a previously-failed unit is healthy again -> resolve incidents.
        if state == "active" and prev_state == "failed":
            self._loop_active[unit] = False
            out.append(self._recovery_marker(unit, now))

        # Failure edge: newly entered the failed state.
        if state == "failed" and prev_state != "failed":
            out.append(self._failure_event(unit, p, now))

        # Restart-loop edge: NRestarts crossed the threshold within the window.
        loop = self._check_restart_loop(unit, p, prev, now)
        if loop:
            out.append(loop)

        return out

    def _failure_event(self, unit: str, p: dict, now: float) -> Event:
        result = (p.get("Result") or "").lower()
        code = _int(p.get("ExecMainCode"))
        status = _int(p.get("ExecMainStatus"))

        kind = KIND_FAILED
        exit_code = signal = None
        detail = f"result '{result}'"
        if result == "oom-kill":
            kind = KIND_OOM
        elif code == _CLD_EXITED and status not in (None, 0):
            kind, exit_code = KIND_CRASH, status
            detail = f"exit code {status}"
        elif code in (_CLD_KILLED, _CLD_DUMPED) and status:
            kind, signal = KIND_CRASH, status
            detail = f"signal {status}"

        return Event(
            ts=now,
            message=f"{unit}: entered failed state ({detail})",
            priority=int(Severity.ERR),
            unit=unit,
            kind=kind,
            exit_code=exit_code,
            signal=signal,
            source="units",
        )

    def _check_restart_loop(self, unit: str, p: dict, prev: dict | None,
                            now: float) -> Event | None:
        cur = _int(p.get("NRestarts")) or 0
        prev_n = _int(prev.get("NRestarts")) if prev else None
        if prev_n is not None and cur > prev_n:
            for _ in range(cur - prev_n):
                self._restarts[unit].append(now)

        window = self.config.restart_loop_window_sec
        dq = self._restarts[unit]
        while dq and now - dq[0] > window:
            dq.popleft()

        over = len(dq) >= self.config.restart_loop_threshold
        if over and not self._loop_active[unit]:
            self._loop_active[unit] = True
            return Event(
                ts=now,
                message=(f"{unit}: restart loop — {len(dq)} restarts in "
                         f"{int(window)}s"),
                priority=int(Severity.ERR),
                unit=unit,
                kind=KIND_RESTART_LOOP,
                source="units",
            )
        if not over:
            self._loop_active[unit] = False
        return None

    def _recovery_marker(self, unit: str, now: float) -> Event:
        # An informational event; not a problem, so it won't create an incident.
        # The daemon uses these to resolve open incidents for the unit.
        return Event(
            ts=now,
            message=f"{unit}: recovered (active)",
            priority=int(Severity.NOTICE),
            unit=unit,
            source="units",
            extra={"recovery": True},
        )
