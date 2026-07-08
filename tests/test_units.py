from hblog.config import Config
from hblog.models import KIND_CRASH, KIND_OOM, KIND_RESTART_LOOP
from hblog.sources.units import UnitMonitor, parse_list, parse_show


def show_text(units: dict[str, dict]) -> str:
    blocks = []
    for unit, props in units.items():
        p = {"Id": unit, **props}
        blocks.append("\n".join(f"{k}={v}" for k, v in p.items()))
    return "\n\n".join(blocks) + "\n"


class Stub:
    """Injectable systemctl stand-in with a mutable snapshot."""

    def __init__(self):
        self.units: dict[str, dict] = {}

    def list(self) -> str:
        return "\n".join(self.units) + "\n"

    def show(self, units) -> str:
        return show_text({u: self.units[u] for u in units if u in self.units})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(stub, clock, **cfg):
    config = Config(**cfg)
    return UnitMonitor(config, show_runner=stub.show, list_runner=stub.list, now=clock)


def test_parse_show_multiblock():
    txt = show_text({
        "a.service": {"ActiveState": "active"},
        "b.service": {"ActiveState": "failed"},
    })
    parsed = parse_show(txt)
    assert parsed["a.service"]["ActiveState"] == "active"
    assert parsed["b.service"]["ActiveState"] == "failed"


def test_parse_list_filters_services():
    assert parse_list("a.service loaded active running\nfoo.timer loaded\n") == \
        ["a.service"]


def test_healthy_service_no_events():
    stub, clock = Stub(), Clock()
    stub.units = {"a.service": {"ActiveState": "active", "SubState": "running",
                                "NRestarts": "0"}}
    mon = make(stub, clock)
    events, statuses = mon.poll()
    assert events == []
    assert statuses[0].state == "active"
    assert statuses[0].last_heartbeat == 1000.0


def test_failed_with_exit_code_is_crash():
    stub, clock = Stub(), Clock()
    stub.units = {"a.service": {"ActiveState": "active", "NRestarts": "0"}}
    mon = make(stub, clock)
    mon.poll()  # establish 'active' baseline
    stub.units["a.service"] = {"ActiveState": "failed", "Result": "exit-code",
                               "ExecMainCode": "1", "ExecMainStatus": "1",
                               "NRestarts": "0"}
    clock.t += 10
    events, _ = mon.poll()
    assert len(events) == 1
    assert events[0].kind == KIND_CRASH
    assert events[0].exit_code == 1


def test_failed_oom():
    stub, clock = Stub(), Clock()
    stub.units = {"a.service": {"ActiveState": "active", "NRestarts": "0"}}
    mon = make(stub, clock)
    mon.poll()
    stub.units["a.service"] = {"ActiveState": "failed", "Result": "oom-kill",
                               "ExecMainCode": "2", "ExecMainStatus": "9",
                               "NRestarts": "0"}
    clock.t += 10
    events, _ = mon.poll()
    assert events[0].kind == KIND_OOM


def test_restart_loop_fires_once():
    stub, clock = Stub(), Clock()
    stub.units = {"a.service": {"ActiveState": "active", "NRestarts": "0"}}
    mon = make(stub, clock, restart_loop_threshold=3, restart_loop_window_sec=120)
    mon.poll()
    fired = 0
    for i in range(1, 6):
        stub.units["a.service"] = {"ActiveState": "active", "NRestarts": str(i)}
        clock.t += 5
        events, _ = mon.poll()
        fired += sum(1 for e in events if e.kind == KIND_RESTART_LOOP)
    assert fired == 1  # only emits on the edge, not every poll


def test_recovery_emits_marker():
    stub, clock = Stub(), Clock()
    stub.units = {"a.service": {"ActiveState": "failed", "Result": "exit-code",
                                "ExecMainCode": "1", "ExecMainStatus": "1",
                                "NRestarts": "0"}}
    mon = make(stub, clock)
    mon.poll()  # failed
    stub.units["a.service"] = {"ActiveState": "active", "NRestarts": "0"}
    clock.t += 10
    events, _ = mon.poll()
    assert any(e.extra.get("recovery") for e in events)
