"""Bot dig planning uses the stock melee cadence the server enforces.

The nonsteam shared/constants.py ends with a modded block (spade 0.4 s,
pickaxe 0.4 s / 9 block damage, knife 0.25 s, crowbar 0.6 s) the stock Steam
client never runs. Bots that planned swings from it fired faster than
Player.consume_shot accepts and blacklisted edges on cooldown rejections.
"""

import pytest
import shared.constants as C

from server.dig_profiles import (
    BUILT_BLOCK_HEALTH,
    MAP_BLOCK_HEALTH,
    MELEE_DIG_PROFILES,
    PRIMARY_DIG_PROFILES,
    DIG_COLUMN,
    navigation_dig_profile,
)
from server.game_constants import WEAPON_CATALOG


def test_every_dig_profile_matches_the_stock_catalog_row():
    for tool_id, profile in PRIMARY_DIG_PROFILES.items():
        stock = WEAPON_CATALOG[tool_id]
        assert profile.fire_interval == stock.fire_interval, tool_id
        assert profile.block_damage == stock.block_damage, tool_id
        assert MELEE_DIG_PROFILES[tool_id][1] == stock.block_damage, tool_id


@pytest.mark.parametrize("tool,interval,damage", [
    (C.SPADE_TOOL, 0.8, 5.0),
    (C.PICKAXE_TOOL, 0.6, 7.0),
    (C.KNIFE_TOOL, 0.5, 1.0),
    (C.CROWBAR_TOOL, 0.5, 5.0),
    (C.RIOTSHIELD_TOOL, 1.0, 2.0),
])
def test_stock_melee_cadence_and_block_damage(tool, interval, damage):
    profile = PRIMARY_DIG_PROFILES[int(tool)]
    assert (profile.fire_interval, profile.block_damage) == (interval, damage)


def test_swing_estimates_follow_stock_block_damage():
    pickaxe = PRIMARY_DIG_PROFILES[int(C.PICKAXE_TOOL)]
    assert pickaxe.swings_for_health(MAP_BLOCK_HEALTH) == 1
    assert pickaxe.swings_for_health(BUILT_BLOCK_HEALTH) == 2  # 7 < 9
    knife = PRIMARY_DIG_PROFILES[int(C.KNIFE_TOOL)]
    assert knife.swings_for_health(MAP_BLOCK_HEALTH) == 5
    assert knife.swings_for_health(BUILT_BLOCK_HEALTH) == 9


def test_riot_shield_digs_the_client_column_footprint():
    # RIOTSHIELD_DAMAGE (36) expands as a z-1..z+1 column on the client.
    profile = navigation_dig_profile(int(C.RIOTSHIELD_TOOL))
    assert profile is not None and profile.pattern == DIG_COLUMN


def test_modded_constants_no_longer_reach_consumers():
    assert C.SPADE_SHOOT_INTERVAL == 0.8
    assert C.PICKAXE_SHOOT_INTERVAL == 0.6
    assert C.PICKAXE_DAMAGE_AMOUNT == 7
    assert C.PICKAXE_HITPLAYER_DAMAGE_AMOUNT == 40
    assert C.KNIFE_SHOOT_INTERVAL == 0.5
    assert C.KNIFE_HITPLAYER_DAMAGE_AMOUNT == 80
    assert C.CROWBAR_SHOOT_INTERVAL == 0.5
    assert (C.PISTOL_RANGE, C.PISTOL_SHOOT_INTERVAL, C.PISTOL_RELOAD_TIME,
            C.PISTOL_DAMAGE_HEAD) == (550, 0.4, 0.6, 45)
    assert C.SMG_RANGE == 350
    assert C.ROCKET2_EXPLOSION_DAMAGE == 40


def test_director_holds_a_swing_until_the_authoritative_cadence():
    from types import SimpleNamespace

    from server.bot_ai.director import BotDirector
    from server.bot_ai.messages import BotAction, BotActionKind

    executed = []
    director = BotDirector.__new__(BotDirector)
    director._enforce_weapon_capability = lambda runtime: True
    director._execute_pending = lambda runtime, action, now: executed.append(now)
    player = SimpleNamespace(alive=True, spawned=True, next_shot_time=10.0,
                             eye_x=0.0, eye_y=0.0, eye_z=0.0,
                             o_x=1.0, o_y=0.0, o_z=0.0)
    action = BotAction(BotActionKind.MELEE, tool_id=int(C.SPADE_TOOL))
    runtime = SimpleNamespace(
        player=player, pending_action=action, pending_action_deadline=99.0,
        pending_action_life_id=-1, next_fire_at=0.0, intent=None,
        pending_action_look=None, pending_action_visible=False,
    )
    director._try_pending_action(runtime, 9.8)  # short wait: held
    assert executed == [] and runtime.pending_action is action
    director._try_pending_action(runtime, 10.0)
    assert executed == [10.0]

    executed.clear()
    player.next_shot_time = 20.0
    director._try_pending_action(runtime, 19.0)  # far off: dropped
    assert executed == [] and runtime.pending_action is None
