"""Hardcore class and rule policy, shared by menus, authority and bots.

Role identities reuse retail models. No global class/weapon tables are changed;
leaving a Hardcore mode restores the normal server rules immediately.
"""
from __future__ import annotations

from collections import ChainMap
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from server.class_selection import ClassSelection
    from server.game_rules import GameRules
    from server.player import MovementProfile, Player

import shared.constants as C

BASE_MODES = ("tdm", "ctf", "cctf", "zom", "vip", "mh", "tc", "dia", "dem", "oc")
MODE_CODES = tuple("hc_" + code for code in BASE_MODES)


def canonical(code: object) -> str:
    value = str(code or "").strip().lower()
    if value.startswith("hardcore_") and value[9:] in BASE_MODES:
        return "hc_" + value[9:]
    return value


def base_code(code: object) -> str:
    value = canonical(code)
    return value[3:] if value in MODE_CODES else value


def enabled(config: object) -> bool:
    return canonical(getattr(config, "game_mode", getattr(config, "default_mode", ""))) in MODE_CODES


@dataclass(frozen=True)
class Role:
    key: str
    title: str
    class_id: int
    primary: tuple[int, ...]
    speed_target: float
    equipment: tuple[int, ...] = ()


SMGS = (C.SMG_TOOL, C.CLASSIC_SMG_TOOL, C.TOMMYGUN_TOOL)
SHOTGUNS = (C.SHOTGUN_TOOL, C.CLASSIC_SHOTGUN_TOOL, C.AUTO_SHOTGUN_TOOL)
ROLES = (
    Role("soldier", "Soldier (Deuce)", C.CLASS_CLASSIC_SOLDIER,
         (C.ASSAULT_RIFLE_TOOL,) + SMGS, .80),
    Role("heavy", "Heavy (Commando)", C.CLASS_SOLDIER,
         (C.LIGHT_MACHINE_GUN_TOOL, C.SHOTGUN2_TOOL), .68),
    Role("recon", "Recon (Marksman)", C.CLASS_SCOUT,
         (C.SNIPER_TOOL, C.SNIPER2_TOOL), .80),
    Role("demolitionist", "Demolitionist (Miner)", C.CLASS_MINER,
         SMGS + SHOTGUNS, .76, (C.C4_TOOL, C.DYNAMITE_TOOL)),
    Role("medic", "Medic", C.CLASS_MEDIC,
         SMGS + SHOTGUNS, .80, (C.MEDPACK_TOOL,)),
)
BY_CLASS = {int(role.class_id): role for role in ROLES}
BY_NAME = {role.key: role for role in ROLES}
CLASS_IDS = tuple(BY_CLASS)
GRENADES = (int(C.GRENADE_TOOL), int(C.MOLOTOV_TOOL))


def slots(class_id: int) -> dict[int, tuple[int, ...]]:
    """Native slot overrides, including shared inventory and objective items.

    Retail has four selectable slots. Melee is common; its menu page selects a
    grenade instead. Equipment remains available for Medic/Miner special tools.
    """
    role = BY_CLASS[int(class_id)]
    pistols = (C.PISTOL_TOOL, C.AUTOMATIC_PISTOL_TOOL) if role.key == "recon" else (C.PISTOL_TOOL,)
    return {
        C.CLASS_MELEE: GRENADES,
        C.CLASS_PRIMARY_WEAPONS: role.primary,
        C.CLASS_SECONDARY_WEAPONS: pistols,
        C.CLASS_EQUIPMENT: role.equipment,
        C.CLASS_PREFABS: (),
        C.CLASS_COMMON: (C.BLOCK_TOOL, C.SPADE_TOOL, C.A370,
                         C.BOMB_TOOL, C.DIAMOND_TOOL, C.INTEL_TOOL),
        C.CLASS_UGC_TOOLS: (),
    }


def loadout_overrides() -> dict[tuple[int, int], list[int]]:
    return {(cid, slot): list(items) for cid in CLASS_IDS for slot, items in slots(cid).items()}


