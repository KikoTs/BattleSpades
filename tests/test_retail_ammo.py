"""Retail (stock Steam client) gun ammunition: spawn wallet, crates, reloads.

Ground truth (docs/WEAPONS_RETAIL.md, stock ``aoslib.weapons.weapon`` and the
stock ``aoslib.character.pyd`` reload/end_reload/restock methods):

* ``Weapon.ammo`` = (clip, initial clip, max reserve, initial reserve,
  crate restock); a non-crate restock (spawn) sets the INITIAL reserve.
* ``Weapon.restock(AMMO_CRATE)`` adds the restock amount to the reserve,
  capped at the max, and keeps the magazine.
* ``get_ammo_after_reload``: clip_reload guns (shotguns, snub pistol) load
  one round per ``reload_time``; ``Character.end_reload`` re-calls
  ``reload()`` while reloadable unless the trigger is pressed, so a shotgun
  can stop its reload and fire with the rounds loaded so far.
"""

import shared.constants as C
from server.class_selection import normalize_class_selection
from server.game_constants import TEAM1, WEAPON_CATALOG
from server.player import Player


def _player(class_id, weapon, selected=()):
    player = Player(3, "Ammo", TEAM1, weapon, None)
    player.apply_class_selection(normalize_class_selection(class_id, list(selected)))
    player.spawn(100.5, 100.5, 60.0)
    player.set_tool(weapon, raw=True)
    assert player.weapon == weapon
    return player


def _shotgun():
    return _player(C.CLASS_MINER, C.SHOTGUN_TOOL)


def test_spawn_grants_stock_initial_reserve_not_the_maximum():
    rifle = _player(C.CLASS_CLASSIC_SOLDIER, C.RIFLE_TOOL)
    assert (rifle.ammo_clip, rifle.ammo_reserve) == (10, 30)
    assert WEAPON_CATALOG[int(C.RIFLE_TOOL)].reserve_ammo == 50
    assert rifle._weapon_ammo[int(C.CLASSIC_SHOTGUN_TOOL)] == (5, 20)
    rifle.set_tool(C.CLASSIC_SHOTGUN_TOOL, raw=True)
    assert (rifle.ammo_clip, rifle.ammo_reserve) == (5, 20)
    # A new life (and the type-0 spawn Restock) resets to the initial wallet.
    rifle.ammo_clip, rifle.ammo_reserve = 0, 45
    rifle.restock_ammo()
    assert (rifle.ammo_clip, rifle.ammo_reserve) == (5, 20)


def test_ammo_crate_adds_restock_amount_capped_at_stock_max():
    rifle = _player(C.CLASS_CLASSIC_SOLDIER, C.RIFLE_TOOL)
    rifle.ammo_clip, rifle.ammo_reserve = 4, 10
    rifle.restock_ammo(int(C.AMMO_CRATE))
    assert (rifle.ammo_clip, rifle.ammo_reserve) == (4, 50)  # 10 + 50 -> cap 50
    rifle.ammo_reserve = 0
    rifle.set_tool(C.CLASSIC_SHOTGUN_TOOL, raw=True)
    rifle.ammo_clip, rifle.ammo_reserve = 1, 10
    rifle.restock_ammo(int(C.AMMO_CRATE))
    assert (rifle.ammo_clip, rifle.ammo_reserve) == (1, 30)  # +20
    rifle.restock_ammo(int(C.AMMO_CRATE))
    assert rifle.ammo_reserve == 45  # capped at the classic shotgun max
    # The stowed rifle got its own crate restock too.
    rifle.set_tool(C.RIFLE_TOOL, raw=True)
    assert (rifle.ammo_clip, rifle.ammo_reserve) == (4, 50)


def test_shotgun_reload_loads_one_round_per_cycle_and_chains():
    player = _shotgun()
    player.ammo_clip, player.ammo_reserve = 1, 10
    assert player.start_reload(now=0.0)
    assert player.reload_end_time == 0.5
    assert not player._advance_reload(0.49)
    assert player.ammo_clip == 1
    assert not player._advance_reload(0.5)
    assert (player.ammo_clip, player.ammo_reserve) == (2, 9)
    assert player.reloading and player.reload_end_time == 1.0
    assert not player._advance_reload(1.6)
    assert (player.ammo_clip, player.ammo_reserve) == (4, 7)
    assert player._advance_reload(2.0)
    assert (player.ammo_clip, player.ammo_reserve) == (5, 6)
    assert not player.reloading


