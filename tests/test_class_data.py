"""Class-data parity test.

Asserts server/class_data.py movement/damage multipliers and loadouts match
the ground-truth client values in docs/CONTENT_TABLES.md §2. Catches drift in
the per-class stat tables that drive movement prediction and combat.
"""
import shared.constants as C
from server import class_data as CD


# id: (sprint, jump, damage_mult, headshot_mult) — CONTENT_TABLES.md §2.
_CLASS_STATS = {
    0:  (1.4,  1.2, 1.0,    1.0),   # SOLDIER
    1:  (1.45, 1.5, 1.43,   1.5),   # SCOUT
    2:  (1.1,  1.0, 1.43,   1.5),   # ROCKETEER
    3:  (1.4,  1.2, 1.1765, 0.5),   # MINER
    4:  (1.65, 1.5, 0.6,    1.0),   # ZOMBIE
    5:  (1.33, 1.0, 1.0,    1.0),   # CLASSIC_SOLDIER
    6:  (1.5,  1.2, 1.0,    1.2),   # GANGSTER_1
    12: (1.25, 1.0, 1.1765, 1.5),   # ENGINEER
    13: (3.0,  1.0, 0.0,    0.0),   # UGCBUILDER
    14: (3.0,  2.5, 0.5,    2.5),   # FAST_ZOMBIE
    15: (1.0,  3.0, 0.5,    1.5),   # JUMP_ZOMBIE
    16: (1.55, 1.5, 1.1765, 1.5),   # SPECIALIST
    17: (1.35, 1.2, 1.0,    1.0),   # MEDIC
}


def test_movement_multipliers_match_client():
    for cid, (sprint, jump, _, _) in _CLASS_STATS.items():
        m = CD.get_movement(cid)
        assert abs(m.sprint_multiplier - sprint) < 1e-4, f"class {cid} sprint"
        assert abs(m.jump_multiplier - jump) < 1e-4, f"class {cid} jump"


def test_damage_multipliers_match_client():
    for cid, (_, _, dmg, head) in _CLASS_STATS.items():
        d = CD.get_damage(cid)
        assert abs(d.damage_multiplier - dmg) < 1e-4, f"class {cid} dmg mult"
        assert abs(d.headshot_multiplier - head) < 1e-4, f"class {cid} headshot mult"


def test_all_classes_have_movement_damage_loadout():
    for cid in CD.CLASS_IDS:
        assert cid in CD.MOVEMENT
        assert cid in CD.DAMAGE
        assert cid in CD.LOADOUTS


def test_default_loadouts_match_client():
    # (class_id, primary, secondary, equipment, melee, jetpack) — first option per slot.
    expect = [
        (0,  C.MINIGUN_TOOL, C.RPG_TOOL, C.GRENADE_TOOL, C.SPADE_TOOL, C.NO_JETPACK),
        (2,  C.SMG_TOOL, C.ROCKET_TURRET_TOOL, -1, C.SPADE_TOOL, C.JETPACK2),
        (12, C.SMG_TOOL, C.ROCKET_TURRET_TOOL, C.DISGUISE_TOOL, C.PICKAXE_TOOL, C.JETPACK_ENGINEER),
        (13, C.UGC_DRILLGUN_TOOL, C.UGC_SNOWBLOWER_TOOL, -1, C.UGC_SUPERSPADE_TOOL, C.JETPACK_UGCBUILDER),
        (16, C.AUTO_SHOTGUN_TOOL, C.AUTOMATIC_PISTOL_TOOL, C.CHEMICALBOMB_TOOL, C.SPADE_TOOL, C.NO_JETPACK),
        (17, C.LIGHT_MACHINE_GUN_TOOL, C.RIOTSHIELD_TOOL, C.MEDPACK_TOOL, C.PICKAXE_TOOL, C.NO_JETPACK),
    ]
    for cid, primary, secondary, equipment, melee, jetpack in expect:
        lo = CD.default_loadout(cid)
        assert lo["primary"] == int(primary), f"class {cid} primary"
        assert lo["secondary"] == int(secondary), f"class {cid} secondary"
        assert lo["equipment"] == int(equipment), f"class {cid} equipment"
        assert lo["melee"] == int(melee), f"class {cid} melee"
        assert lo["jetpack"] == int(jetpack), f"class {cid} jetpack"


def test_zombie_classes_only_have_zombiehand():
    for cid in (C.CLASS_ZOMBIE, C.CLASS_FAST_ZOMBIE, C.CLASS_JUMP_ZOMBIE):
        lo = CD.get_loadout(cid)
        assert lo.primary == (int(C.ZOMBIEHAND_TOOL),)
        assert lo.secondary == ()
        assert lo.jetpack == int(C.NO_JETPACK)
