"""A capped soak must not turn missing evidence or a restarted crash into a pass."""

import csv
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location(
    "soak_supervisor", Path(__file__).parents[1] / "scripts/soak/soak_supervisor.py"
)
supervisor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(supervisor)
CHECK_SPEC = importlib.util.spec_from_file_location(
    "check_memory_soak", Path(__file__).parents[1] / "scripts/soak/check_memory_soak.py"
)
checker = importlib.util.module_from_spec(CHECK_SPEC)
CHECK_SPEC.loader.exec_module(checker)
ANALYZE_SPEC = importlib.util.spec_from_file_location(
    "analyze_memory_soak", Path(__file__).parents[1] / "scripts/soak/analyze_memory_soak.py"
)
analyzer = importlib.util.module_from_spec(ANALYZE_SPEC)
ANALYZE_SPEC.loader.exec_module(analyzer)


def state(**overrides):
    result = {
        "status": "finished", "cap_requested_mb": 512, "cap_mb": 512,
        "restarts": [], "client_restarts": 0, "oom_kills": 0,
        "clients": 2, "client_started": True, "failures": [],
    }
    return result | overrides


@pytest.mark.parametrize("change", [
    {"cap_mb": 0}, {"restarts": [{"exit_code": -11}]},
    {"client_restarts": 1}, {"oom_kills": 1}, {"client_started": False},
])
def test_completion_does_not_erase_failures(change):
    run = state(**change)
    assert supervisor.final_verdict(run) == ("failed", 1)
    assert run["failures"]


def test_stopped_run_is_incomplete():
    assert supervisor.final_verdict(state(status="stopped")) == ("incomplete", 2)


def test_clean_completed_run_passes():
    assert supervisor.final_verdict(state()) == ("passed", 0)


def test_unavailable_requested_cap_never_starts_process(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "make_cgroup", lambda *args: None)

    def forbidden(*args, **kwargs):
        pytest.fail("the server must not start without the requested cap")

    monkeypatch.setattr(supervisor.subprocess, "Popen", forbidden)
    assert supervisor.main(["--out", str(tmp_path)]) == 1
    recorded = json.loads((tmp_path / "supervisor.json").read_text())
    assert recorded["verdict"] == "failed"
    assert "requested memory cap unavailable" in recorded["failures"]


@pytest.mark.parametrize("override", ["--minutes=0.1", "--out=other", "--port=27015"])
def test_child_cannot_override_supervised_run(override):
    with pytest.raises(SystemExit):
        supervisor.parse_args(["--out", "unused", "--", override])


def test_evidence_requires_memory_and_successful_join(tmp_path):
    run = state()
    supervisor.inspect_evidence(tmp_path, run)
    assert supervisor.final_verdict(run)[0] == "failed"
    with (tmp_path / "memory.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["cg_current_mb"])
        writer.writeheader()
        writer.writerows([{"cg_current_mb": "70"}, {"cg_current_mb": "72"}])
    (tmp_path / "churn.jsonl").write_text(json.dumps({"joins": 2}) + "\n")
    run = state()
    supervisor.inspect_evidence(tmp_path, run)
    assert supervisor.final_verdict(run) == ("passed", 0)
    assert run["memory_samples"] == 2
    assert run["churn_joins"] == 2


def test_connected_clients_without_game_join_are_not_churn_evidence(tmp_path):
    (tmp_path / "churn.jsonl").write_text(json.dumps({"connects": 99, "joins": 0}) + "\n")
    run = state()
    supervisor.inspect_evidence(tmp_path, run)
    assert "churn clients never joined a game" in run["failures"]


@pytest.mark.parametrize("crashes,expected", [(0, "passed"), (1, "failed")])
def test_planned_worker_recycles_are_distinct_from_crashes(tmp_path, crashes, expected):
    (tmp_path / "memory.csv").write_text(
        "cg_current_mb,worker_restarts,worker_planned_recycles,worker_crash_restarts\n"
        f"90,2,2,0\n92,{2 + crashes},2,{crashes}\n"
    )
    run = state(clients=0)
    supervisor.inspect_evidence(tmp_path, run)
    assert supervisor.final_verdict(run)[0] == expected


def test_worker_status_exports_planned_and_unexpected_restart_counts():
    from scripts.soak.soak_server import SoakHarness

    status = SimpleNamespace(restarts=4, planned_recycles=3, crash_restarts=1)
    harness = object.__new__(SoakHarness)
    harness.server = SimpleNamespace(bots=SimpleNamespace(status=lambda: status))
    recorded = harness.worker_status()
    assert recorded["restarts"] == 4
    assert recorded["planned_recycles"] == 3
    assert recorded["crash_restarts"] == 1


@pytest.mark.parametrize("minutes,status,verdict,expected", [
    (3, "finished", "passed", "incomplete"),
    (360, "running", "pending", "incomplete"),
    (360, "finished", "passed", "passed"),
    (360, "failed", "failed", "failed"),
])
def test_checker_requires_the_actual_six_hours(minutes, status, verdict, expected):
    run = state(minutes=minutes, elapsed_minutes=minutes, status=status, verdict=verdict,
                heartbeat_wall=100)
    assert checker.check(run, now=100)["verdict"] == expected


def test_checker_identifies_stale_supervisor():
    result = checker.check(state(status="running", heartbeat_wall=100), now=400)
    assert result["status"] == "stale"
    assert result["verdict"] == "incomplete"


@pytest.mark.parametrize("exit_after,stop_reason,expected", [
    (3, "duration reached", 1),
    (6, "server task ended: None", 1),
    (6, "duration reached", 0),
])
def test_supervisor_checks_real_duration_and_child_reason(
    tmp_path, monkeypatch, exit_after, stop_reason, expected,
):
    clock = {"now": 0.0}
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(supervisor.time, "time", lambda: clock["now"])
    monkeypatch.setattr(supervisor.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))
    monkeypatch.setattr(supervisor.signal, "signal", lambda *args: None)
    monkeypatch.setattr(supervisor, "make_cgroup", lambda *args: None)

    class Process:
        pid = 123

        def __init__(self, *args, **kwargs):
            (tmp_path / "summary.json").write_text(json.dumps({"stop_reason": stop_reason}))
            (tmp_path / "memory.csv").write_text("cg_current_mb\n70\n71\n")

        def poll(self):
            return 0 if clock["now"] >= exit_after else None

    monkeypatch.setattr(supervisor.subprocess, "Popen", Process)
    result = supervisor.main([
        "--out", str(tmp_path), "--minutes", "0.1", "--cap-mb", "0",
        "--clients", "0", "--max-restarts", "0",
    ])
    assert result == expected
    recorded = json.loads((tmp_path / "supervisor.json").read_text())
    assert recorded["verdict"] == ("passed" if expected == 0 else "failed")


