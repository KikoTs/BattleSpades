"""The InitialInfo class speed scale is the lobby rule, not the sprint table.

Retail ``GameClass`` multiplies its accel, sprint and crouch/sneak tables by
``InitialInfo.movement_speed_multipliers[class_id]``, so that list has to be
1.0 at default rules. Ground terminal speed is then acceleration over the
ground friction 4, 32 blocks per native unit: 8 blocks/s per class multiplier.
"""

import pytest

import shared.constants as C
from modes.zombie import ZombieMode
from server import class_data
from server.builders.initial_info import build_initial_info
from server.config import ServerConfig
from server.main import BattleSpadesServer
from shared.bytes import ByteReader
from shared.packet import InitialInfo
from tests.test_reversed_movement_engine import (
    GROUND_Z,
    TEST_COLOR,
    advance_player,
    make_player,
    make_world_manager,
)


# InitialInfo sent by a live Jagex TDM server ("US East AT23-9", London), as
# recorded in the community pyckaxe protocol notes. 0x31 is the transport
# prefix and 0x69 the packet id of that build; the body has today's layout.
_RETAIL_INITIAL_INFO = bytes.fromhex(
    "3169007c55817a0e4001cbed5e407f80000054444d5f5449544c450054444d5f"
    "4445534352495054494f4e0054444d5f494e464f475241504849435f54455854"
    "310054444d5f494e464f475241504849435f54455854320054444d5f494e464f"
    "475241504849435f5445585433004c6f6e646f6e004c6f6e646f6e00801b5323"
    "0600eb69000100c0010001010100010001000140004000022526000e40004000"
    "4000400040004000400040004000400040004000400040000001555320456173"
    "7420415432332d3900023b3a37ee283640ef000000000106"
)

_ZOMBIE_CLASSES = (C.CLASS_ZOMBIE, C.CLASS_FAST_ZOMBIE, C.CLASS_JUMP_ZOMBIE)


def _received(packet):
    """The InitialInfo a client decodes, 1/64 fixed point included."""
    return InitialInfo(ByteReader(bytes(packet.generate())[1:]))


def _server(mode="tdm", **rules):
    config = ServerConfig()
    config.default_mode = mode
    config.game_rules.apply(rules)
    return BattleSpadesServer(config)


class _NativeProfile:
    """Records the class multipliers Player hands the native mover."""

    def __init__(self):
        self.values = {}

    def __getattr__(self, name):
        if not name.startswith("set_class_"):
            raise AttributeError(name)
        return lambda value: self.values.__setitem__(name[10:], value)


def _authority_profile(config, class_id):
    player = make_player()
    player.connection.server.config = config
    player.class_id = int(class_id)
    native = _NativeProfile()
    player._apply_class_profile_to_world(native)
    return native.values


def _predicted_profile(info, class_id):
    """Retail GameClass.__init__ for the received InitialInfo."""
    scale = info.movement_speed_multipliers[int(class_id)]
    return {
        "accel_multiplier": C.CLASS_ACCEL_MULTIPLIER[class_id] * scale,
        "sprint_multiplier": C.CLASS_SPRINT_MULTIPLIER[class_id] * scale,
        "crouch_sneak_multiplier": C.CLASS_CROUCH_SNEAK_MULTIPLIER[class_id] * scale,
    }


def _assert_prediction_matches_authority(server):
    info = _received(build_initial_info(server))
    for class_id in class_data.CLASS_IDS:
        authority = _authority_profile(server.config, class_id)
        for field, predicted in _predicted_profile(info, class_id).items():
            assert authority[field] == pytest.approx(predicted, abs=1e-9), (
                class_id, field,
            )
    return info


def test_original_server_sent_unit_speed_for_every_class():
    info = InitialInfo(ByteReader(_RETAIL_INITIAL_INFO[2:]))

    assert (info.mode_name, info.filename) == ("TDM_TITLE", "London")
    assert info.movement_speed_multipliers == [1.0] * 14


def test_default_rules_send_unit_speed_for_every_class():
    info = _received(build_initial_info(_server()))

    assert info.movement_speed_multipliers == [1.0] * (max(class_data.CLASS_IDS) + 1)
    assert class_data.initial_info_movement_multipliers() == info.movement_speed_multipliers
    for class_id in class_data.CLASS_IDS:
        assert class_data.speed_scale(class_id) == 1.0


