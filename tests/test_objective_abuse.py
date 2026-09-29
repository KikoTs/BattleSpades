"""Objective anti-abuse: LOS pickups, buried objectives, spawn protection,
base pits, AFK zone holders."""

import asyncio
import time
from types import SimpleNamespace

import pytest

import shared.constants as C
from modes.ctf import CTFMode
from modes.diamond_mine import DiamondMineMode
from modes.occupation import OccupationMode
from modes.territory_control import TerritoryControlMode
from modes import objective_guard
from server.bot_ai.director import BotDirector
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer

GROUND = 62
STANDING = float(C.PLAYER_STANDING_POS_ABOVE_GROUND)
WALL = 0x808080


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
    asyncio.set_event_loop(None)


def _server(loop, mode_cls):
    async def build():
        server = BattleSpadesServer(ServerConfig())
        server.config.game_rules.apply({"RULE_SPAWN_PROTECTION_TIME": "3"})
        server.world_manager.generate_flat_map()
        server.config.objective_entomb_seconds = 0.0
        server.mode = mode_cls(server)
        await server.mode.on_mode_start()
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        return server, director

    return loop.run_until_complete(build())


def _bot(loop, director, team):
    return loop.run_until_complete(
        director.add_bot(team=team, class_id=int(C.CLASS_SOLDIER))
    )


def _wall(world, x, y, z0=GROUND - 3, z1=GROUND - 1, solid=True):
    for z in range(z0, z1 + 1):
        world.set_block(x, y, z, solid, WALL)


# ---------------------------------------------------------------------------
# CTF
# ---------------------------------------------------------------------------

def test_ctf_intel_cannot_be_picked_up_through_a_wall(loop):
    server, director = _server(loop, CTFMode)
    mode = server.mode
    thief = _bot(loop, director, TEAM1)
    ix, iy, iz = mode.intel_positions[TEAM2]
    thief.set_position(ix - 2.0, iy, iz)
    _wall(server.world_manager, int(ix - 1.0), int(iy))
    mode.intel_drop_time[TEAM2] = 0.0
    loop.run_until_complete(mode.on_tick(1))
    assert mode.intel_holder[TEAM2] is None

    _wall(server.world_manager, int(ix - 1.0), int(iy), solid=False)
    loop.run_until_complete(mode.on_tick(2))
    assert mode.intel_holder[TEAM2] is thief


def test_ctf_pickup_ends_spawn_protection(loop):
    server, director = _server(loop, CTFMode)
    mode = server.mode
    thief = _bot(loop, director, TEAM1)
    assert thief.spawn_protection_remaining() > 0.0
    ix, iy, iz = mode.intel_positions[TEAM2]
    thief.set_position(ix, iy, iz)
    loop.run_until_complete(mode.on_tick(1))
    assert mode.intel_holder[TEAM2] is thief
    assert thief.spawn_protection_remaining() == 0.0


def test_ctf_buried_home_intel_resurfaces_on_top_of_the_tomb(loop):
    server, director = _server(loop, CTFMode)
    mode = server.mode
    world = server.world_manager
    ix, iy, iz = mode.intel_home_positions[TEAM2]
    cx, cy = int(ix), int(iy)
    # Bury the intel under a 4-high pillar and wall it in.
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            _wall(world, cx + dx, cy + dy, GROUND - 4, GROUND - 1)
    assert objective_guard.ground_objective_trapped(
        world, (ix, iy, iz), player_space=True
    ) == "buried"
    mode._guard_next_at = 0.0
    loop.run_until_complete(mode.on_tick(1))
    x, y, z = mode.intel_positions[TEAM2]
    assert z == pytest.approx(GROUND - 4 - STANDING)
    assert mode.intel_home_positions[TEAM2] == mode.intel_positions[TEAM2]
    assert objective_guard.ground_objective_trapped(
        world, (x, y, z), player_space=True
    ) is None
    # The intel entity was re-created on the new surface.
    entity = server.entity_registry.get(mode._intel_entities[TEAM2])
    assert entity is not None and entity.z == pytest.approx(GROUND - 4)


def test_ctf_buried_dropped_intel_returns_home(loop):
    server, director = _server(loop, CTFMode)
    mode = server.mode
    world = server.world_manager
    home = mode.intel_home_positions[TEAM1]
    dropped = (200.5, 200.5, GROUND - STANDING)
    mode.intel_positions[TEAM1] = dropped
    mode.intel_drop_time[TEAM1] = time.time()
    _wall(world, 200, 200, GROUND - 2, GROUND - 1)
    mode._guard_next_at = 0.0
    loop.run_until_complete(mode.on_tick(1))
    assert mode.intel_positions[TEAM1] == home
    assert mode.intel_drop_time[TEAM1] == 0.0


def test_ctf_open_intel_is_left_alone(loop):
    server, director = _server(loop, CTFMode)
    mode = server.mode
    before = dict(mode.intel_positions)
    mode._guard_next_at = 0.0
    loop.run_until_complete(mode.on_tick(1))
    assert mode.intel_positions == before