def test_client_crash_at_server_completion_cannot_pass(tmp_path, monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(supervisor.time, "time", lambda: clock["now"])
    monkeypatch.setattr(supervisor.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))
    monkeypatch.setattr(supervisor.signal, "signal", lambda *args: None)
    monkeypatch.setattr(supervisor, "make_cgroup", lambda *args: None)

    class Process:
        pid = 123
        returncode = None

        def __init__(self, command, **kwargs):
            self.is_client = any("churn_client.py" in part for part in command)
            (tmp_path / "summary.json").write_text(json.dumps({"stop_reason": "duration reached"}))
            (tmp_path / "memory.csv").write_text("cg_current_mb\n70\n71\n")
            (tmp_path / "churn.jsonl").write_text(json.dumps({"joins": 3}) + "\n")

        def poll(self):
            if clock["now"] >= 30:
                self.returncode = -11 if self.is_client else 0
            return self.returncode

    monkeypatch.setattr(supervisor.subprocess, "Popen", Process)
    assert supervisor.main([
        "--out", str(tmp_path), "--minutes", "0.5", "--cap-mb", "0", "--clients", "2",
    ]) == 1
    recorded = json.loads((tmp_path / "supervisor.json").read_text())
    assert "churn client exited with code -11" in recorded["failures"]


def test_memory_growth_compares_same_map_instead_of_changing_map_mix():
    rows = []
    for t, map_name, memory in (
        (0, "small", 50), (10, "small", 50), (20, "small", 50),
        (30, "large", 200), (40, "large", 200), (50, "large", 200),
        (200, "small", 55), (210, "small", 55), (220, "small", 55),
        (230, "large", 205), (240, "large", 205), (250, "large", 205),
        (260, "large", 205), (270, "large", 205), (300, "large", 205),
    ):
        rows.append({"t_s": t, "map": map_name, "mode": "dia", "cg_anon_mb": memory})
    result = analyzer.memory_growth(rows, [], warmup_seconds=0, settle_seconds=0)
    assert len(result["matched_groups"]) == 2
    assert result["median_matched_delta_mb"]["cg_anon_mb"] == 5
    assert result["median_matched_delta_mb"]["total_pss_mb"] is None


def test_memory_growth_excludes_warmup_transitions_and_tracer():
    rows = [{"t_s": t, "map": "same", "mode": "dia", "cg_anon_mb": 100,
             "tm_active": int(t == 250)} for t in (0, 100, 110, 120, 200, 240, 250, 260, 300)]
    result = analyzer.memory_growth(rows, [{"t": 105, "ok": True}],
                                    warmup_seconds=100, settle_seconds=30)
    assert result["status"] == "insufficient comparable samples"
    assert result["matched_groups"] == []


def test_mode_cycle_requires_every_mode_and_return_to_start():
    transitions = [{"type": "mode", "to_mode": mode, "ok": ok}
                   for mode, ok in (("dia", True), ("tdm", True), ("tc", False),
                                    ("dia", True), ("tc", True), ("tdm", True),
                                    ("dia", True), ("tc", True))]
    assert analyzer.completed_mode_cycles(["tdm", "dia", "tc"], transitions) == 1


def test_report_preserves_missing_pss_and_uses_last_churn_counters(tmp_path):
    (tmp_path / "memory.csv").write_text(
        "t_s,rss_mb,worker_planned_recycles,worker_crash_restarts\n0,80,0,0\n30,90,1,0\n"
    )
    (tmp_path / "churn.jsonl").write_text('{"joins": 2}\n{"joins": 5,"decode_errors": 0}\n')
    result = analyzer.build_report(tmp_path)
    assert result["peak_memory_mb"]["rss_mb"] == 90
    assert result["peak_memory_mb"]["pss_mb"] is None
    assert result["worker"]["worker_planned_recycles"] == 1
    assert result["worker"]["worker_crash_restarts"] == 0
    assert result["churn"]["joins"] == 5
    assert result["child_stop_reason"] is None
