"""Client-side protocol-168 packet encoding/decoding for the lab clients.

Only packets listed in docs/PROTOCOL.md are produced. Client -> server
layouts follow what the server's receive path decodes (the ``runtime``
decoders in ``protocol/runtime_packets.py`` for the packets whose live layout
differs from the generated loader, the ``shared.packet`` loaders otherwise).
"""

from __future__ import annotations

import math
import struct

import shared.packet as P
from shared.bytes import ByteReader
from server.util import lzf_decompress

# Packet ids the lab clients send.
CLOCK_SYNC = 0
CLIENT_DATA = 4
SHOOT = 6
ORIENTED_ITEM = 10
SET_COLOR = 11
SET_CLASS_LOADOUT = 13
NEW_PLAYER = 15
BUILD_PREFAB = 30
BLOCK_LINE = 40
WEAPON_RELOAD = 76
CHANGE_TEAM = 77
CHANGE_CLASS = 78
USE_COMMAND = 86
PLACE_MG = 87
PLACE_ROCKET_TURRET = 88
PLACE_LANDMINE = 89
PLACE_MEDPACK = 90
PLACE_RADAR = 91
PLACE_C4 = 92
DETONATE_C4 = 93
DISGUISE = 95
PLACE_FLARE = 104
STEAM_TICKET = 105
CLIENT_IN_MENU = 110
MAP_VALIDATION = 60
PLACE_DYNAMITE = 1

# Only these two leave the stock client unsequenced (gameScene.pyd
# send_client_data / send_clock_sync); every other packet is reliable.
UNSEQUENCED_IDS = frozenset({CLOCK_SYNC, CLIENT_DATA})


def _fixed_orientation(value: float) -> int:
    """Sign-magnitude 16-bit orientation component (see runtime_packets)."""

    value = float(value)
    magnitude = abs(value)
    if magnitude < 1.0:
        raw = int(round(magnitude * 8192.0))
        raw = min(raw, 8191) if magnitude < 1.0 and raw >= 8192 else raw
    else:
        raw = int(round(16384.0 + (magnitude - 1.0) * 8192.0))
    raw = max(0, min(0x7FFF, raw))
    if value < 0.0:
        raw |= 0x8000
    return raw


def decode_orientation_component(raw: int) -> float:
    sign = -1.0 if (raw & 0x8000) else 1.0
    magnitude = raw & 0x7FFF
    if magnitude >= 16384:
        return sign * ((magnitude - 8192) / 8192.0)
    return sign * (magnitude / 8192.0)


def quantized_orientation(orientation) -> tuple:
    """The aim exactly as the server decodes it from ClientData."""

    return tuple(
        decode_orientation_component(_fixed_orientation(component))
        for component in orientation
    )


def _fixed(value: float) -> int:
    """Signed 1/64 fixed point as a sign-magnitude short."""

    value = float(value)
    raw = min(0x7FFF, int(round(abs(value) * 64.0)))
    return raw | 0x8000 if value < 0.0 else raw


def client_data(
    *, loop: int, player_id: int, tool: int, orientation, flags: int,
    actions: int, palette: bool = False, yaw: float = 0.0,
) -> bytes:
    ox, oy, oz = (_fixed_orientation(c) for c in orientation)
    return struct.pack(
        "<BiBBHHHBBBH",
        CLIENT_DATA,
        int(loop),
        (int(player_id) & 0x7F) | (0x80 if palette else 0),
        int(tool) & 0xFF,
        ox, oy, oz,
        0,
        int(flags) & 0xFF,
        int(actions) & 0xFF,
        _fixed(yaw),
    )


def pack_flags(bits) -> int:
    return sum((1 << index) for index, bit in enumerate(bits) if bit)


def clock_sync(client_time_ms: int) -> bytes:
    packet = P.ClockSync()
    packet.client_time = int(client_time_ms)
    packet.server_loop_count = 0
    return bytes(packet.generate())


def shoot(
    *, loop: int, player_id: int, world_loop: int, origin, direction,
    damage: int = 0, penetration: int = 2, secondary: bool = False,
    seed: int = 0,
) -> bytes:
    packet = P.ShootPacket()
    packet.loop_count = int(loop)
    packet.shooter_id = int(player_id)
    packet.shot_on_world_update = int(world_loop)
    packet.x, packet.y, packet.z = (float(v) for v in origin)
    packet.ori_x, packet.ori_y, packet.ori_z = (float(v) for v in direction)
    packet.damage = int(damage)
    packet.penetration = int(penetration)
    packet.affect_shooter = 0
    packet.secondary = 1 if secondary else 0
    packet.seed = int(seed) & 0xFF
    return bytes(packet.generate())


def oriented_item(*, loop, player_id, tool, fuse, position, velocity) -> bytes:
    packet = P.UseOrientedItem()
    packet.loop_count = int(loop)
    packet.player_id = int(player_id)
    packet.tool = int(tool)
    packet.value = float(fuse)
    packet.position = tuple(float(v) for v in position)
    packet.velocity = tuple(float(v) for v in velocity)
    return bytes(packet.generate())


def block_line(*, loop, player_id, start, end) -> bytes:
    packet = P.BlockLine()
    packet.loop_count = int(loop)
    packet.player_id = int(player_id)
    packet.x1, packet.y1, packet.z1 = (int(v) for v in start)
    packet.x2, packet.y2, packet.z2 = (int(v) for v in end)
    return bytes(packet.generate())


def weapon_reload(*, player_id, tool, done=False) -> bytes:
    packet = P.WeaponReload()
    packet.player_id = int(player_id)
    packet.tool_id = int(tool)
    packet.is_done = 1 if done else 0
    return bytes(packet.generate())


