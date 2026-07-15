import asyncio
from types import SimpleNamespace

import pytest

import shared.constants as C
from protocol.packet_handler import PacketHandler
from server.class_selection import (
    DEFAULT_DISABLED_TOOLS,
    deployable_authorized,
    normalize_class_selection,
)
from server.config import ServerConfig
from server.class_data import default_client_loadout
from server.game_constants import TEAM1
from server.player import Player
from shared.packet import ChangeClass, SetClassLoadout


def _live_medic() -> Player:
    player = Player(3, "SelectionTest", TEAM1, C.LIGHT_MACHINE_GUN_TOOL, None)
    player.apply_class_selection(normalize_class_selection(C.CLASS_MEDIC))
    player.spawn(100.5, 100.5, 60.0)
    return player


def _handler() -> PacketHandler:
    return PacketHandler(SimpleNamespace(config=ServerConfig()))


def _loadout_packet(class_id: int, loadout: list[int], instant: int = 0) -> bytes:
    packet = SetClassLoadout()
    packet.player_id = 3
    packet.class_id = int(class_id)
    packet.instant = int(instant)
    packet.loadout = list(loadout)
    packet.prefabs = []
    packet.ugc_tools = []
    return bytes(packet.generate())


def _change_class_packet(class_id: int) -> bytes:
    packet = ChangeClass()
    packet.player_id = 3
    packet.class_id = int(class_id)
    return bytes(packet.generate())


def test_normalizer_rejects_cross_class_tools_and_fills_required_items():
    selection = normalize_class_selection(
        C.CLASS_MINER,
        [C.BLOCK_TOOL, C.MEDPACK_TOOL, C.BLOCK_SUCKER_TOOL, C.C4_TOOL],
    )

    assert selection.class_id == C.CLASS_MINER
    assert C.MEDPACK_TOOL not in selection.loadout
    assert C.BLOCK_SUCKER_TOOL in selection.loadout
    assert C.C4_TOOL in selection.loadout
    assert selection.loadout.count(C.BLOCK_TOOL) == 1


@pytest.mark.parametrize(
    "class_id",
    [
        C.CLASS_SOLDIER,
        C.CLASS_SCOUT,
        C.CLASS_ROCKETEER,
        C.CLASS_MINER,
        C.CLASS_CLASSIC_SOLDIER,
        C.CLASS_ENGINEER,
        C.CLASS_SPECIALIST,
        C.CLASS_MEDIC,
    ],
)
def test_normalized_default_loadout_preserves_stock_tool_carousel_order(class_id):
    """Visually similar normal/flare blocks must keep their retail slots."""
    expected = list(dict.fromkeys(default_client_loadout(
        class_id,
        disabled_tools=DEFAULT_DISABLED_TOOLS,
    )))

    assert list(normalize_class_selection(class_id).loadout) == expected


def test_prefab_selection_preserves_three_slots_and_wire_order():
    selection = normalize_class_selection(
        C.CLASS_ENGINEER,
        prefabs=[
            "prefab_superbridge",
            "prefab_platform",
            "prefab_supertower",
        ],
    )

    assert selection.prefabs == (
        "prefab_superbridge",
        "prefab_platform",
        "prefab_supertower",
    )


def test_stray_flare_block_is_not_part_of_spawn_loadout():
    selection = normalize_class_selection(C.CLASS_ENGINEER)

    assert C.FLAREBLOCK_TOOL in DEFAULT_DISABLED_TOOLS
    assert C.FLAREBLOCK_TOOL not in selection.loadout


def test_normalizer_keeps_requested_jetpack_without_adding_class_default():
    selection = normalize_class_selection(
        C.CLASS_ROCKETEER,
        [C.JETPACK_NORMAL],
    )

    assert C.JETPACK_NORMAL in selection.loadout
    assert C.JETPACK2 not in selection.loadout


def test_engineer_disguise_replaces_jetpack_in_the_equipment_slot():
    selection = normalize_class_selection(
        C.CLASS_ENGINEER,
        [C.DISGUISE_TOOL],
    )

    assert C.DISGUISE_TOOL in selection.loadout
    assert C.JETPACK_ENGINEER not in selection.loadout

    player = Player(4, "DisguisedEngineer", TEAM1, C.SMG_TOOL, None)
    player.apply_class_selection(selection)
    player.spawn(100.5, 100.5, 60.0)
    assert player.jetpack_id == 0


