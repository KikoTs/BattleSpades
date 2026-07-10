"""Server-local gameplay helpers derived from the reversed shared constants."""

from __future__ import annotations

from dataclasses import dataclass

import shared.constants as C


TEAM_SPECTATOR = int(C.TEAM_SPECTATOR)
TEAM_NEUTRAL = int(C.TEAM_NEUTRAL)
TEAM1 = int(C.TEAM1)
TEAM2 = int(C.TEAM2)
PLAYABLE_TEAMS = (TEAM1, TEAM2)

CHAT_ALL = int(getattr(C, "CHAT_ALL", 0))
CHAT_SYSTEM = int(getattr(C, "CHAT_SYSTEM", 2))

BLOCK_ACTION_BUILD = int(C.ACTION.BUILD)
BLOCK_ACTION_DESTROY = int(C.ACTION.DESTROY)
BLOCK_ACTION_SPADE = int(C.ACTION.SPADE)

KILL_WEAPON = int(C.KILL.WEAPON_KILL)
KILL_HEADSHOT = int(C.KILL.HEADSHOT_KILL)
KILL_MELEE = int(C.KILL.MELEE_KILL)
KILL_GRENADE = int(C.KILL.GRENADE_KILL)
KILL_TEAM_CHANGE = int(C.KILL.TEAM_CHANGE_KILL)
KILL_CLASS_CHANGE = int(C.KILL.CLASS_CHANGE_KILL)

MAX_HEALTH = int(getattr(C, "INITIAL_HEALTH", 100))
MAX_GRENADES = int(getattr(C, "GRENADE_INITIAL_STOCK", 2))
MAX_BLOCKS = max(max_blocks for _, max_blocks in C.CLASS_BLOCKS.values())
DEFAULT_BLOCK_HEALTH = float(C.DEFAULT_BLOCK_HEALTH)
MELEE_RANGE = float(getattr(C, "MELEE_WORLD_RANGE", 4.0))
WEAPON_RANGE = float(getattr(C, "WEAPON_WORLD_RANGE", 10000.0))
PLAYER_WIDTH_HALF = 0.45
PLAYER_STANDING_POS_ABOVE_GROUND = 2.25
PLAYER_CROUCHING_POS_ABOVE_GROUND = 1.35
PLAYER_HEIGHT = PLAYER_STANDING_POS_ABOVE_GROUND + PLAYER_WIDTH_HALF
PLAYER_CROUCH_HEIGHT = PLAYER_CROUCHING_POS_ABOVE_GROUND + PLAYER_WIDTH_HALF
WATER_LEVEL = int(C.Z_ABOVE_WATERPLANE)

DEFAULT_TEAM_CLASSES = list(C.DEFAULT_TEAM_CLASSES)

BLOCK_TOOL_IDS = frozenset({int(C.BLOCK_TOOL), int(getattr(C, "FLAREBLOCK_TOOL", C.BLOCK_TOOL))})
SPADE_TOOL_IDS = frozenset(int(tool) for tool in getattr(C, "ALL_MELEE_WEAPONS", (C.SPADE_TOOL,)))
GRENADE_TOOL_IDS = frozenset(int(tool) for tool in getattr(C, "THROWABLE_EXPLOSIVE_TOOLS", (C.GRENADE_TOOL,)))

DEFAULT_WEAPON_TOOL = int(C.RIFLE_TOOL)

WEAPON_TOOL_IDS = frozenset(
    int(tool)
    for tool in (
        C.RIFLE_TOOL,
        C.SMG_TOOL,
        C.MINIGUN_TOOL,
        C.SHOTGUN_TOOL,
        C.SHOTGUN2_TOOL,
        C.RPG_TOOL,
        C.RPG2_TOOL,
        C.DRILLGUN_TOOL,
        C.MG_TOOL,
        C.PISTOL_TOOL,
        C.SNIPER_TOOL,
        C.TOMMYGUN_TOOL,
        C.CLASSIC_SHOTGUN_TOOL,
        C.CLASSIC_SMG_TOOL,
        C.ASSAULT_RIFLE_TOOL,
        C.LIGHT_MACHINE_GUN_TOOL,
        C.AUTO_SHOTGUN_TOOL,
        getattr(C, "AUTOMATIC_PISTOL_TOOL", C.PISTOL_TOOL),
    )
)

