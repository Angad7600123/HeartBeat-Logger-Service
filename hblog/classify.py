"""Turn raw events into classified problems.

Pure functions (no I/O, no state) so they are trivially testable off-Pi. The
classifier looks at journal/kernel message text and priority to decide whether an
event is a problem, and computes a stable `signature` used to group repeated
occurrences into a single incident.

Stateful detection (restart loops) lives in the unit monitor, which owns the
restart-count deltas; here we only classify individual events.
"""

from __future__ import annotations

import re

from .models import (
    KIND_CRASH,
    KIND_ERROR,
    KIND_FAILED,
    KIND_OOM,
    Event,
    Severity,
)

# --- signal name/number handling -------------------------------------------
_SIGNALS = {
    "SIGHUP": 1, "SIGINT": 2, "SIGQUIT": 3, "SIGILL": 4, "SIGABRT": 6,
    "SIGBUS": 7, "SIGFPE": 8, "SIGKILL": 9, "SIGSEGV": 11, "SIGPIPE": 13,
    "SIGTERM": 15,
}


def _to_signal(token: str | None) -> int | None:
    if not token:
        return None
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _SIGNALS.get(token.upper())


# --- detection patterns ----------------------------------------------------
# systemd: "Main process exited, code=exited, status=1/FAILURE"
#          "Main process exited, code=dumped, status=11/SEGV"
#          "Main process exited, code=killed, status=SIGKILL"
_RE_MAIN_EXIT = re.compile(
    r"main process exited.*code=(?P<code>\w+).*status=(?P<status>[\w/]+)",
    re.IGNORECASE,
)
# systemd: "Failed with result 'exit-code'." / 'signal' / 'core-dump' / 'oom-kill'
_RE_FAILED_RESULT = re.compile(
    r"failed with result '(?P<result>[\w-]+)'", re.IGNORECASE
)
_RE_ENTERED_FAILED = re.compile(r"entered failed state", re.IGNORECASE)
# kernel OOM killer
_RE_OOM = re.compile(
    r"out of memory|oom-kill|killed process \d+", re.IGNORECASE
)
# kernel segfault: "app[123]: segfault at ... "
_RE_SEGFAULT = re.compile(r"segfault at|general protection fault", re.IGNORECASE)


def classify(event: Event, *, error_priority: int = int(Severity.ERR)) -> Event:
    """Classify `event` in place (also returns it). Sets kind/exit_code/signal and
    extra['signature']. If the source already set a kind (e.g. the unit monitor),
    that wins; we only fill in a signature."""
    msg = event.message or ""

    if event.kind is None:
        _detect(event, msg, error_priority)

    if event.is_problem and "signature" not in event.extra:
        event.extra["signature"] = _signature(event)
    return event


def _detect(event: Event, msg: str, error_priority: int) -> None:
    # Order matters: OOM before generic crash, crash before generic error.
    if _RE_OOM.search(msg):
        event.kind = KIND_OOM
        return

    if _RE_SEGFAULT.search(msg):
        event.kind = KIND_CRASH
        event.signal = 11
        return

    m = _RE_MAIN_EXIT.search(msg)
    if m:
        code = m.group("code").lower()
        status = m.group("status").split("/")[0]
        if code == "exited":
            try:
                event.exit_code = int(status)
            except ValueError:
                event.exit_code = None
            # exit 0 from a oneshot is not a crash; anything non-zero is.
            if event.exit_code == 0:
                event.kind = None
                return
            event.kind = KIND_CRASH
        else:  # dumped / killed => died on a signal
            event.signal = _to_signal(status)
            event.kind = KIND_CRASH
        return

    m = _RE_FAILED_RESULT.search(msg)
    if m:
        result = m.group("result").lower()
        event.kind = KIND_OOM if result == "oom-kill" else KIND_CRASH
        return

    if _RE_ENTERED_FAILED.search(msg):
        event.kind = KIND_FAILED
        return

    if event.priority <= error_priority:
        event.kind = KIND_ERROR


# --- signature (grouping key) ----------------------------------------------
_RE_HEX = re.compile(r"0x[0-9a-fA-F]+")
_RE_NUM = re.compile(r"\d+")
_RE_WS = re.compile(r"\s+")


def _signature(event: Event) -> str:
    """A stable-ish key so repeated occurrences of the same problem group together.

    For crashes we key on the failure mode (exit code / signal), which is more
    stable than the message. For errors we normalize the message: strip addresses
    and numbers so "connection to 10.0.0.5 failed" and "...10.0.0.9 failed" collapse.
    """
    if event.kind == KIND_CRASH:
        if event.signal is not None:
            return f"crash:signal={event.signal}"
        if event.exit_code is not None:
            return f"crash:exit={event.exit_code}"
        return "crash"  # systemd "Failed with result" summary, no code available
    if event.kind == KIND_OOM:
        return "oom"
    if event.kind == KIND_FAILED:
        return "failed"

    norm = _RE_HEX.sub("0x#", event.message.lower())
    norm = _RE_NUM.sub("#", norm)
    norm = _RE_WS.sub(" ", norm).strip()
    return norm[:80]
