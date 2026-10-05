"""Classic Ace of Spades capture-the-flag rules for the retail CTF scene.

The native client does not enter a separate scene for Classic CTF.  It enters
the ordinary CTF scene and reads ``InitialInfo.classic`` to select the Deuce
models, classic HUD behavior, and classic tool implementations.  Keeping that
wire invariant is important: sending the unused ``MODE_CCTF`` enum as the
scene id leaves this retail build without a compatible game scene.
"""

from __future__ import annotations

import shared.constants as C
import shared.constants_gamemode as CG

from server.class_selection import (
    DEFAULT_DISABLED_TOOLS,
    ClassSelection,
    normalize_class_selection,
)
from server.game_constants import TEAM1, TEAM2
from server.corpse_lifecycle import CLASSIC_CORPSE_REPRESENTATION

from .ctf import CTFMode


_CLASSIC_DISABLED_TOOLS = (
    *DEFAULT_DISABLED_TOOLS,
    # Classic has no Flare Block: selectClass shows no tile for the Classic
    # Soldier and CLASS_CLASSIC_COMMON_TOOLS does not carry tool 22.
    int(C.FLAREBLOCK_TOOL),
)

# Classic weapon rules that default ON in Classic CTF unless set explicitly.
_CLASSIC_WEAPON_RULES = {
    "RULE_ENABLE_WEAPON_CLASSIC_SMG": int(C.CLASSIC_SMG_TOOL),
    "RULE_ENABLE_WEAPON_CLASSIC_SHOTGUN": int(C.CLASSIC_SHOTGUN_TOOL),
}


class ClassicCTFMode(CTFMode):
    """Run the shipped ``classic.txt`` playlist on the CTF wire protocol.

    Both teams use the single Deuce class.  The playlist enables shooting
    while carrying intel and turns off the ordinary 60-second dropped-intel
    return. The Deuce chooses rifle, SMG or shotgun by default (the shipped
    playlist had both OFF); RULE_ENABLE_WEAPON_CLASSIC_SMG and
    RULE_ENABLE_WEAPON_CLASSIC_SHOTGUN set explicitly still decide.  All hooks run on the gameplay
    tick and accept only normalized class selections.
    """

    name = "Classic CTF"
    description = "Classic Deuce CTF: capture the enemy intel!"
    mode_code = "cctf"
    death_representation = CLASSIC_CORPSE_REPRESENTATION
    intel_auto_return_default = False
    shoot_with_intel_default = True
    # Retail Classic CTF: the intel keeps the Classic-only
    # ``CLASSIC_CTF_INTEL_MIN_RADIUS_FROM_BASE`` (3) from the base point, which
    # the retail capture radius (``CLASSIC_CTF_BASE_CAPTURE_DISTANCE`` 5) still
    # covers. Its direction is not recoverable; we use toward the enemy base.
    intel_offset_from_base = float(CG.CLASSIC_CTF_INTEL_MIN_RADIUS_FROM_BASE)
    intel_fallback_offset_from_base = intel_offset_from_base
    stock_maps = (
        "Crossroads",
        "Hiesville",
        "ToTheBridge",
        "Trenches",
        "WinterValley",
        "WW1",
        "Classic",
    )

    def __init__(self, server) -> None:
        super().__init__(server)
        # BattleSpades historically used ten captures when no TOML was loaded,
        # while the retail Match Lobby and Classic playlist use five. Preserve
        # an explicit operator rule/overlay, otherwise recover the stock value.
        overlay = getattr(server.config, "mode_settings", {}).get("cctf", {})
        from server.game_rules import get_rules

        rules = get_rules(server.config)
        # Default the Deuce weapon choice ON in the shared rules, so every
        # path agrees: InitialInfo, join and class-change normalisation
        # (normalize_server_selection strips rule-disabled tools before the
        # mode sees a selection). A rule the operator named is left alone.
        for key in _CLASSIC_WEAPON_RULES:
            if key not in getattr(rules, "explicit", set()):
                rules.values[key] = True
        if (
            "score_limit" not in overlay
            and "RULE_CTF_SCORE_TARGET" not in getattr(rules, "explicit", set())
        ):
            self.score_limit = 5

    def _disabled_tools(self) -> tuple[int, ...]:
        """Classic's fixed exclusions plus every tool the game rules turn off."""

        from server.game_rules import get_rules

        rules = get_rules(self.server.config)
        disabled = set(_CLASSIC_DISABLED_TOOLS)
        disabled.update(int(tool) for tool in rules.selection_disabled_tools())
        # BattleSpades default: the Deuce chooses rifle, SMG or shotgun. The
        # shipped playlist had both OFF; an operator who names either rule
        # (TOML, Create Match) gets exactly what they set.
        explicit = getattr(rules, "explicit", set())
        for key, tool in _CLASSIC_WEAPON_RULES.items():
            if key not in explicit:
                disabled.discard(tool)
        return tuple(sorted(disabled))

    def prepare_join_selection(
        self,
        team: int,
        selection: ClassSelection,
    ) -> ClassSelection:
        """Coerce every playable-team join to the retail Deuce loadout."""

        if int(team) not in (TEAM1, TEAM2):
            return selection
        return normalize_class_selection(
            int(C.CLASS_CLASSIC_SOLDIER),
            selection.loadout,
            selection.prefabs,
            selection.ugc_tools,
            disabled_tools=self._disabled_tools(),
        )

    def allows_class_selection(
        self,
        player,
        selection: ClassSelection,
    ) -> bool:
        """Reject forged class/tool combinations during the active life."""

        if int(getattr(player, "team", -1)) not in (TEAM1, TEAM2):
            return False
        return selection == self.prepare_join_selection(player.team, selection)

    def configure_state_data(self, packet) -> None:
        """Expose the single Deuce class to both native team menus.

        The class is not locked: a locked team never opens SelectClass, which
        hid the weapon choice when the rules enable the Classic SMG/shotgun.
        With the rifle alone the client still spawns without a menu
        (GameScene.class_selection_has_choices).
        """

        classes = [int(C.CLASS_CLASSIC_SOLDIER)]
        packet.team1_classes = classes
        packet.team2_classes = list(classes)
        packet.team1_locked_class = False
        packet.team2_locked_class = False

    def configure_initial_info(self, packet) -> None:
        """Apply the shipped Classic playlist feature and weapon switches."""

        # ``classic`` is consumed by GameScene.is_in_classic_mode(); the mode
        # id remains ordinary CTF so the stock executable builds a valid scene.
        packet.classic = 1
        packet.enable_minimap = 0
        packet.allow_shooting_holding_intel = int(self.shoot_with_intel)
        classic_disabled = self._disabled_tools()
        # The builder filled this from the raw rules (both Classic weapons
        # OFF by default); drop the ones Classic CTF enables.
        enabled = set(_CLASSIC_WEAPON_RULES.values()) - set(classic_disabled)
        disabled = [int(tool) for tool in packet.disabled_tools if int(tool) not in enabled]
        for tool in classic_disabled:
            if int(tool) not in disabled:
                disabled.append(int(tool))
        packet.disabled_tools = disabled


__all__ = ["ClassicCTFMode"]
