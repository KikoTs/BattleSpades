"""CLI and packaged health-check behavior."""

import asyncio
from pathlib import Path
import sys

import pytest

from server import launcher
from server.launcher import run
from server.release_check import run_release_check
from server.runtime_paths import RuntimePaths


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_import_graph_gc_hardening_collects_then_freezes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supported CPython performs one pre-game collection and one freeze."""

    events: list[str] = []
    monkeypatch.setattr(launcher.gc, "collect", lambda: events.append("collect"))
    monkeypatch.setattr(launcher.gc, "freeze", lambda: events.append("freeze"))

    assert launcher._freeze_import_graph_for_gc() is True
    assert events == ["collect", "freeze"]


def test_import_graph_gc_hardening_tolerates_missing_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Python runtime without gc.freeze keeps the normal collector enabled."""

    collections: list[None] = []
    monkeypatch.setattr(
        launcher.gc,
        "collect",
        lambda: collections.append(None),
    )
    monkeypatch.delattr(launcher.gc, "freeze", raising=False)

    assert launcher._freeze_import_graph_for_gc() is False
    assert collections == []


def test_serve_freezes_only_import_graph_before_server_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GC boundary runs once before telemetry and server-owned state exist."""

    events: list[str] = []
    instances: list[object] = []

    class FakeServer:
        def __init__(self, _config, telemetry=None) -> None:
            assert telemetry is not None
            events.append("server-created")
            instances.append(self)
            self.running = False

        async def start(self) -> None:
            events.append("server-started")

        async def stop(self) -> None:
            events.append("server-stopped")

    def freeze_import_graph() -> bool:
        assert instances == []
        events.append("gc-frozen")
        return True

    def make_telemetry(_runtime):
        events.append("telemetry-created")
        return object()

    monkeypatch.setattr("server.main.BattleSpadesServer", FakeServer)
    monkeypatch.setattr("server.telemetry.TelemetryService", make_telemetry)
    monkeypatch.setattr(launcher, "_freeze_import_graph_for_gc", freeze_import_graph)
    monkeypatch.setattr(launcher.signal, "signal", lambda *_args: None)

    asyncio.run(launcher._serve(object(), object()))

    assert events == [
        "gc-frozen",
        "telemetry-created",
        "server-created",
        "server-started",
        "server-stopped",
    ]


def test_version_does_not_import_native_server(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Version reporting works even when gameplay/native imports are broken."""

    monkeypatch.delitem(sys.modules, "server.main", raising=False)

    assert run(["--version"], paths=RuntimePaths.from_root(PROJECT_ROOT)) == 0

    assert capsys.readouterr().out.strip() == "BattleSpades 0.0.3-alpha.2"
    assert "server.main" not in sys.modules


def test_help_has_no_log_side_effect(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Argument discovery does not initialize logging or native runtime."""

    assert run(["--help"], paths=RuntimePaths.from_root(tmp_path)) == 0

    assert "BattleSpades" in capsys.readouterr().out
    assert not (tmp_path / "logs").exists()


def test_check_returns_nonzero_when_config_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A partial archive fails closed with an actionable missing-file line."""

    assert run(["--check"], paths=RuntimePaths.from_root(tmp_path)) == 1

    captured = capsys.readouterr()
    assert "FAIL config.toml" in captured.err
    assert captured.out == ""


def test_source_release_check_passes() -> None:
    """The checked-out runtime satisfies the same checks as a staged bundle."""

    report = run_release_check(RuntimePaths.from_root(PROJECT_ROOT))

    assert report.ok, "\n".join(report.lines)
    assert report.exit_code == 0
    assert any(line.startswith("OK native imports") for line in report.lines)
    worker_line = next(
        line for line in report.lines if line.startswith("OK worker spawn")
    )
    assert "processed full map" in worker_line
    assert "intent frame=2" in worker_line
