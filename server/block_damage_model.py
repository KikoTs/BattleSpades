"""Retail BlockManager terrain-damage footprints, reproduced exactly.

Every terrain change a retail client makes comes from ``Damage(37)``:
``BlockManager.handle_damage`` dispatches on the packet's damage TYPE and
calls ``add_damage(x, y, z, amount)`` for each solid cell of that type's
footprint.  Measured live on the stock client (2026-09-26) by logging every
``add_damage`` call and every draw of ``BlockManager.rnd_generator`` (a
Python 2 ``random.Random`` re-seeded with ``Damage.seed`` per packet):

* the damaged centre is ``floor(position + 0.5)`` on each axis;
* single / column / machete types are deterministic and apply the packet
  amount to each cell;
* "cube" types (Super Spade 3, Zombie hands 17, UGC Super Spade RMB 31) draw
  one ``random()`` per cell of the 3x3x3 cube in x-major order and apply
  ``ceil4(amount + extra * r)``;
* "radius" types draw one ``random()`` per cell with ``dx²+dy²+dz² < R²`` in
  z-major (z, x, y) order and apply
  ``ceil4(amount * (1 - d²/R²) + 2 * r)``;
* a draw is consumed for every footprint cell, solid or not; only solid,
  damageable cells (z <= 238) receive ``add_damage``;
* a cell breaks when its accumulated damage reaches its health.

``ceil4`` rounds up to the 0.25 block-damage quantum.  Python 3's Mersenne
Twister yields the same ``random()`` sequence as the client's Python 2 one for
an integer seed, so the server computes exactly the per-cell damage every
client computes from the same packet.  The packet's ``damage`` byte is itself
quantized to 0.25 on the wire (round to nearest), so callers must model the
amount the client will decode (:func:`wire_damage`).
"""

from __future__ import annotations

import math
import random
from functools import lru_cache
from typing import Iterator

import shared.constants as C

Cell = tuple[int, int, int]

# Highest z the retail BlockManager damages (``max_modifiable_z``); below is
# the indestructible water/bedrock layer.
MAX_DAMAGEABLE_Z = 238

SINGLE = "single"
COLUMN = "column"
MACHETE = "machete"
CUBE = "cube"
RADIUS = "radius"
NONE = "none"


def _t(name: str, default: int) -> int:
    return int(getattr(C, name, default))


