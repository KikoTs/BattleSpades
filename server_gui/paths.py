"""Where the desktop host finds the server and keeps its own small state."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SERVER_ENTRY = PROJECT_ROOT / "run_server.py"

SERVER_EXE_NAME = "BattleSpades"
GUI_EXE_NAME = "BattleSpadesServer"
DEFAULTS_NAME = "config.defaults.toml"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_suffix() -> str:
    return ".exe" if sys.platform == "win32" else ""


@dataclass(frozen=True)
class HostPaths:
    """Absolute locations used by the desktop host."""

    root: Path          # server application folder (config.toml lives here)
    config: Path
    logs: Path
    maps: Path
    state_dir: Path     # GUI state, lock and status files

    @property
    def gui_state(self) -> Path:
        return self.state_dir / "server-gui.json"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "server-gui.lock"

    @property
    def status_file(self) -> Path:
        return self.state_dir / "server-gui-status.json"

    @property
    def defaults_snapshot(self) -> Path:
        return self.state_dir / DEFAULTS_NAME

    @property
    def server_executable(self) -> Path:
        return self.root / f"{SERVER_EXE_NAME}{exe_suffix()}"

    def server_command(self) -> list[str]:
        """The command that starts the dedicated server, frozen or source."""

        if is_frozen():
            return [str(self.server_executable)]
        return [sys.executable, str(self.root / "run_server.py")]

    def firewall_program(self) -> Path:
        """The executable a firewall rule should name."""

        if is_frozen():
            return self.server_executable
        return Path(sys.executable)

    def bundled_defaults(self) -> list[Path]:
        """Candidate pristine copies of the shipped config.toml."""

        candidates = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "server_gui" / DEFAULTS_NAME)
        candidates.append(self.root / "_internal" / "server_gui" / DEFAULTS_NAME)
        candidates.append(Path(__file__).resolve().parent / DEFAULTS_NAME)
        candidates.append(self.defaults_snapshot)
        return candidates


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        return True
    except OSError:
        return False


def user_state_dir() -> Path:
    """Per-user fallback when the server folder is read-only."""

    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "BattleSpades"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BattleSpades"
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "battlespades"


def discover(root: Path | None = None) -> HostPaths:
    """Locate the server folder the same way the console launcher does."""

    if root is None:
        root = Path(sys.executable).resolve().parent if is_frozen() else PROJECT_ROOT
    root = Path(root).resolve()
    state = root / "state"
    if not _writable_dir(state):
        state = user_state_dir()
        state.mkdir(parents=True, exist_ok=True)
    return HostPaths(
        root=root,
        config=root / "config.toml",
        logs=root / "logs",
        maps=root / "maps",
        state_dir=state,
    )


def steam_relay_helper(root: Path) -> Path | None:
    """The retail Steam relay helper, found exactly where the server looks."""

    if sys.platform != "win32":
        return None
    candidates = [root / "relay" / "aos-retail-relay.exe"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "relay" / "aos-retail-relay.exe")
    candidates.append(root / "_internal" / "relay" / "aos-retail-relay.exe")
    candidates.append(root / "out" / "retail-mousefix" / "relay" / "aos-retail-relay.exe")
    return next((path for path in candidates if path.is_file()), None)
