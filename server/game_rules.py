"""Validated Match Lobby game-rule catalog.

The retail client stores lobby rules under stable ``RULE_*`` names.  Keeping
those names at the server boundary makes reverse-engineering evidence,
``config.toml``, InitialInfo feature bits, and authoritative action gates refer
to the same setting.  This module has no runtime or network dependencies and
is safe to use from configuration validation and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


RuleValue = bool | int | float


@dataclass(frozen=True)
class RuleDefinition:
    """One recovered Match Lobby option and its accepted wire-menu values."""

    key: str
    category: str
    default: RuleValue
    choices: tuple[RuleValue, ...]
    modes: tuple[str, ...] = ()
    tool_id: int | None = None
    class_id: int | None = None
    menu_visible: bool = True

    @property
    def config_key(self) -> str:
        """Return the optional lower-case alias accepted in TOML."""

        return self.key.removeprefix("RULE_").lower()


def _toggle(
    key: str,
    category: str,
    *,
    default: bool = True,
    modes: Iterable[str] = (),
    tool_id: int | None = None,
    class_id: int | None = None,
    menu_visible: bool = True,
) -> RuleDefinition:
    return RuleDefinition(
        key=key,
        category=category,
        default=default,
        choices=(False, True),
        modes=tuple(modes),
        tool_id=tool_id,
        class_id=class_id,
        menu_visible=menu_visible,
    )


def _choice(
    key: str,
    category: str,
    default: RuleValue,
    choices: Iterable[RuleValue],
    *,
    modes: Iterable[str] = (),
    menu_visible: bool = True,
) -> RuleDefinition:
    return RuleDefinition(
        key=key,
        category=category,
        default=default,
        choices=tuple(choices),
        modes=tuple(modes),
        menu_visible=menu_visible,
    )


# A2712 in shared.constants_matchmaking.pyc.
CLASS_RULE_IDS: dict[str, int] = {
    "RULE_ENABLE_CLASS_COMMANDO": 0,
    "RULE_ENABLE_CLASS_MARKSMAN": 1,
    "RULE_ENABLE_CLASS_MINER": 3,
    "RULE_ENABLE_CLASS_ENGINEER": 12,
    "RULE_ENABLE_CLASS_ROCKETEER": 2,
    "RULE_ENABLE_CLASS_SPECIALIST": 16,
    "RULE_ENABLE_CLASS_MEDIC": 17,
}


# A2711 in shared.constants_matchmaking.pyc.  The normal parachute and riot
# shield are hidden from this build's visible rule rows but are retained so an
# operator can control every recovered tool switch without changing code.
TOOL_RULE_IDS: dict[str, int] = {
    "RULE_ENABLE_BLOCKS": 5,
    "RULE_ENABLE_EQUIPMENT_CLASSIC_SPADE": 4,
    "RULE_ENABLE_EQUIPMENT_CLASSIC_GRENADE": 31,
    "RULE_ENABLE_EQUIPMENT_SPADE": 2,
    "RULE_ENABLE_EQUIPMENT_GRENADE": 11,
    "RULE_ENABLE_EQUIPMENT_ANTIPERSONNEL_GRENADE": 32,
    "RULE_ENABLE_EQUIPMENT_SNOWBLOWER": 29,
    "RULE_ENABLE_EQUIPMENT_PICKAXE": 0,
    "RULE_ENABLE_EQUIPMENT_LANDMINE": 20,
    "RULE_ENABLE_EQUIPMENT_ROCKET_TURRET": 16,
    "RULE_ENABLE_EQUIPMENT_GLIDE_JETPACK": 67,
    "RULE_ENABLE_EQUIPMENT_JUMP_JETPACK": 66,
    "RULE_ENABLE_EQUIPMENT_JETPACK": 68,
    "RULE_ENABLE_EQUIPMENT_SUPER_SPADE": 3,
    "RULE_ENABLE_EQUIPMENT_DRILL_CANNON": 14,
    "RULE_ENABLE_EQUIPMENT_DYNAMITE": 21,
    "RULE_ENABLE_EQUIPMENT_MEDPACK": 51,
    "RULE_ENABLE_EQUIPMENT_CHEMICALBOMB": 54,
    "RULE_ENABLE_EQUIPMENT_RADAR_STATION": 56,
    "RULE_ENABLE_EQUIPMENT_C4": 59,
    "RULE_ENABLE_EQUIPMENT_DISGUISE": 64,
    "RULE_ENABLE_EQUIPMENT_PARACHUTE_NORMAL": 72,
    "RULE_ENABLE_FLARE_BLOCKS": 22,
    "RULE_ENABLE_PREFABS": 23,
    "RULE_ENABLE_WEAPON_KNIFE": 1,
    "RULE_ENABLE_WEAPON_MINIGUN": 8,
    "RULE_ENABLE_WEAPON_RPG": 12,
    "RULE_ENABLE_WEAPON_TRIPLE_BARREL_RPG": 13,
    "RULE_ENABLE_WEAPON_PISTOL": 17,
    "RULE_ENABLE_WEAPON_SNIPER_RIFLE": 18,
    "RULE_ENABLE_WEAPON_SNIPER_RIFLE2": 19,
    "RULE_ENABLE_WEAPON_RIFLE": 6,
    "RULE_ENABLE_WEAPON_DOUBLE_BARREL_SHOTGUN": 10,
    "RULE_ENABLE_WEAPON_PUMP_ACTION_SHOTGUN": 9,
    "RULE_ENABLE_WEAPON_SMG": 7,
    "RULE_ENABLE_WEAPON_CLASSIC_SHOTGUN": 37,
    "RULE_ENABLE_WEAPON_CLASSIC_SMG": 38,
    "RULE_ENABLE_WEAPON_TOMMYGUN": 35,
    "RULE_ENABLE_WEAPON_SNUB_PISTOL": 36,
    "RULE_ENABLE_WEAPON_CROWBAR": 34,
    "RULE_ENABLE_WEAPON_MOLOTOV": 33,
    "RULE_ENABLE_WEAPON_RIOTSTICK": 49,
    "RULE_ENABLE_WEAPON_RIOTSHIELD": 52,
    "RULE_ENABLE_WEAPON_MACHETE": 50,
    "RULE_ENABLE_WEAPON_AUTOPISTOL": 53,
    "RULE_ENABLE_WEAPON_GRENADE_LAUNCHER": 55,
    "RULE_ENABLE_WEAPON_STICKY_GRENADE": 57,
    "RULE_ENABLE_WEAPON_MINE_LAUNCHER": 58,
    "RULE_ENABLE_WEAPON_ASSAULTRIFLE": 60,
    "RULE_ENABLE_WEAPON_LIGHTMACHINEGUN": 61,
    "RULE_ENABLE_WEAPON_AUTOSHOTGUN": 62,
    "RULE_ENABLE_WEAPON_BLOCKSUCKER": 63,
}


_GENERAL_TOGGLES = (
    "RULE_ENABLE_BLOCKS",
    "RULE_ENABLE_FLARE_BLOCKS",
    "RULE_ENABLE_PREFABS",
    "RULE_ENABLE_GRAVESTONES",
    "RULE_ENABLE_CORPSE_EXPLOSION",
    "RULE_ENABLE_SNIPER_BEAM",
    "RULE_ENABLE_DEATH_CAM",
    "RULE_ENABLE_MINI_MAP",
    "RULE_ENABLE_SPECTATORS",
    "RULE_ENABLE_FALL_ON_WATER_DAMAGE",
    "RULE_ENABLE_COLOUR_PICKER",
)

_EQUIPMENT_TOGGLES = tuple(
    key for key in TOOL_RULE_IDS if key.startswith("RULE_ENABLE_EQUIPMENT_")
)
_WEAPON_TOGGLES = tuple(
    key for key in TOOL_RULE_IDS if key.startswith("RULE_ENABLE_WEAPON_")
)


def _build_catalog() -> dict[str, RuleDefinition]:
    rules: list[RuleDefinition] = []
    for key in _GENERAL_TOGGLES:
        rules.append(_toggle(key, "GENERAL", tool_id=TOOL_RULE_IDS.get(key)))
    rules.extend((
        _toggle("RULE_ONE_HIT_KILL", "GENERAL", default=False),
        _toggle("RULE_POINTS_FROM_TEABAGGING", "GENERAL", default=False),
        _choice("RULE_RESPAWN_TIMES", "GENERAL", 10, range(0, 61, 5)),
        _choice("RULE_BLOCK_HEALTH", "GENERAL", 1.0, (0.5, 1.0, 2.0)),
        _choice("RULE_WEAPON_DAMAGE", "GENERAL", 1.0, (0.5, 1.0, 2.0)),
        _choice("RULE_SPAWN_PROTECTION_TIME", "GENERAL", 3.0,
                (0.0, 1.0, 2.0, 3.0)),
        _choice("RULE_CHARACTER_BLOCK_WALLETS", "GENERAL", 1.0,
                (0.5, 1.0, 2.0)),
        _choice("RULE_CHARACTER_SPEED", "GENERAL", 1.0,
                (0.5, 1.0, 1.5, 2.0)),
        _choice("RULE_CRATES_SPAWN_TIME", "GENERAL", 25,
                range(10, 61, 5)),
        _choice("RULE_VOTES_REQUIRED_FOR_KICK", "GENERAL", 0.5,
                (0.25, 0.5, 0.75), menu_visible=False),
    ))

    for key, class_id in CLASS_RULE_IDS.items():
        rules.append(_toggle(key, "CLASSES", class_id=class_id))
    for key in _EQUIPMENT_TOGGLES:
        rules.append(_toggle(
            key,
            "EQUIPMENT",
            tool_id=TOOL_RULE_IDS[key],
            menu_visible=key != "RULE_ENABLE_EQUIPMENT_PARACHUTE_NORMAL",
        ))
    for key in _WEAPON_TOGGLES:
        rules.append(_toggle(
            key,
            "WEAPONS",
            default=key not in {
                "RULE_ENABLE_WEAPON_CLASSIC_SHOTGUN",
                "RULE_ENABLE_WEAPON_CLASSIC_SMG",
            },
            tool_id=TOOL_RULE_IDS[key],
            menu_visible=key != "RULE_ENABLE_WEAPON_RIOTSHIELD",
        ))

    rules.extend((
        _choice("RULE_TDM_SCORE_TARGET", "tdm", 200,
                (False, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50,
                 60, 70, 80, 90, 100, 200), modes=("tdm",)),
        _toggle("RULE_CTF_ENABLE_SHOOT_WITH_INTEL", "ctf", default=False,
                modes=("ctf", "cctf")),
        _toggle("RULE_CTF_ENABLE_INTEL_RETURN_ON_TOUCH", "ctf", default=False,
                modes=("ctf", "cctf")),
        _choice("RULE_CTF_SCORE_TARGET", "ctf", 5, range(1, 11),
                modes=("ctf", "cctf")),
        _toggle("RULE_CTF_ENABLE_INTEL_AUTO_RETURN", "ctf", modes=("ctf", "cctf")),
        _toggle("RULE_CTF_ENABLE_INTEL_IN_OWN_BASE_TO_SCORE", "ctf",
                default=False, modes=("ctf", "cctf"), menu_visible=False),
        _choice("RULE_ZOMBIE_NOOF_ROUNDS", "zom", 3, range(1, 6), modes=("zom",)),
        _choice("RULE_NOOF_FIRST_INFECTED_ZOMBIES", "zom", 2,
                range(1, 6), modes=("zom",)),
        _choice("RULE_CLASS_SPEED", "zom", 1.0, (0.5, 1.0, 2.0), modes=("zom",)),
        _choice("RULE_ZOMBIE_CLASS_DAMAGE", "zom", 1.0,
                (0.5, 1.0, 2.0), modes=("zom",)),
        _choice("RULE_VIP_NOOF_ROUNDS", "vip", 3, range(1, 6), modes=("vip",)),
        _choice("RULE_VIP_HEALTH", "vip", 1.0, (0.5, 1.0, 2.0), modes=("vip",)),
        _toggle("RULE_ENABLE_SUDDEN_DEATH", "vip", modes=("vip",)),
        _choice("RULE_MULTIHILL_MAX_ACTIVE_BASES", "mh", 1, range(1, 6), modes=("mh",)),
        _choice("RULE_BASE_ACTIVE_TIME", "mh", 240,
                (30, 60, 90, 120, 180, 240, 300, 360, 420, 480, 540, 600),
                modes=("mh",)),
        _choice("RULE_TC_MAX_ACTIVE_BASES", "tc", 5, range(2, 6), modes=("tc",)),
        _choice("RULE_CAPTURE_RATE", "tc", 1.0, (0.5, 1.0, 2.0), modes=("tc",)),
        _choice("RULE_DIAMOND_MAX_ACTIVE_BASES", "dia", 1, range(1, 6), modes=("dia",)),
        _choice("RULE_DIA_SCORE_TARGET", "dia", 15, range(5, 61, 5), modes=("dia",)),
        _choice("RULE_MAX_ACTIVE_DIAMONDS", "dia", 2, range(1, 6), modes=("dia",)),
        _choice("RULE_DIAMOND_LIFETIME", "dia", 60, range(10, 61, 10), modes=("dia",)),
        _choice("RULE_BUILD_STATE_LENGTH", "dem", 30,
                (False, *range(10, 121, 10)), modes=("dem",)),
        _choice("RULE_OCC_SCORE_TARGET", "oc", 30,
                (False, 3, 6, 9, 15, 30, 45, 60, 75, 90, 150), modes=("oc",)),
        _choice("RULE_MAX_ACTIVE_BOMBS", "oc", 1, range(1, 4), modes=("oc",)),
        _choice("RULE_BOMB_FUSE_TIME", "oc", 10, (5, 10, 15, 20), modes=("oc",)),
    ))
    return {rule.key: rule for rule in rules}


RULE_DEFINITIONS: dict[str, RuleDefinition] = _build_catalog()
_ALIASES = {
    definition.config_key: definition.key
    for definition in RULE_DEFINITIONS.values()
}


def _same_value(value: Any, allowed: RuleValue) -> bool:
    """Compare choices without treating ``False`` as integer zero."""

    if isinstance(allowed, bool):
        return isinstance(value, bool) and value is allowed
    if isinstance(value, bool):
        return False
    if isinstance(allowed, int):
        return isinstance(value, int) and value == allowed
    return isinstance(value, (int, float)) and float(value) == float(allowed)


def _coerce_value(definition: RuleDefinition, raw: Any) -> RuleValue:
    if isinstance(raw, str):
        text = raw.strip().upper()
        if text in ("ON", "TRUE"):
            raw = True
        elif text in ("OFF", "FALSE"):
            # Some retail sliders spell their numeric zero choice "OFF";
            # score-target sliders instead use a real boolean False sentinel.
            raw = (
                False
                if any(isinstance(value, bool) and value is False
                       for value in definition.choices)
                else 0.0
            )
        elif text.endswith("%"):
            try:
                raw = float(text[:-1]) / 100.0
            except ValueError:
                pass
        else:
            try:
                raw = float(text) if "." in text else int(text)
            except ValueError:
                pass
    for allowed in definition.choices:
        if _same_value(raw, allowed):
            return allowed
    accepted = ", ".join(repr(value) for value in definition.choices)
    raise ValueError(
        f"{definition.key} must be one of {accepted}; received {raw!r}"
    )


@dataclass
class GameRules:
    """Resolved immutable-key rule values used by all server domains."""

    values: dict[str, RuleValue] = field(default_factory=dict)
    explicit: set[str] = field(default_factory=set)

    @classmethod
    def retail_defaults(cls) -> "GameRules":
        return cls({key: rule.default for key, rule in RULE_DEFINITIONS.items()})

    @classmethod
    def server_defaults(cls) -> "GameRules":
        """Compatibility defaults for a server created without TOML.

        The retail lobby defaults respawn to ten seconds and exposes the flare
        tool. BattleSpades historically uses five seconds and suppresses the
        native flare-as-first-prefab menu defect; the exhaustive sample TOML
        makes both differences visible and adjustable.
        """

        result = cls.retail_defaults()
        result.values["RULE_RESPAWN_TIMES"] = 5
        result.values["RULE_CTF_SCORE_TARGET"] = 10
        # Preserve established dedicated-server behavior unless the new
        # exhaustive rule table explicitly opts into the retail three-second
        # protection window.
        result.values["RULE_SPAWN_PROTECTION_TIME"] = 0.0
        result.values["RULE_CRATES_SPAWN_TIME"] = 15
        return result

    def apply(self, mapping: Mapping[str, Any]) -> None:
        """Validate and apply a TOML ``[game_rules]`` mapping in place."""

        for raw_key, raw_value in mapping.items():
            requested = str(raw_key).strip()
            key = requested.upper()
            if key not in RULE_DEFINITIONS:
                key = _ALIASES.get(requested.lower(), "")
            if not key:
                raise ValueError(f"Unknown Match Lobby game rule: {raw_key}")
            self.values[key] = _coerce_value(RULE_DEFINITIONS[key], raw_value)
            self.explicit.add(key)

    def get(self, key: str) -> RuleValue:
        canonical = str(key).upper()
        if canonical not in RULE_DEFINITIONS:
            raise KeyError(canonical)
        return self.values.get(canonical, RULE_DEFINITIONS[canonical].default)

    def enabled(self, key: str) -> bool:
        return bool(self.get(key))

    def is_tool_enabled(self, tool_id: int) -> bool:
        tool_id = int(tool_id)
        return all(
            self.enabled(key)
            for key, mapped_tool in TOOL_RULE_IDS.items()
            if int(mapped_tool) == tool_id
        )

    def is_class_enabled(self, class_id: int) -> bool:
        class_id = int(class_id)
        return all(
            self.enabled(key)
            for key, mapped_class in CLASS_RULE_IDS.items()
            if int(mapped_class) == class_id
        )

    def disabled_tools(self) -> tuple[int, ...]:
        return tuple(sorted({
            int(tool_id)
            for key, tool_id in TOOL_RULE_IDS.items()
            if not self.enabled(key)
        }))

    def selection_disabled_tools(self) -> tuple[int, ...]:
        """Return menu/loadout disables including the legacy flare safeguard.

        A programmatic ``ServerConfig()`` predates the exhaustive TOML and
        historically hid flare tool 22 from the buggy prefab page while still
        allowing tests/plugins to place it explicitly. An explicit TOML flare
        rule removes that compatibility ambiguity: false disables it fully;
        true exposes and authorizes it.
        """

        disabled = set(self.disabled_tools())
        if "RULE_ENABLE_FLARE_BLOCKS" not in self.explicit:
            disabled.add(22)
        return tuple(sorted(disabled))

    def disabled_classes(self) -> tuple[int, ...]:
        return tuple(sorted({
            int(class_id)
            for key, class_id in CLASS_RULE_IDS.items()
            if not self.enabled(key)
        }))


def get_rules(config: object) -> GameRules:
    """Return a config's rule service, including lightweight test doubles."""

    rules = getattr(config, "game_rules", None)
    return rules if isinstance(rules, GameRules) else GameRules.server_defaults()


__all__ = [
    "CLASS_RULE_IDS",
    "GameRules",
    "RULE_DEFINITIONS",
    "RuleDefinition",
    "TOOL_RULE_IDS",
    "get_rules",
]