def set_color(*, player_id, value) -> bytes:
    packet = P.SetColor()
    packet.player_id = int(player_id)
    packet.value = int(value) & 0xFFFFFF
    return bytes(packet.generate())


def change_class(*, player_id, class_id) -> bytes:
    packet = P.ChangeClass()
    packet.player_id = int(player_id)
    packet.class_id = int(class_id)
    return bytes(packet.generate())


def change_team(*, player_id, team) -> bytes:
    packet = P.ChangeTeam()
    packet.player_id = int(player_id)
    packet.team = int(team)
    return bytes(packet.generate())


def set_class_loadout(
    *, player_id, class_id, loadout, prefabs=(), ugc_tools=(), instant=0,
) -> bytes:
    body = bytearray([SET_CLASS_LOADOUT, int(player_id) & 0xFF,
                      int(class_id) & 0xFF, int(instant) & 0xFF,
                      len(loadout)])
    body.extend(int(tool) & 0xFF for tool in loadout)
    body.append(len(prefabs))
    for name in prefabs:
        body.extend(str(name).encode("utf-8") + b"\x00")
    body.append(len(ugc_tools))
    body.extend(int(tool) & 0xFF for tool in ugc_tools)
    return bytes(body)


def new_player(*, name, team, class_id, language=0) -> bytes:
    packet = P.NewPlayerConnection()
    packet.team = int(team)
    packet.class_id = int(class_id)
    packet.forced_team = 0
    packet.local_language = int(language)
    packet.name = str(name)
    return bytes(packet.generate())


def steam_ticket() -> bytes:
    """The ticket-less legacy hello (an empty ticket, no XOR key)."""

    packet = P.SteamSessionTicket()
    packet.ticket = b""
    packet.ticket_size = 0
    return bytes(packet.generate())


def map_validation(crc: int) -> bytes:
    packet = P.MapDataValidation()
    crc = int(crc) & 0xFFFFFFFF
    packet.crc = crc - (1 << 32) if crc >= (1 << 31) else crc
    return bytes(packet.generate())


def client_in_menu(in_menu: bool) -> bytes:
    packet = P.ClientInMenu()
    packet.in_menu = 1 if in_menu else 0
    return bytes(packet.generate())


def build_prefab(
    *, loop, player_id, name, position, yaw=0, color=0x707070,
    from_index=0, to_index=0,
) -> bytes:
    packet = P.BuildPrefabAction()
    packet.loop_count = int(loop)
    packet.player_id = int(player_id)
    packet.prefab_name = str(name)
    packet.prefab_yaw = int(yaw)
    packet.prefab_pitch = 0
    packet.prefab_roll = 0
    packet.from_block_index = int(from_index)
    packet.to_block_index = int(to_index)
    packet.position = tuple(int(v) for v in position)
    packet.color = int(color) & 0xFFFFFF
    packet.add_to_user_blocks = 1
    return bytes(packet.generate())


def place_entity(packet_id: int, *, loop, cell, player_id=None, face=None,
                 yaw=None) -> bytes:
    """Deployable placement in the raw-voxel retail layout."""

    body = bytearray([int(packet_id)])
    body.extend(struct.pack("<i", int(loop)))
    if packet_id in (PLACE_MG, PLACE_ROCKET_TURRET, PLACE_LANDMINE,
                     PLACE_MEDPACK, PLACE_RADAR):
        body.append(int(player_id or 0) & 0xFF)
    body.extend(struct.pack("<HHH", *(int(v) & 0xFFFF for v in cell)))
    if packet_id in (PLACE_DYNAMITE, PLACE_MEDPACK, PLACE_C4):
        body.append(int(face or 0) & 0xFF)
    if packet_id in (PLACE_MG, PLACE_ROCKET_TURRET):
        body.extend(struct.pack("<H", _fixed(float(yaw or 0.0))))
    return bytes(body)


def place_flare(*, loop, cell) -> bytes:
    return bytes([PLACE_FLARE]) + struct.pack(
        "<iHHH", int(loop), *(int(v) & 0xFFFF for v in cell)
    )


def simple(packet_class, **fields) -> bytes:
    packet = packet_class()
    for name, value in fields.items():
        setattr(packet, name, value)
    return bytes(packet.generate())


# -- server -> client -------------------------------------------------------


def unwrap(datagram: bytes) -> bytes:
    """Strip the server's prefix byte and its chunk framing."""

    if len(datagram) < 2:
        return b""
    return bytes(lzf_decompress(datagram[1:]))


def wrap(packet: bytes) -> bytes:
    """Client -> server framing: prefix 0x30, raw body."""

    return b"\x30" + bytes(packet)


_LOADERS = {
    0: P.ClockSync,
    2: P.WorldUpdate,
    5: P.SetHP,
    28: P.CreatePlayer,
    37: P.Damage,
    45: P.StateData,
    46: P.KillAction,
    60: P.MapDataValidation,
    69: P.Restock,
    114: P.InitialInfo,
}


def decode(packet: bytes):
    """``(packet_id, parsed or None)`` for the packets the client acts on."""

    if not packet:
        return None, None
    packet_id = packet[0]
    loader = _LOADERS.get(packet_id)
    if loader is None:
        return packet_id, None
    try:
        parsed = loader()
        parsed.read(ByteReader(packet[1:]))
    except Exception:  # noqa: BLE001 - an undecodable packet is just ignored
        return packet_id, None
    return packet_id, parsed


def normalize(vector) -> tuple:
    x, y, z = (float(v) for v in vector)
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-9:
        return (1.0, 0.0, 0.0)
    return (x / length, y / length, z / length)
