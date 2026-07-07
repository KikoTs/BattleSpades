"""Per-class data tables, sourced from shared.constants (the reversed
authoritative values from the original game).

This module is the single source of truth for anything keyed on class_id —
movement multipliers, headshot/damage multipliers, fall thresholds, etc.

The InitialInfo `movement_speed_multipliers` array order is **not** strict
class-id order; the original game's wire format puts ENGINEER before MINER.
We capture the exact wire order in `INITIAL_INFO_CLASS_ORDER` so the
builder can iterate it deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import shared.constants as C


# ---------------------------------------------------------------------------
# Class-id catalog
# ---------------------------------------------------------------------------

# All public class ids exposed to gameplay. CLASS_NOOF (=19) is the count
# sentinel and is intentionally excluded.
CLASS_IDS: tuple[int, ...] = (
    int(C.CLASS_SOLDIER),
    int(C.CLASS_SCOUT),
    int(C.CLASS_ROCKETEER),
    int(C.CLASS_MINER),
    int(C.CLASS_ZOMBIE),
    int(C.CLASS_CLASSIC_SOLDIER),
    int(C.CLASS_GANGSTER_1),
    int(C.CLASS_GANGSTER_2),
    int(C.CLASS_GANGSTER_3),
    int(C.CLASS_GANGSTER_4),
    int(C.CLASS_GANGSTER_VIP_1),
    int(C.CLASS_GANGSTER_VIP_2),
    int(C.CLASS_ENGINEER),
    int(C.CLASS_UGCBUILDER),
    int(C.CLASS_FAST_ZOMBIE),
    int(C.CLASS_JUMP_ZOMBIE),
    int(C.CLASS_SPECIALIST),
    int(C.CLASS_MEDIC),
)


# Wire order for the InitialInfo.movement_speed_multipliers list. Verified
# empirically from the original server's hardcoded array: SOLDIER, SCOUT,
# ROCKETEER, ENGINEER, MINER, ZOMBIE, CLASSIC_SOLDIER, GANGSTER_1..4,
# GANGSTER_VIP_1..2, UGCBUILDER, FAST_ZOMBIE, JUMP_ZOMBIE, SPECIALIST, MEDIC.
INITIAL_INFO_CLASS_ORDER: tuple[int, ...] = (
    int(C.CLASS_SOLDIER),
    int(C.CLASS_SCOUT),
    int(C.CLASS_ROCKETEER),
    int(C.CLASS_ENGINEER),     # NB: not class-id order — engineer (12) before miner (3)
    int(C.CLASS_MINER),
    int(C.CLASS_ZOMBIE),
    int(C.CLASS_CLASSIC_SOLDIER),
    int(C.CLASS_GANGSTER_1),
    int(C.CLASS_GANGSTER_2),
    int(C.CLASS_GANGSTER_3),
    int(C.CLASS_GANGSTER_4),
    int(C.CLASS_GANGSTER_VIP_1),
    int(C.CLASS_GANGSTER_VIP_2),
    int(C.CLASS_UGCBUILDER),
    int(C.CLASS_FAST_ZOMBIE),
    int(C.CLASS_JUMP_ZOMBIE),
    int(C.CLASS_SPECIALIST),
    int(C.CLASS_MEDIC),
)


# ---------------------------------------------------------------------------
# Per-class movement profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassMovement:
    """Class-keyed movement profile — drives both server simulation and the
    multipliers we send the client in InitialInfo so client prediction agrees.

    All values sourced from shared.constants.* tables. See class_data tests.
    """
    class_id: int
    accel_multiplier: float
    sprint_multiplier: float
    crouch_sneak_multiplier: float
    jump_multiplier: float
    water_friction: float
    can_sprint_uphill: bool
    fall_on_water_damage_multiplier: float
    falling_damage_min_distance: int
    falling_damage_max_distance: int
    falling_damage_max_damage: int


_DEF = lambda name, default: getattr(C, name, default)


def _build_movement_table() -> dict[int, ClassMovement]:
    """Pull per-class multipliers from shared.constants. The constants module
    explicitly lists 9 named classes (soldier..ugcbuilder); for the rest we
    use the 'unknown' entries (A137, A150, A189, etc.) at the documented
    indices. Anything still missing falls back to soldier-like defaults.
    """
    # Helper that picks the constant from the table named ``base`` (e.g.
    # SPRINT_MULTIPLIER) for a class. Falls back to a default.
    def get(class_const: str, suffix: str, default: float) -> float:
        # SOLDIER + _SPRINT_MULTIPLIER → SOLDIER_SPRINT_MULTIPLIER
        return float(_DEF('{}_{}'.format(class_const, suffix), default))

    table: dict[int, ClassMovement] = {}

    # Named classes with explicit constants
    named: tuple[tuple[int, str], ...] = (
        (int(C.CLASS_SOLDIER),         'SOLDIER'),
        (int(C.CLASS_SCOUT),           'SCOUT'),
        (int(C.CLASS_ROCKETEER),       'ROCKETEER'),
        (int(C.CLASS_MINER),           'MINER'),
        (int(C.CLASS_ZOMBIE),          'ZOMBIE'),
        (int(C.CLASS_CLASSIC_SOLDIER), 'CLASSIC_SOLDIER'),
        (int(C.CLASS_GANGSTER_1),      'GANGSTER'),
        (int(C.CLASS_ENGINEER),        'ENGINEER'),
        (int(C.CLASS_UGCBUILDER),      'UGCBUILDER'),
    )
    for cid, prefix in named:
        table[cid] = ClassMovement(
            class_id=cid,
            accel_multiplier=get(prefix, 'ACCEL_MULTIPLIER', 0.7),
            sprint_multiplier=get(prefix, 'SPRINT_MULTIPLIER', 1.4),
            crouch_sneak_multiplier=get(prefix, 'CROUCH_SNEAK_MULTIPLIER', 0.5),
            jump_multiplier=get(prefix, 'JUMP_MULTIPLIER', 1.2),
            water_friction=get(prefix, 'WATER_FRICTION', 8.0),
            can_sprint_uphill=bool(_DEF('{}_CAN_SPRINT_UPHILL'.format(prefix), True)),
            fall_on_water_damage_multiplier=get(prefix, 'FALL_ON_WATER_DAMAGE_MULTIPLIER', 0.5),
            falling_damage_min_distance=int(get(prefix, 'FALLING_DAMAGE_MIN_DISTANCE', 10)),
            falling_damage_max_distance=int(get(prefix, 'FALLING_DAMAGE_MAX_DISTANCE', 40)),
            falling_damage_max_damage=int(get(prefix, 'FALLING_DAMAGE_MAX_DAMAGE', 100)),
        )

    # Other gangster slots clone GANGSTER_1's table (verified game behavior).
    for cid in (int(C.CLASS_GANGSTER_2), int(C.CLASS_GANGSTER_3),
                int(C.CLASS_GANGSTER_4),
                int(C.CLASS_GANGSTER_VIP_1), int(C.CLASS_GANGSTER_VIP_2)):
        table[cid] = ClassMovement(class_id=cid,
                                   **{k: v for k, v in table[int(C.CLASS_GANGSTER_1)].__dict__.items()
                                      if k != 'class_id'})

    # Classes without explicit named constants: FAST_ZOMBIE, JUMP_ZOMBIE,
    # SPECIALIST, MEDIC. Use the "unknown" A### entries from constants.py at
    # the documented offsets, falling back to ZOMBIE for fast/jump and
    # SOLDIER for specialist/medic.
    unknown_sprint = (
        float(_DEF('A150', 3.0)),    # FAST_ZOMBIE sprint
        float(_DEF('A151', 1.0)),    # JUMP_ZOMBIE sprint
        float(_DEF('A152', 1.55)),   # SPECIALIST sprint
        float(_DEF('A153', 1.35)),   # MEDIC sprint
    )
    unknown_jump = (
        float(_DEF('A189', 2.5)),   # FAST_ZOMBIE jump
        float(_DEF('A190', 3.0)),   # JUMP_ZOMBIE jump
        float(_DEF('A191', 1.5)),   # SPECIALIST jump
        float(_DEF('A192', 1.2)),   # MEDIC jump
    )
    unknown_classes = (
        (int(C.CLASS_FAST_ZOMBIE),  table[int(C.CLASS_ZOMBIE)],   0),
        (int(C.CLASS_JUMP_ZOMBIE),  table[int(C.CLASS_ZOMBIE)],   1),
        (int(C.CLASS_SPECIALIST),   table[int(C.CLASS_SOLDIER)],  2),
        (int(C.CLASS_MEDIC),        table[int(C.CLASS_SOLDIER)],  3),
    )
    for cid, base, idx in unknown_classes:
        d = base.__dict__.copy()
        d['class_id'] = cid
        d['sprint_multiplier'] = unknown_sprint[idx]
        d['jump_multiplier'] = unknown_jump[idx]
        table[cid] = ClassMovement(**d)

    return table


MOVEMENT: dict[int, ClassMovement] = _build_movement_table()


def get_movement(class_id: int) -> ClassMovement:
    """Return the movement profile for a class id. Falls back to soldier
    if the id is unknown."""
    return MOVEMENT.get(int(class_id), MOVEMENT[int(C.CLASS_SOLDIER)])


def wire_round(value: float) -> float:
    """Round a float the way the InitialInfo fixed-point wire encoding does
    (1/64 steps): the client receives e.g. 1.4 as 1.40625. Server-side
    simulation must use the wire-rounded value or client prediction drifts.
    """
    return round(float(value) * 64.0) / 64.0


def speed_scale(class_id: int) -> float:
    """The per-class speed scale sent in InitialInfo, as the client decodes
    it (wire-rounded).

    Verified against the live client (aoslib/scenes/main/gameClass.py):
    the client multiplies ALL of its local CLASS_ACCEL/SPRINT/CROUCH_SNEAK
    multipliers by this value:
        accel_eff  = CLASS_ACCEL_MULTIPLIER[id]  * scale   # 0.7*1.40625
        sprint_eff = CLASS_SPRINT_MULTIPLIER[id] * scale   # 1.4*1.40625
        crouch_eff = CLASS_CROUCH_SNEAK[id]      * scale
    (jump_multiplier is NOT scaled — confirmed by the measured -0.36*1.2
    jump impulse.)
    """
    return wire_round(get_movement(class_id).sprint_multiplier)


def initial_info_movement_multipliers() -> list[float]:
    """Build the InitialInfo.movement_speed_multipliers list.

    The client indexes this list directly by class id (see selectTeam.py /
    selectClass.py: `manager.movement_speed_multipliers[class_id]`), so it
    MUST be in ascending class-id order — not the engineer-before-miner
    permutation previously assumed.
    """
    size = max(CLASS_IDS) + 1
    out = [1.0] * size
    for cid in CLASS_IDS:
        out[cid] = get_movement(cid).sprint_multiplier
    return out


# ---------------------------------------------------------------------------
# Per-class damage profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassDamage:
    class_id: int
    damage_multiplier: float
    headshot_multiplier: float


def _build_damage_table() -> dict[int, ClassDamage]:
    table: dict[int, ClassDamage] = {}
    named: tuple[tuple[int, str], ...] = (
        (int(C.CLASS_SOLDIER),         'SOLDIER'),
        (int(C.CLASS_SCOUT),           'SCOUT'),
        (int(C.CLASS_ROCKETEER),       'ROCKETEER'),
        (int(C.CLASS_MINER),           'MINER'),
        (int(C.CLASS_ZOMBIE),          'ZOMBIE'),
        (int(C.CLASS_CLASSIC_SOLDIER), 'CLASSIC_SOLDIER'),
        (int(C.CLASS_GANGSTER_1),      'GANGSTER'),
        (int(C.CLASS_ENGINEER),        'ENGINEER'),
        (int(C.CLASS_UGCBUILDER),      'UGCBUILDER'),
    )
    for cid, prefix in named:
        table[cid] = ClassDamage(
            class_id=cid,
            damage_multiplier=float(_DEF('{}_DAMAGE_MULTIPLIER'.format(prefix), 1.0)),
            headshot_multiplier=float(_DEF('{}_HEADSHOT_DAMAGE_MULTIPLIER'.format(prefix), 1.0)),
        )
    # Clone gangster table for the other gangster IDs
    g1 = table[int(C.CLASS_GANGSTER_1)]
    for cid in (int(C.CLASS_GANGSTER_2), int(C.CLASS_GANGSTER_3),
                int(C.CLASS_GANGSTER_4),
                int(C.CLASS_GANGSTER_VIP_1), int(C.CLASS_GANGSTER_VIP_2)):
        table[cid] = ClassDamage(class_id=cid,
                                  damage_multiplier=g1.damage_multiplier,
                                  headshot_multiplier=g1.headshot_multiplier)
    # Unknown — use soldier
    soldier = table[int(C.CLASS_SOLDIER)]
    for cid in (int(C.CLASS_FAST_ZOMBIE), int(C.CLASS_JUMP_ZOMBIE),
                int(C.CLASS_SPECIALIST), int(C.CLASS_MEDIC)):
        if cid in (int(C.CLASS_FAST_ZOMBIE), int(C.CLASS_JUMP_ZOMBIE)):
            base = table[int(C.CLASS_ZOMBIE)]
        else:
            base = soldier
        table[cid] = ClassDamage(class_id=cid,
                                  damage_multiplier=base.damage_multiplier,
                                  headshot_multiplier=base.headshot_multiplier)
    return table


DAMAGE: dict[int, ClassDamage] = _build_damage_table()


def get_damage(class_id: int) -> ClassDamage:
    return DAMAGE.get(int(class_id), DAMAGE[int(C.CLASS_SOLDIER)])