def test_ctf_capture_counts_in_a_dug_out_base_pit_but_not_a_tunnel(loop):
    server, director = _server(loop, CTFMode)
    mode = server.mode
    world = server.world_manager
    x0, x1, y0, y1, _z0, _z1 = mode.base_bounds[TEAM1]
    bx, by, bz = mode.base_positions[TEAM1]
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            for z in range(GROUND - 1, GROUND + 10):
                world.set_block(x, y, z, False)
            world.set_block(x, y, GROUND + 10, True, WALL)
    carrier = SimpleNamespace(x=bx, y=by, z=bz + 10.0)
    assert mode._is_at_base(carrier, TEAM1)
    # Far deeper than any pit: no.
    assert not mode._is_at_base(SimpleNamespace(x=bx, y=by, z=bz + 40.0), TEAM1)
    # Roof over the carrier's column: a tunnel under the base, not the base.
    world.set_block(int(bx), int(by), GROUND + 2, True, WALL)
    assert not mode._is_at_base(carrier, TEAM1)
    # Ordinary base capture unchanged.
    assert mode._is_at_base(SimpleNamespace(x=bx, y=by, z=bz), TEAM1)


def test_ctf_marker_hooks_track_the_carrier(loop):
    server, director = _server(loop, CTFMode)
    mode = server.mode
    carrier = _bot(loop, director, TEAM1)
    assert not mode.mode_marks_player(carrier)
    mode.intel_holder[TEAM2] = carrier
    # The mode owns the marker only once the carrier is exposed (30 s).
    assert not mode.mode_marks_player(carrier)
    assert mode.escape_watch_objective_player(carrier)
    mode._carrier_exposed[TEAM2] = True
    assert mode.mode_marks_player(carrier)


# ---------------------------------------------------------------------------
# Occupation / Diamond Mine
# ---------------------------------------------------------------------------

def test_occupation_buried_idle_bomb_is_resurfaced(loop):
    server, director = _server(loop, OccupationMode)
    mode = server.mode
    world = server.world_manager
    bomb = next(iter(mode.bombs.values()))
    bx, by, bz = bomb.position
    old_entity = bomb.entity_id
    _wall(world, int(bx), int(by), GROUND - 3, GROUND - 1)
    mode._guard_next_at = 0.0
    mode._guard_ground_bombs(time.time())
    assert bomb.position[2] == pytest.approx(GROUND - 3)
    assert bomb.entity_id != old_entity
    assert server.entity_registry.get(old_entity) is None
    assert mode.entity_to_bomb[bomb.entity_id] == bomb.serial


def test_occupation_bomb_pickup_needs_line_of_sight_and_ends_protection(loop):
    server, director = _server(loop, OccupationMode)
    mode = server.mode
    world = server.world_manager
    runner = _bot(loop, director, TEAM1)
    bomb = next(iter(mode.bombs.values()))
    bx, by, bz = bomb.position
    runner.set_position(bx - 1.9, by, bz - STANDING)
    _wall(world, int(bx - 1.0), int(by))
    assert mode._nearest_ground_bomb(runner, time.time()) is None
    _wall(world, int(bx - 1.0), int(by), solid=False)
    assert mode._nearest_ground_bomb(runner, time.time()) is bomb
    assert runner.spawn_protection_remaining() > 0.0
    mode._pickup_bomb(runner, bomb)
    assert runner.spawn_protection_remaining() == 0.0
    assert mode.escape_watch_objective_player(runner)


def test_diamond_pickup_needs_line_of_sight(loop):
    server, director = _server(loop, DiamondMineMode)
    mode = server.mode
    world = server.world_manager
    miner = _bot(loop, director, TEAM1)
    diamond = mode._spawn_diamond((150.5, 150.5, float(GROUND)), now=time.time())
    miner.set_position(148.6, 150.5, GROUND - STANDING)
    _wall(world, 149, 150)
    assert mode._nearest_ground_diamond(miner) is None
    _wall(world, 149, 150, solid=False)
    assert mode._nearest_ground_diamond(miner) is diamond


# ---------------------------------------------------------------------------
# Territory Control: AFK holders
# ---------------------------------------------------------------------------

def test_afk_players_do_not_hold_territories(loop):
    server, director = _server(loop, TerritoryControlMode)
    server.config.objective_afk_seconds = 0.05
    mode = server.mode
    territory = mode.territories[0]
    holder = _bot(loop, director, TEAM1)
    cx, cy, _cz = territory.zone.center
    top = server.world_manager._get_surface_z(int(cx), int(cy))
    holder.set_position(cx, cy, top - STANDING)
    assert territory.zone.contains(holder.position)
    assert holder in mode._occupants(territory.zone)[TEAM1]
    time.sleep(0.1)
    assert holder not in mode._occupants(territory.zone)[TEAM1]
    # Turning the camera is activity again.
    holder.set_orientation_vector(0.0, 1.0, 0.0)
    assert holder in mode._occupants(territory.zone)[TEAM1]
