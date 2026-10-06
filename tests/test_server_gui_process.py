"""The desktop host supervises a real dedicated-server child process."""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import tomlkit

from server_gui.process import ServerProcess, build_command, read_status

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _session_config(tmp_path: Path) -> Path:
    document = tomlkit.parse((PROJECT_ROOT / "config.toml").read_text(encoding="utf-8"))
    document["server"]["port"] = _free_udp_port()
    document["revival"]["enabled"] = False
    document["steam"]["enabled"] = False
    document["updates"]["update_check_enabled"] = False
    document["bots"]["enabled"] = False
    document["plugins"]["enabled"] = False
    document["logging"]["file"] = str(tmp_path / "server.log")
    # A started server opens its achievement store: keep it out of the checkout.
    document["achievements"]["path"] = str(tmp_path / "achievements.sqlite3")
    path = tmp_path / "session.toml"
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    return path


def _wait(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def test_start_command_and_graceful_stop_of_a_real_server(tmp_path: Path) -> None:
    lines: list[str] = []
    exited = threading.Event()
    codes: list[int] = []
    status_file = tmp_path / "status.json"
    command = build_command(
        [sys.executable, str(PROJECT_ROOT / "run_server.py")],
        status_file=status_file,
        config_path=_session_config(tmp_path),
    )
    assert command[2:4] == ["--control-stdin", "--config"]
    process = ServerProcess(
        command=command,
        cwd=PROJECT_ROOT,
        on_line=lines.append,
        on_exit=lambda code: (codes.append(code), exited.set()),
    )
    process.start()
    try:
        assert _wait(lambda: (read_status(status_file) or {}).get("state") == "running", 120), "\n".join(lines[-40:])
        status = read_status(status_file)
        assert status["humans"] == 0 and status["map"] and status["mode"] == "tdm"
        assert (tmp_path / "achievements.sqlite3").is_file()
        assert process.send_command("status")
        assert process.send_command("tp 1 2 3")
        assert _wait(lambda: any("| players 0+0 bots" in line for line in lines), 20), "\n".join(lines[-40:])
        assert _wait(lambda: any("needs an in-game player" in line for line in lines), 20)
        assert not process.send_command("x" * 5000)
    finally:
        code = process.stop(timeout=60)
    assert code == 0, "\n".join(lines[-60:])
    assert exited.wait(10)
    assert codes == [0]
    assert any("parent requested shutdown on stdin" in line.lower() for line in lines)
    assert read_status(status_file)["state"] == "stopped"
    assert not process.running


def test_closing_the_pipe_stops_the_server_like_a_crashed_window(tmp_path: Path) -> None:
    """If the window dies, the server sees EOF and shuts down cleanly."""

    lines: list[str] = []
    exited = threading.Event()
    status_file = tmp_path / "status.json"
    process = ServerProcess(
        command=build_command([sys.executable, str(PROJECT_ROOT / "run_server.py")], status_file=status_file,
                              config_path=_session_config(tmp_path)),
        cwd=PROJECT_ROOT,
        on_line=lines.append,
        on_exit=lambda _code: exited.set(),
    )
    process.start()
    try:
        assert _wait(lambda: (read_status(status_file) or {}).get("state") == "running", 120), "\n".join(lines[-40:])
        process.process.stdin.close()
        assert process.process.wait(60) == 0
    finally:
        if process.running:
            process.stop(timeout=30)
    assert exited.wait(10)  # the output pump has delivered every line
    assert any("parent stdin reached eof" in line.lower() for line in lines), chr(10).join(lines[-25:])
