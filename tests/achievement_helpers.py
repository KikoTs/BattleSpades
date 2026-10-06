"""Shared fakes for the achievement tests (not collected: no ``test_`` prefix)."""

from __future__ import annotations

from types import SimpleNamespace

import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import LocalisedMessage

from server import achievements
from server.achievements import AchievementEngine, AchievementStore
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2

WEAPON = int(C.WEAPON_KILL)
HEADSHOT = int(C.HEADSHOT_KILL)
MELEE = int(C.MELEE_KILL)


class Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def attach_engine(server, *, store=None, clock=None) -> AchievementEngine:
    """Give ``server`` a started engine on an in-memory (or given) store."""
    engine = AchievementEngine(
        server,
        store=store if store is not None else AchievementStore(":memory:"),
        monotonic=clock if clock is not None else Clock(),
    )
    assert engine.start()
    server.achievements = engine
    return engine


def make_server(mode_code: str = "tdm", *, count_bot_kills: bool = True,
                store=None, clock=None):
    config = ServerConfig()
    config.default_mode = mode_code
    config.achievements.count_bot_kills = count_bot_kills
    server = SimpleNamespace(
        players={},
        sent=[],
        config=config,
        mode=SimpleNamespace(ended=False, mode_code=mode_code),
    )
    server.broadcast = lambda data, **_kwargs: server.sent.append(bytes(data))
    attach_engine(server, store=store, clock=clock)
    return server


def make_player(server, player_id: int, team: int = TEAM1, *, name: str | None = None,
                bot: bool = False, tool: int = int(C.RIFLE_TOOL), **extra):
    player = SimpleNamespace(
        id=player_id,
        name=name if name is not None else f"P{player_id}",
        team=team,
        is_bot=bot,
        tool=int(tool),
        weapon=int(tool),
        health=100,
        max_health=100,
        alive=True,
        kill_streak=0,
        jetpack_active=False,
        airborne=False,
        wade=False,
        x=100.0, y=100.0, z=50.0,
        connection=SimpleNamespace(server=server, peer=None),
    )
    for key, value in extra.items():
        setattr(player, key, value)
    server.players[player_id] = player
    return player


def kill(server, killer, victim, kill_type: int = WEAPON, *, jetpacking: bool = False) -> None:
    """One accepted cross-team kill, as ``Player.die`` reports it."""
    killer.kill_streak = int(getattr(killer, "kill_streak", 0)) + 1
    achievements.died(server, victim, killer, int(kill_type), jetpacking)


def announcements(server) -> list[tuple[str, list[str]]]:
    """Decoded ``LocalisedMessage`` broadcasts, oldest first."""
    packets = ()
    for name in ("sent", "packets", "broadcast_packets"):
        if hasattr(server, name):
            packets = getattr(server, name)
            break
    rows = []
    for data in packets:
        if data and data[0] == LocalisedMessage.id:
            packet = LocalisedMessage(ByteReader(data[1:]))
            if packet.string_id == achievements.ANNOUNCEMENT_STRING_ID:
                rows.append((packet.string_id, list(packet.parameters)))
    return rows


def unlocked(server, player) -> set[str]:
    engine = server.achievements
    identity = engine.identity_for(player)
    return set(engine.unlocked(identity)) if identity else set()


def progress(server, player, stat: str) -> int:
    engine = server.achievements
    return engine.progress(engine.identity_for(player)).get(stat, 0)


__all__ = [
    "Clock", "HEADSHOT", "MELEE", "TEAM1", "TEAM2", "WEAPON",
    "announcements", "attach_engine", "kill", "make_player", "make_server",
    "progress", "unlocked",
]
