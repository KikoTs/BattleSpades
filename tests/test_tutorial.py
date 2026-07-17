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

        assert server.world_manager.set_block(70, 70, 220, True, 0x123456)
        assert server.world_manager.set_block(71, 70, 220, True, 0x123456)
        asyncio.run(mode.on_tick(2))

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