def test_legacy_rocketeer_defaults_to_jetpack2_as_its_equipment_choice():
    selection = normalize_class_selection(C.CLASS_ROCKETEER)
    equipped_packs = [
        tool for tool in selection.loadout if tool in C.JETPACK_PROPERTIES
    ]

    assert equipped_packs == [C.JETPACK2]

    player = Player(5, "Rocketeer", TEAM1, C.SMG_TOOL, None)
    player.apply_class_selection(selection)
    player.spawn(100.5, 100.5, 60.0)
    assert player.jetpack_id == C.JETPACK2


def test_ugc_builder_normalization_accepts_recovered_range_catalog_key():
    selection = normalize_class_selection(
        C.CLASS_UGCBUILDER,
        ugc_tools=[0],
    )

    assert selection.class_id == C.CLASS_UGCBUILDER
    assert selection.ugc_tools == (0,)


def test_set_loadout_then_change_class_stages_one_selection_and_kills_once():
    player = _live_medic()
    handler = _handler()

    asyncio.run(handler.handle(
        player,
        _loadout_packet(
            C.CLASS_MINER,
            [C.BLOCK_TOOL, C.SUPERSPADE_TOOL, C.SHOTGUN_TOOL,
             C.BLOCK_SUCKER_TOOL, C.DYNAMITE_TOOL],
        ),
    ))
    asyncio.run(handler.handle(player, _change_class_packet(C.CLASS_MINER)))

    assert player.deaths == 1
    assert player.class_id == C.CLASS_MEDIC
    assert player.pending_selection.class_id == C.CLASS_MINER
    assert C.DYNAMITE_TOOL in player.pending_selection.loadout
    player.apply_pending_selection()
    assert player.class_id == C.CLASS_MINER
    assert C.DYNAMITE_TOOL in player.loadout
    assert C.MEDPACK_TOOL not in player.loadout


def test_change_class_then_set_loadout_replaces_defaults_without_second_death():
    player = _live_medic()
    handler = _handler()

    asyncio.run(handler.handle(player, _change_class_packet(C.CLASS_MINER)))
    asyncio.run(handler.handle(
        player,
        _loadout_packet(
            C.CLASS_MINER,
            [C.SHOTGUN2_TOOL, C.BLOCK_SUCKER_TOOL, C.C4_TOOL,
             C.SUPERSPADE_TOOL],
        ),
    ))

    assert player.deaths == 1
    assert C.SHOTGUN2_TOOL in player.pending_selection.loadout
    assert C.C4_TOOL in player.pending_selection.loadout


def test_instant_selection_commits_class_and_loadout_without_split_state():
    player = _live_medic()
    handler = _handler()

    asyncio.run(handler.handle(
        player,
        _loadout_packet(C.CLASS_MINER, [C.SHOTGUN_TOOL, C.DYNAMITE_TOOL], 1),
    ))

    assert player.deaths == 0
    assert player.class_id == C.CLASS_MINER
    assert C.DYNAMITE_TOOL in player.loadout
    assert C.MEDPACK_TOOL not in player.loadout
    assert player.pending_selection is None


def test_same_class_live_loadout_change_commits_without_forcing_death():
    player = _live_medic()
    handler = _handler()

    asyncio.run(handler.handle(
        player,
        _loadout_packet(
            C.CLASS_MEDIC,
            [C.SHOTGUN2_TOOL, C.RIOTSHIELD_TOOL, C.MEDPACK_TOOL],
        ),
    ))

    assert player.deaths == 0
    assert player.class_id == C.CLASS_MEDIC
    assert C.SHOTGUN2_TOOL in player.loadout
    assert player.pending_selection is None


def test_deployable_authorization_requires_matching_active_class_and_loadout():
    player = _live_medic()
    player.set_tool(C.MEDPACK_TOOL, raw=True)
    assert deployable_authorized(player, C.MEDPACK_TOOL)

    player.class_id = C.CLASS_MINER
    assert not deployable_authorized(player, C.MEDPACK_TOOL)
