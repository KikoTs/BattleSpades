"""Game modes and maps the dedicated server really supports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from server.lobby import LOBBY_MAX_PLAYER_OPTIONS, LOBBY_MODES

#: Canonical registry codes accepted by ``[game] default_mode`` (modes/__init__).
#: tests/test_server_gui_logic.py keeps this in step with the registry.
REGISTERED_MODE_CODES = (
    "tdm", "ctf", "cctf", "zom", "vip", "mh", "tc", "dia", "dem", "oc", "arena",
)

DIFFICULTIES = (
    ("casual", "Casual"),
    ("normal", "Normal"),
    ("hard", "Hard"),
    ("mixed", "Mixed"),
)


@dataclass(frozen=True)
class ModeChoice:
    code: str
    title: str
    default_minutes: int


def mode_choices() -> list[ModeChoice]:
    """The ten public Match Lobby modes, in the stock menu order."""

    return [
        ModeChoice(code, mode.title, int(mode.default_seconds // 60))
        for code, mode in LOBBY_MODES.items()
    ]


def mode_title(code: str) -> str:
    mode = LOBBY_MODES.get(str(code).lower())
    if mode is not None:
        return mode.title
    return {"arena": "Arena"}.get(str(code).lower(), str(code).upper())


def available_maps(maps_dir: Path) -> list[str]:
    """Every ``.vxl`` map the server can load, sorted case-insensitively."""

    try:
        names = {path.stem for path in Path(maps_dir).glob("*.vxl") if path.is_file()}
    except OSError:
        return []
    return sorted(names, key=str.casefold)


def mode_pool(maps_dir: Path, mode: str) -> list[str]:
    """Maps the retail playlist offers for ``mode`` and that are installed.

    Uses the same ``retail_mode_pool`` the server's map vote uses, falling back
    to the recovered Match Lobby preset when the catalogue file is absent.
    """

    installed = {name.casefold(): name for name in available_maps(maps_dir)}
    pool: list[str] = []
    try:
        from server.map_metadata import retail_mode_pool

        pool = list(retail_mode_pool(maps_dir, mode))
    except Exception:
        pool = []
    if not pool:
        preset = LOBBY_MODES.get(str(mode).lower())
        pool = list(preset.maps) if preset else []
    result = [installed[name.casefold()] for name in pool if name.casefold() in installed]
    return result + [name for name in custom_maps_for_mode(maps_dir, mode) if name not in result]


def custom_maps_for_mode(maps_dir: Path, mode: str) -> list[str]:
    """Custom/Workshop maps whose ``.ugc`` tags list ``mode`` (sorted)."""

    from server_gui.workshop_import import custom_map_modes

    code = str(mode).lower()
    return sorted((stem for stem, modes in custom_map_modes(maps_dir).items() if code in modes), key=str.casefold)


def map_title(name: str) -> str:
    """``MayanJungle`` -> ``Mayan Jungle``; ``WW1`` stays ``WW1``."""

    spaced = re.sub(r"(?<=[a-z])(?=[A-Z0-9])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return spaced.replace("_", " ").strip() or name


def player_count_options() -> tuple[int, ...]:
    """Stock lobby choices (2..24) plus 32 for Classic CTF."""

    return tuple(LOBBY_MAX_PLAYER_OPTIONS) + (32,)
