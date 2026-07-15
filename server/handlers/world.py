"""Oriented projectile use and objective pickup handlers."""

from __future__ import annotations

from protocol.handler_registry import register_handler


@register_handler(10)  # UseOrientedItem — thrown grenades / RPG rockets
async def handle_oriented_item(server, player, packet):
    """A player threw a grenade (or fired an RPG). The client sends its own
    predicted position+velocity+fuse; we rebroadcast it so every OTHER client
    renders and simulates the projectile (arc + explosion FX + sound), and we
    register a server-authoritative grenade that applies blast damage and
    block destruction when the fuse expires."""
    service = getattr(server, "oriented_actions", None)
    if service is None:
        # Focused protocol tests/embedders may construct only the legacy
        # server facade. Production wires this dependency explicitly.
        from server.oriented_actions import OrientedActionService

        service = OrientedActionService(server)
    return service.use(
        player,
        tool_id=int(getattr(packet, "tool", -1)),
        position=getattr(packet, "position", (0.0, 0.0, 0.0)),
        velocity=getattr(packet, "velocity", (0.0, 0.0, 0.0)),
        fuse=float(getattr(packet, "value", 0.0)),
    )


@register_handler(71)  # DropPickup
async def handle_drop_pickup(server, player, packet):
    """Validate a carrier's drop and relay only authoritative identity/type."""
    if not player.alive or not player.spawned:
        return
    pickup_id = getattr(player, "pickup_id", None)
    if pickup_id is None or int(getattr(packet, "pickup_id", -1)) != int(pickup_id):
        return
    mode_handler = getattr(getattr(server, "mode", None), "handle_drop_pickup", None)
    if mode_handler is not None and await mode_handler(
        player, packet.position, packet.velocity
    ):
        return

    from server.pickups import broadcast_drop
    result = broadcast_drop(server, player, packet.position, packet.velocity)
    if result is None:
        return
    dropped_type, state, position, velocity = result
    server.entity_registry.place(
        dropped_type, *position, state=state, kind="pickup",
        vel=velocity, radius=0.5,
    )