RIFLE_LIKE_TOOLS = frozenset(
    int(tool)
    for tool in (
        C.RIFLE_TOOL,
        C.RPG_TOOL,
        C.RPG2_TOOL,
        C.DRILLGUN_TOOL,
        C.PISTOL_TOOL,
        C.SNIPER_TOOL,
        C.ASSAULT_RIFLE_TOOL,
        getattr(C, "AUTOMATIC_PISTOL_TOOL", C.PISTOL_TOOL),
    )
)

SMG_LIKE_TOOLS = frozenset(
    int(tool)
    for tool in (
        C.SMG_TOOL,
        C.MINIGUN_TOOL,
        C.MG_TOOL,
        C.TOMMYGUN_TOOL,
        C.CLASSIC_SMG_TOOL,
        C.LIGHT_MACHINE_GUN_TOOL,
    )
)

SHOTGUN_LIKE_TOOLS = frozenset(
    int(tool)
    for tool in (
        C.SHOTGUN_TOOL,
        C.SHOTGUN2_TOOL,
        C.CLASSIC_SHOTGUN_TOOL,
        C.AUTO_SHOTGUN_TOOL,
    )
)


# ---------------------------------------------------------------------------
# Weapon catalog
# ---------------------------------------------------------------------------
#
# Every tool's stats, transcribed from the ground-truth extraction in
# docs/CONTENT_TABLES.md (values read from the ORIGINAL client's own resolved
# constants, not the untrustworthy aoslib-reversed dump). This is the Phase-1
# data layer: the full 66-tool catalog is data; the hitscan gun subset drives
# combat today, while melee/projectile/deployable rows are ready for the
# Phase-2 systems that will route them.

# Weapon categories.
CAT_MELEE = "melee"
CAT_RIFLE = "rifle"
CAT_SMG = "smg"
CAT_SHOTGUN = "shotgun"
CAT_SNIPER = "sniper"
CAT_PISTOL = "pistol"
CAT_MG = "mg"
CAT_GRENADE = "grenade"
CAT_LAUNCHER = "launcher"
CAT_DEPLOYABLE = "deployable"
CAT_OBJECTIVE = "objective"
CAT_SPECIAL = "special"

# Categories whose tools are hit-scan guns (routed through WEAPON_PROFILES).
HITSCAN_CATEGORIES = frozenset({CAT_RIFLE, CAT_SMG, CAT_SHOTGUN, CAT_SNIPER, CAT_PISTOL, CAT_MG})

# Damage-type / kill-type enum ids, mirrored from the client (see
# docs/CONTENT_TABLES.md §3). The 37-member kill enum lives on C.KILL. The
# 44-member tool damage-type enum (TOOLS_DAMAGE_TYPE, constants.py:931) is NOT
# a single object in shared.constants — module-level `*_DAMAGE` scanning is
# ambiguous (block-damage amounts collide) — so it's transcribed in id order
# from the ground-truth doc. id == index.
KILL_TYPES: dict[str, int] = {name: int(getattr(C.KILL, name)) for name in dir(C.KILL) if name.isupper()}

