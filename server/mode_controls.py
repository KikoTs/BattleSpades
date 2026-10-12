"""Optional synchronous gameplay gates; official modes keep their defaults."""
from __future__ import annotations


def allows(server, player, action: str, position=None) -> bool:
    gate = getattr(getattr(server, "mode", None), "allow_action", None)
    return not callable(gate) or bool(gate(player, action, position))


def enemies(server, a, b) -> bool:
    gate = getattr(getattr(server, "mode", None), "players_are_enemies", None)
    if callable(gate):
        return bool(gate(a, b))
    return a.team != b.team or bool(getattr(server.config, "friendly_fire", False))


def opponents(server, a, b) -> bool:
    """Scoring relation: friendly fire alone does not make an ally an enemy."""
    gate = getattr(getattr(server, "mode", None), "players_are_enemies", None)
    return bool(gate(a, b)) if callable(gate) else a.team != b.team
