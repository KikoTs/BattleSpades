"""High-frequency client movement and clock packet handlers.

Packet 4 arrives roughly once per rendered frame.  Production handling must
not format or emit per-frame logs; optional traces are gated by the explicit
``movement_debug_capture`` switch and DEBUG level.
"""

from __future__ import annotations

import logging
import time

from protocol.handler_registry import register_handler

logger = logging.getLogger(__name__)


def _input_flags(packet) -> int:
    raw = getattr(packet, "input_flags", None)
    if raw is not None:
        return int(raw) & 0xFF
    values = (
        "up", "down", "left", "right", "jump", "crouch", "sneak", "sprint"
    )
    return sum(
        (1 << index) if getattr(packet, name, False) else 0
        for index, name in enumerate(values)
    )


def _action_flags(packet) -> int:
    raw = getattr(packet, "action_flags", None)
    if raw is not None:
        return int(raw) & 0xFF
    values = (
        "primary", "secondary", "zoom", "can_pickup",
        "can_display_weapon", "is_on_fire", "is_weapon_deployed", "hover",
    )
    return sum(
        (1 << index) if getattr(packet, name, False) else 0
        for index, name in enumerate(values)
    )


@register_handler(4)  # ClientData
async def handle_client_data(server, player, packet) -> None:
    """Buffer input by retail loop stamp and refresh immediate action state."""
    previous_jump = player.jump_held
    previous_pending = getattr(player, "pending_jump", False)
    flags = (
        packet.up,
        packet.down,
        packet.left,
        packet.right,
        packet.jump,
        packet.crouch,
        packet.sneak,
        packet.sprint,
    )
    player.record_input_frame(
        packet.loop_count,
        flags,
        (packet.o_x, packet.o_y, packet.o_z),
        action_flags=(
            packet.primary,
            packet.secondary,
            packet.zoom,
            packet.can_pickup,
            packet.can_display_weapon,
            packet.is_on_fire,
            packet.is_weapon_deployed,
            packet.hover,
            packet.palette_enabled,
        ),
        # Real servers always expose loop_count. The fallback keeps the domain
        # handler usable by isolated protocol tests and administrative probes;
        # zero simply makes the age filter conservative within that seam.
        received_server_tick=int(getattr(server, "loop_count", 0)),
        wire_unknown_byte=int(packet.ooo),
    )
    player.set_orientation_vector(packet.o_x, packet.o_y, packet.o_z)
    player.update_input(*flags)
    player.update_action_input(
        packet.primary,
        packet.secondary,
        packet.zoom,
        packet.can_pickup,
        packet.can_display_weapon,
        packet.is_on_fire,
        packet.is_weapon_deployed,
        packet.hover,
        packet.palette_enabled,
    )
    from server.game_rules import get_rules
    if get_rules(server.config).is_tool_enabled(packet.tool_id):
        player.set_tool(packet.tool_id, raw=True)

    capture = bool(getattr(server.config, "movement_debug_capture", False))
    if capture and logger.isEnabledFor(logging.DEBUG):
        jump_changed = previous_jump != player.jump_held
        pending = getattr(player, "pending_jump", False)
        if packet.jump or jump_changed or previous_pending != pending:
            logger.debug(
                "ClientData jump %s loop=%s input=0x%02X action=0x%02X "
                "held=%s pending=%s",
                player.name,
                packet.loop_count,
                _input_flags(packet),
                _action_flags(packet),
                player.jump_held,
                pending,
            )


@register_handler(0)  # ClockSync
async def handle_clock_sync(server, player, packet) -> None:
    """Reply with the authoritative loop anchor used for client pacing."""
    if player.connection:
        player.connection.send_clock_sync_response(packet.client_time)


@register_handler(116)  # PositionData
async def handle_position_data(server, player, packet) -> None:
    """Record the latest client sample for drift measurement/correction."""
    reported = (packet.x, packet.y, packet.z)
    player.position_reports_received += 1
    player.last_reported_position = reported
    player.last_position_update = time.time()
    dx = reported[0] - player.x
    dy = reported[1] - player.y
    dz = reported[2] - player.z
    player.last_position_drift_vector = (dx, dy, dz)
    player.last_position_drift = (dx * dx + dy * dy + dz * dz) ** 0.5
