"""Dedicated-launch and reconstructed Training.vxl tutorial regressions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.constants as C
from modes import get_mode_class
from modes.tutorial import TutorialMode, TutorialStage
from server.config import ServerConfig
from server.runtime_paths import RuntimePaths
from server.tutorial_launcher import (
    TRAINING_MAP_SHA256,
    configure_tutorial_runtime,
    inspect_training_map,
)
from server.world_manager import WorldManager
from shared.bytes import ByteReader
from shared.packet import HelpMessage, SetClassLoadout


ROOT = Path(__file__).resolve().parents[1]


class _Server:
    def __init__(self, config, world_manager):
        self.config = config
        self.world_manager = world_manager
        self.loop_count = 1
        self.sent: list[tuple[bytes, dict]] = []

    def broadcast(self, data, **kwargs):
        self.sent.append((bytes(data), kwargs))


class _Player:
    def __init__(self, player_id: int):
        self.id = player_id
        self.x = self.y = self.z = 0.0
        self.spawned = True
        self.alive = True
        self.input = SimpleNamespace(jump=False, crouch=False)
        self.last_trigger_jump = False
        self.sent: list[bytes] = []
        self.disconnected_reason = None
        self.class_id = int(C.CLASS_SOLDIER)
        self.loadout: list[int] = []
        self.tool = int(C.PISTOL_TOOL)

    def send(self, data, reliable=True):
        self.sent.append(bytes(data))

    def disconnect(self, reason=0):
        self.disconnected_reason = int(reason)

    def apply_class_selection(self, selection):
        self.class_id = int(selection.class_id)
        self.loadout = list(selection.loadout)

    def set_tool(self, tool, raw=True):
        self.tool = int(tool)


def _new_mode() -> tuple[_Server, TutorialMode]:
    paths = RuntimePaths.from_root(ROOT)
    config = configure_tutorial_runtime(ServerConfig(), paths, port=32901)
    world = WorldManager(config)
    world.load_map("Training")
    server = _Server(config, world)
    mode = TutorialMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    return server, mode


def test_normal_mode_registry_cannot_select_tutorial():
    assert get_mode_class("tut") is None
    assert get_mode_class("tutorial") is None


def test_tutorial_launcher_locks_runtime_without_rewriting_config():
    paths = RuntimePaths.from_root(ROOT)
    config = ServerConfig()
    config.default_mode = "tdm"
    config.default_map = "London"

    result = configure_tutorial_runtime(config, paths, port=32901)

    assert result is config
    assert result.tutorial_runtime is True
    assert result.default_mode == "tut"
    assert result.default_map == "Training"
    assert result.port == 32901
    assert result.max_players == 12
    assert result.map_rotation == []
    assert result.plugins_enabled is False
    assert result.bots.enabled is False
    assert result.steam.enabled is False
    assert result.revival.enabled is False
    assert result.game_rules.enabled("RULE_ENABLE_BLOCKS") is True
    assert result.game_rules.enabled("RULE_ENABLE_COLOUR_PICKER") is False

    with pytest.raises(ValueError, match="between 1 and 65535"):
        configure_tutorial_runtime(ServerConfig(), paths, port=0)


def test_genuine_training_map_and_repeated_target_geometry_are_present():
    detail = inspect_training_map(RuntimePaths.from_root(ROOT))
    assert TRAINING_MAP_SHA256 in detail

    _server, mode = _new_mode()
    try:
        assert len(mode._target_voxels) == 12
        assert [len(lane) for lane in mode._target_voxels] == [5] * 12
        assert {
            len(target)
            for lane in mode._target_voxels
            for target in lane
        } == {13}
    finally:
        asyncio.run(mode.deactivate())


def test_twelve_interior_lanes_are_unique_and_reusable_by_object_identity():
    _server, mode = _new_mode()
    try:
        players = [_Player(index) for index in range(12)]
        spawns = [mode.get_spawn_point(player) for player in players]

        assert len(set(spawns)) == 12
        assert spawns[0] == (140.5, 76.5, 230.75)
        assert spawns[-1] == (438.5, 448.5, 230.75)
        with pytest.raises(RuntimeError, match="twelve tutorial lanes"):
            mode.get_spawn_point(_Player(12))

        asyncio.run(mode.on_player_leave(players[0]))
        replacement = _Player(0)  # numeric id reuse must not alias old state
        assert mode.get_spawn_point(replacement) == spawns[0]
        assert mode.session_for(replacement) is not None
        assert mode.session_for(players[0]) is None
    finally:
        asyncio.run(mode.deactivate())


def test_target_destruction_and_block_line_complete_the_native_lessons():
    server, mode = _new_mode()
    player = _Player(1)
    player.x, player.y, player.z = mode.get_spawn_point(player)
    asyncio.run(mode.on_player_join(player))
    connection = SimpleNamespace(player=player, send=player.send)
    mode.reveal_to(connection)

    try:
        intro_data = next(data for data in player.sent if data[0] == 109)
        intro = HelpMessage()
        intro.read(ByteReader(intro_data[1:]))
        assert intro.message_ids == ["TUTORIAL_INTRO"]

        session = mode.session_for(player)
        assert session is not None
        mode._enter_stage(session, TutorialStage.SHOOTING, 1.0)
        shooting_data = [data for data in player.sent if data[0] == 13][-1]
        shooting = SetClassLoadout()
        shooting.read(ByteReader(shooting_data[1:]))
        assert shooting.instant == 1
        assert shooting.loadout == [int(C.PISTOL_TOOL)]
        assert player.loadout == [int(C.PISTOL_TOOL)]
        assert player.tool == int(C.PISTOL_TOOL)
        assert mode.allows_equipped_tool(player, int(C.PISTOL_TOOL)) is True
        assert mode.allows_equipped_tool(player, int(C.BLOCK_TOOL)) is False

        for target in mode._target_voxels[0]:
            coordinate = next(iter(target))
            assert server.world_manager.set_block(*coordinate, False, 0)

        asyncio.run(mode.on_tick(1))
        assert session.destroyed_targets == {0, 1, 2, 3, 4}
        assert session.stage is TutorialStage.CLIMB
        assert sum(data[0] == 50 for data in player.sent) == 5
        climb_data = [data for data in player.sent if data[0] == 13][-1]
        climb = SetClassLoadout()
        climb.read(ByteReader(climb_data[1:]))
        assert climb.loadout == [
            int(C.PISTOL_TOOL), int(C.BLOCK_TOOL), int(C.SPADE_TOOL)
        ]
        assert player.loadout == [
            int(C.PISTOL_TOOL), int(C.BLOCK_TOOL), int(C.SPADE_TOOL)
        ]
        assert player.tool == int(C.SPADE_TOOL)
        assert mode.allows_equipped_tool(player, int(C.BLOCK_TOOL)) is True

        # Every disc (21 cells) is gone, not only the shot red voxel.
        for disc in mode._target_discs[0]:
            assert len(disc) == 21
            assert not any(server.world_manager.get_solid(*cell) for cell in disc)

        # Building alone no longer finishes CLIMB ("to the top of the tower").
        assert server.world_manager.set_block(70, 70, 220, True, 0x123456)
        assert server.world_manager.set_block(71, 70, 220, True, 0x123456)
        asyncio.run(mode.on_tick(2))
        assert session.stage is TutorialStage.CLIMB

        player.x, player.y, player.z = 118.5 + 3.0, 51.5, 194.75
        asyncio.run(mode.on_tick(3))
        assert session.stage is TutorialStage.COMPLETE
        assert {23, 84, 109}.issubset({data[0] for data in player.sent})
    finally:
        asyncio.run(mode.deactivate())


def test_basic_lesson_advances_at_retail_capsule_collision_plane():
    """The player cannot physically reach the obstacle's raw x=134 plane."""

    _server, mode = _new_mode()
    player = _Player(1)
    player.x, player.y, player.z = mode.get_spawn_point(player)
    asyncio.run(mode.on_player_join(player))
    session = mode.session_for(player)
    assert session is not None
    session.revealed = True
    session.stage = TutorialStage.BASIC_CONTROLS
    session.stage_started = 0.0
    player.x = 134.45

    try:
        asyncio.run(mode.on_tick(1))
        assert session.stage is TutorialStage.JUMP
    finally:
        asyncio.run(mode.deactivate())


