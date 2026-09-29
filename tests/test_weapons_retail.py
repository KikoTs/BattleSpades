"""Pin every weapon/tool to the STOCK Steam client (docs/WEAPONS_RETAIL.md).

Values were extracted by executing the stock ``aos.pkg`` PYZ bytecode (weapon
classes + obfuscated shared.constants) under Python 2.7, and the explosion
curve by running the stock 32-bit ``shared.explosionDamageManager`` module
with instrumented damageables. The nonsteam decompile's trailing "named"
constants block (pistol 0.3 s/800/head 50, knife 0.25 s/20, spade 0.4 s, ...)
is a later mod and is deliberately NOT what these tests expect.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

import shared.constants as C
from server import weapons_retail as R
from server.explosions import explosion_impulse
from server.game_constants import (
    PART_ARMS,
    PART_HEAD,
    PART_LEFT_LEG,
    PART_RIGHT_LEG,
    PART_TORSO,
    WEAPON_CATALOG,
    WEAPON_PROFILES,
)
from server.main import BattleSpadesServer
from server.projectiles import PROJECTILE_SPECS


# ---------------------------------------------------------------------------
# Stock hit-scan classes
# ---------------------------------------------------------------------------

# tool: ((torso, head, arms, l.leg, r.leg), interval, (clip, initial clip,
#        max reserve, initial reserve, restock), reload, pellets, range,
#        block damage, entity damage, clip_reload)
STOCK_GUNS = {
    6: ((70, 150, 35, 35, 35), 0.5, (10, 10, 50, 30, 50), 2.5, 1, 10000, 2, 25, False),
    7: ((10, 15, 10, 10, 10), 0.1, (25, 25, 100, 100, 100), 1.25, 1, 350, 1, 15, False),
    8: ((15, 30, 15, 15, 15), 0.3, (100, 100, 300, 300, 300), 2.0, 1, 100, 2.5, 20, False),
    9: ((20, 30, 12, 12, 12), 1.0, (5, 5, 20, 20, 20), 0.5, 10, 60, 1, 25, True),
    10: ((40, 50, 50, 50, 50), 1.0, (2, 2, 14, 14, 14), 1.0, 10, 20, 2.5, 25, True),
    15: ((30, 20, 20, 20, 20), 0.5, (100, 100, 400, 400, 400), 4.0, 1, 300, 2, 20, False),
    17: ((20, 45, 20, 20, 20), 0.4, (6, 6, 30, 30, 30), 0.6, 1, 550, 3, 20, False),
    18: ((50, 175, 50, 50, 50), 1.0, (1, 1, 7, 7, 7), 2.0, 1, 10000, 5, 100, False),
    19: ((34, 85, 34, 34, 34), 1.1, (5, 5, 15, 15, 15), 3.0, 1, 10000, 3, 100, False),
    35: ((30, 35, 30, 30, 30), 0.12, (30, 30, 120, 120, 120), 2.0, 1, 500, 1, 30, False),
    36: ((40, 70, 30, 30, 30), 0.5, (6, 6, 30, 30, 30), 0.75, 1, 500, 1, 20, True),
    37: ((20, 30, 12, 12, 12), 1.0, (5, 5, 45, 20, 20), 0.5, 12, 75, 1, 25, True),
    38: ((20, 20, 20, 20, 20), 0.1, (25, 25, 100, 100, 100), 1.25, 1, 100, 2, 20, False),
    53: ((15, 30, 15, 15, 15), 0.175, (15, 15, 50, 50, 50), 1.0, 1, 300, 2.5, 15, False),
    60: ((20, 40, 20, 20, 20), 0.5, (15, 15, 60, 60, 60), 0.9, 1, 400, 2.5, 20, False),
    61: ((20, 37, 20, 20, 20), 0.15, (50, 50, 250, 250, 250), 2.0, 1, 175, 2.5, 20, False),
    62: ((20, 25, 10, 10, 10), 0.35, (8, 8, 40, 40, 40), 2.5, 10, 60, 2, 20, False),
}


@pytest.mark.parametrize("tool", sorted(STOCK_GUNS))
def test_gun_profile_matches_stock_class(tool):
    (parts, interval, ammo, reload_time, pellets, rng, block, entity,
     clip_reload) = STOCK_GUNS[tool]
    profile = WEAPON_PROFILES[tool]
    assert profile.part_damage == tuple(float(v) for v in parts)
    assert profile.base_damage == parts[0]
    assert profile.head_damage == parts[1]
    assert profile.fire_interval == pytest.approx(interval)
    assert profile.clip_size == ammo[0]
    assert profile.reserve_ammo == ammo[2]
    assert profile.initial_reserve == ammo[3]
    assert profile.restock_amount == ammo[4]
    assert profile.reload_time == pytest.approx(reload_time)
    assert profile.pellet_count == pellets
    assert profile.max_range == rng
    assert profile.block_damage == block
    assert profile.entity_damage == entity
    assert profile.clip_reload is clip_reload


def test_stock_damage_tuple_is_ordered_torso_head_not_by_part_id():
    # PART_HEAD is 0 and PART_TORSO is 1 in the stock enum, but every stock
    # damage tuple is (TORSO, HEAD, ARMS, LEGS, LEGS).
    assert (PART_HEAD, PART_TORSO, PART_ARMS, PART_LEFT_LEG, PART_RIGHT_LEG) == (0, 1, 2, 3, 4)
    rifle = WEAPON_CATALOG[int(C.RIFLE_TOOL)]
    assert rifle.damage_for_part(PART_HEAD) == 150
    assert rifle.damage_for_part(PART_TORSO) == 70
    assert rifle.damage_for_part(PART_ARMS) == 35
    assert rifle.damage_for_part(PART_LEFT_LEG) == 35
    assert rifle.damage_for_part(PART_RIGHT_LEG) == 35
    mg = WEAPON_CATALOG[int(C.MG_TOOL)]
    assert mg.damage_for_part(PART_HEAD) == 20  # stock: head below torso
    assert mg.damage_for_part(PART_TORSO) == 30


# (tool, player-hit damage, block damage, interval, secondary interval)
STOCK_MELEE = {
    0: (40, 7, 0.6, 0.0),
    1: (80, 1, 0.5, 0.0),
    2: (35, 5, 0.8, 1.0),
    3: (50, 7.5, 0.6, 0.0),
    4: (50, 3, 0.3, 0.8),
    24: (70, 2, 0.4, 0.0),
    34: (80, 5, 0.5, 0.0),
    44: (0, 9, 0.2, 0.0),
    45: (0, 7.5, 0.2, 0.2),
    49: (85, 1.75, 0.5, 0.0),
    50: (100, 2, 0.7, 0.0),
    52: (2, 2, 1.0, 0.0),
}


@pytest.mark.parametrize("tool", sorted(STOCK_MELEE))
def test_melee_profile_matches_stock_tool(tool):
    player_damage, block, interval, secondary = STOCK_MELEE[tool]
    profile = WEAPON_CATALOG[tool]
    assert profile.is_melee
    assert profile.base_damage == player_damage
    assert profile.block_damage == block
    assert profile.fire_interval == pytest.approx(interval)
    assert profile.secondary_fire_interval == pytest.approx(secondary)
    # Melee has one figure for every body part.
    assert profile.damage_for_part(PART_HEAD) == player_damage


def test_throwables_and_launchers_match_stock_classes():
    grenade = WEAPON_CATALOG[int(C.GRENADE_TOOL)]
    assert (grenade.fuse_time, grenade.fire_interval, grenade.clip_size) == (2.5, 0.5, 4)
    assert WEAPON_CATALOG[int(C.CLASSIC_GRENADE_TOOL)].fuse_time == 3.0
    assert WEAPON_CATALOG[int(C.ANTIPERSONNEL_GRENADE_TOOL)].fuse_time == 2.5
    rpg = WEAPON_CATALOG[int(C.RPG_TOOL)]
    assert (rpg.fire_interval, rpg.clip_size, rpg.reserve_ammo, rpg.reload_time) == (0.7, 1, 3, 1.5)
    rpg2 = WEAPON_CATALOG[int(C.RPG2_TOOL)]
    assert (rpg2.fire_interval, rpg2.clip_size, rpg2.reload_time) == (0.75, 3, 1.0)
    drill = WEAPON_CATALOG[int(C.DRILLGUN_TOOL)]
    assert (drill.fire_interval, drill.reload_time) == (0.2, 4.0)
    for tool in (int(C.GRENADE_LAUNCHER_WEAPON_TOOL), int(C.MINE_LAUNCHER_TOOL)):
        launcher = WEAPON_CATALOG[tool]
        assert (launcher.fire_interval, launcher.clip_size, launcher.reserve_ammo,
                launcher.reload_time) == (0.35, 1, 5, 2.0)


# ---------------------------------------------------------------------------
# Stock ExplosionDamageManager
# ---------------------------------------------------------------------------

# handler: (kill type, radius, knockback min, knockback max, damage, classic)
STOCK_HANDLERS = {
    "airstrike": (16, 6, 1.0, 2.0, 400.0, False),
    "antipersonnel_grenade": (23, 6.0, 0.25, 0.5, 500.0, False),
    "bomb": (17, 7, 2.0, 3.0, 500, False),
    "c4": (36, 8, 0.1, 0.15, 300.0, False),
    "classic_grenade": (22, 9.0, 0.1, 0.1, 130.0, True),
    "corpse": (12, 3, 0.05, 0.1, 0, False),
    "drill": (6, 3.0, 0.01, 0.1, 50, False),
    "drill_destroyed": (6, 3.5, 0.1, 0.2, 95, False),
    "dynamite": (15, 8, 0.1, 0.15, 300.0, False),
    "gl_grenade": (32, 4, 0.0, 0.25, 100, False),
    "grenade": (3, 4, 0.5, 1.0, 230.0, False),
    "landmine": (14, 6.0, 0.75, 0.75, 100, False),
    "mine_launcher": (35, 6.0, 0.75, 0.75, 100, False),
    "molotov": (24, 4, 0.0, 0.1, 50, False),
    "radar_station": (33, 3.0, 0.0, 0.1, 7, False),
    "rocket2": (5, 6.0, 0, 0.25, 40, False),
    "rocket": (4, 6.0, 0, 0.25, 140, False),
    "rocket_turret": (18, 3.0, 0.2, 1.0, 100, False),
    "rocket_turret_rocket": (18, 3, 0.1, 0.3, 50, False),
    "snowball": (21, 5.0, 0.3, 0.3, 10, False),
    "sticky_grenade": (34, 5, 0.75, 0.1, 200, False),
    "ugc_drill": (28, 3.0, 0.01, 0.1, 50, False),
    "ugc_rocket2": (27, 4.0, 0, 0.25, 50, False),
}


@pytest.mark.parametrize("name", sorted(STOCK_HANDLERS))
def test_explosion_table_matches_stock_handler_arguments(name):
    kill_type, radius, kmin, kmax, damage, classic = STOCK_HANDLERS[name]
    spec = R.RETAIL_EXPLOSIONS_BY_NAME[name]
    assert (spec.kill_type, spec.radius, spec.knockback_min,
            spec.knockback_max, spec.damage, spec.classic) == (
        kill_type, float(radius), float(kmin), float(kmax), float(damage),
        classic)
    assert R.retail_explosion_for(kill_type, damage) is spec


def test_projectile_specs_use_the_stock_blast_arguments():
    for tool, spec in PROJECTILE_SPECS.items():
        stock = R.retail_explosion_for(spec.kill_type, spec.damage)
        if stock is None:
            # UGC snowball / chemical bomb have no stock manager handler.
            assert tool in (int(C.UGC_SNOWBLOWER_TOOL), int(C.CHEMICALBOMB_TOOL))
            continue
        assert spec.blast_radius == stock.radius, spec.name
        assert spec.knockback_min == stock.knockback_min, spec.name
        assert spec.knockback_max == stock.knockback_max, spec.name
    assert PROJECTILE_SPECS[int(C.RPG2_TOOL)].damage == 40
    assert PROJECTILE_SPECS[int(C.DYNAMITE_TOOL)].damage == 300


# Executed against the stock 32-bit explosionDamageManager with the stock
# constants (explosion at the origin): (target position, radius, damage,
# crouched, (head, torso, legs) blocked, classic, self, target team,
# (kmin, kmax), stock damage, stock added velocity).
STOCK_GOLDEN = [
    ((5.597, -0.711, -3.94), 9.0, 500.0, False, (False, True, False), False, True, 3, (0.0, 0.25), 84.2419, (0.06852, -0.0087, -0.04823)),
    ((1.479, 1.39, -2.812), 4.0, 50.0, True, (False, False, True), False, False, 2, (0.5, 1.0), 23.6015, (0.27124, 0.25492, -0.51571)),
    ((0.948, -2.765, 0.451), 6.0, 130.0, False, (True, False, False), False, True, 2, (0.5, 1.0), 23.4846, (0.13804, -0.40261, 0.06567)),
    ((-0.289, 3.395, -0.233), 8.0, 500.0, False, (False, False, False), True, True, 2, (0.5, 1.0), 407.2122, (-0.07677, 0.90184, -0.06189)),
    ((0.358, 3.953, -0.118), 8.0, 230.0, False, (False, False, False), True, False, 1, (0.5, 1.0), 171.9473, (0.07878, 0.86985, -0.02597)),
    ((-3.515, 3.874, -0.95), 9.0, 500.0, False, (False, False, False), False, False, 1, (0.0, 0.25), 165.4225, (-0.10937, 0.12054, -0.02956)),
    ((5.156, -0.511, 1.346), 6.0, 300.0, False, (False, True, False), False, True, 1, (0.1, 0.15), 13.8872, (0.07188, -0.00712, 0.01876)),
    ((1.056, 5.762, 2.029), 9.0, 100.0, False, (False, False, False), True, False, 1, (0.75, 0.1), 48.1005, (0.0745, 0.40649, 0.14314)),
    ((1.226, -1.924, -1.326), 4.0, 100.0, False, (False, False, False), False, True, 1, (0.5, 1.0), 32.698, (0.38422, -0.60297, -0.41556)),
    ((5.888, 5.686, 1.333), 9.0, 230.0, False, (False, True, False), False, False, 3, (0.5, 1.0), 19.2047, (0.27814, 0.26859, 0.06297)),
    ((0.053, 1.838, 2.488), 8.0, 100.0, False, (True, False, False), False, True, 2, (0.75, 0.1), 19.5837, (0.00206, 0.07154, 0.09684)),
    ((-0.644, -1.54, 3.438), 8.0, 300.0, False, (False, True, True), False, False, 3, (0.75, 0.1), 102.3617, (-0.02582, -0.06174, 0.13783)),
    ((-1.443, -0.087, -3.58), 5.0, 50.0, True, (False, True, False), False, False, 3, (0.1, 0.15), 24.4738, (-0.03531, -0.00213, -0.0876)),
    ((-1.721, -1.543, -2.608), 6.0, 100.0, True, (False, False, False), False, True, 3, (0.75, 0.1), 40.0183, (-0.11347, -0.10173, -0.17195)),
    ((4.381, 0.408, 0.749), 6.0, 100.0, False, (False, False, False), False, False, 3, (0.1, 0.15), 39.9816, (0.11778, 0.01097, 0.02014)),
    ((0.523, -1.097, -2.466), 4.0, 500.0, True, (False, False, False), False, False, 2, (0.1, 0.15), 407.6377, (0.02678, -0.05617, -0.12626)),
    ((0.172, 2.69, 2.428), 5.0, 50.0, False, (False, False, True), False, False, 2, (0.5, 1.0), 12.2154, (0.02476, 0.38718, 0.34947)),
    ((-0.701, 0.298, 0.313), 9.0, 50.0, True, (False, False, False), False, True, 1, (0.1, 0.15), 24.0669, (-0.1261, 0.0536, 0.0563)),
    ((-4.275, -0.326, -0.981), 3.0, 230.0, True, (False, True, False), False, False, 1, (0.0, 0.25), 0, (0.0, 0.0, 0.0)),
    ((-5.525, -2.679, 2.452), 4.0, 50.0, False, (True, False, True), False, False, 2, (0.1, 0.15), 0, (0.0, 0.0, 0.0)),
    ((4.086, 2.642, 2.348), 5.0, 50.0, False, (True, False, True), False, True, 3, (0.75, 0.1), 0, (0.0, 0.0, 0.0)),
    ((-2.196, 5.341, -3.47), 4.0, 50.0, False, (True, False, False), False, False, 3, (0.0, 0.25), 0, (0.0, 0.0, 0.0)),
]


@pytest.mark.parametrize("case", STOCK_GOLDEN)
def test_explosion_damage_and_impulse_match_stock_manager(case):
    (position, radius, damage, crouched, blocked, classic, is_self, team,
     (kmin, kmax), stock_damage, stock_velocity) = case
    fraction = sum(
        weight for weight, hidden in zip(R.LINE_OF_SIGHT_WEIGHTS, blocked)
        if not hidden
    )
    ours = R.explosion_player_damage(
        (0.0, 0.0, 0.0), position, radius, damage,
        crouched=crouched, los_fraction=fraction, is_self=is_self,
        target_team=team, classic=classic,
    )
    assert ours == pytest.approx(stock_damage, abs=1e-3)
    scaled_min, scaled_max = R.scale_knockback(kmin, kmax, fraction)
    impulse = explosion_impulse(
        (0.0, 0.0, 0.0), position, radius, scaled_min, scaled_max,
        crouched=crouched,
    ) if ours > 0.0 else None
    assert (impulse or (0.0, 0.0, 0.0)) == pytest.approx(stock_velocity, abs=2e-4)


def test_stock_reductions_and_non_player_damage():
    # Enemy on team 2 at 3 blocks (body 0.75 lower), grenade-like 100/r4.
    full = R.explosion_player_damage((0, 0, 0), (3, 0, 0), 4.0, 100.0,
                                     target_team=2)
    assert full == pytest.approx(40.234375)
    assert R.explosion_player_damage((0, 0, 0), (3, 0, 0), 4.0, 100.0,
                                     is_self=True, target_team=2) == pytest.approx(full / 2)
    assert R.explosion_player_damage((0, 0, 0), (3, 0, 0), 4.0, 100.0,
                                     target_team=R.TEAM_NEUTRAL) == pytest.approx(full / 2)
    assert R.explosion_player_damage((0, 0, 0), (3, 0, 0), 4.0, 100.0,
                                     is_self=True, classic=True) == pytest.approx(full)
    # Crouching measures to z + 1.25.
    assert R.explosion_player_damage((0, 0, 0), (3, 0, 0), 4.0, 100.0,
                                     crouched=True, target_team=2) == pytest.approx(33.984375)
    # Non-player damageable: no body offset.
    assert R.explosion_entity_damage((0, 0, 0), (3, 0, 0), 4.0, 100.0) == pytest.approx(43.75)
    # Head ray blocked leaves 0.3 + 0.2 of the blast.
    assert R.explosion_player_damage((0, 0, 0), (3, 0, 0), 4.0, 100.0,
                                     los_fraction=0.5, target_team=2) == pytest.approx(full / 2)


def test_stock_sight_points_and_ray_span():
    assert R.los_sight_points((10.0, 20.0, 30.0), False) == (
        (10.0, 20.0, 30.0), (10.0, 20.0, 30.9), (10.0, 20.0, 31.8))
    assert R.los_sight_points((10.0, 20.0, 30.0), True) == (
        (10.0, 20.0, 30.0), (10.0, 20.0, 30.45), (10.0, 20.0, 30.9))
    start, end = R.los_ray_segment((0.0, 0.0, 0.0), (3.0, 0.0, -1.0))
    assert start == pytest.approx((0.3, 0.0, -0.1))
    assert end == pytest.approx((3.3, 0.0, -1.1))


# ---------------------------------------------------------------------------
# Victim-side multipliers
# ---------------------------------------------------------------------------

def test_class_multipliers_scale_damage_taken():
    scout = SimpleNamespace(class_id=int(C.CLASS_SCOUT))
    miner = SimpleNamespace(class_id=int(C.CLASS_MINER))
    builder = SimpleNamespace(class_id=int(C.CLASS_UGCBUILDER))
    zombie = SimpleNamespace(class_id=int(C.CLASS_ZOMBIE))
    assert R.victim_damage_multiplier(scout) == pytest.approx(1.43)
    assert R.victim_damage_multiplier(scout, headshot=True) == pytest.approx(1.43 * 1.5)
    assert R.victim_damage_multiplier(miner, headshot=True) == pytest.approx(1.1765 * 0.5)
    assert R.victim_damage_multiplier(builder) == 0.0
    assert R.victim_damage_multiplier(zombie) == pytest.approx(0.6)


def test_flying_rocketeer_pack_doubles_damage_taken():
    flying = SimpleNamespace(class_id=int(C.CLASS_SOLDIER), jetpack_active=True,
                             jetpack_id=int(C.JETPACK_NORMAL))
    grounded = SimpleNamespace(class_id=int(C.CLASS_SOLDIER), jetpack_active=False,
                               jetpack_id=int(C.JETPACK_NORMAL))
    engineer = SimpleNamespace(class_id=int(C.CLASS_SOLDIER), jetpack_active=True,
                               jetpack_id=int(C.JETPACK_ENGINEER))
    assert R.jetpack_damage_multiplier(flying) == 2.0
    assert R.jetpack_damage_multiplier(grounded) == 1.0
    assert R.jetpack_damage_multiplier(engineer) == 1.0


# ---------------------------------------------------------------------------
# Integration through the real combat runtime / blast path
# ---------------------------------------------------------------------------

def _duel(tool, target_class=None):
    from tests.test_reversed_combat import DummyServer, make_player
    from server.combat_runtime import get_combat_system
    from server.game_constants import TEAM1, TEAM2

    server = DummyServer()
    attacker, _ = make_player(server, 0, "Attacker", TEAM1, tool, (100.5, 100.5, 60.0))
    target, _ = make_player(server, 1, "Target", TEAM2, C.RIFLE_TOOL, (106.5, 100.5, 60.0))
    if target_class is not None:
        target.class_id = int(target_class)
    attacker.set_tool(tool)
    return server, attacker, target, get_combat_system(server)


def _shoot_at(attacker, combat, point):
    dx = point[0] - attacker.eye_x
    dy = point[1] - attacker.eye_y
    dz = point[2] - attacker.eye_z
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    direction = (dx / length, dy / length, dz / length)
    attacker.set_orientation_vector(*direction)
    return combat._resolve_hitscan(attacker, direction, attacker.eye)


@pytest.mark.parametrize("offset,expected", [
    (0.0, 150),   # head (the eye)
    (0.6, 70),    # torso
])
def test_rifle_head_and_torso_damage(offset, expected):
    _server, attacker, target, combat = _duel(C.RIFLE_TOOL)
    assert _shoot_at(attacker, combat, (target.x, target.y, target.z + offset))
    assert target.health == max(0, 100 - expected)


def test_rifle_leg_hit_uses_the_stock_leg_figure():
    _server, attacker, target, combat = _duel(C.RIFLE_TOOL)
    target.set_orientation_vector(0.0, 1.0, 0.0)
    # Side-on target: aim at one leg box, below the torso.
    hit = combat._ray_hits_target(
        (target.x - 5.0, target.y, target.z + 2.0), (1.0, 0.0, 0.0), 10.0, target)
    assert hit is not None and hit[3] in (PART_LEFT_LEG, PART_RIGHT_LEG)
    assert combat._calculate_damage(
        attacker, attacker.get_weapon_profile(), False, target=target, part=hit[3]
    ) == 35


def test_headshot_on_a_miner_is_softened_by_the_helmet():
    _server, attacker, target, combat = _duel(C.RIFLE_TOOL, C.CLASS_MINER)
    assert _shoot_at(attacker, combat, (target.x, target.y, target.z))
    # 150 * 1.1765 * 0.5 = 88.2 -> 88
    assert target.health == 12


def test_bullet_on_a_deployable_uses_stock_entity_damage():
    from tests.test_reversed_combat import DummyServer, make_player
    from server.combat_runtime import get_combat_system

    server = DummyServer()
    attacker, _ = make_player(server, 0, "Sniper", 2, C.SNIPER_TOOL, (100.5, 100.5, 60.0))
    attacker.set_tool(C.SNIPER_TOOL)
    assert get_combat_system(server)._entity_damage(attacker.get_weapon_profile()) == 100


class _Player:
    def __init__(self, player_id, team, position, class_id=0):
        self.id = player_id
        self.team = team
        self.class_id = class_id
        self.x, self.y, self.z = position
        self.alive = True
        self.spawned = True
        self.input = SimpleNamespace(crouch=False)
        self.velocity = (0.0, 0.0, 0.0)
        self.damage_calls = []

    @property
    def position(self):
        return (self.x, self.y, self.z)

    def damage(self, amount, source=None, kill_type=0):
        self.damage_calls.append((amount, source, kill_type))


class _Registry:
    def all(self):
        return []


class _World:
    """World stub: ``occluder(start..., dir..., length)`` or open air."""

    def __init__(self, occluder=None):
        self.occluder = occluder

    def raycast(self, x, y, z, dx, dy, dz, length):
        if self.occluder is None:
            return None
        return self.occluder(x, y, z, dx, dy, dz, length)


class _BlastServer:
    _apply_blast = BattleSpadesServer._apply_blast

    def __init__(self, players, occluder=None, friendly_fire=False):
        self.players = {p.id: p for p in players}
        self.config = SimpleNamespace(build_damage=False, friendly_fire=friendly_fire)
        self.entity_registry = _Registry()
        self.world_manager = _World(occluder)

    def _build_entity_ctx(self):
        return None


def test_self_grenade_damage_is_halved():
    thrower = _Player(1, int(C.TEAM1), (3.0, 0.0, 0.0))
    server = _BlastServer([thrower])
    server._apply_blast(0, 0, 0, 230.0, 0, int(C.KILL.GRENADE_KILL), thrower)
    # 230 * 0.40234375 * 0.5
    assert thrower.damage_calls == [(46, thrower, int(C.KILL.GRENADE_KILL))]


def test_classic_grenade_uses_its_blast_wave_radius_and_no_self_reduction():
    thrower = _Player(1, int(C.TEAM1), (3.0, 0.0, 0.0))
    server = _BlastServer([thrower])
    server._apply_blast(0, 0, 0, 130.0, 0, int(C.KILL.CLASSIC_GRENADE_KILL),
                        thrower, blast_radius=2.0)
    falloff = (81.0 - 9.5625) / 81.0
    assert thrower.damage_calls[0][0] == round(130.0 * falloff)


def test_caller_radius_is_replaced_by_the_stock_handler():
    # Placed dynamite used to pass radius 5; stock handle_dynamite_damage
    # uses 8, so a target 6 blocks away is still hit.
    thrower = _Player(1, int(C.TEAM1), (50.0, 50.0, 0.0))
    enemy = _Player(2, int(C.TEAM2), (6.0, 0.0, 0.0))
    server = _BlastServer([thrower, enemy])
    server._apply_blast(0, 0, 0, 300.0, 0, int(C.KILL.DYNAMITE_KILL), thrower,
                        blast_radius=5.0)
    expected = round(300.0 * (64.0 - 36.5625) / 64.0)
    assert enemy.damage_calls == [(expected, thrower, int(C.KILL.DYNAMITE_KILL))]


def _cover_head_and_torso(x, y, z, dx, dy, dz, length):
    # Rays ending above z 1.5 (head ~0, torso ~0.99) are blocked; the legs
    # ray (ends ~1.98) passes under the cover.
    return (1, 0, 0) if z + dz * length < 1.5 else None


def test_partial_cover_keeps_the_legs_share_only():
    thrower = _Player(1, int(C.TEAM1), (50.0, 50.0, 0.0))
    enemy = _Player(2, int(C.TEAM2), (3.0, 0.0, 0.0))
    server = _BlastServer([thrower, enemy])
    server._apply_blast(0, 0, 0, 230.0, 0, int(C.KILL.GRENADE_KILL), thrower)
    open_damage = 230.0 * 0.40234375
    assert enemy.damage_calls[0][0] == round(open_damage)
    open_push = enemy.velocity[0]

    covered = _Player(3, int(C.TEAM2), (3.0, 0.0, 0.0))
    server = _BlastServer([thrower, covered], occluder=_cover_head_and_torso)
    server._apply_blast(0, 0, 0, 230.0, 0, int(C.KILL.GRENADE_KILL), thrower)
    assert covered.damage_calls[0][0] == round(open_damage * 0.2)
    assert covered.velocity[0] == pytest.approx(open_push * 0.2)


def test_friendly_fire_off_keeps_the_push_but_not_the_damage():
    thrower = _Player(1, int(C.TEAM1), (50.0, 50.0, 0.0))
    mate = _Player(2, int(C.TEAM1), (3.0, 0.0, 0.0))
    server = _BlastServer([thrower, mate])
    server._apply_blast(0, 0, 0, 230.0, 0, int(C.KILL.GRENADE_KILL), thrower)
    assert mate.damage_calls == []
    assert mate.velocity[0] == pytest.approx(0.5 + 0.40234375 * 0.5)


def test_scout_takes_more_blast_damage():
    thrower = _Player(1, int(C.TEAM1), (50.0, 50.0, 0.0))
    scout = _Player(2, int(C.TEAM2), (3.0, 0.0, 0.0), class_id=int(C.CLASS_SCOUT))
    server = _BlastServer([thrower, scout])
    server._apply_blast(0, 0, 0, 230.0, 0, int(C.KILL.GRENADE_KILL), thrower)
    assert scout.damage_calls[0][0] == round(230.0 * 0.40234375 * 1.43)


# ---------------------------------------------------------------------------
# Minigun spin model
# ---------------------------------------------------------------------------

def test_prespun_minigun_fires_at_the_cap_immediately():
    _server, attacker, _target, combat = _duel(C.MINIGUN_TOOL)
    attacker.input.secondary_fire = True
    attacker.input.primary_fire = True
    now = 1000.0
    accepted = 0
    for index in range(10):
        if combat._accept_minigun_packet(attacker, now + index * 0.1):
            accepted += 1
    assert accepted == 10


def test_cold_minigun_ramps_down_and_spins_down_gradually():
    _server, attacker, _target, combat = _duel(C.MINIGUN_TOOL)
    attacker.input.primary_fire = True
    now = 2000.0
    assert combat._accept_minigun_packet(attacker, now)
    # 0.1 s after a cold first round is far faster than the stock ramp.
    assert not combat._accept_minigun_packet(attacker, now + 0.1)
    t = now
    for _ in range(20):
        t += 0.3
        assert combat._accept_minigun_packet(attacker, t)
    spun = combat._minigun_runs[attacker.id]["interval"]
    assert spun < 0.2
    # Released for 1.5 s: stock spins down by 0.075/s, not back to 0.3.
    attacker.input.primary_fire = False
    t += 1.5
    assert combat._accept_minigun_packet(attacker, t)
    assert combat._minigun_runs[attacker.id]["interval"] == pytest.approx(
        min(0.28, spun + 0.075 * 1.5)
    )
