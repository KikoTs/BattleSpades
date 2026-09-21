"""Retail weapons retain their own ammunition across selection changes."""

import shared.constants as C
from server.class_selection import normalize_class_selection
from server.game_constants import TEAM1
from server.player import Player


def scout() -> Player:
    player = Player(3, "Ammo", TEAM1, C.SNIPER_TOOL, None)
    player.apply_class_selection(normalize_class_selection(C.CLASS_SCOUT))
    player.spawn(100.5, 100.5, 60.0)
    player.set_tool(C.SNIPER_TOOL, raw=True)
    return player


def test_switching_between_allowed_weapons_preserves_both_ammo_wallets():
    player = scout()
    assert C.SNIPER_TOOL in player.loadout and C.PISTOL_TOOL in player.loadout
    player.ammo_clip, player.ammo_reserve = 0, 2
    player.set_tool(C.PISTOL_TOOL, raw=True)
    player.ammo_clip, player.ammo_reserve = 3, 4
    for _ in range(10):
        player.set_tool(C.SNIPER_TOOL, raw=True)
        assert (player.ammo_clip, player.ammo_reserve) == (0, 2)
        player.set_tool(C.PISTOL_TOOL, raw=True)
        assert (player.ammo_clip, player.ammo_reserve) == (3, 4)


def test_switching_cancels_reload_without_transferring_ammo_to_another_weapon():
    player = scout()
    player.ammo_clip, player.ammo_reserve = 0, 2
    assert player.start_reload(now=1.0)
    player.set_tool(C.PISTOL_TOOL, raw=True)
    assert not player.reloading and player.reload_end_time == 0.0
    assert not player.finish_reload()
    player.set_tool(C.SNIPER_TOOL, raw=True)
    assert (player.ammo_clip, player.ammo_reserve) == (0, 2)
    assert player.start_reload(now=2.0)
    assert player.finish_reload()
    assert (player.ammo_clip, player.ammo_reserve) == (1, 1)


def test_repeated_clientdata_for_same_tool_does_not_cancel_reload():
    player = scout()
    player.ammo_clip = 0
    assert player.start_reload(now=1.0)
    deadline = player.reload_end_time
    player.set_tool(C.SNIPER_TOOL, raw=True)
    assert player.reloading and player.reload_end_time == deadline


def test_block_selection_cancels_reload_but_keeps_depleted_weapon():
    player = scout()
    player.ammo_clip, player.ammo_reserve = 0, 2
    assert player.start_reload(now=1.0)
    player.set_tool(C.BLOCK_TOOL, raw=True)
    assert not player.reloading
    player.set_tool(C.SNIPER_TOOL, raw=True)
    assert (player.ammo_clip, player.ammo_reserve) == (0, 2)


def test_ammo_pickup_restocks_stowed_weapons_too():
    player = scout()
    full_sniper = (player.ammo_clip, player.ammo_reserve)
    player.ammo_clip, player.ammo_reserve = 0, 0
    player.set_tool(C.PISTOL_TOOL, raw=True)
    full_pistol = (player.ammo_clip, player.ammo_reserve)
    player.ammo_clip, player.ammo_reserve = 0, 0
    player.set_tool(C.BLOCK_TOOL, raw=True)
    player.restock_ammo(restock_type=3)
    player.set_tool(C.SNIPER_TOOL, raw=True)
    assert (player.ammo_clip, player.ammo_reserve) == full_sniper
    player.set_tool(C.PISTOL_TOOL, raw=True)
    assert (player.ammo_clip, player.ammo_reserve) == full_pistol


def test_new_life_replaces_all_previous_weapon_ammo():
    player = scout()
    full_sniper = (player.ammo_clip, player.ammo_reserve)
    player.ammo_clip, player.ammo_reserve = 0, 0
    player.set_tool(C.PISTOL_TOOL, raw=True)
    full_pistol = (player.ammo_clip, player.ammo_reserve)
    player.ammo_clip, player.ammo_reserve = 0, 0
    player.spawn(100.5, 100.5, 60.0)
    player.set_tool(C.SNIPER_TOOL, raw=True)
    assert (player.ammo_clip, player.ammo_reserve) == full_sniper
    player.set_tool(C.PISTOL_TOOL, raw=True)
    assert (player.ammo_clip, player.ammo_reserve) == full_pistol
