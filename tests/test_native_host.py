"""Local host bridge is optional, atomic, session-bound and credential-free."""

import json
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.native_host import NativeHostStatus
from server import launcher


def test_status_lifecycle_and_no_credential_disclosure(tmp_path: Path) -> None:
    directory = tmp_path / "session-test-123"
    directory.mkdir()
    path = directory / "host-status.json"
    environment = {
        "AOS_NATIVE_HOST_STATUS": str(path),
        "AOS_NATIVE_HOST_SESSION": directory.name,
        "AOS_MASTER_WRITE_TOKEN": "must-not-be-published",
    }
    bridge = NativeHostStatus.from_environment(27015, "tdm", environment)
    assert bridge is not None
    for state in ("starting", "ready", "stopping", "stopped", "failed"):
        bridge.publish(state)
        assert json.loads(path.read_text()) == {
            "schema_version": 1, "session": directory.name,
            "port": 27015, "mode": "tdm", "state": state,
        }
        assert not path.with_suffix(".tmp").exists()


@pytest.mark.parametrize("path,session", [
    ("host-status.json", "session-test"),
    ("/tmp/session-other/host-status.json", "session-test"),
    ("/tmp/session-test/arbitrary.json", "session-test"),
    ("/tmp/not-a-session/host-status.json", "not-a-session"),
])
def test_foreign_paths_are_ignored(path: str, session: str) -> None:
    assert NativeHostStatus.from_environment(27015, "tdm", {
        "AOS_NATIVE_HOST_STATUS": path, "AOS_NATIVE_HOST_SESSION": session,
    }) is None


def test_missing_channel_and_io_failure_do_not_stop_server(tmp_path: Path) -> None:
    assert NativeHostStatus.from_environment(27015, "tdm", {}) is None
    bridge = NativeHostStatus(tmp_path / "missing" / "host-status.json", "session-test", 27015, "tdm")
    bridge.publish("ready")
    with pytest.raises(ValueError, match="Unknown native host state"):
        bridge.publish("invalid")


@pytest.mark.parametrize("fail_start", [False, True])
def test_launcher_reports_ready_only_after_initialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail_start: bool,
) -> None:
    directory = tmp_path / "session-lifecycle"
    directory.mkdir()
    monkeypatch.setenv("AOS_NATIVE_HOST_STATUS", str(directory / "host-status.json"))
    monkeypatch.setenv("AOS_NATIVE_HOST_SESSION", directory.name)
    states: list[str] = []
    instances = []
    original_publish = NativeHostStatus.publish

    def publish(bridge: NativeHostStatus, state: str) -> None:
        if state == "ready":
            assert instances[0].running
        states.append(state)
        original_publish(bridge, state)

    class FakeServer:
        def __init__(self, _config, telemetry=None) -> None:
            self.running = False
            self.stopped = asyncio.Event()
            instances.append(self)

        async def start(self) -> None:
            await asyncio.sleep(0.05)
            assert states == ["starting"]
            if fail_start:
                raise RuntimeError("map initialization failed")
            self.running = True
            await self.stopped.wait()

        async def stop(self) -> None:
            self.running = False
            self.stopped.set()

    def monitor(_loop, shutdown):
        async def wait_until_ready():
            while "ready" not in states:
                await asyncio.sleep(0.005)
            shutdown("test complete")
        return asyncio.create_task(wait_until_ready())

    monkeypatch.setattr(NativeHostStatus, "publish", publish)
    monkeypatch.setattr("server.main.BattleSpadesServer", FakeServer)
    monkeypatch.setattr("server.telemetry.TelemetryService", lambda _runtime: object())
    monkeypatch.setattr(launcher, "_freeze_import_graph_for_gc", lambda: None)
    monkeypatch.setattr(launcher, "_start_control_stdin_monitor", monitor)
    monkeypatch.setattr(launcher.signal, "signal", lambda *_args: None)

    async def serve() -> None:
        await asyncio.wait_for(launcher._serve(
            SimpleNamespace(port=27015, game_mode="tdm"), object(), control_stdin=True,
        ), timeout=2)

    if fail_start:
        with pytest.raises(RuntimeError, match="map initialization failed"):
            asyncio.run(serve())
        assert states == ["starting", "failed"]
    else:
        asyncio.run(serve())
        assert states == ["starting", "ready", "stopping", "stopped"]