@pytest.mark.parametrize("label,scale", [
    ("50%", 0.5), ("100%", 1.0), ("150%", 1.5), ("200%", 2.0),
])
def test_character_speed_rule_is_the_whole_class_scale(label, scale):
    server = _server(RULE_CHARACTER_SPEED=label)

    info = _assert_prediction_matches_authority(server)

    assert info.movement_speed_multipliers == [scale] * len(info.movement_speed_multipliers)
    soldier = _authority_profile(server.config, C.CLASS_SOLDIER)
    assert soldier["accel_multiplier"] == pytest.approx(0.7 * scale)
    assert soldier["sprint_multiplier"] == pytest.approx(1.4 * scale)
    assert soldier["crouch_sneak_multiplier"] == pytest.approx(0.5 * scale)
    assert soldier["jump_multiplier"] == pytest.approx(1.2)


@pytest.mark.parametrize("character_label,character", [("100%", 1.0), ("150%", 1.5)])
@pytest.mark.parametrize("zombie_label,zombie", [
    ("50%", 0.5), ("100%", 1.0), ("200%", 2.0),
])
def test_zombie_speed_rule_reaches_prediction_and_authority_once(
    character_label, character, zombie_label, zombie,
):
    server = _server(
        "zom", RULE_CHARACTER_SPEED=character_label, RULE_CLASS_SPEED=zombie_label,
    )
    # With the mode attached, its configure_initial_info runs on the packet.
    server.mode = ZombieMode(server)

    info = _assert_prediction_matches_authority(server)

    for class_id in class_data.CLASS_IDS:
        expected = character * (zombie if class_id in _ZOMBIE_CLASSES else 1.0)
        assert info.movement_speed_multipliers[class_id] == expected, class_id
    infected = _authority_profile(server.config, C.CLASS_ZOMBIE)
    assert infected["sprint_multiplier"] == pytest.approx(1.65 * character * zombie)


def test_zombie_speed_rule_is_ignored_outside_zombie_mode():
    server = _server(RULE_CLASS_SPEED="200%")

    info = _assert_prediction_matches_authority(server)

    assert info.movement_speed_multipliers[C.CLASS_ZOMBIE] == 1.0


def _ground_speed(class_id, *, sprint=False, crouch=False, sneak=False):
    """Blocks travelled in one second at terminal speed on flat ground."""
    manager = make_world_manager()
    for x in range(96, 150):
        for y in range(97, 104):
            manager.map.set_point(x, y, GROUND_Z, True, TEST_COLOR)
    player = make_player(manager, flatten=False)
    player.connection.server.config = ServerConfig()
    player.class_id = int(class_id)
    player.set_orientation_vector(1.0, 0.0, 0.0)
    player.update_input(True, False, False, False, False, crouch, sneak, sprint)
    advance_player(player, 150)  # friction 4/s: terminal within 1e-4 after 2.5 s
    start = player.x
    advance_player(player, 60)
    return player.x - start


@pytest.mark.parametrize("class_id,gait,blocks_per_second", [
    (C.CLASS_SOLDIER, {}, 5.6),
    (C.CLASS_SOLDIER, {"sprint": True}, 11.2),
    (C.CLASS_SOLDIER, {"crouch": True}, 4.0),
    (C.CLASS_SOLDIER, {"sneak": True}, 4.0),
    # Classic Soldier walks at the 8 blocks/s of Ace of Spades 0.75.
    (C.CLASS_CLASSIC_SOLDIER, {}, 8.0),
    (C.CLASS_CLASSIC_SOLDIER, {"sprint": True}, 10.64),
    (C.CLASS_SCOUT, {"sprint": True}, 11.6),
    (C.CLASS_ENGINEER, {"sprint": True}, 10.0),
    (C.CLASS_ZOMBIE, {}, 4.0),
    (C.CLASS_ZOMBIE, {"sprint": True}, 13.2),
])
def test_ground_speed_is_eight_blocks_per_class_multiplier(
    class_id, gait, blocks_per_second,
):
    assert _ground_speed(class_id, **gait) == pytest.approx(
        blocks_per_second, abs=0.01
    )