# damage type -> (kind, parameter).  RADIUS parameter = R, CUBE parameter =
# random extra.  Every entry was fitted exactly against the live client.
FOOTPRINTS: dict[int, tuple[str, float]] = {
    _t("PICKAXE_DAMAGE", 0): (SINGLE, 0),
    _t("KNIFE_DAMAGE", 1): (SINGLE, 0),
    _t("SPADE_DAMAGE", 2): (COLUMN, 0),
    _t("SUPERSPADE_DAMAGE", 3): (CUBE, 5.0),
    _t("CLASSIC_SPADE_DAMAGE", 4): (SINGLE, 0),
    _t("CLASSIC_SPADE_SECONDARY_DAMAGE", 5): (COLUMN, 0),
    _t("WEAPON_DAMAGE", 6): (SINGLE, 0),
    _t("GRENADE_DAMAGE", 7): (RADIUS, 4),
    _t("ROCKET_DAMAGE", 8): (RADIUS, 4),
    _t("ROCKET2_DAMAGE", 9): (RADIUS, 6),
    _t("DRILL_DAMAGE", 10): (RADIUS, 3),
    _t("DRILL_DESTROYED_DAMAGE", 11): (RADIUS, 3),
    _t("ROCKET_TURRET_DAMAGE", 12): (RADIUS, 3),
    _t("CORPSE_DAMAGE", 13): (RADIUS, 3),
    _t("GRAVE_DAMAGE", 14): (RADIUS, 3),
    _t("LANDMINE_DAMAGE", 15): (RADIUS, 3),
    _t("DYNAMITE_DAMAGE", 16): (RADIUS, 8),
    _t("ZOMBIE_DAMAGE", 17): (CUBE, 8.0),
    _t("AIRSTRIKE_DAMAGE", 18): (RADIUS, 6),
    _t("BOMB_DAMAGE", 19): (RADIUS, 7),
    _t("SNOWBALL_DAMAGE", 20): (NONE, 0),
    _t("ROCKET_TURRET_ROCKET_DAMAGE", 21): (RADIUS, 3),
    _t("CLASSIC_GRENADE_DAMAGE", 22): (RADIUS, 2),
    _t("ANTIPERSONNEL_GRENADE_DAMAGE", 23): (RADIUS, 2),
    _t("MOLOTOV_DAMAGE", 24): (RADIUS, 4),
    _t("BLOCKFIRE_DAMAGE", 25): (SINGLE, 0),
    _t("CROWBAR_DAMAGE", 26): (SINGLE, 0),
    _t("MG_DAMAGE", 27): (NONE, 0),
    _t("UGC_PICKAXE_DAMAGE", 28): (SINGLE, 0),
    _t("UGC_SUPERSPADE_DAMAGE", 29): (SINGLE, 0),
    _t("UGC_ROCKET2_DAMAGE", 30): (RADIUS, 4),
    _t("UGC_SUPERSPADE_SECONDARY_DAMAGE", 31): (CUBE, 0.0),
    _t("UGC_SNOWBALL_DAMAGE", 32): (NONE, 0),
    _t("UGC_DRILL_DAMAGE", 33): (RADIUS, 3),
    _t("RIOTSTICK_DAMAGE", 34): (SINGLE, 0),
    _t("MACHETE_DAMAGE", 35): (MACHETE, 0),
    _t("RIOTSHIELD_DAMAGE", 36): (COLUMN, 0),
    _t("GRENADE_LAUNCHER_DAMAGE", 37): (RADIUS, 4),
    _t("SOME_DAMAGE", 38): (RADIUS, 3),
    _t("STICKY_GRENADE_DAMAGE", 39): (RADIUS, 5),
    _t("MINE_LAUNCHER_DAMAGE", 40): (RADIUS, 3),
    _t("C4_DAMAGE", 41): (RADIUS, 8),
    _t("BLOCK_SUCKER_DAMAGE", 42): (SINGLE, 0),
    _t("UNKNOWN_DAMAGE", 43): (SINGLE, 0),
}


def ceil4(value: float) -> float:
    """Round a damage amount up to the retail 0.25 quantum."""

    return math.ceil(value * 4.0) / 4.0


def wire_damage(amount: float) -> float:
    """Return the amount a client decodes from ``Damage.damage``.

    The field is one unsigned byte in quarter units, rounded to nearest.
    """

    quarters = int(math.floor(float(amount) * 4.0 + 0.5))
    return max(0, min(255, quarters)) / 4.0


def center_cell(position) -> Cell:
    """Resolve a Damage position to its centre cell like the client."""

    return tuple(int(math.floor(float(value) + 0.5)) for value in position)


def footprint_kind(damage_type: int) -> tuple[str, float] | None:
    """Return ``(kind, parameter)`` for a damage type, or ``None``."""

    return FOOTPRINTS.get(int(damage_type))


def is_native(damage_type: int) -> bool:
    """True when the server can predict this type's client footprint."""

    entry = FOOTPRINTS.get(int(damage_type))
    return entry is not None and entry[0] != NONE


@lru_cache(maxsize=16)
def _radius_offsets(radius: int) -> tuple[tuple[int, int, int, int], ...]:
    limit = int(radius) * int(radius)
    span = range(-int(radius), int(radius) + 1)
    out = []
    for dz in span:
        for dx in span:
            for dy in span:
                d2 = dx * dx + dy * dy + dz * dz
                if d2 < limit:
                    out.append((dx, dy, dz, d2))
    return tuple(out)


