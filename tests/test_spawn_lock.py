"""A dug-out spawn area must never hard-lock spawning.

Live report: once the whole team spawn area was dug down to the water, nobody
could spawn. The old fallback walked the whole team region cell by cell
(seconds per life on the gameplay thread, for every respawn) and then handed
back a water column. These tests dig ArcticBase's real TEAM1 side down to the
water plane and require a fast, valid, dry spawn - or, when the whole map is
water, a spawn in the water at the base that is never inside solid blocks.
"""

import asyncio
import math
import time

import pytest

import shared.constants as C
from modes.tdm import TDMMode
from server import spawn_selection
from server.bot_ai.director import BotDirector
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.round_lifecycle import resolve_player_spawn
from types import SimpleNamespace

WATERLINE = int(C.Z_ABOVE_WATERPLANE)


def _dig_to_water(world, x0, y0, x1, y1):
    vxl = world.map
    for x in range(max(0, x0), min(512, x1 + 1)):
        for y in range(max(0, y0), min(512, y1 + 1)):
            top = vxl.get_z(x, y)
            for z in range(top, WATERLINE + 1):
                if vxl.get_solid(x, y, z):
                    vxl.remove_point_nochecks(x, y, z)
            world._surface_cache.pop((x, y), None)
    world.topology_version += 1


@pytest.fixture(scope="module")
def dug():
    loop = asyncio.new_event_loop()

    async def build():
        server = BattleSpadesServer(ServerConfig())
        assert server.world_manager.load_map("ArcticBase")
        server.mode = TDMMode(server)
        await server.mode.on_mode_start()
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        world = server.world_manager
        # Warm the production caches first (as a live server has them), then
        # dig TEAM1's entire spawn region (+margin) down to the water plane.
        world._get_spawn_candidates(TEAM1)
        x0, y0, x1, y1 = world._spawn_region(TEAM1)
        _dig_to_water(world, x0 - 8, y0 - 8, x1 + 8, y1 + 8)
        return server, director

    server, director = loop.run_until_complete(build())
    yield SimpleNamespace(server=server, director=director, loop=loop)
    loop.close()


def _team1_player(ctx):
    return ctx.loop.run_until_complete(
        ctx.director.add_bot(team=TEAM1, class_id=int(C.CLASS_SOLDIER))
    )


def test_region_is_really_dug_to_water(dug):
    world = dug.server.world_manager
    x0, y0, x1, y1 = world._spawn_region(TEAM1)
    for x, y in ((x0, y0), ((x0 + x1) // 2, (y0 + y1) // 2), (x1, y1)):
        assert world.is_water_column(x, y)


def test_dug_out_base_still_spawns_fast_on_dry_ground(dug):
    world = dug.server.world_manager
    player = _team1_player(dug)  # add_bot itself spawns through resolve
    assert player.alive
    timings = []
    for _ in range(12):
        started = time.perf_counter()
        position = resolve_player_spawn(dug.server, player)
        timings.append(time.perf_counter() - started)
        assert world.spawn_position_is_safe(position), position
        assert not world.is_water_column(int(position[0]), int(position[1]))
    # The old path took ~3 s per life here.
    assert max(timings) < 0.5, timings
    assert sum(timings) / len(timings) < 0.1, timings


def test_emergency_spawn_prefers_dry_land_nearest_the_team(dug):
    world = dug.server.world_manager
    x, y, z = spawn_selection.emergency_spawn(dug.server, TEAM1)
    assert world.spawn_position_is_safe((x, y, z))
    # Nearer TEAM1's base than TEAM2's.
    t1 = world.team_base_anchor(TEAM1)
    t2 = world.team_base_anchor(TEAM2)
    assert math.hypot(x - t1[0], y - t1[1]) < math.hypot(x - t2[0], y - t2[1])


def test_all_water_map_spawns_in_the_water_at_the_base_never_in_blocks(dug, monkeypatch):
    world = dug.server.world_manager
    monkeypatch.setattr(spawn_selection, "_dry_grid", lambda server, world: [])
    monkeypatch.setattr(world, "_nearest_safe_spawn_point", lambda *a, **k: None)
    player = SimpleNamespace(team=TEAM1, id=99, name="probe", position=(0, 0, 0))
    started = time.perf_counter()
    x, y, z = resolve_player_spawn(dug.server, player)
    assert time.perf_counter() - started < 0.5
    assert all(math.isfinite(v) for v in (x, y, z))
    assert world.is_water_column(int(x), int(y))
    assert world._player_body_is_clear(x, y, z)
    base = world.team_base_anchor(TEAM1)
    assert math.hypot(x - base[0], y - base[1]) < 2.0


def test_water_fallback_lifts_the_body_out_of_solid_blocks(dug):
    world = dug.server.world_manager
    # An intact land column on TEAM2's side: the body must stand on top.
    bx, by, _ = world.team_base_anchor(TEAM2)
    x, y, z = spawn_selection.water_fallback(world, bx, by)
    assert world._player_body_is_clear(x, y, z)


def test_a_raising_mode_resolver_still_produces_a_life(dug, monkeypatch):
    world = dug.server.world_manager

    def broken(player):
        raise RuntimeError("mode bug")

    monkeypatch.setattr(dug.server.mode, "get_spawn_point", broken)
    player = SimpleNamespace(team=TEAM1, id=98, name="probe", position=(0, 0, 0))
    position = resolve_player_spawn(dug.server, player)
    assert world.spawn_position_is_safe(position)


def test_dead_players_respawn_on_a_dug_out_base(dug):
    server = dug.server
    player = _team1_player(dug)
    player.die(kill_type=int(C.WEAPON_KILL))
    assert not player.alive
    player.death_time = time.time() - 60.0
    dug.loop.run_until_complete(server.round_lifecycle.process_respawns())
    assert player.alive
    assert server.world_manager.spawn_position_is_safe(player.position)


def test_a_failing_respawn_backs_off_instead_of_stalling_every_tick(dug, monkeypatch):
    server = dug.server
    victim = _team1_player(dug)
    other = _team1_player(dug)
    for p in (victim, other):
        p.die(kill_type=int(C.WEAPON_KILL))
        p.death_time = time.time() - 60.0
    lifecycle = server.round_lifecycle
    real = lifecycle.respawn_player

    def flaky(player):
        if player is victim:
            raise RuntimeError("boom")
        real(player)

    monkeypatch.setattr(lifecycle, "respawn_player", flaky)
    dug.loop.run_until_complete(lifecycle.process_respawns())
    assert other.alive
    assert not victim.alive
    assert time.time() - victim.death_time < 5.0
