"""High-frequency client movement and clock packet handlers.

Packet 4 arrives roughly once per rendered frame.  Production handling must
not format or emit per-frame logs; optional traces are gated by the explicit
``movement_debug_capture`` switch and DEBUG level.
"""

from __future__ import annotations

import logging
import math
import time

import shared.constants as C

from protocol.handler_registry import register_handler
from server import anticheat
from server.class_selection import equipped_tool_authorized

logger = logging.getLogger(__name__)

# Per-peer packet budgets: (rate per second, burst, [anticheat] override keys).
# A retail client sends one ClientData per 60 Hz update, so 240/s with a
# 64-packet burst leaves 4x headroom plus a full second of catch-up frames.
# The drain runs at tick start, so a server hitch delivers a whole backlog
# at once; the bucket therefore grows with the elapsed time (up to the
# simulation's 2 s catch-up horizon) instead of capping at the burst.
_RATE_LIMITS = {
    "client_data": (240.0, 64.0, "client_data_rate", "client_data_burst"),
    "clock_sync": (4.0, 16.0, "clock_sync_rate", "clock_sync_burst"),
    "position_data": (240.0, 64.0, "position_data_rate", "position_data_burst"),
}
_RATE_CATCH_UP_SECONDS = 2.0


def _rate_allowed(server, player, kind: str, now: float | None = None) -> bool:
    """Token-bucket admission for one high-frequency packet from ``player``."""
    if player is None or getattr(player, "is_bot", False):
        return True
    default_rate, default_burst, rate_key, burst_key = _RATE_LIMITS[kind]
    rate = float(anticheat.setting(server, rate_key, default_rate))
    burst = float(anticheat.setting(server, burst_key, default_burst))
    if rate <= 0.0:
        return True
    buckets = getattr(player, "_packet_rate_buckets", None)
    if not isinstance(buckets, dict):
        buckets = {}
        try:
            player._packet_rate_buckets = buckets
        except AttributeError:
            return True
    now = time.monotonic() if now is None else float(now)
    # Simulated time as a second clock: the fixed-step loop can legitimately
    # run ahead of the wall clock (catch-up batches, offline replays), and
    # every simulated tick earns the client its 60 Hz frame.
    tick_rate = float(getattr(server, "tick_rate", 60) or 60)
    sim_now = float(getattr(server, "loop_count", 0) or 0) / tick_rate
    state = buckets.get(kind)
    if state is None:
        tokens = burst
    else:
        tokens, last, last_sim = state
        elapsed = max(0.0, now - last, sim_now - last_sim)
        capacity = max(burst, rate * min(elapsed, _RATE_CATCH_UP_SECONDS))
        tokens = max(tokens, 0.0)
        # Refill up to the capacity earned by this gap; never shrink a
        # post-hitch allowance before the drained backlog has used it.
        tokens += min(rate * elapsed, max(0.0, capacity - tokens))
    if tokens >= 1.0:
        buckets[kind] = (tokens - 1.0, now, sim_now)
        return True
    buckets[kind] = (tokens, now, sim_now)
    anticheat.report(server, player, f"rate:{kind}", rate=rate, burst=burst)
    return False


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
    if not _rate_allowed(server, player, "client_data"):
        return
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
    if equipped_tool_authorized(player, packet.tool_id):
        player.set_tool(packet.tool_id, raw=True)
        # The original UGC host paints its local VXL before packet-7
        # replication.  Direct dedicated-editor clients safely join as UGC
        # clients and may therefore expose only held input here.  Reconstruct
        # the brush through the same authoritative paint service.
        if int(packet.tool_id) == int(C.PAINTBRUSH_TOOL):
            player.ugc_paint_secondary_held = bool(
                getattr(packet, "secondary", False)
            )
        if int(packet.tool_id) == int(C.PAINTBRUSH_TOOL) and (
            bool(getattr(packet, "primary", False))
            or bool(getattr(packet, "secondary", False))
        ):
            from server.combat_runtime import get_combat_system

            painted = get_combat_system(server).handle_paintbrush_input(
                player, packet
            )
            # Single-cell LMB strokes play the retail PAINT_PRIMARY_SOUND.
            if (
                painted
                and not bool(getattr(packet, "secondary", False))
                and bool(getattr(server.config, "ugc_runtime", False))
            ):
                _ugc_brush_paint_cue(server, player)
    elif player.alive and player.spawned:
        # ClientData is a high-frequency packet, so do not emit attacker-
        # controlled log spam.  This counter is intentionally cheap and gives
        # diagnostics/admin tooling a way to identify repeated forged states.
        player.rejected_tool_updates = int(
            getattr(player, "rejected_tool_updates", 0)
        ) + 1
        anticheat.report(
            server,
            player,
            "tool_rejected",
            tool=int(packet.tool_id),
            held=int(getattr(player, "tool", -1)),
        )
        # The held tool itself may have become illegal (MG left, loadout
        # changed); never keep acting with a tool this life cannot hold.
        ensure_legal = getattr(player, "ensure_legal_tool", None)
        if callable(ensure_legal):
            ensure_legal()

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
    if not _rate_allowed(server, player, "clock_sync"):
        return
    if player.connection:
        player.connection.send_clock_sync_response(packet.client_time)


@register_handler(116)  # PositionData
async def handle_position_data(server, player, packet) -> None:
    """Record the latest client sample for drift measurement/correction."""
    if not _rate_allowed(server, player, "position_data"):
        return
    reported = (packet.x, packet.y, packet.z)
    try:
        reported = tuple(float(value) for value in reported)
    except (TypeError, ValueError):
        return
    if not all(math.isfinite(value) for value in reported):
        anticheat.report(server, player, "position_nonfinite")
        return
    player.position_reports_received += 1
    player.last_reported_position = reported
    player.last_position_update = time.time()
    dx = reported[0] - player.x
    dy = reported[1] - player.y
    dz = reported[2] - player.z
    player.last_position_drift_vector = (dx, dy, dz)
    player.last_position_drift = (dx * dx + dy * dy + dz * dz) ** 0.5


def _ugc_brush_paint_cue(server, player) -> None:
    """Locate the brushed cell like the paint service and play its cue."""

    cue = getattr(getattr(server, "mode", None), "on_single_paint", None)
    world = getattr(server, "world_manager", None)
    raycast = getattr(world, "raycast", None)
    if not callable(cue) or not callable(raycast):
        return
    try:
        dx, dy, dz = (float(v) for v in player.orientation)
        length = (dx * dx + dy * dy + dz * dz) ** 0.5
        if length <= 1e-6:
            return
        ex, ey, ez = (float(v) for v in player.eye)
        cell = raycast(
            ex, ey, ez, dx / length, dy / length, dz / length,
            float(getattr(C, "PAINTBRUSH_RANGE", 15.0)),
        )
        if cell is None:
            return
        cue(player, int(cell[0]), int(cell[1]), int(cell[2]))
    except (AttributeError, TypeError, ValueError):
        return
