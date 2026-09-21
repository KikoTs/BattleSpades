"""Project proposals against production navigation and authored KV6 geometry."""
from __future__ import annotations

from dataclasses import replace
import itertools
import math
import pickle
from types import SimpleNamespace

import pytest
import shared.constants as C

from server import prefabs
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import BotAction, BotActionKind, MapSnapshot, PlayerSnapshot
from server.bot_ai.prefab_policy import BOT_PREFAB_BLOCK_COUNTS, load_bot_prefab_geometry
from server.bot_ai.project_sites import (
    MAX_PROJECT_BUILD_REACH, _exits, _ray_clear, find_breach_project,
    find_bridge_project, find_decorative_site, find_mine_approach,
    find_prefab_cover, find_sniper_outpost,
)
from server.bot_ai.simple_navigation import SimpleVoxelWorld
from server.prefab_actions import PrefabActionService


class VoxelFixture:
    def __init__(self, solids: set[tuple[int, int, int]]) -> None:
        self.solids = set(solids)
        self.reads = 0

    def get_solid(self, x: int, y: int, z: int) -> bool:
        self.reads += 1
        return (x, y, z) in self.solids

    def surface_z(self, x: int, y: int) -> int:
        return min((z for cx, cy, z in self.solids if cx == x and cy == y), default=239)

    def get_height(self, x: int, y: int) -> int:
        return self.surface_z(x, y)

    def set_block(self, x: int, y: int, z: int, solid: bool, color: object) -> bool:
        if solid:
            self.solids.add((x, y, z))
        else:
            self.solids.discard((x, y, z))
        return True


def make_world(*, thick: bool = False) -> SimpleVoxelWorld:
    world = SimpleVoxelWorld()
    world._vxl = VoxelFixture({(x, y, z) for x in range(5, 51) for y in range(5, 36)
                               for z in (range(20, 24) if thick else (20,))})
    return world


def player(**overrides: object) -> PlayerSnapshot:
    snapshot = PlayerSnapshot(1, 1, 2, int(C.CLASS_SCOUT), True, True,
                              (20.5, 20.5, 17.75), (20.5, 20.5, 17.75),
                              (1., 0., 0.), (0., 0., 0.), 100, int(C.SNIPER_TOOL),
                              100, 5, 20, True,
                              loadout=(int(C.SNIPER_TOOL), int(C.PREFAB_TOOL),
                                       int(C.BLOCK_TOOL), int(C.LANDMINE_TOOL), int(C.SUPERSPADE_TOOL)),
                              prefabs=("prefab_small_wall",),
                              deployable_stock=((int(C.LANDMINE_TOOL), 1),))
    return replace(snapshot, **overrides)


@pytest.fixture(scope="module")
def geometry():
    return {item.name: item for item in load_bot_prefab_geometry(BOT_PREFAB_BLOCK_COUNTS)}


def test_cached_geometry_matches_real_authoritative_expansion_all_yaws(geometry):
    assert len(geometry) <= 20
    for name, shape in geometry.items():
        model = prefabs.get_registry().get(name)
        assert shape.block_count == len(model.get_points()) <= 256
        for yaw in range(4):
            expected = tuple(cell for cell, _ in prefabs.expand_prefab(model, (0, 0, 0), yaw, 0, 0))
            assert shape.cells_by_yaw[yaw] == expected
    assert pickle.loads(pickle.dumps(tuple(geometry.values()))) == tuple(geometry.values())
    assert "prefab_superdome" not in geometry


def test_metadata_loader_bounds_even_an_infinite_duplicate_input():
    result = load_bot_prefab_geometry(itertools.repeat("prefab_small_wall"))
    assert len(result) == 1 and result[0].block_count == 6


def test_optional_sites_fail_closed_when_friendly_roster_exceeds_bound(geometry):
    world, bot = make_world(thick=True), player()
    world.prefab_geometry = geometry
    friends = ((10.5, 10.5, 17.75),) * 33
    lane = (40.5, 20.5, 17.75)
    for helper in (find_sniper_outpost, find_prefab_cover, find_mine_approach):
        assert helper(world, bot, lane, friendly_positions=friends) is None
    assert find_decorative_site(world, bot, friendly_positions=friends) is None


def test_map_load_replaces_prefab_metadata_including_empty_maps(geometry):
    world = SimpleVoxelWorld()
    world.load(MapSnapshot(1, 1, b"", "tdm", prefab_geometry=tuple(geometry.values())))
    assert world.prefab_geometry == geometry
    world.load(MapSnapshot(2, 0, b"", "tdm"))
    assert world.prefab_geometry == {}


