"""Recovered retail Match Lobby presets.

Source: the shipped ``matchSettingsPanel.pyc``, ``constants_gamemode.pyc``,
and plaintext ``playlists/*.txt``.  This is descriptive data, not objective
logic.  Dedicated operators may use other maps and player counts, while lobby
ports can consume these exact choices without decompiling the client again.
"""

from __future__ import annotations

from dataclasses import dataclass


LOBBY_MAX_PLAYER_OPTIONS = tuple(range(2, 25, 2))
LOBBY_MATCH_LENGTH_OPTIONS = (
    5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 90,
)


@dataclass(frozen=True)
class LobbyModeDefinition:
    """One of the ten public hosting modes in the stock Match Lobby."""

    code: str
    title: str
    default_seconds: float
    maps: tuple[str, ...]
    recommended_players: tuple[int, ...]
    classic: bool = False
    mafia: bool = False


_STANDARD = (
    "AncientEgypt", "ArcticBase", "Atlantis", "BlockNess", "BranCastle",
    "CastleWars", "DoubleDragon", "DragonIsland", "Frontier", "GreatWall",
    "Invasion", "London", "LunarBase", "MayanJungle", "SpookyMansion",
    "TheColosseum", "TokyoNeon",
)


LOBBY_MODES: dict[str, LobbyModeDefinition] = {
    "tdm": LobbyModeDefinition(
        "tdm", "Team Deathmatch", 900.0,
        tuple(name for name in _STANDARD if name != "BranCastle"),
        (16, 20, 24, 24),
    ),
    "ctf": LobbyModeDefinition(
        "ctf", "Capture the Flag", 1800.0,
        ("Atlantis", "BlockNess", "CastleWars", "DoubleDragon", "Invasion", "TokyoNeon"),
        (16, 20, 24),
    ),
    "cctf": LobbyModeDefinition(
        "cctf", "Classic CTF", 5400.0,
        ("Crossroads", "Hiesville", "ToTheBridge", "Trenches", "WinterValley", "WW1", "Classic"),
        (32,), classic=True,
    ),
    "zom": LobbyModeDefinition(
        "zom", "Zombie", 600.0,
        tuple(name for name in _STANDARD if name not in {"LunarBase"}),
        (16, 20, 24, 24),
    ),
    "vip": LobbyModeDefinition(
        "vip", "VIP", 900.0,
        ("Alcatraz", "CityOfChicago"), (16, 20, 24), mafia=True,
    ),
    "mh": LobbyModeDefinition(
        "mh", "Multi-Hill", 1500.0,
        tuple(name for name in _STANDARD if name not in {"ArcticBase", "TokyoNeon"}),
        (16, 20, 24),
    ),
    "tc": LobbyModeDefinition(
        "tc", "Territory Control", 1500.0,
        ("Alcatraz", "CityOfChicago"), (16, 20, 24), mafia=True,
    ),
    "dia": LobbyModeDefinition(
        "dia", "Diamond Mine", 900.0,
        tuple(name for name in _STANDARD if name != "Invasion"),
        (16, 20, 24, 24),
    ),
    "dem": LobbyModeDefinition(
        "dem", "Demolition", 900.0,
        ("Atlantis", "BlockNess", "CastleWars", "DoubleDragon", "DragonIsland", "Frontier", "GreatWall", "LunarBase", "TokyoNeon"),
        (16,),
    ),
    "oc": LobbyModeDefinition(
        "oc", "Occupation", 900.0,
        tuple(name for name in _STANDARD if name not in {"CastleWars", "DoubleDragon", "TokyoNeon"}),
        (16, 20, 24),
    ),
}


__all__ = [
    "LOBBY_MATCH_LENGTH_OPTIONS",
    "LOBBY_MAX_PLAYER_OPTIONS",
    "LOBBY_MODES",
    "LobbyModeDefinition",
]