def test_first_damage_on_a_red_cell_drops_the_whole_disc():
    server, mode = _new_mode()
    player = _Player(1)
    player.x, player.y, player.z = mode.get_spawn_point(player)
    asyncio.run(mode.on_player_join(player))
    mode.reveal_to(SimpleNamespace(player=player, send=player.send))
    session = mode.session_for(player)
    try:
        mode._enter_stage(session, TutorialStage.SHOOTING, 1.0)
        red = next(iter(mode._target_voxels[0][2]))
        # One pistol hit leaves a partial-damage entry, no destroyed voxel.
        server.world_manager.block_damage[red] = 1.0
        asyncio.run(mode.on_tick(1))
        assert session.destroyed_targets == {2}
        assert not any(
            server.world_manager.get_solid(*cell) for cell in mode._target_discs[0][2]
        )
        # 21 checked kill-damage packets replicate the removal.
        assert sum(data[0] == 37 for data, _ in server.sent) == 21
        remaining = [data for data in player.sent if data[0] == 50]
        assert len(remaining) == 1
    finally:
        asyncio.run(mode.deactivate())


def test_tower_gate_requires_the_dome_not_the_ledge():
    _server, mode = _new_mode()
    player = _Player(1)
    player.x, player.y, player.z = mode.get_spawn_point(player)
    session = mode.session_for(player)
    try:
        player.x, player.y, player.z = 118.5, 51.5 + 9.0, 204.75  # ledge
        assert not mode._on_tower_top(session, player)
        player.z = 194.75
        assert mode._on_tower_top(session, player)
        player.x = 118.5 + 12.0  # a pillar beside the tower
        assert not mode._on_tower_top(session, player)
    finally:
        asyncio.run(mode.deactivate())


