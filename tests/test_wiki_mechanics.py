"""Owner-approved wiki mechanics, separate from historical retail defaults."""
import shared.constants as C

from modes import get_mode_class
from modes.classic_ctf import ClassicCTFMode
from server.class_data import get_damage
from server.class_selection import normalize_class_selection
from server.config import ServerConfig
from server.main import BattleSpadesServer
from tests.test_pickups import _server_player


def test_engineer_ammo_refill_restores_fuel_without_health_or_clip_reset():
    server, player, connection = _server_player()
    player.apply_class_selection(normalize_class_selection(C.CLASS_ENGINEER))
    player.spawn(*player.position)
    player.health = 37
    player.jetpack_fuel = 3.0
    player.ammo_clip = 2
    connection.sent.clear()
    player.restock_ammo(int(C.AMMO_CRATE))
    assert player.jetpack_fuel == 100.0
    assert player.health == 37 and player.ammo_clip == 2
    assert [packet[2] for packet in connection.sent if packet[0] == 69] == [int(C.AMMO_CRATE)]


def test_zombie_has_200_effective_body_hp_and_full_2500_block_wallet():
    _, player, _ = _server_player()
    player.apply_class_selection(normalize_class_selection(C.CLASS_ZOMBIE))
    player.spawn(*player.position)
    assert player.max_health / get_damage(C.CLASS_ZOMBIE).damage_multiplier == 200.0
    assert player.blocks == player._block_wallet_max() == 2500


def test_wiki_timers_and_classic_weapon_choices():
    server = BattleSpadesServer(ServerConfig())
    assert get_mode_class("zom")(server).infection_delay == 50.0
    assert get_mode_class("mh")(server).base_active_time == 300.0
    classic = ClassicCTFMode(server)
    assert int(C.CLASSIC_SMG_TOOL) not in classic._disabled_tools()
    assert int(C.CLASSIC_SHOTGUN_TOOL) not in classic._disabled_tools()