_DAMAGE_TYPE_ORDER = (
    "PICKAXE_DAMAGE", "KNIFE_DAMAGE", "SPADE_DAMAGE", "SUPERSPADE_DAMAGE",
    "CLASSIC_SPADE_DAMAGE", "CLASSIC_SPADE_SECONDARY_DAMAGE", "WEAPON_DAMAGE",
    "GRENADE_DAMAGE", "ROCKET_DAMAGE", "ROCKET2_DAMAGE", "DRILL_DAMAGE",
    "DRILL_DESTROYED_DAMAGE", "ROCKET_TURRET_DAMAGE", "CORPSE_DAMAGE",
    "GRAVE_DAMAGE", "LANDMINE_DAMAGE", "DYNAMITE_DAMAGE", "ZOMBIE_DAMAGE",
    "AIRSTRIKE_DAMAGE", "BOMB_DAMAGE", "SNOWBALL_DAMAGE",
    "ROCKET_TURRET_ROCKET_DAMAGE", "CLASSIC_GRENADE_DAMAGE",
    "ANTIPERSONNEL_GRENADE_DAMAGE", "MOLOTOV_DAMAGE", "BLOCKFIRE_DAMAGE",
    "CROWBAR_DAMAGE", "MG_DAMAGE", "UGC_PICKAXE_DAMAGE", "UGC_SUPERSPADE_DAMAGE",
    "UGC_ROCKET2_DAMAGE", "UGC_SUPERSPADE_SECONDARY_DAMAGE", "UGC_SNOWBALL_DAMAGE",
    "UGC_DRILL_DAMAGE", "RIOTSTICK_DAMAGE", "MACHETE_DAMAGE", "RIOTSHIELD_DAMAGE",
    "GRENADE_LAUNCHER_DAMAGE", "SOME_DAMAGE", "STICKY_GRENADE_DAMAGE",
    "MINE_LAUNCHER_DAMAGE", "C4_DAMAGE", "BLOCK_SUCKER_DAMAGE", "UNKNOWN_DAMAGE",
)
DAMAGE_TYPES: dict[str, int] = {name: idx for idx, name in enumerate(_DAMAGE_TYPE_ORDER)}

DMG_WEAPON = DAMAGE_TYPES["WEAPON_DAMAGE"]


@dataclass(frozen=True)
class WeaponProfile:
    """Full per-tool stats. Fields after ``kill_type`` are catalog metadata
    (defaulted) so existing constructors and consumers stay valid.

    For hit-scan guns ``base_damage`` is the torso figure and ``head_damage``
    the absolute head figure. For melee ``base_damage`` is the player-hit
    damage and ``block_damage`` the dig damage. For projectiles ``base_damage``
    is the blast damage (``is_projectile`` set); those are spawned/handled by
    the Phase-2 projectile engine, not hit-scan.
    """
    base_damage: float
    headshot_multiplier: float
    block_damage: float
    fire_interval: float
    max_range: float
    clip_size: int
    reserve_ammo: int
    reload_time: float
    pellet_count: int
    spread: float
    kill_type: int
    # --- catalog metadata (defaulted for backward compatibility) ---
    tool_id: int = -1
    name: str = ""
    category: str = ""
    head_damage: float = 0.0
    damage_type: int = DMG_WEAPON
    is_melee: bool = False
    is_projectile: bool = False
    blast_radius: float = 0.0
    fuse_time: float = 0.0


def _gun(tool_id, name, category, torso, head, fire_interval, clip, reserve,
         reload_time, pellets, rng, block_dmg, spread=0.0):
    return WeaponProfile(
        base_damage=float(torso),
        headshot_multiplier=(float(head) / float(torso)) if torso else 1.0,
        block_damage=float(block_dmg),
        fire_interval=float(fire_interval),
        max_range=float(rng),
        clip_size=int(clip),
        reserve_ammo=int(reserve),
        reload_time=float(reload_time),
        pellet_count=int(pellets),
        spread=float(spread),
        kill_type=KILL_WEAPON,
        tool_id=int(tool_id),
        name=name,
        category=category,
        head_damage=float(head),
        damage_type=DMG_WEAPON,
    )


def _melee(tool_id, name, player_dmg, block_dmg, fire_interval):
    return WeaponProfile(
        base_damage=float(player_dmg),
        headshot_multiplier=1.0,
        block_damage=float(block_dmg),
        fire_interval=float(fire_interval),
        max_range=MELEE_RANGE,
        clip_size=0,
        reserve_ammo=0,
        reload_time=0.0,
        pellet_count=1,
        spread=0.0,
        kill_type=KILL_MELEE,
        tool_id=int(tool_id),
        name=name,
        category=CAT_MELEE,
        is_melee=True,
    )


