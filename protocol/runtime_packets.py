"""Runtime decoders for packet variants seen from live clients.

These helpers let the Python server accept the packet layouts we have observed
in working clients without depending on a rebuilt Cython module for every
receive-path tweak.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from shared.bytes import ByteReader


def _read_uint16(reader: ByteReader) -> int:
    # Some existing compiled shared.bytes builds expose only read_short().
    return int(reader.read_short()) & 0xFFFF


def _fromfixed(value: int) -> float:
    sign = -1.0 if (value & 0x8000) else 1.0
    magnitude = value & 0x7FFF
    return sign * (magnitude / 64.0)


def _fromfixed_orientation(value: int) -> float:
    sign = -1.0 if (value & 0x8000) else 1.0
    magnitude = value & 0x7FFF
    return sign * (magnitude / 8192.0)


@dataclass(slots=True)
class RuntimeClientData:
    loop_count: int
    player_id: int
    tool_id: int
    o_x: float
    o_y: float
    o_z: float
    ooo: int
    weapon_deployment_yaw: float
    input_flags: int
    action_flags: int
    up: bool
    down: bool
    left: bool
    right: bool
    jump: bool
    crouch: bool
    sneak: bool
    sprint: bool
    primary: bool
    secondary: bool
    zoom: bool
    can_pickup: bool
    can_display_weapon: bool
    is_on_fire: bool
    is_weapon_deployed: bool
    hover: bool
    palette_enabled: bool


@dataclass(slots=True)
class RuntimePositionData:
    x: float
    y: float
    z: float


def decode_client_data_payload(payload: bytes) -> RuntimeClientData:
    """Decode ClientData from either known live-client payload layout."""
    if len(payload) < 15:
        raise ValueError(f"ClientData payload too short: {len(payload)}")

    reader = ByteReader(payload)
    loop_count = int(reader.read_int())
    raw_player_id = int(reader.read_byte())
    tool_id = int(reader.read_byte())
    o_x = _fromfixed_orientation(_read_uint16(reader))
    o_y = _fromfixed_orientation(_read_uint16(reader))
    o_z = _fromfixed_orientation(_read_uint16(reader))
    ooo = int(reader.read_byte())

    input_flags = int(reader.read_byte())
    action_flags = int(reader.read_byte())

    remaining = len(payload) - 15
    if remaining >= 4:
        weapon_deployment_yaw = float(reader.read_float())
    elif remaining >= 2:
        weapon_deployment_yaw = _fromfixed(_read_uint16(reader))
    else:
        weapon_deployment_yaw = 0.0

    palette_enabled = bool(raw_player_id & 0x80)
    player_id = raw_player_id & 0x7F

    return RuntimeClientData(
        loop_count=loop_count,
        player_id=player_id,
        tool_id=tool_id,
        o_x=o_x,
        o_y=o_y,
        o_z=o_z,
        ooo=ooo,
        weapon_deployment_yaw=weapon_deployment_yaw,
        input_flags=input_flags,
        action_flags=action_flags,
        up=bool(input_flags & 0x01),
        down=bool(input_flags & 0x02),
        left=bool(input_flags & 0x04),
        right=bool(input_flags & 0x08),
        jump=bool(input_flags & 0x10),
        crouch=bool(input_flags & 0x20),
        sneak=bool(input_flags & 0x40),
        sprint=bool(input_flags & 0x80),
        primary=bool(action_flags & 0x01),
        secondary=bool(action_flags & 0x02),
        zoom=bool(action_flags & 0x04),
        can_pickup=bool(action_flags & 0x08),
        can_display_weapon=bool(action_flags & 0x10),
        is_on_fire=bool(action_flags & 0x20),
        is_weapon_deployed=bool(action_flags & 0x40),
        hover=bool(action_flags & 0x80),
        palette_enabled=palette_enabled,
    )


def decode_position_data_payload(payload: bytes) -> RuntimePositionData:
    """Decode PositionData from either raw-float or fixed-point payloads."""
    reader = ByteReader(payload)
    if len(payload) >= 12:
        return RuntimePositionData(
            x=float(reader.read_float()),
            y=float(reader.read_float()),
            z=float(reader.read_float()),
        )
    if len(payload) >= 6:
        return RuntimePositionData(
            x=_fromfixed(_read_uint16(reader)),
            y=_fromfixed(_read_uint16(reader)),
            z=_fromfixed(_read_uint16(reader)),
        )
    raise ValueError(f"PositionData payload too short: {len(payload)}")


def decode_runtime_packet(packet_id: int, payload: bytes) -> Optional[object]:
    """Decode only the packet types that need live runtime compatibility."""
    if packet_id == 4:
        return decode_client_data_payload(payload)
    if packet_id == 116:
        return decode_position_data_payload(payload)
    return None
