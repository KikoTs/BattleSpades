"""Threat-aware spawns and attack-cancelled spawn protection."""

import asyncio
import math
import random
import time
from types import SimpleNamespace

import shared.constants as C
from modes.tdm import TDMMode
from server.bot_ai.director import BotDirector
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.spawn_selection import (
    DANGER_RADIUS,
    STACK_RADIUS,
    choose_team_spawn,
    record_death,
)


async def _server(map_name=None):
    server = BattleSpadesServer(ServerConfig())
    # Shipped configs enable the retail 3 s window; the code default is off.
    server.config.game_rules.apply({"RULE_SPAWN_PROTECTION_TIME": "3"})
    if map_name:
        assert server.world_manager.load_map(map_name)
    else:
        server.world_manager.generate_flat_map()
    server.mode = TDMMode(server)
    await server.mode.on_mode_start()
    director = BotDirector(server, supervisor=SimpleNamespace())
    server.bots = director
    return server, director


def test_spawns_avoid_a_camper_standing_in_the_base():
    async def scenario():
        server, director = await _server("ArcticBase")
        spawner = await director.add_bot(team=TEAM1, class_id=int(C.CLASS_SOLDIER))
        camper = await director.add_bot(team=TEAM2, class_id=int(C.CLASS_SOLDIER))
        columns = server.world_manager._get_spawn_candidates(TEAM1)
        cx, cy = columns[len(columns) // 2]
        surface = server.world_manager._get_surface_z(cx, cy)
        camper.set_position(cx + 0.5, cy + 0.5, surface - 2.75)
        rng = random.Random(3)
        for _ in range(40):
            x, y, _z = choose_team_spawn(server, spawner, rng=rng)
            assert math.hypot(x - camper.x, y - camper.y) >= DANGER_RADIUS
    asyncio.run(scenario())


def test_back_to_back_spawns_do_not_stack():
    async def scenario():
        server, director = await _server("ArcticBase")
        players = [await director.add_bot(team=TEAM1, class_id=int(C.CLASS_SOLDIER))
                   for _ in range(6)]
        rng = random.Random(5)
        spots = [choose_team_spawn(server, p, rng=rng) for p in players]
        for i, a in enumerate(spots):
            for b in spots[i + 1:]:
                assert math.hypot(a[0] - b[0], a[1] - b[1]) > STACK_RADIUS
    asyncio.run(scenario())


def test_recent_deaths_mark_a_spot_as_camped():
    async def scenario():
        server, director = await _server("ArcticBase")
        spawner = await director.add_bot(team=TEAM1, class_id=int(C.CLASS_SOLDIER))
        columns = server.world_manager._get_spawn_candidates(TEAM1)
        # Teammates keep dying across the whole base except one corner.
        corner = columns[0]
        for x, y in columns[::3]:
            if math.hypot(x - corner[0], y - corner[1]) > 30:
                record_death(server, SimpleNamespace(team=TEAM1, position=(x + .5, y + .5, 60.)))
        # Deaths only lower scores; spawning must still succeed.
        assert choose_team_spawn(server, spawner, rng=random.Random(1)) is not None
    asyncio.run(scenario())


def test_real_map_spawn_choice_is_cheap_with_a_full_lobby():
    async def scenario():
        server, director = await _server("ArcticBase")
        bots = [await director.add_bot(team=TEAM1 if i % 2 else TEAM2,
                                       class_id=int(C.CLASS_SOLDIER)) for i in range(12)]
        spawner = bots[0]
        rng = random.Random(9)
        started = time.perf_counter()
        for _ in range(20):
            assert choose_team_spawn(server, spawner, rng=rng) is not None
        per_spawn_ms = (time.perf_counter() - started) * 1000 / 20
        assert per_spawn_ms < 25.0, per_spawn_ms
    asyncio.run(scenario())


def test_spawn_protection_blocks_damage_and_ends_when_the_protected_player_attacks():
    async def scenario():
        server, director = await _server()
        fresh = await director.add_bot(team=TEAM1, class_id=int(C.CLASS_SOLDIER))
        enemy = await director.add_bot(team=TEAM2, class_id=int(C.CLASS_SOLDIER))
        assert fresh.spawn_protection_remaining() > 0.0
        health = fresh.health
        fresh.damage(30, source=enemy, kill_type=int(C.WEAPON_KILL))
        assert fresh.health == health
        # The protected player deals damage: its own protection ends at once.
        enemy.spawned_at -= 60.0
        enemy.damage(10, source=fresh, kill_type=int(C.WEAPON_KILL))
        assert fresh.spawn_protection_remaining() == 0.0
        assert fresh.world_update_snapshot()[12] == 0.0
        fresh.damage(30, source=enemy, kill_type=int(C.WEAPON_KILL))
        assert fresh.health < health
    asyncio.run(scenario())


def test_firing_a_gun_ends_protection_but_digging_does_not():
    import time as _time

    from tests.test_anticheat_combat import _fire, _player, _server as _combat_server, _shot

    server = _combat_server()
    server.config.game_rules.apply({"RULE_SPAWN_PROTECTION_TIME": "3"})
    rifle = _player(server, C.RIFLE_TOOL, player_id=0)
    digger = _player(server, C.SPADE_TOOL, player_id=1, position=(110.5, 100.5, 59.75))
    for fresh in (rifle, digger):
        fresh.spawned_at = _time.monotonic()
        assert fresh.spawn_protection_remaining() > 0.0
    # handle_shot returns whether something was hit; acceptance is what counts.
    _fire(server, rifle, _shot(rifle))
    assert rifle.spawn_protection_remaining() == 0.0
    _fire(server, digger, _shot(digger))
    assert digger.spawn_protection_remaining() > 0.0
    # A new life gets fresh protection.
    rifle.spawn(*rifle.position)
    assert rifle.spawn_protection_remaining() > 0.0


def test_a_fully_camped_base_moves_spawns_to_a_safer_part_of_the_side():
    async def scenario():
        server, director = await _server("ArcticBase")
        spawner = await director.add_bot(team=TEAM1, class_id=int(C.CLASS_SOLDIER))
        columns = server.world_manager._get_spawn_candidates(TEAM1)
        xs = [x for x, _ in columns]
        ys = [y for _, y in columns]
        # Campers spread over the whole base footprint.
        campers = []
        for fx in (0.2, 0.5, 0.8):
            for fy in (0.2, 0.5, 0.8):
                cx = int(min(xs) + fx * (max(xs) - min(xs)))
                cy = int(min(ys) + fy * (max(ys) - min(ys)))
                camper = await director.add_bot(team=TEAM2, class_id=int(C.CLASS_SOLDIER))
                camper.set_position(cx + 0.5, cy + 0.5,
                                    server.world_manager._get_surface_z(cx, cy) - 2.75)
                campers.append(camper)
        spot = choose_team_spawn(server, spawner, rng=random.Random(2))
        assert spot is not None
        nearest = min(math.hypot(spot[0] - c.x, spot[1] - c.y) for c in campers)
        assert nearest >= DANGER_RADIUS
    asyncio.run(scenario())


def test_bots_hold_fire_on_a_spawn_protected_enemy():
    from dataclasses import replace

    from server.bot_ai.simple_worker import SimpleBotBrain, _BotState
    from tests.test_simple_bot_tactics import _TacticalWorld, _frame, _player

    observer = _player(1, TEAM1, (10.5, 10.5, 20.0), is_bot=True)
    enemy = _player(2, TEAM2, (20.5, 10.5, 20.0))
    brain = SimpleBotBrain(_TacticalWorld())
    state = _BotState(1, 1, observer.life_id)
    assert brain._visible_target(_frame(observer, enemy), observer, state) is not None
    shielded = replace(enemy, spawn_protected=True)
    assert brain._visible_target(_frame(observer, shielded), observer, _BotState(1, 1, observer.life_id)) is None
