"""Bounded prefab complexity metadata for bot tactical decisions.

The counts mirror the KV6 voxel-count header. Keeping this tiny table in the
AI package avoids loading models or touching the filesystem in either the
gameplay hot path or the isolated worker.
"""

from __future__ import annotations


BOT_PREFAB_BLOCK_COUNTS: dict[str, int] = {
    "prefab_caltrop": 11,
    "prefab_fort_wall": 39,
    "prefab_ladder": 20,
    "prefab_platform": 36,
    "prefab_safety_corridor": 452,
    "prefab_safety_tube": 120,
    "prefab_small_platform": 6,
    "prefab_small_wall": 6,
    "prefab_square_bunker": 50,
    "prefab_superbarrier": 96,
    "prefab_superbridge": 222,
    "prefab_superdome": 675,
    "prefab_superminibunker": 68,
    "prefab_superpole": 126,
    "prefab_supersmallwall": 32,
    "prefab_supertower": 138,
    "prefab_ultrabarrier": 268,
    "prefab_zombiebone": 142,
    "prefab_zombiehand": 135,
    "prefab_zombiehead": 446,
}

_PURPOSE_BLOCK_LIMITS = {
    "climb": 160,
    "cover": 128,
    "traversal": 256,
    "variety": 160,
}


def bot_prefab_block_count(name: str) -> int | None:
    """Return the authored voxel count for a bot-selectable prefab."""

    return BOT_PREFAB_BLOCK_COUNTS.get(str(name).lower())


def bot_prefab_is_suitable(name: str, purpose: str) -> bool:
    """Return whether a prefab is bounded enough for one tactical purpose."""

    count = bot_prefab_block_count(name)
    limit = _PURPOSE_BLOCK_LIMITS.get(
        str(purpose), _PURPOSE_BLOCK_LIMITS["cover"]
    )
    return count is not None and count <= limit


def is_zombie_prefab(name: str) -> bool:
    """Return whether a name belongs to the mode-authored zombie set."""

    return str(name).lower().startswith("prefab_zombie")