def _proj(tool_id, name, category, blast, radius, fire_interval, clip, reserve,
          reload_time, block_dmg=0.0, fuse=0.0, kill_type=None):
    return WeaponProfile(
        base_damage=float(blast),
        headshot_multiplier=1.0,
        block_damage=float(block_dmg),
        fire_interval=float(fire_interval),
        max_range=float(radius),
        clip_size=int(clip),
        reserve_ammo=int(reserve),
        reload_time=float(reload_time),
        pellet_count=1,
        spread=0.0,
        kill_type=KILL_GRENADE if kill_type is None else int(kill_type),
        tool_id=int(tool_id),
        name=name,
        category=category,
        is_projectile=True,
        blast_radius=float(radius),
        fuse_time=float(fuse),
    )


def _util(tool_id, name, category, fire_interval=0.5, clip=0, reload_time=0.0):
    return WeaponProfile(
        base_damage=0.0,
        headshot_multiplier=1.0,
        block_damage=0.0,
        fire_interval=float(fire_interval),
        max_range=0.0,
        clip_size=int(clip),
        reserve_ammo=0,
        reload_time=float(reload_time),
        pellet_count=1,
        spread=0.0,
        kill_type=KILL_WEAPON,
        tool_id=int(tool_id),
        name=name,
        category=category,
    )


