"""Classic+ survivor equipment with the existing Zombie infection rules."""

from __future__ import annotations

from collections.abc import Iterable

import shared.constants as C
from server.class_selection import ClassSelection, normalize_class_selection
from server.game_rules import get_rules

from .classic_ctf import _CLASSIC_DISABLED_TOOLS, _CLASSIC_WEAPON_RULES
from .zombie import ZombieMode


class ClassicZombieMode(ZombieMode):
    """Deuce survivors versus the ordinary horde on the Zombie wire scene."""

    name = "Classic+ Zombies"
    description = "Survive the outbreak with the Classic+ rifle, SMG or shotgun."
    mode_code = "zom"
    default_survivor_class = int(C.CLASS_CLASSIC_SOLDIER)
    survivor_class_order = (int(C.CLASS_CLASSIC_SOLDIER),)
    survivor_classes = frozenset(survivor_class_order)

    def __init__(self, server) -> None:
        super().__init__(server)
        rules = get_rules(server.config)
        for key in _CLASSIC_WEAPON_RULES:
            if key not in rules.explicit:
                rules.values[key] = True

    def normalize_survivor_selection(
        self, class_id: int, loadout: Iterable[int] = (),
        prefabs: Iterable[str] = (), ugc_tools: Iterable[int] = (),
    ) -> ClassSelection:
        disabled = set(_CLASSIC_DISABLED_TOOLS)
        disabled.update(get_rules(self.server.config).selection_disabled_tools())
        return normalize_class_selection(
            self.default_survivor_class, loadout, prefabs, ugc_tools,
            disabled_tools=tuple(sorted(disabled)),
        )
