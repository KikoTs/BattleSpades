"""Offline hosting and master overrides must work before network startup."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest

from server import launcher, ugc_launcher
from server.config import load_config
from server.network_options import normalize_master_url
from server.revival_master import JoinTicketUnavailable, RevivalMasterService
from server.runtime_paths import RuntimePaths
from server.steam_host import SteamHostService
from server_gui.process import build_command


@pytest.mark.parametrize("value", [
    "https://master.example", "https://master.example:8443/",
    "http://127.0.0.1:8000", "http://localhost", "http://[::1]:8000",
])
def test_valid_master_origins(value: str) -> None:
    assert normalize_master_url(value) == value.rstrip("/")


@pytest.mark.parametrize("value", [
    "http://192.168.1.2:8000", "http://localhost.evil", "http://127.0.0.10",
    "http://127.0.0.1@evil.example", "https://user:pass@master.example",
    "https://master.example/api", "https://master.example?q=x", "https://master.example#x",
    "https://master.example?", "https://master.example#", "https://master.example:",
    "https://master.example:0", "https://master.example:65536", "https://master.example:abc",
    "https://master.example\\other", "https://master.example\n", "https://", "file:///tmp/master",
])
def test_bad_master_origins_fail_before_startup(value: str, tmp_path: Path) -> None:
    assert launcher.run(["--master-url", value], paths=RuntimePaths.from_root(tmp_path)) == 2
    assert not (tmp_path / "logs").exists()


def test_offline_replaces_external_settings_without_rewriting_file(tmp_path: Path) -> None:
    config_file = tmp_path / "offline.toml"
    content = '''[server]
port = 32123
lan_discovery = true
[revival]
enabled = true
require_identity = true
official = true
base_url = "invalid old master"
[steam]
enabled = true
require_registration = true
steam_port = -1
[steam_host]
enabled = true
app_ids = []
[updates]
update_manifest_url = "invalid old update mirror"
'''
    config_file.write_text(content, encoding="utf-8")
    config = load_config(config_file, offline=True)
    assert config.port == 32123 and config.lan_discovery
    assert config.offline_mode
    assert not config.revival.enabled and not config.revival.require_identity and not config.revival.official
    assert not config.steam.enabled and not config.steam.require_registration
    assert not config.steam_host.enabled and not config.steam_p2p_enabled
    assert not config.update_check_enabled
    assert config_file.read_text(encoding="utf-8") == content


def test_master_override_precedes_validation_and_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "master.toml"
    config_file.write_text('[revival]\nbase_url = "old invalid origin"\n', encoding="utf-8")
    monkeypatch.setenv("AOS_MASTER_URL", "https://environment.example")
    config = load_config(config_file, master_url="http://127.0.0.1:8000/")
    bridge = RevivalMasterService(SimpleNamespace(config=config, players={}))
    assert config.revival.base_url == "http://127.0.0.1:8000"
    assert bridge.base_url == config.revival.base_url
    assert not config.offline_mode
    assert "old invalid origin" in config_file.read_text(encoding="utf-8")


def test_offline_env_credentials_cannot_start_relays_or_consume_tickets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(tmp_path / "missing.toml", offline=True)
    server = SimpleNamespace(config=config, players={})
    bridge = RevivalMasterService(server)
    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("AOS_STEAM_HOST", "1")
    monkeypatch.setattr("server.revival_master.urlopen", lambda *_args, **_kwargs: pytest.fail("offline HTTP"))
    monkeypatch.setattr("server.steam_host.subprocess.Popen", lambda *_args, **_kwargs: pytest.fail("offline relay"))
    host = SteamHostService(server)
    assert not host.enabled() and not bridge.enabled

    async def verify() -> None:
        await bridge.start()
        await host.start()
        with pytest.raises(JoinTicketUnavailable, match="offline"):
            await bridge.consume_join_ticket("~abcdefghijklmn")
        assert bridge._heartbeat_task is None and bridge.cosmetics.task is None

    asyncio.run(verify())


@pytest.mark.parametrize("extra", [["--update"], ["--workshop-download", "123"], ["--steam-p2p"], ["--fleet", "fleet.toml"]])
def test_offline_conflicts_are_explicit(extra: list[str], tmp_path: Path) -> None:
    assert launcher.run(["--offline", *extra], paths=RuntimePaths.from_root(tmp_path)) == 2


def test_console_start_loads_overrides_before_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[revival]\nbase_url = "unusable"\n', encoding="utf-8")
    seen: list[Any] = []

    async def serve(config: Any, _logging: Any, **_kwargs: Any) -> None:
        seen.append(config)

    monkeypatch.setattr(launcher, "_serve", serve)
    assert launcher.run(
        ["--offline", "--master-url", "http://localhost:8000", "--control-stdin"],
        paths=RuntimePaths.from_root(tmp_path),
    ) == 0
    assert seen[0].offline_mode and seen[0].revival.base_url == "http://localhost:8000"
    assert not seen[0].revival.require_identity


@pytest.mark.parametrize("entrypoint", [launcher.run, ugc_launcher.run])
def test_check_uses_the_same_offline_override(
    entrypoint, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server import release_check

    (tmp_path / "VERSION").write_text("0.2.1", encoding="utf-8")
    (tmp_path / "config.toml").write_text('[revival]\nbase_url = "broken"\n', encoding="utf-8")
    seen: list[Any] = []

    def load(path: Path, **kwargs: Any) -> Any:
        config = load_config(path, **kwargs)
        seen.append(config)
        return config

    monkeypatch.setattr(release_check, "load_config", load)
    monkeypatch.setattr(release_check, "_NATIVE_MODULES", ("not_a_module_just_stop_after_config",))
    assert entrypoint(["--check", "--offline"], paths=RuntimePaths.from_root(tmp_path)) == 1
    assert len(seen) == 1 and seen[0].offline_mode and not seen[0].steam.enabled


def test_desktop_command_passes_options_and_suppresses_saved_relay_preference() -> None:
    command = build_command(["BattleSpades.exe"], steam_p2p=True, offline=True, master_url="https://master.example/")
    assert "--offline" in command and "--steam-p2p" not in command
    assert command[-2:] == ["--master-url", "https://master.example"]


def test_desktop_offline_probes_do_not_contact_public_services(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("customtkinter")
    from server_gui.app import App

    monkeypatch.setattr("server_gui.network.public_ip", lambda: pytest.fail("offline public IP lookup"))
    monkeypatch.setattr("server_gui.network.local_ip", lambda: "192.168.1.2")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: pytest.fail("offline master ping"))
    app = SimpleNamespace(
        offline=True, call_soon=lambda callback: callback(),
        network_page=SimpleNamespace(refresh_addresses=lambda: None),
    )
    App._refresh_addresses(app)
    App._ping_master_loop(app)
    assert app.public_ip is None and app.lan_ip == "192.168.1.2" and app.master_latency == "offline"


def test_offline_server_starts_and_answers_local_queries(tmp_path: Path) -> None:
    """Run the actual ENet server without any reachable master or Steam host."""
    pytest.importorskip("enet")
    from server_gui.network import a2s_probe

    root = Path(__file__).resolve().parents[1]
    if not (root / "maps" / "ArcticBase.vxl").is_file():
        pytest.skip("native server map fixture is unavailable")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    config = tmp_path / "config.toml"
    config.write_text(
        f'[server]\nport={port}\nlan_discovery=false\n'
        f'[world]\nmaps_path={json.dumps(str(root / "maps"))}\n'
        f'prefabs_path={json.dumps(str(root / "prefabs"))}\n'
        '[game]\ndefault_map="ArcticBase"\ndefault_mode="tdm"\n'
        '[bots]\nenabled=false\n'
        '[revival]\nenabled=true\nrequire_identity=true\n'
        '[steam_host]\nenabled=true\n', encoding="utf-8",
    )
    script = tmp_path / "run_offline.py"
    # A separate application root keeps logs, bans and mutable files isolated.
    script.write_text(
        'from pathlib import Path\nimport sys\n'
        f'sys.path.insert(0, {str(root)!r})\n'
        'if __name__ == "__main__":\n'
        '    from server.launcher import run\n'
        '    from server.runtime_paths import RuntimePaths\n'
        '    raise SystemExit(run(paths=RuntimePaths.from_root(Path(__file__).parent)))\n',
        encoding="utf-8",
    )
    status_file = tmp_path / "status.json"
    environment = dict(os.environ, AOS_MASTER_WRITE_TOKEN="synthetic-offline-test", AOS_STEAM_HOST="1")
    with subprocess.Popen(
        [sys.executable, str(script), "--offline", "--master-url", "http://127.0.0.1:9",
         "--control-stdin", "--status-file", str(status_file)],
        cwd=root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    ) as process:
        try:
            deadline = time.monotonic() + 30
            ready = False
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    ready = json.loads(status_file.read_text(encoding="utf-8")).get("state") == "running"
                except (OSError, ValueError):
                    pass
                if ready:
                    break
                time.sleep(0.05)
            assert ready, "offline server did not reach running state"
            assert a2s_probe("127.0.0.1", port), "direct localhost query did not answer"
            output, _ = process.communicate("shutdown\n", timeout=15)
            assert process.returncode == 0, output
            assert "AoS Revival master registration disabled" in output
            assert "Initial Revival heartbeat failed" not in output
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
