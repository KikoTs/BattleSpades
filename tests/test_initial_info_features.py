from server.builders.initial_info import build_initial_info
from server.config import ServerConfig
from server.main import BattleSpadesServer


def test_client_feature_switches_enable_stock_deathcam_and_sniper_lasers():
    packet = build_initial_info(BattleSpadesServer(ServerConfig()))

    assert packet.enable_deathcam == 1
    assert packet.enable_sniper_beam == 1
    assert packet.enable_corpse_explosion == 1
