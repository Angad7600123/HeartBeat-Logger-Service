import time

from hblog.classify import classify
from hblog.models import (
    KIND_CRASH,
    KIND_ERROR,
    KIND_FAILED,
    KIND_OOM,
    Event,
    Severity,
)


def ev(msg, priority=Severity.INFO, unit=None, **kw):
    return Event(ts=time.time(), message=msg, priority=int(priority), unit=unit, **kw)


def test_info_log_is_not_a_problem():
    e = classify(ev("GET /health 200", Severity.INFO))
    assert e.kind is None
    assert not e.is_problem


def test_error_priority_becomes_error():
    e = classify(ev("disk write failed", Severity.ERR))
    assert e.kind == KIND_ERROR
    assert "signature" in e.extra


def test_main_process_exit_nonzero_is_crash():
    e = classify(ev("myapp.service: Main process exited, code=exited, status=1/FAILURE",
                    Severity.WARNING, unit="myapp.service"))
    assert e.kind == KIND_CRASH
    assert e.exit_code == 1
    assert e.extra["signature"] == "crash:exit=1"


def test_main_process_exit_zero_is_not_crash():
    e = classify(ev("foo.service: Main process exited, code=exited, status=0/SUCCESS",
                    Severity.INFO))
    assert e.kind is None


def test_dumped_signal_is_crash_with_signal():
    e = classify(ev("app.service: Main process exited, code=dumped, status=11/SEGV",
                    Severity.WARNING))
    assert e.kind == KIND_CRASH
    assert e.signal == 11
    assert e.extra["signature"] == "crash:signal=11"


def test_segfault_detected():
    e = classify(ev("worker[2400]: segfault at 0 ip 00007f error 4", Severity.ERR))
    assert e.kind == KIND_CRASH
    assert e.signal == 11


def test_oom_detected():
    e = classify(ev("Out of memory: Killed process 2345 (worker)", Severity.ERR))
    assert e.kind == KIND_OOM
    assert e.extra["signature"] == "oom"


def test_failed_result_oom():
    e = classify(ev("w.service: Failed with result 'oom-kill'.", Severity.WARNING))
    assert e.kind == KIND_OOM


def test_entered_failed_state():
    e = classify(ev("x.service: Unit entered failed state.", Severity.WARNING))
    assert e.kind == KIND_FAILED


def test_error_signature_normalizes_numbers():
    a = classify(ev("connection to 10.0.0.5 failed", Severity.ERR))
    b = classify(ev("connection to 10.0.0.9 failed", Severity.ERR))
    assert a.extra["signature"] == b.extra["signature"]


def test_preclassified_kind_is_respected():
    e = ev("silent death", Severity.INFO)
    e.kind = KIND_FAILED
    classify(e)
    assert e.kind == KIND_FAILED
    assert e.extra["signature"] == "failed"
