"""Direct weapon fire and reload packet handlers."""

from __future__ import annotations

from protocol.handler_registry import register_handler
from server.combat_runtime import get_combat_system


@register_handler(6)  # ShootPacket
async def handle_shoot(server, player, packet) -> None:
    """Validate and resolve one server-authoritative weapon shot."""
    if not player.alive:
        return
    player.disguised = False
    get_combat_system(server).handle_shot(player, packet)


@register_handler(76)  # WeaponReload
async def handle_weapon_reload(server, player, packet) -> None:
    """Start or finish the authoritative reload state machine."""
    if player.alive:
        get_combat_system(server).handle_weapon_reload(player)