def test_outpost_requires_known_clear_lane_access_and_multiple_exits():
    world, bot = make_world(), player()
    site = find_sniper_outpost(world, bot, (40.5, 20.5, 17.75))
    assert site is not None and len(site.exits) >= 2
    assert find_sniper_outpost(world, bot, (math.nan, 20., 17.75)) is None
    assert find_sniper_outpost(world, bot, (23., 20., 17.75)) is None
    world._vxl.solids.update((30, y, z) for y in range(5, 36) for z in range(10, 20))
    assert find_sniper_outpost(world, bot, (40.5, 20.5, 17.75)) is None


def test_cover_uses_equipped_shape_preserves_firing_gap_and_two_exits(geometry, monkeypatch):
    world, bot, lane = make_world(), player(), (40.5, 20.5, 17.75)
    world.prefab_geometry = geometry
    before = set(world._vxl.solids)
    monkeypatch.setattr("builtins.open", lambda *_a, **_k: pytest.fail("worker project did disk I/O"))
    site = find_prefab_cover(world, bot, lane, rear=True)
    assert site is not None and site.prefab_name == "prefab_small_wall"
    assert site.required_blocks == site.authored_blocks == 6
    assert math.dist(bot.eye, site.position) <= MAX_PROJECT_BUILD_REACH
    assert _ray_clear(world, bot.eye, lane, frozenset(site.cells))
    assert len(_exits(world, bot.position, frozenset(site.cells))) >= 2
    assert site.support_cells and before == world._vxl.solids
    assert find_prefab_cover(world, replace(bot, prefabs=()), lane) is None
    assert find_prefab_cover(world, replace(bot, blocks=5), lane) is None
    assert find_prefab_cover(world, replace(bot, loadout=(int(C.SNIPER_TOOL),)), lane) is None


def test_cover_rejects_reserved_sites_and_covered_surface_snap(geometry):
    world, bot, lane = make_world(), player(), (40.5, 20.5, 17.75)
    reserved = frozenset((x, y, z) for x in range(10, 31) for y in range(10, 31) for z in range(15, 20))
    assert find_prefab_cover(world, bot, lane, geometry, reserved_cells=reserved) is None
    world._vxl.solids.update((x, y, 10) for x in range(10, 31) for y in range(10, 31))
    assert find_prefab_cover(world, bot, lane, geometry) is None


def test_cover_geometry_not_name_substrings_and_no_flat_platform_misclassified(geometry):
    world, bot, lane = make_world(), player(), (40.5, 20.5, 17.75)
    renamed = replace(geometry["prefab_small_wall"], name="prefab_authored_cover")
    assert find_prefab_cover(world, replace(bot, prefabs=(renamed.name,)), lane,
                             {renamed.name: renamed}) is not None
    assert find_prefab_cover(world, replace(bot, prefabs=("prefab_small_platform",)), lane,
                             geometry) is None
    # The old proposal must be re-evaluated after its floor is removed.
    world._vxl.solids = {(x, y, 20) for x in range(19, 22) for y in range(19, 22)}
    assert find_prefab_cover(world, bot, lane, geometry) is None


def test_planned_prefab_passes_real_gateway_and_service_exactly(geometry, monkeypatch):
    world, bot = make_world(), player()
    site = find_prefab_cover(world, bot, (40.5, 20.5, 17.75), geometry, rear=True)
    assert site is not None
    owner_packets, remote_packets = [], []
    owner = SimpleNamespace(id=1, team=2, name="ProjectFixture", class_id=bot.class_id,
                            is_bot=True, alive=True, spawned=True, x=bot.position[0],
                            y=bot.position[1], z=bot.position[2], blocks=bot.blocks,
                            prefabs=list(bot.prefabs), loadout=list(bot.loadout), tool=bot.tool,
                            tool_is_raw=True, block_color=0xAABBCC,
                            send=lambda data, **_kwargs: owner_packets.append(data))
    owner.set_tool = lambda tool, raw=True: setattr(owner, "tool", tool)
    server = SimpleNamespace(world_manager=world._vxl, players={1: owner}, loop_count=10,
                             teams={2: SimpleNamespace(color=(10, 20, 30), infinite_blocks=False)},
                             broadcast=lambda data, **_kwargs: remote_packets.append(data))
    server.prefab_actions = PrefabActionService(server)
    monkeypatch.setattr("server.prefab_actions.play_sound", lambda *_args, **_kwargs: None)
    before = set(world._vxl.solids)
    assert BotActionGateway(server).execute(owner, BotAction(BotActionKind.PLACE_PREFAB,
        tool_id=int(C.PREFAB_TOOL), position=site.position, argument=site.prefab_name, yaw=site.yaw))
    assert world._vxl.solids - before == set(site.cells)
    assert owner.blocks == bot.blocks - site.required_blocks
    assert len(remote_packets) == 6 and len(owner_packets) == 7


