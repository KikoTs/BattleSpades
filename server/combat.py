"""Compatibility entrypoint for the active combat runtime."""

from server.combat_runtime import CombatSystem, get_combat_system

__all__ = ["CombatSystem", "get_combat_system"]
