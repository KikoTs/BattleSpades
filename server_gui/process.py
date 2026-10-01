"""The dedicated server as a supervised child process.

The window never runs gameplay code itself. It starts the normal console
server with ``--control-stdin`` and talks to it through that pipe:

* ``shutdown\\n``          - graceful stop (same contract the game client uses)
* ``command <text>\\n``    - one operator command (server/control_channel.py)
* closing the pipe         - also a graceful stop, so if the window crashes the
                             server notices EOF and shuts itself down cleanly.

Output (stdout + stderr) is read on a background thread and handed to a
callback line by line; the toolkit side marshals it onto its own thread.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from server.control_channel import MAX_COMMAND_CHARS

STOP_TIMEOUT_SECONDS = 25.0
STATUS_STALE_SECONDS = 6.0


def build_command(
    base: Sequence[str],
    *,
    steam_p2p: bool = False,
    status_file: Path | None = None,
    config_path: Path | None = None,
    port: int | None = None,
) -> list[str]:
    """Command line for one supervised server session."""

    command = list(base) + ["--control-stdin"]
    if config_path is not None:
        command += ["--config", str(config_path)]
    if port is not None:
        command += ["--port", str(int(port))]
    if status_file is not None:
        command += ["--status-file", str(status_file)]
    if steam_p2p:
        command.append("--steam-p2p")
    return command


def child_environment(base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@dataclass
class ServerProcess:
    """Start, talk to and stop one server child."""

    command: list[str]
    cwd: Path
    on_line: Callable[[str], None]
    on_exit: Callable[[int], None]
    env: dict | None = None
    process: subprocess.Popen | None = field(default=None, init=False)
    started_at: float = field(default=0.0, init=False)
    stopping: bool = field(default=False, init=False)
    _write_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    def start(self) -> None:
        if self.running:
            raise RuntimeError("the server is already running")
        flags = 0
        kwargs: dict = {}
        if sys.platform == "win32":
            # The console server must not open a console window of its own.
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        else:
            # Keep terminal Ctrl+C aimed at the window from killing the child
            # mid-write; the window stops it through the pipe instead.
            kwargs["start_new_session"] = True
        self.stopping = False
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=child_environment(self.env),
            creationflags=flags,
            bufsize=0,
            **kwargs,
        )
        self.started_at = time.time()
        threading.Thread(target=self._pump, name="server-output", daemon=True).start()

    def _pump(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        for raw in iter(process.stdout.readline, b""):
            try:
                self.on_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            except Exception:
                pass
        code = process.wait()
        try:
            self.on_exit(code)
        except Exception:
            pass

    def _write(self, data: bytes) -> bool:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            return False
        with self._write_lock:
            try:
                process.stdin.write(data)
                process.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def send_command(self, text: str) -> bool:
        """Queue one operator command; returns False if it cannot be sent."""

        line = " ".join(str(text).split())
        if not line or len(line) > MAX_COMMAND_CHARS:
            return False
        return self._write(b"command " + line.encode("utf-8") + b"\n")

    def request_stop(self) -> bool:
        """Ask for a graceful stop; returns immediately."""

        self.stopping = True
        sent = self._write(b"shutdown\n")
        return sent

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> int | None:
        """Stop gracefully, escalating only if the server does not exit."""

        process = self.process
        if process is None:
            return None
        if process.poll() is None:
            self.request_stop()
            try:
                return process.wait(timeout)
            except subprocess.TimeoutExpired:
                pass
            try:
                process.stdin and process.stdin.close()
            except OSError:
                pass
            try:
                return process.wait(5)
            except subprocess.TimeoutExpired:
                process.terminate()
            try:
                return process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                return process.wait(5)
        return process.returncode

    def stop_async(self, done: Callable[[int | None], None] | None = None) -> threading.Thread:
        def run() -> None:
            code = self.stop()
            if done is not None:
                done(code)

        thread = threading.Thread(target=run, name="server-stop", daemon=True)
        thread.start()
        return thread


def read_status(path: Path, *, now: float | None = None) -> dict | None:
    """The newest status snapshot, or None if missing/unreadable."""

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None
    current = time.time() if now is None else now
    data["stale"] = current - float(data.get("updated_at", 0) or 0) > STATUS_STALE_SECONDS
    return data


def format_uptime(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"
