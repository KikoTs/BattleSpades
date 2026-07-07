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
WATER_LEVEL = 62

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


@dataclass(frozen=True)
class WeaponProfile:
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


RIFLE_PROFILE = WeaponProfile(
    base_damage=50.0,
    headshot_multiplier=3.0,
    block_damage=2.0,
    fire_interval=0.5,
    max_range=WEAPON_RANGE,
    clip_size=10,
    reserve_ammo=50,
    reload_time=2.5,
    pellet_count=1,
    spread=0.0,
    kill_type=KILL_WEAPON,
)

SMG_PROFILE = WeaponProfile(
    base_damage=30.0,
    headshot_multiplier=80.0 / 30.0,
    block_damage=1.0,
    fire_interval=0.1,
    max_range=WEAPON_RANGE,
    clip_size=30,
    reserve_ammo=120,
    reload_time=2.5,
    pellet_count=1,
    spread=0.0,
    kill_type=KILL_WEAPON,
)

SHOTGUN_PROFILE = WeaponProfile(
    base_damage=25.0,
    headshot_multiplier=30.0 / 25.0,
    block_damage=1.0,
    fire_interval=1.0,
    max_range=WEAPON_RANGE,
    clip_size=6,
    reserve_ammo=48,
    reload_time=0.5,
    pellet_count=8,
    spread=0.08,
    kill_type=KILL_WEAPON,
)

SPADE_PROFILE = WeaponProfile(
    base_damage=50.0,
    headshot_multiplier=1.0,
    block_damage=5.0,
    fire_interval=0.2,
    max_range=MELEE_RANGE,
    clip_size=0,
    reserve_ammo=0,
    reload_time=0.0,
    pellet_count=1,
    spread=0.0,
    kill_type=KILL_MELEE,
)

WEAPON_PROFILES = {}
for tool in RIFLE_LIKE_TOOLS:
    WEAPON_PROFILES[int(tool)] = RIFLE_PROFILE
for tool in SMG_LIKE_TOOLS:
    WEAPON_PROFILES[int(tool)] = SMG_PROFILE
for tool in SHOTGUN_LIKE_TOOLS:
    WEAPON_PROFILES[int(tool)] = SHOTGUN_PROFILE


def is_playable_team(team_id: int) -> bool:
    return int(team_id) in PLAYABLE_TEAMS


def get_enemy_team(team_id: int) -> int:
    return TEAM2 if int(team_id) == TEAM1 else TEAM1
