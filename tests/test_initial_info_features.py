import shared.constants as C

from server.builders.initial_info import build_initial_info
from server.config import ServerConfig
from server.main import BattleSpadesServer


def test_client_feature_switches_enable_stock_deathcam_and_sniper_lasers():
    packet = build_initial_info(BattleSpadesServer(ServerConfig()))

    assert packet.enable_deathcam == 1
    assert packet.enable_sniper_beam == 1
    assert packet.enable_corpse_explosion == 1


def test_client_feature_switches_enable_block_palette_and_world_picker():
    packet = build_initial_info(BattleSpadesServer(ServerConfig()))

    assert packet.enable_colour_palette == 1
    assert packet.enable_colour_picker == 1


def test_same_team_collision_wire_flag_matches_authoritative_config():
    config = ServerConfig()
    config.same_team_collision = True

    packet = build_initial_info(BattleSpadesServer(config))

    assert packet.same_team_collision == 1


def test_flare_block_tile_follows_the_flare_rule():
    """Retail shows tool 22 as the first Constructs tile unless it is disabled."""

    packet = build_initial_info(BattleSpadesServer(ServerConfig()))
    assert C.FLAREBLOCK_TOOL not in packet.disabled_tools

    config = ServerConfig()
    config.game_rules.apply({"RULE_ENABLE_FLARE_BLOCKS": False})
    packet = build_initial_info(BattleSpadesServer(config))
    assert C.FLAREBLOCK_TOOL in packet.disabled_tools


def test_normal_matches_do_not_advertise_map_creator_prefab_sets():
    packet = build_initial_info(BattleSpadesServer(ServerConfig()))

    assert packet.ugc_prefab_sets == []