def test_bridge_requires_complete_affordable_crossing_and_far_exit():
    world, bot = make_world(), player()
    world._vxl.solids.difference_update((x, y, 20) for x in range(21, 25) for y in range(5, 36))
    site = find_bridge_project(world, bot, (40.5, 20.5, 17.75))
    assert site is not None and site.required_blocks == 4 and site.landing == (25.5, 20.5, 17.75)
    assert find_bridge_project(world, replace(bot, blocks=3), (40.5, 20.5, 17.75)) is None
    world._vxl.solids.update(site.cells)
    route = world.plan(bot.position, site.exits[-1], abilities=frozenset())
    assert route.reached_segment_goal and route.steps
    world._vxl.solids.difference_update(site.cells)
    world._vxl.solids.update((26, 20, z) for z in range(17, 20))
    assert find_bridge_project(world, bot, (40.5, 20.5, 17.75)) is None


def test_mine_needs_real_stock_thick_support_and_no_friendly_blast_exposure():
    world, bot, lane = make_world(thick=True), player(), (40.5, 20.5, 17.75)
    site = find_mine_approach(world, bot, lane)
    assert site is not None and site.kind == "mine_approach"
    assert find_mine_approach(world, replace(bot, deployable_stock=()), lane) is None
    assert find_mine_approach(make_world(), bot, lane) is None  # fragile one-cell bridge/floor
    alternate = find_mine_approach(world, bot, lane, friendly_positions=(site.position, bot.position))
    assert alternate is not None and math.dist(alternate.position, site.position) >= 6
    assert find_mine_approach(world, bot, lane, friendly_positions=(site.position,
        (20.5, 27.5, 20.), (20.5, 13.5, 20.))) is None


def test_decoration_is_one_supported_non_destructive_block_off_body_and_routes():
    world, bot = make_world(), player()
    before = set(world._vxl.solids)
    site = find_decorative_site(world, bot)
    assert site is not None and len(site.cells) == site.required_blocks == 1
    assert len(site.exits) >= 3 and world.solid(*site.support_cells[0])
    assert world._vxl.solids == before
    assert find_decorative_site(world, replace(bot, blocks=0)) is None


def test_breach_uses_real_dig_footprint_has_dry_exit_and_never_removes_floor():
    world, bot = make_world(), player()
    wall = {(x, y, z) for x in (23, 24) for y in range(18, 23) for z in range(16, 20)}
    world._vxl.solids.update(wall)
    site = find_breach_project(world, bot, (40.5, 20.5, 17.75))
    assert site is not None and site.kind == "breach"
    assert site.landing is not None and site.landing[0] > 24
    assert set(site.cells) <= wall and max(cell[2] for cell in site.cells) < 20
    assert 0 < site.estimated_seconds <= 8 and site.tool_id == int(C.SUPERSPADE_TOOL)
    assert set(site.cells).isdisjoint(site.support_cells)
    assert find_breach_project(world, replace(bot, loadout=(int(C.SNIPER_TOOL),)), (40.5, 20.5, 17.75)) is None
    assert find_breach_project(world, bot, (40.5, 20.5, 17.75), reserved_cells=frozenset(wall)) is None
    world._vxl.solids.difference_update(site.cells)
    route = world.plan(bot.position, site.exits[-1], abilities=frozenset())
    assert route.reached_segment_goal and route.steps
    assert find_breach_project(world, bot, (40.5, 20.5, 17.75)) is None
    world._vxl.solids.update(site.cells)
    world._vxl.solids.update((x, y, z) for x in range(25, 28) for y in range(18, 23) for z in range(16, 20))
    assert find_breach_project(world, bot, (40.5, 20.5, 17.75)) is None


def test_helpers_have_small_fixed_search_cost_even_on_blocked_maps(geometry):
    world, bot, lane = make_world(), player(), (40.5, 20.5, 17.75)
    find_sniper_outpost(world, bot, lane)
    find_prefab_cover(world, bot, lane, geometry)
    find_mine_approach(world, bot, lane)
    find_decorative_site(world, bot)
    find_breach_project(world, bot, lane)
    assert world._vxl.reads < 20000


@pytest.mark.parametrize("change", [{"alive": False}, {"spawned": False},
                                  {"grounded": False}, {"wade": True},
                                  {"position": (math.nan, 20., 17.75)}])
def test_projects_fail_closed_for_non_actionable_lives(change, geometry):
    world, bot, lane = make_world(), player(**change), (40.5, 20.5, 17.75)
    assert find_sniper_outpost(world, bot, lane) is None
    assert find_prefab_cover(world, bot, lane, geometry) is None
    assert find_bridge_project(world, bot, lane) is None
    assert find_mine_approach(world, bot, lane) is None
    assert find_breach_project(world, bot, lane) is None
    assert find_decorative_site(world, bot) is None