def normalize_selection(
    class_id: int, loadout: Iterable[int] = (), *, fallback_class_id: int | None = None,
) -> ClassSelection:
    from server.class_selection import ClassSelection

    cid = int(class_id)
    if cid == int(C.CLASS_ZOMBIE):
        # Zombie mode, alone, authorizes this choice at the team boundary.
        from server.class_selection import normalize_class_selection
        return normalize_class_selection(cid)
    if cid not in BY_CLASS:
        cid = int(fallback_class_id) if fallback_class_id in BY_CLASS else CLASS_IDS[0]
    requested = set(int(item) for item in loadout)
    table = slots(cid)
    chosen = []
    for slot in (C.CLASS_MELEE, C.CLASS_PRIMARY_WEAPONS,
                 C.CLASS_SECONDARY_WEAPONS, C.CLASS_EQUIPMENT):
        options = table[slot]
        if options:
            chosen.append(next((item for item in options if item in requested), options[0]))
    common = table[C.CLASS_COMMON]
    return ClassSelection(cid, tuple(dict.fromkeys((C.BLOCK_TOOL, *chosen, *common))))


def rule_overrides() -> dict[str, object]:
    from server.game_rules import CLASS_RULE_IDS, TOOL_RULE_IDS

    allowed = {int(tool) for cid in CLASS_IDS for items in slots(cid).values() for tool in items}
    result = {key: int(tool) in allowed for key, tool in TOOL_RULE_IDS.items()}
    result.update({key: int(cid) in BY_CLASS for key, cid in CLASS_RULE_IDS.items()})
    result.update({
        "RULE_ENABLE_GRAVESTONES": False,
        "RULE_ENABLE_CORPSE_EXPLOSION": False,
        "RULE_POINTS_FROM_TEABAGGING": True,
        "RULE_ONE_HIT_KILL": False,
        "RULE_CHARACTER_SPEED": 1.0,
        "RULE_WEAPON_DAMAGE": 1.0,
        "RULE_ENABLE_MINI_MAP": True,
        "RULE_ENABLE_DEATH_CAM": True,
        "RULE_ENABLE_SPECTATORS": True,
        "RULE_ENABLE_COLOUR_PICKER": True,
        "RULE_ENABLE_SNIPER_BEAM": False,
    })
    return result


def effective_rules(config: object, rules: GameRules) -> GameRules:
    """A cached live view: normal-rule edits never leak across mode switches."""
    from server.game_rules import GameRules

    cached = getattr(config, "_hardcore_rules", None)
    if cached is None or cached[0] is not rules:
        overlay = GameRules(ChainMap(rule_overrides(), rules.values), rules.explicit)
        cached = (rules, overlay)
        setattr(config, "_hardcore_rules", cached)
    return cached[1]


def speed_multiplier(class_id: int) -> float:
    """Scale the negotiated common movement profile, not the model's stock one."""
    role = BY_CLASS.get(int(class_id))
    if role is None:
        return 1.0
    return role.speed_target


def movement_profile(profile: MovementProfile) -> MovementProfile:
    """BSCF v3 / BATTLESPADES_HARDCORE v1 profile; mirrored in the native client.

    The .77 impulse reaches a two-block ledge using the one-block native step,
    but cannot reach a three-block wall, including a crouch jump.
    """
    from dataclasses import replace
    return replace(profile, accel_multiplier=.7, sprint_multiplier=1.05,
                   crouch_sneak_multiplier=.35, jump_multiplier=.77,
                   can_sprint_uphill=True, water_friction=8.0,
                   damage_multiplier=1.0, headshot_damage_multiplier=1.0,
                   fall_on_water_damage_multiplier=1.0,
                   falling_damage_min_distance=10, falling_damage_max_distance=40,
                   falling_damage_max_damage=100)


def bullet_multiplier(target: Player, *, headshot: bool, melee: bool) -> float | None:
    config = getattr(getattr(getattr(target, "connection", None), "server", None), "config", None)
    if not enabled(config) or int(getattr(target, "class_id", -1)) not in BY_CLASS:
        return None
    return .8 if int(target.class_id) == int(C.CLASS_SOLDIER) and not headshot and not melee else 1.0


def can_heal(player: Player, now: float) -> bool:
    """Shared recipient cooldown prevents simultaneous packs stacking heals."""
    return now - float(getattr(player, "_last_damage_at", 0.0)) >= 3.0 and now >= float(getattr(player, "_hardcore_next_heal", 0.0))
