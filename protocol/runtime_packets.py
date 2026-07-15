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
    """Decode the original AoS orientation type (sign-magnitude 16-bit).

    Encoding is piecewise (verified empirically against shared.packet.pyd):
        |v| < 1.0:  mag = round(|v| * 8192)            -> mag in [0, 8192)
        |v| >= 1.0: mag = round(16384 + (|v|-1)*8192)  -> mag in [16384, 24576]
    The gap [8192, 16384) is never emitted by the encoder. For decoding,
    we treat mag >= 16384 as the extended-range branch.
    """
    sign = -1.0 if (value & 0x8000) else 1.0
    magnitude = value & 0x7FFF
    if magnitude >= 16384:
        return sign * ((magnitude - 8192) / 8192.0)
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


@dataclass(slots=True)
class RuntimePlaceFlareBlock:
    """Live packet-104 layout.

    Unlike most Place* packets, the retail client sends the three voxel
    coordinates as raw unsigned shorts.  The generated PlaceFlareBlock loader
    currently applies ``fromfixed`` and therefore divides each coordinate by
    64; keeping this compatibility decoder local avoids changing the layouts
    of unrelated deployment packets.
    """

    loop_count: int
    x: int
    y: int
    z: int


@dataclass(slots=True)
class RuntimePlaceEntity:
    """Retail layout shared by deployable placement packets.

    The stock client writes world coordinates as literal voxel ``uint16``
    values.  Only a turret's yaw remains a signed fixed-point short.  Optional
    fields mirror the small layout differences between the packet ids.
    """

    loop_count: int
    x: int
    y: int
    z: int
    player_id: Optional[int] = None
    face: Optional[int] = None
    yaw: float = 0.0
    ugc_item_id: Optional[int] = None
    placing: Optional[int] = None


@dataclass(slots=True)
class RuntimeSetClassLoadout:
    """Tolerant packet-13 view used for stock-client selection messages.

    Some retail builds omit the final zero UGC-count byte.  The generated
    Cython loader reads that byte unconditionally, which emits a synchronous
    ``NoDataLeft`` traceback on the simulation thread.  Treat the missing
    optional tail as an empty UGC list while keeping every preceding field
    strictly bounded.
    """

    player_id: int
    class_id: int
    instant: int
    loadout: list[int]
    prefabs: list[str]
    ugc_tools: list[int]


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
        # ClientData action-flag layout (client SEND side, MEASURED from the
        # compiled client's ClientData.read, 2026-07-07): 0x04=zoom,
        # 0x40=is_weapon_deployed, 0x80=hover. NOTE this is DIFFERENT from the
        # WorldUpdate action byte the client APPLIES as remote state
        # (0x04=jetpack, 0x40=zoom, 0x80=weapon_deployed) — the client itself
        # remaps send-vs-display, so server/player.py pack_action_flags uses
        # the display layout while this parse uses the send layout.
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


def decode_place_flare_block_payload(payload: bytes) -> RuntimePlaceFlareBlock:
    if len(payload) != 10:
        raise ValueError(f"PlaceFlareBlock payload must be 10 bytes: {len(payload)}")
    reader = ByteReader(payload)
    return RuntimePlaceFlareBlock(
        loop_count=int(reader.read_int()),
        x=_read_uint16(reader),
        y=_read_uint16(reader),
        z=_read_uint16(reader),
    )


def decode_place_entity_payload(packet_id: int, payload: bytes) -> RuntimePlaceEntity:
    """Decode the raw-voxel retail layouts for packets 1 and 87..92/97."""
    layouts = {
        1: (11, False, True, False, False),   # PlaceDynamite
        87: (13, True, False, True, False),   # PlaceMG
        88: (13, True, False, True, False),   # PlaceRocketTurret
        89: (11, True, False, False, False),  # PlaceLandmine
        90: (12, True, True, False, False),   # PlaceMedPack
        91: (11, True, False, False, False),  # PlaceRadarStation
        92: (11, False, True, False, False),  # PlaceC4
        97: (12, False, False, False, True),  # PlaceUGC
    }
    try:
        expected_size, has_player, has_face, has_yaw, has_ugc = layouts[packet_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported deployable packet id: {packet_id}") from exc
    if len(payload) != expected_size:
        raise ValueError(
            f"Place packet {packet_id} payload must be {expected_size} bytes: "
            f"{len(payload)}"
        )

    reader = ByteReader(payload)
    loop_count = int(reader.read_int())
    player_id = int(reader.read_byte()) if has_player else None
    x = _read_uint16(reader)
    y = _read_uint16(reader)
    z = _read_uint16(reader)
    face = int(reader.read_byte()) if has_face else None
    yaw = _fromfixed(_read_uint16(reader)) if has_yaw else 0.0
    ugc_item_id = int(reader.read_byte()) if has_ugc else None
    placing = int(reader.read_byte()) if has_ugc else None
    return RuntimePlaceEntity(
        loop_count=loop_count,
        player_id=player_id,
        x=x,
        y=y,
        z=z,
        face=face,
        yaw=yaw,
        ugc_item_id=ugc_item_id,
        placing=placing,
    )


def decode_set_class_loadout_payload(payload: bytes) -> RuntimeSetClassLoadout:
    """Decode packet 13, accepting only its observed optional empty tail."""

    if len(payload) < 4:
        raise ValueError(f"SetClassLoadout payload too short: {len(payload)}")
    position = 0

    def read_byte(field: str) -> int:
        nonlocal position
        if position >= len(payload):
            raise ValueError(f"SetClassLoadout missing {field}")
        value = int(payload[position])
        position += 1
        return value

    player_id = read_byte("player_id")
    class_id = read_byte("class_id")
    instant = read_byte("instant")
    loadout_count = read_byte("loadout_count")
    if loadout_count > 64 or position + loadout_count > len(payload):
        raise ValueError("SetClassLoadout invalid loadout_count")
    loadout = [int(value) for value in payload[position:position + loadout_count]]
    position += loadout_count

    prefab_count = read_byte("prefab_count")
    if prefab_count > 64:
        raise ValueError("SetClassLoadout invalid prefab_count")
    prefabs: list[str] = []
    for _ in range(prefab_count):
        terminator = payload.find(b"\x00", position)
        if terminator < 0:
            raise ValueError("SetClassLoadout unterminated prefab")
        prefabs.append(payload[position:terminator].decode("utf-8", "replace"))
        position = terminator + 1

    # Retail may omit this byte when there are no UGC tools.
    ugc_count = read_byte("ugc_count") if position < len(payload) else 0
    if ugc_count > 64 or position + ugc_count != len(payload):
        raise ValueError("SetClassLoadout invalid ugc_count")
    ugc_tools = [int(value) for value in payload[position:position + ugc_count]]
    return RuntimeSetClassLoadout(
        player_id=player_id,
        class_id=class_id,
        instant=instant,
        loadout=loadout,
        prefabs=prefabs,
        ugc_tools=ugc_tools,
    )


def decode_runtime_packet(packet_id: int, payload: bytes) -> Optional[object]:
    """Decode only the packet types that need live runtime compatibility."""
    if packet_id == 4:
        return decode_client_data_payload(payload)
    if packet_id == 13:
        return decode_set_class_loadout_payload(payload)
    if packet_id == 116:
        return decode_position_data_payload(payload)
    if packet_id == 104:
        return decode_place_flare_block_payload(payload)
    if packet_id in (1, 87, 88, 89, 90, 91, 92, 97):
        return decode_place_entity_payload(packet_id, payload)
    return None