def test_shotgun_chain_stops_when_reserve_runs_out():
    player = _shotgun()
    player.ammo_clip, player.ammo_reserve = 0, 2
    assert player.start_reload(now=0.0)
    assert player._advance_reload(5.0)
    assert (player.ammo_clip, player.ammo_reserve) == (2, 0)
    assert not player.start_reload(now=6.0)


def test_per_round_reload_requests_acknowledge_the_running_chain():
    player = _shotgun()
    player.ammo_clip, player.ammo_reserve = 0, 10
    assert player.start_reload(now=0.0)
    # The client's chained reload() sends another is_done=0 per round.
    assert player.start_reload(now=0.5)
    assert player.ammo_clip == 1 and player.reload_end_time == 1.0


def test_shotgun_fires_mid_reload_with_the_rounds_loaded_so_far():
    player = _shotgun()
    player.ammo_clip, player.ammo_reserve = 0, 10
    player.next_shot_time = 0.0
    assert player.start_reload(now=0.0)
    player._advance_reload(1.1)  # two rounds loaded, third in progress
    assert player.ammo_clip == 2
    assert player.consume_shot(now=1.2)
    assert not player.reloading
    assert (player.ammo_clip, player.ammo_reserve) == (1, 8)


def test_empty_shotgun_shot_just_before_the_round_lands_is_accepted():
    player = _shotgun()
    player.ammo_clip, player.ammo_reserve = 0, 10
    player.next_shot_time = 0.0
    assert player.start_reload(now=0.0)
    assert player.consume_shot(now=0.46)  # client clock ran slightly ahead
    assert (player.ammo_clip, player.ammo_reserve) == (0, 9)
    assert not player.reloading


def test_empty_shotgun_cannot_fire_early_in_its_first_round():
    player = _shotgun()
    player.ammo_clip, player.ammo_reserve = 0, 10
    player.next_shot_time = 0.0
    assert player.start_reload(now=0.0)
    assert not player.consume_shot(now=0.1)
    assert player.reloading


def test_magazine_reload_still_blocks_the_trigger_until_done():
    rifle = _player(C.CLASS_CLASSIC_SOLDIER, C.RIFLE_TOOL)
    rifle.ammo_clip, rifle.ammo_reserve = 3, 30
    rifle.next_shot_time = 0.0
    assert rifle.start_reload(now=0.0)
    assert not rifle.consume_shot(now=1.0)
    assert rifle.consume_shot(now=2.5)
    assert (rifle.ammo_clip, rifle.ammo_reserve) == (9, 23)


def test_reload_completion_is_announced_once(monkeypatch):
    player = _shotgun()
    sent = []
    monkeypatch.setattr(player, "_broadcast_reload_state", sent.append)
    player.ammo_clip, player.ammo_reserve = 3, 10
    assert player.start_reload(now=0.0)
    player.reload_end_time = -1.0  # both remaining rounds are due now
    player._advance_reload_and_announce()
    assert (player.ammo_clip, sent) == (5, [True])
    player._advance_reload_and_announce()
    assert sent == [True]


def test_snub_pistol_is_one_round_per_reload():
    profile = WEAPON_CATALOG[int(C.SNUB_PISTOL_TOOL)]
    assert profile.clip_reload and profile.reload_time == 0.75
    player = Player(3, "Ammo", TEAM1, C.SNUB_PISTOL_TOOL, None)
    player.spawn(100.5, 100.5, 60.0)
    player.set_tool(C.SNUB_PISTOL_TOOL, raw=True)
    player.ammo_clip, player.ammo_reserve = 0, 30
    assert player.start_reload(now=0.0)
    player._advance_reload(0.75)
    assert player.ammo_clip == 1 and player.reloading