_CUBE_OFFSETS = tuple(
    (dx, dy, dz)
    for dx in (-1, 0, 1)
    for dy in (-1, 0, 1)
    for dz in (-1, 0, 1)
)


def footprint(damage_type: int, position, amount: float,
              seed: int = 0) -> list[tuple[Cell, float]]:
    """Return ``[(cell, damage), ...]`` exactly as a client applies it.

    ``amount`` must already be the wire-decoded value.  Cells are returned
    for the whole footprint (solid or not) in the client's order; callers
    filter to solid, damageable cells.
    """

    entry = FOOTPRINTS.get(int(damage_type))
    if entry is None or entry[0] == NONE:
        return []
    kind, param = entry
    x, y, z = center_cell(position)
    amount = float(amount)
    if kind == SINGLE:
        return [((x, y, z), amount)]
    if kind == COLUMN:
        return [((x, y, z - 1), amount), ((x, y, z), amount),
                ((x, y, z + 1), amount)]
    if kind == MACHETE:
        return [((x, y, z), amount), ((x, y, z + 1), amount)]
    rng = random.Random(int(seed) & 0xFF)
    if kind == CUBE:
        extra = float(param)
        return [
            ((x + dx, y + dy, z + dz), ceil4(amount + extra * rng.random()))
            for dx, dy, dz in _CUBE_OFFSETS
        ]
    radius = int(param)
    limit = float(radius * radius)
    extra = float(getattr(C, "RADIUS_BLOCK_DAMAGE_RANDOM_EXTRA", 2))
    out = []
    for dx, dy, dz, d2 in _radius_offsets(radius):
        roll = rng.random()
        out.append((
            (x + dx, y + dy, z + dz),
            ceil4(amount * (1.0 - d2 / limit) + extra * roll),
        ))
    return out


def retail_round(value: float) -> int:
    """Python 2 ``int(round(value))``: halves round away from zero."""

    value = float(value)
    magnitude = int(math.floor(abs(value) + 0.5))
    return magnitude if value >= 0.0 else -magnitude


def dim(component: int, damage: float) -> int:
    """The stock client's ``shared.common.dim`` for one colour channel.

    Recovered from ``shared.common.pyd`` (headless IDA, 2026-09-26)::

        def dim(value, damage):
            damage = int(round(damage))
            value = value - ((value * damage) >> 3)
            return 0 if 0 > value else value

    ``BlockManager.add_damage`` applies it to each RGB channel of the voxel's
    CURRENT colour with that hit's amount, so repeated hits compound; the
    ``BlockManagerState(38)`` damaged-row path instead applies it once to the
    row's original colour with ``get_initial_health - remaining``.
    """

    component = int(component)
    value = component - ((component * retail_round(damage)) >> 3)
    return 0 if value < 0 else value


def dim_rgb(rgb: int, damage: float) -> int:
    """Apply :func:`dim` to every channel of a packed ``0xRRGGBB`` colour."""

    rgb = int(rgb)
    r = dim((rgb >> 16) & 0xFF, damage) & 0xFF
    g = dim((rgb >> 8) & 0xFF, damage) & 0xFF
    b = dim(rgb & 0xFF, damage) & 0xFF
    return (r << 16) | (g << 8) | b


def iter_damageable(cells) -> Iterator[tuple[Cell, float]]:
    """Filter a footprint to cells inside the damageable map volume."""

    for cell, damage in cells:
        cx, cy, cz = cell
        if 0 <= cx < 512 and 0 <= cy < 512 and 0 <= cz <= MAX_DAMAGEABLE_Z:
            if damage > 0.0:
                yield cell, damage


__all__ = [
    "CUBE",
    "COLUMN",
    "FOOTPRINTS",
    "MACHETE",
    "MAX_DAMAGEABLE_Z",
    "RADIUS",
    "SINGLE",
    "ceil4",
    "center_cell",
    "dim",
    "dim_rgb",
    "retail_round",
    "footprint",
    "footprint_kind",
    "is_native",
    "iter_damageable",
    "wire_damage",
]