# The full catalog, keyed by tool id. Rows transcribed from CONTENT_TABLES.md.
_CATALOG_LIST = [
    # --- melee (block dig / player hit) ---
    _melee(0,  "PICKAXE",        50, 7,   0.4),
    _melee(1,  "KNIFE",          20, 1,   0.25),
    _melee(2,  "SPADE",          35, 5,   0.4),
    _melee(3,  "SUPERSPADE",     50, 7.5, 0.6),
    _melee(4,  "CLASSIC_SPADE",  50, 3,   0.3),
    _melee(24, "ZOMBIEHAND",     70, 2,   0.4),
    _melee(34, "CROWBAR",        80, 5,   0.6),
    _melee(44, "UGC_PICKAXE",     0, 9,   0.2),
    _melee(45, "UGC_SUPERSPADE",  0, 7.5, 0.2),
    _melee(49, "RIOTSTICK",    1.75, 0,   0.5),
    _melee(50, "MACHETE",       2.0, 0,   0.7),
    _melee(52, "RIOTSHIELD",      2, 0,   1.0),
    # --- hit-scan guns: (id, name, cat, torso, head, fire_int, clip, reserve, reload, pellets, range, block_dmg) ---
    _gun(6,  "RIFLE",             CAT_RIFLE,   70, 150, 0.5,  10, 50,  2.5, 1, 10000, 2),
    # SMG range 350 (A1151), was 250.
    _gun(7,  "SMG",               CAT_SMG,     10, 15,  0.1,  25, 100, 1.25, 1, 350,   1),
    _gun(8,  "MINIGUN",           CAT_MG,      15, 30,  0.3,  100, 300, 2.0, 1, 100,   2.5),
    _gun(9,  "SHOTGUN",           CAT_SHOTGUN, 20, 30,  1.0,  5, 20,  0.5, 10, 60,    1, spread=0.08),
    _gun(10, "SHOTGUN2",          CAT_SHOTGUN, 40, 50,  1.0,  2, 14,  1.0, 10, 20,    2.5, spread=0.10),
    _gun(15, "MG",                CAT_MG,      30, 20,  0.5,  100, 400, 4.0, 1, 300,   2),
    # PISTOL named constants loaded by stock pistolWeapon.pyc: damage tuple
    # (20,50,20,20,20), interval .3, reload .5, range 800, ammo 6/30.
    _gun(17, "PISTOL",            CAT_PISTOL,  20, 50,  0.3,  6, 30,  0.5, 1, 800,   3),
    _gun(18, "SNIPER",            CAT_SNIPER,  50, 175, 1.0,  1, 7,   2.0, 1, 10000, 5),
    _gun(19, "SNIPER2",           CAT_SNIPER,  34, 85,  1.1,  5, 15,  3.0, 1, 10000, 3),
    _gun(35, "TOMMYGUN",          CAT_SMG,     30, 35,  0.12, 30, 120, 2.0, 1, 500,   1),
    _gun(36, "SNUB_PISTOL",       CAT_PISTOL,  40, 70,  0.5,  6, 30,  0.75, 1, 500,   1),
    _gun(37, "CLASSIC_SHOTGUN",   CAT_SHOTGUN, 20, 30,  1.0,  5, 45,  0.5, 12, 75,    1, spread=0.08),
    _gun(38, "CLASSIC_SMG",       CAT_SMG,     20, 20,  0.1,  25, 100, 1.25, 1, 100,   2),
    _gun(53, "AUTOMATIC_PISTOL",  CAT_PISTOL,  15, 30,  0.175, 15, 50, 1.0, 1, 300,   2.5),
    _gun(60, "ASSAULT_RIFLE",     CAT_RIFLE,   20, 40,  0.5,  15, 60,  0.9, 1, 400,   2.5),
    _gun(61, "LIGHT_MACHINE_GUN", CAT_MG,      20, 37,  0.15, 50, 250, 2.0, 1, 175,   2.5),
    _gun(62, "AUTO_SHOTGUN",      CAT_SHOTGUN, 20, 25,  0.35, 8, 40,  2.5, 10, 60,    2, spread=0.08),
    # --- projectiles / explosives: (id, name, cat, blast, radius, fire_int, clip, reserve, reload, block_dmg, fuse) ---
    _proj(11, "GRENADE",               CAT_GRENADE,  230, 4, 0.5,  4, 0, 0.0, 0, 2.5),
    _proj(12, "RPG",                   CAT_LAUNCHER, 140, 4, 0.7,  1, 3, 1.5, 5, 0.0, kill_type=KILL_TYPES.get("ROCKET_KILL")),
    _proj(13, "RPG2",                  CAT_LAUNCHER, 50,  4, 0.75, 3, 3, 1.0, 0, 0.0, kill_type=KILL_TYPES.get("ROCKET2_KILL")),
    _proj(14, "DRILLGUN",              CAT_LAUNCHER, 50,  3, 0.2,  1, 3, 4.0, 5, 0.0, kill_type=KILL_TYPES.get("DRILL_KILL")),
    _proj(16, "ROCKET_TURRET",         CAT_DEPLOYABLE, 100, 3, 1.5, 4, 0, 0.0, 0, 0.0, kill_type=KILL_TYPES.get("ROCKET_KILL")),
    _proj(20, "LANDMINE",              CAT_DEPLOYABLE, 100, 3, 1.0, 5, 0, 0.0, 0, 0.0, kill_type=KILL_TYPES.get("LANDMINE_KILL")),
    _proj(21, "DYNAMITE",              CAT_DEPLOYABLE, 300, 5, 1.0, 1, 0, 0.0, 0, 7.0, kill_type=KILL_TYPES.get("DYNAMITE_KILL")),
    _proj(25, "BOMB",                  CAT_OBJECTIVE, 500, 7, 0.0, 0, 0, 0.0, 0, 10.0, kill_type=KILL_TYPES.get("BOMB_KILL")),
    _proj(29, "SNOWBLOWER",            CAT_LAUNCHER, 10,  5, 0.2,  0, 0, 3.0, 0, 0.0, kill_type=KILL_TYPES.get("SNOWBALL_KILL")),
    _proj(31, "CLASSIC_GRENADE",       CAT_GRENADE,  130, 2, 0.5,  4, 0, 0.0, 0, 3.0),
    _proj(32, "ANTIPERSONNEL_GRENADE", CAT_GRENADE,  500, 2, 0.5,  4, 0, 0.0, 0, 2.5),
    _proj(33, "MOLOTOV",               CAT_GRENADE,  50,  4, 1.0,  3, 0, 0.0, 3, 0.0),
    _proj(46, "UGC_RPG2",              CAT_LAUNCHER, 50,  4, 0.5,  1, 1, 1.0, 0, 0.0),
    _proj(47, "UGC_DRILLGUN",          CAT_LAUNCHER, 50,  3, 0.2,  1, 3, 4.0, 0, 0.0),
    _proj(48, "UGC_SNOWBLOWER",        CAT_LAUNCHER, 10,  5, 0.2,  0, 0, 3.0, 0, 0.0),
    _proj(54, "CHEMICALBOMB",          CAT_GRENADE,  50, 3, 1.0,  4, 0, 0.0, 3, 0.0,
          kill_type=KILL_TYPES.get("CHEMICALBOMB_KILL")),
    _proj(55, "GRENADE_LAUNCHER_WEAPON", CAT_LAUNCHER, 100, 4, 0.35, 1, 5, 2.0, 6, 3.0,
          kill_type=KILL_TYPES.get("GRENADE_LAUNCHER_KILL")),
    _proj(57, "STICKY_GRENADE",        CAT_GRENADE, 200, 5, 1.0, 4, 0, 0.0, 6, 5.0,
          kill_type=KILL_TYPES.get("STICKY_GRENADE_KILL")),
    _proj(58, "MINE_LAUNCHER",         CAT_LAUNCHER, 100, 3, 0.35, 1, 5, 2.0, 15, 0.0,
          kill_type=KILL_TYPES.get("MINE_KILL")),
    _proj(59, "C4",                    CAT_DEPLOYABLE, 300, 8, 1.0, 2, 0, 0.0, 7, 0.0,
          kill_type=KILL_TYPES.get("C4_KILL")),
    # --- deployable / utility tools (no direct combat damage) ---
    _util(5,  "BLOCK",          CAT_DEPLOYABLE, 0.5),
    _util(22, "FLAREBLOCK",     CAT_DEPLOYABLE, 0.5),
    _util(23, "PREFAB",         CAT_DEPLOYABLE, 0.5),
    _util(28, "ZOMBIE_PREFAB",  CAT_DEPLOYABLE, 0.5),
    _util(43, "PAINTBRUSH",     CAT_DEPLOYABLE, 0.05),
    _util(51, "MEDPACK",        CAT_DEPLOYABLE, 1.0, clip=2, reload_time=1.5),
    _util(56, "RADAR_STATION",  CAT_DEPLOYABLE, 1.5, clip=1),
    _util(63, "BLOCK_SUCKER",   CAT_DEPLOYABLE, 0.2, reload_time=1.0),
    _util(64, "DISGUISE",       CAT_SPECIAL,    0.5, clip=3),
    # --- objectives / special / placeholders ---
    _util(26, "DIAMOND",        CAT_OBJECTIVE, 0.0),
    _util(27, "SHRAPNEL",       CAT_SPECIAL,   0.0),
    _util(30, "INTEL",          CAT_OBJECTIVE, 0.0),
    _util(39, "NULL",           CAT_SPECIAL,   0.0),
    _util(40, "FAKE_PISTOL",    CAT_SPECIAL,   0.0),
    _util(41, "UGC",            CAT_SPECIAL,   0.5),
    _util(42, "UGC_PREFAB",     CAT_SPECIAL,   0.1),
]

