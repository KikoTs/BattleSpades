"""Remembered window state and the single-instance guard."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class GuiState:
    geometry: str = ""
    zoomed: bool = False
    tab: str = "Host"
    first_run: bool = True
    steam_p2p: bool = True
    auto_forward: bool = False
    console_level: str = "INFO"
    console_autoscroll: bool = True
    appearance: str = "dark"
    port_default_applied: bool = False
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "GuiState":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {f.name: f for f in fields(cls)}
        values = {}
        for name, value in data.items():
            spec = known.get(name)
            if spec is None:
                continue
            default = getattr(cls(), name)
            if isinstance(default, bool) and not isinstance(value, bool):
                continue
            if isinstance(default, str) and not isinstance(value, str):
                continue
            if isinstance(default, dict) and not isinstance(value, dict):
                continue
            values[name] = value
        return cls(**values)

    def save(self, path: Path) -> bool:
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(temporary, path)
            return True
        except OSError:
            return False


class SingleInstance:
    """An OS-level exclusive lock on a file; released when the process ends.

    The operating system drops the lock even if the window crashes, so a
    stale lock file never blocks the next start.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode("ascii"))
            handle.flush()
        except OSError:
            pass
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()