def test_reused_lane_restores_dug_and_built_cells():
    server, mode = _new_mode()
    world = server.world_manager
    first = _Player(1)
    mode.get_spawn_point(first)
    try:
        dug = (130, 76, 233)
        assert world.get_solid(*dug)
        color = int(world.get_color(*dug)) & 0xFFFFFF
        assert world.destroy_blocks([dug])
        built = (130, 70, 220)
        assert not world.get_solid(*built)
        assert world.set_block(*built, True, 0x123456)
        asyncio.run(mode.on_player_leave(first))

        second = _Player(2)
        mode.get_spawn_point(second)
        assert world.get_solid(*dug)
        assert int(world.get_color(*dug)) & 0xFFFFFF == color
        assert not world.get_solid(*built)
        server.sent.clear()
        asyncio.run(mode.on_player_join(second))
        kinds = [data[0] for data, _ in server.sent]
        assert kinds.count(37) == 1  # the built cell's removal
        assert 33 in kinds  # the dug cell's BlockBuildColored
    finally:
        asyncio.run(mode.deactivate())


def test_initial_info_overrides_only_the_retail_colour_picker():
    _server, mode = _new_mode()
    packet = SimpleNamespace(
        enable_minimap=1, enable_deathcam=1, enable_spectator=1,
        enable_fall_on_water_damage=1, enable_colour_picker=1,
        enable_colour_palette=0, enable_player_score=1,
    )
    try:
        mode.configure_initial_info(packet)
        assert packet.enable_colour_picker == 0
        assert packet.enable_colour_palette == 1
        assert packet.enable_minimap == 1
        assert packet.enable_deathcam == 1
        assert packet.enable_spectator == 1
        assert packet.enable_fall_on_water_damage == 1
        assert packet.enable_player_score == 0
    finally:
        asyncio.run(mode.deactivate())


def test_tutorial_script_matches_shared_fixture():
    """Pins the server lesson script to the JSON shared with the client."""

    import json

    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "tutorial_script.json").read_text(
            encoding="utf-8"
        )
    )
    stages = [stage.name.lower() for stage in TutorialStage]
    assert fixture["stages"] == stages
    for stage in TutorialStage:
        assert fixture["messages"][stage.name.lower()] == list(
            TutorialMode.HELP_BY_STAGE[stage]
        )
    assert fixture["intro_seconds"] == TutorialMode.INTRO_SECONDS
    assert fixture["help_transition_delay"] == TutorialMode.HELP_TRANSITION_DELAY
    assert fixture["completion_seconds"] == TutorialMode.COMPLETION_SECONDS
    tools = {
        "pistol": int(C.PISTOL_TOOL),
        "block": int(C.BLOCK_TOOL),
        "spade": int(C.SPADE_TOOL),
    }
    loadouts = fixture["loadouts"]
    assert [tools[t] for t in loadouts["movement"]] == list(TutorialMode.MOVEMENT_LOADOUT)
    assert [tools[t] for t in loadouts["shooting"]] == list(TutorialMode.SHOOTING_LOADOUT)
    assert [tools[t] for t in loadouts["climb"]] == list(TutorialMode.CLIMB_LOADOUT)
    assert loadouts["climb_equips"] == loadouts["climb"][-1]
    tower = fixture["tower"]
    assert tuple(tower["center_local"]) == TutorialMode.TOWER_CENTER_LOCAL
    assert tower["radius"] == TutorialMode.TOWER_RADIUS
    assert tower["max_z"] == TutorialMode.TOWER_TOP_MAX_Z
    targets = fixture["targets"]
    assert [tuple(c) for c in targets["centers_local"]] == list(TutorialMode.TARGET_CENTERS)
    assert targets["count"] == len(TutorialMode.TARGET_CENTERS) == 5
    assert targets["rgb"] == TutorialMode.TARGET_RGB
    assert targets["disc_cells"] == 21
    assert targets["red_cells"] == 13
    assert [tuple(o) for o in fixture["lane_origins"]] == list(TutorialMode.LANE_ORIGINS)
    assert tuple(fixture["spawn_local"]) == TutorialMode.SPAWN_LOCAL
    assert fixture["target_counter_string"] == "TUTORIAL_DESTROY_TARGET"
    assert fixture["complete_sound_id"] == 27

    # Gate thresholds are literals in on_tick; pin them by behaviour.
    import inspect

    source = inspect.getsource(TutorialMode.on_tick)
    gates = fixture["gates"]
    assert f"session.minimum_local_x <= {gates['basic_controls_max_local_x']}" in source
    assert f"session.minimum_local_x <= {gates['jump_with_input_max_local_x']}" in source
    assert f"session.minimum_local_x <= {gates['jump_fallback_max_local_x']}" in source
    assert f"session.minimum_local_x <= {gates['crouch_with_input_max_local_x']}" in source
    assert f"session.minimum_local_x <= {gates['crouch_fallback_max_local_x']}" in source