# Full catalog keyed by tool id (all 65 selectable tools).
WEAPON_CATALOG: dict[int, WeaponProfile] = {p.tool_id: p for p in _CATALOG_LIST}

# Hit-scan gun profiles that drive combat routing today (keyed by tool id).
WEAPON_PROFILES: dict[int, WeaponProfile] = {
    tid: p for tid, p in WEAPON_CATALOG.items() if p.category in HITSCAN_CATEGORIES
}

# Named references retained for backward-compatible imports.
RIFLE_PROFILE = WEAPON_CATALOG[int(C.RIFLE_TOOL)]
SMG_PROFILE = WEAPON_CATALOG[int(C.SMG_TOOL)]
SHOTGUN_PROFILE = WEAPON_CATALOG[int(C.SHOTGUN_TOOL)]
# Generic melee routing profile (Phase-2 will route each melee tool individually).
SPADE_PROFILE = WEAPON_CATALOG[int(C.SPADE_TOOL)]


def get_weapon_profile(tool_id: int) -> WeaponProfile:
    """Return the full catalog profile for a tool id (falls back to RIFLE)."""
    return WEAPON_CATALOG.get(int(tool_id), RIFLE_PROFILE)


def is_playable_team(team_id: int) -> bool:
    return int(team_id) in PLAYABLE_TEAMS


def get_enemy_team(team_id: int) -> int:
    return TEAM2 if int(team_id) == TEAM1 else TEAM1
