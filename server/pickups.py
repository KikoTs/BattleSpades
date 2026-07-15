"""Authoritative objective-pickup carry/drop helpers."""
from __future__ import annotations

import math

import shared.constants as C
from shared.packet import DropPickup, PickPickup


PICKUP_TYPES = frozenset((
    int(C.BOMB_PICKUP), int(C.DIAMOND_PICKUP), int(C.INTEL_PICKUP),
))
THROW_SPEEDS = {
    int(C.BOMB_PICKUP): float(C.BOMB_THROW_SPEED),
    int(C.DIAMOND_PICKUP): float(C.DIAMOND_THROW_SPEED),
    int(C.INTEL_PICKUP): float(C.INTEL_THROW_SPEED),
}


def broadcast_pickup(server, player, pickup_id: int, *, burdensome: bool,
                     state: int) -> bool:
    pickup_id = int(pickup_id)
    if pickup_id not in PICKUP_TYPES or getattr(player, "pickup_id", None) is not None:
        return False
    player.pickup_id = pickup_id
    player.pickup_burdensome = bool(burdensome)
    player.pickup_state = int(state)
    _set_native_burden(player)
    packet = PickPickup()
    packet.player_id = int(player.id)
    packet.pickup_id = pickup_id
    packet.burdensome = int(bool(burdensome))
    server.broadcast(bytes(packet.generate()), reliable=True)
    return True


def broadcast_drop(server, player, position, velocity):
    pickup_id = getattr(player, "pickup_id", None)
    if pickup_id not in PICKUP_TYPES:
        return None
    position, velocity = sanitize_drop(player, pickup_id, position, velocity)
    if position is None:
        return None
    packet = DropPickup()
    packet.loop_count = int(getattr(server, "loop_count", 0))
    packet.player_id = int(player.id)
    packet.pickup_id = int(pickup_id)
    packet.position = position
    packet.velocity = velocity
    server.broadcast(bytes(packet.generate()), reliable=True)
    state_value = getattr(player, "pickup_state", None)
    state = int(state_value) if state_value is not None else 0
    player.pickup_id = None
    player.pickup_burdensome = False
    player.pickup_state = None
    _set_native_burden(player)
    return int(pickup_id), state, position, velocity


def sanitize_drop(player, pickup_id: int, position, velocity):
    """Validate client vectors and cap velocity to the recovered tool speed."""
    try:
        pos = tuple(float(value) for value in position)
        vel = tuple(float(value) for value in velocity)
    except (TypeError, ValueError):
        return None, None
    if len(pos) != 3 or len(vel) != 3 or any(
        not math.isfinite(value) for value in pos + vel
    ):
        return None, None
    current = (float(player.x), float(player.y), float(player.z))
    max_position_error = float(C.PICKUP_DISTANCE) + 2.0
    if sum((pos[i] - current[i]) ** 2 for i in range(3)) > max_position_error ** 2:
        return None, None
    speed = math.sqrt(sum(value * value for value in vel))
    max_speed = THROW_SPEEDS[int(pickup_id)]
    if speed > max_speed and speed > 1e-6:
        scale = max_speed / speed
        vel = tuple(value * scale for value in vel)
    return pos, vel


def _set_native_burden(player) -> None:
    world_object = getattr(player, "_world_object", None)
    if world_object is not None:
        world_object.burdened = bool(getattr(player, "pickup_burdensome", False))
