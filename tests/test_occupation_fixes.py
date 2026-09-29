"""Occupation regressions: team-switch credit, post-end drops, spawn cues,
defender bot disposal, and restart entity-id hygiene."""

from __future__ import annotations

import asyncio

import shared.constants as C
import shared.constants_gamemode as CG

from modes.occupation import _BOT_DEFENDER_DISPOSAL_DISTANCE, OccupationMode
from server.game_constants import TEAM1, TEAM2
from shared.packet import LocalisedMessage
from tests.test_recovered_objective_modes import _decode, _Player, _Server, _zone


def _mode(monkeypatch, now, settings=None, points=((100.0, 100.0, 60.0),)):
    monkeypatch.setattr("modes.occupation.time.time", lambda: now[0])
    server = _Server({"oc": settings or {
        "score_limit": 30, "max_active_bombs": 1, "bomb_fuse_time": 10,
    }})
    metadata = server.world_manager.map_metadata
    metadata.occupation_base_zone = _zone(TEAM2, 400)
    metadata.occupation_bomb_points.extend(points)
    return server, OccupationMode(server)


def _no_end_sequence(mode):
    async def _skip(_winner):
        return None

    mode._run_end_sequence = _skip


def test_team_switch_keeps_bomb_credited_to_pickup_team(monkeypatch) -> None:
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    blue = _Player(1, TEAM1, (100.0, 100.0, 60.0))
    server.players[blue.id] = blue
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_tick(1))
    assert blue.pickup_id == int(C.BOMB_PICKUP)

    # handlers/team.py assigns the new team BEFORE die() queues the death.
    blue.team = TEAM2
    blue.alive = False
    asyncio.run(mode.on_player_death(blue, None, int(C.KILL.TEAM_CHANGE_KILL)))
    bomb = next(iter(mode.bombs.values()))
    assert bomb.last_carrier_team == TEAM1
    assert bomb.armed

    now[0] = 110.1
    asyncio.run(mode.on_tick(2))
    # Exploded outside the base: no disposal credit for the switched player.
    assert blue.score == 0
    assert server.teams[TEAM2].score == 0


def test_intercept_kill_that_ends_match_does_not_arm_a_new_bomb(monkeypatch) -> None:
    now = [200.0]
    server, mode = _mode(monkeypatch, now, {
        "score_limit": 1, "max_active_bombs": 1, "bomb_fuse_time": 10,
    })
    _no_end_sequence(mode)
    blue = _Player(1, TEAM1, (100.0, 100.0, 60.0))
    green = _Player(2, TEAM2, (300.0, 100.0, 60.0))
    server.players = {blue.id: blue, green.id: green}
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_tick(1))
    created_before = len(server.created)

    asyncio.run(mode.on_player_death(blue, green, int(C.KILL.WEAPON_KILL)))

    assert mode.ended and mode.winner == TEAM2
    assert server.teams[TEAM2].score == int(CG.OC_TEAM_SCORE_FOR_KILLING_CARRIER)
    assert len(server.created) == created_before
    assert blue.pickup_id is None
    assert not mode.carriers
    assert not any(bomb.armed for bomb in mode.bombs.values())


def test_bomb_spawn_wave_announces_once(monkeypatch) -> None:
    now = [300.0]
    server, mode = _mode(
        monkeypatch,
        now,
        {"max_active_bombs": 3, "bomb_fuse_time": 10},
        points=((100.0, 100.0, 60.0), (100.0, 150.0, 60.0), (100.0, 200.0, 60.0)),
    )
    blue = _Player(1, TEAM1, (0.0, 0.0, 60.0))
    green = _Player(2, TEAM2, (0.0, 50.0, 60.0))
    for player in (blue, green):
        player.connection = player
        server.players[player.id] = player
    asyncio.run(mode.on_mode_start())

    assert len(mode.bombs) == 3
    # Round start: the per-team start cue, then the wave line queued behind
    # it (override_previous off so it does not wipe the start cue).
    blue_lines = _decode(blue.sent, LocalisedMessage)
    assert [m.string_id for m in blue_lines] == [
        "OCCUPATION_START_ATTACK", "TAKE_BOMB_TO_ENEMY_BASE"
    ]
    assert not blue_lines[1].override_previous_message
    assert [m.string_id for m in _decode(green.sent, LocalisedMessage)] == [
        "OCCUPATION_START_DEFEND", "STOP_BOMB_REACHING_BASE"
    ]

    # A respawn wave of two bombs in one tick is still one cue.
    for serial in list(mode.bombs)[:2]:
        mode.bombs.pop(serial)
    mode._pending_spawns = [300.5, 300.5]
    now[0] = 301.0
    blue.sent.clear()
    asyncio.run(mode.on_tick(1))
    assert len(mode.bombs) == 3
    assert [m.string_id for m in _decode(blue.sent, LocalisedMessage)] == [
        "TAKE_BOMB_TO_ENEMY_BASE"
    ]


def test_defender_bot_disposes_bomb_away_from_base(monkeypatch) -> None:
    now = [400.0]
    server, mode = _mode(monkeypatch, now)
    bot = _Player(3, TEAM2, (100.0, 100.0, 60.0))
    bot.is_bot = True
    server.players[bot.id] = bot
    asyncio.run(mode.on_mode_start())
    assert mode._target_distance(bot) >= _BOT_DEFENDER_DISPOSAL_DISTANCE

    asyncio.run(mode.on_tick(1))

    bomb = next(iter(mode.bombs.values()))
    assert bot.pickup_id is None
    assert not mode.carriers
    assert bomb.armed and bomb.entity_id is not None
    assert bomb.last_carrier_team == TEAM2


def test_defender_bot_near_base_keeps_bomb(monkeypatch) -> None:
    now = [450.0]
    server, mode = _mode(monkeypatch, now, points=((395.0, 100.0, 60.0),))
    bot = _Player(3, TEAM2, (395.0, 100.0, 60.0))
    bot.is_bot = True
    server.players[bot.id] = bot
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_tick(1))
    assert bot.pickup_id == int(C.BOMB_PICKUP)
    assert bot.id in mode.carriers


def test_restart_does_not_destroy_rebuilt_entities_with_recycled_ids(
    monkeypatch,
) -> None:
    now = [500.0]
    server, mode = _mode(monkeypatch, now)
    asyncio.run(mode.on_mode_start())
    old_id = next(iter(mode.bombs.values())).entity_id

    rebuilt = []

    class _Resources:
        def rebuild(self):
            rebuilt.append(server.entity_registry.place(
                int(C.AMMO_CRATE), 10.0, 10.0, 60.0, kind="crate"
            ))

    server.map_resources = _Resources()
    # _restart_round: reset_round_runtime wipes the registry, ids restart at 0.
    server.entity_registry.clear()
    server.destroyed.clear()
    asyncio.run(mode.on_mode_start())

    crate = rebuilt[-1]
    assert crate.entity_id == old_id
    assert server.destroyed == []
    assert server.entity_registry.get(crate.entity_id) is crate
    assert len(mode.bombs) == 1
