"""map_sync_mode = "auto": the retail fast join, at protocol level.

Retail client (network.pyd GameClient, vxl.pyd loader thread):

* InitialInfo: open the local ``<filename>.vxl``; MapDataValidation carries
  ``zlib.crc32`` of the raw file, 0 when there is no such file.
* The server's MapDataValidation: load that file as the world base
  (THREAD_STATE_FULL_LOCAL = 1). Columns are read in file order, z is moved
  down by ``max(0, 239 - highest z in the file)`` and the solid below the last
  span is filled up to that highest z.
* MapSync: ``(u32 x, u32 y, column)`` records replace whole columns
  (THREAD_STATE_PART_REMOTE = 2), no z move, filled to 239; then the world is
  finalized.

The loader below is that code in Python. The tests rebuild the client's
world from "local file + delta" and compare it with the server's.
"""
from __future__ import annotations

import asyncio
import random
import struct
import textwrap
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.bytes import ByteReader
from shared.packet import MapDataValidation, MapSyncChunk, MapSyncEnd, MapSyncStart
from server.config import load_config
from server.connection import Connection
from server.world_manager import WorldManager

MAPS = Path("maps")
HEIGHT = 240


class Peer:
    address = ("127.0.0.1", 40200)

    def disconnect(self, reason=0):
        return None


def _world(name):
    path = MAPS / f"{name}.vxl"
    if not path.exists():
        pytest.skip(f"{name}.vxl is not shipped")
    wm = WorldManager(SimpleNamespace(maps_path=str(MAPS), game_mode="tdm"))
    assert wm.load_map(name)
    return wm, path.read_bytes()


def _server(wm, mode="auto", game_mode=None):
    return SimpleNamespace(
        world_manager=wm,
        config=SimpleNamespace(log_suppress_packets=set(), map_sync_mode=mode),
        players={}, connections={}, mode=game_mode,
    )


def _wire_crc(raw: bytes) -> int:
    crc = zlib.crc32(raw) & 0xFFFFFFFF
    return crc - (1 << 32) if crc >= (1 << 31) else crc


def _join(server, client_crc):
    """Run send_map_data for a client that answered ``client_crc``."""
    connection = Connection(Peer(), server)
    sent = []

    async def answered(packet_class, timeout=5.0):
        packet = packet_class()
        packet.crc = client_crc
        return packet

    connection.wait_for = answered
    connection.send = lambda data, reliable=True, prefix=0x30: sent.append(
        (prefix, bytes(data))
    )
    assert asyncio.run(connection.send_map_data()) is True
    ids = [data[0] for _prefix, data in sent]
    assert ids[0] == MapDataValidation.id
    assert ids[1] == MapSyncStart.id and ids[-1] == MapSyncEnd.id
    assert set(ids[2:-1]) <= {MapSyncChunk.id}
    # The three priorities the retail network thread reads from the prefix.
    assert [prefix for prefix, _ in sent[:2]] == [0x31, 0x32]
    stream = b"".join(
        MapSyncChunk(ByteReader(data[1:])).data
        for _prefix, data in sent if data[0] == MapSyncChunk.id
    )
    reply = MapDataValidation(ByteReader(sent[0][1][1:])).crc
    return reply, (zlib.decompress(stream) if stream else b"")


# -- the retail loader, in Python ---------------------------------------


def _column_end(data: bytes, pos: int) -> int:
    while True:
        words, top_start, top_end = data[pos], data[pos + 1], data[pos + 2]
        if words == 0:
            return pos + 4 + 4 * max(0, top_end - top_start + 1)
        pos += 4 * words


def _file_columns(raw: bytes):
    """``{(x, y): span bytes}`` and the highest z named by any span header."""
    columns, highest, pos = {}, 0, 0
    for y in range(512):
        for x in range(512):
            end = _column_end(raw, pos)
            columns[(x, y)] = raw[pos:end]
            scan = pos
            while True:
                highest = max(highest, raw[scan + 1], raw[scan + 2], raw[scan + 3])
                if raw[scan] == 0:
                    break
                scan += 4 * raw[scan]
            pos = end
    assert pos == len(raw)
    return columns, highest


def _records(stream: bytes):
    records, pos = {}, 0
    while pos < len(stream):
        x, y = struct.unpack_from("<II", stream, pos)
        end = _column_end(stream, pos + 8)
        assert (x, y) not in records
        records[(x, y)] = stream[pos + 8:end]
        pos = end
    return records


def _solids(column: bytes, *, z_move: int, fill_to: int) -> set[int]:
    """One column as the retail loader builds it (vxl.pyd 0x1002A440/7D0)."""
    solid, pos = set(), 0
    while True:
        words, top_start, top_end = column[pos], column[pos + 1], column[pos + 2]
        solid.update(range(top_start + z_move, top_end + 1 + z_move))
        top_len = top_end - top_start + 1
        if words == 0:
            solid.update(range(top_end + 1 + z_move, fill_to + z_move))
            return solid
        following = pos + 4 * words
        air_start = column[following + 3]
        bottom_start = air_start - words + top_len + 1
        solid.update(range(top_end + 1 + z_move, air_start + z_move))
        assert bottom_start <= air_start
        pos = following


def _client_column(position, file_columns, highest, delta):
    if position in delta:
        return _solids(delta[position], z_move=0, fill_to=HEIGHT)
    return _solids(
        file_columns[position], z_move=max(0, 239 - highest), fill_to=highest
    )


def _server_column(wm, position):
    x, y = position
    return {z for z in range(HEIGHT) if wm.get_solid(x, y, z)}


def _assert_same_world(wm, raw, delta, positions):
    file_columns, highest = _file_columns(raw)
    # The loader drops exposed chroma-key markers afterwards, on both sides.
    markers = {(x, y) for x, y, _z in getattr(wm.map, "retail_marker_positions", ())}
    checked = 0
    for position in positions:
        if position in markers:
            continue
        client = _client_column(position, file_columns, highest, delta)
        server = _server_column(wm, position)
        # z 239 is the floor both physics treat as z 238; the file loader
        # leaves it to the span list, the sync loader fills it.
        assert client - {239} == server - {239}, position
        checked += 1
    return checked


def _edit(wm, seed=7, count=60):
    rng = random.Random(seed)
    touched = set()
    for _ in range(count):
        x, y = rng.randrange(40, 470), rng.randrange(40, 470)
        surface = int(wm.map.get_z(x, y))
        # Gameplay never removes the base plane (z 239): stay above it.
        if not 8 <= surface <= 234:
            continue
        kind = rng.randrange(4)
        changed = False
        if kind == 0:      # build a pillar on the surface
            for z in range(surface - 1, max(1, surface - 5), -1):
                changed |= bool(wm.set_block(x, y, z, True, 0x336699))
        elif kind == 1:    # dig a shaft
            changed = bool(wm.destroy_blocks(
                [(x, y, z) for z in range(surface, surface + 4)]
            ))
        elif kind == 2:    # a floating block: a second span
            changed = bool(wm.set_block(x, y, surface - 8, True, 0x993311))
        else:              # tunnel under the surface
            changed = bool(wm.destroy_blocks(
                [(x, y, surface + 2), (x, y, surface + 3)]
            ))
        if changed:
            touched.add((x, y))
    assert len(touched) > 20
    return touched


# -- tests ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["MayanJungle", "20thCenturyTown"])
def test_fresh_map_only_needs_finalized_marker_columns(name):
    wm, raw = _world(name)
    reply, stream = _join(_server(wm), _wire_crc(raw))

    assert reply == _wire_crc(raw)
    delta = _records(stream)
    marker_columns = {(x, y) for x, y, _z in wm.map.retail_marker_positions}
    assert set(delta) == marker_columns
    rng = random.Random(1)
    sample = [(rng.randrange(512), rng.randrange(512)) for _ in range(1500)]
    assert _assert_same_world(wm, raw, delta, sample + sorted(marker_columns)) > 1000


@pytest.mark.parametrize("name", ["MayanJungle", "20thCenturyTown"])
def test_local_file_plus_delta_is_the_server_world(name):
    wm, raw = _world(name)
    touched = _edit(wm)

    _reply, stream = _join(_server(wm), _wire_crc(raw))
    delta = _records(stream)

    marker_columns = {(x, y) for x, y, _z in wm.map.retail_marker_positions}
    assert set(delta) == set(wm.dirty_columns) | marker_columns
    assert touched <= set(delta)
    rng = random.Random(2)
    neighbours = {
        (x + dx, y + dy) for x, y in touched for dx in (-1, 0, 1) for dy in (-1, 0, 1)
    }
    sample = {(rng.randrange(512), rng.randrange(512)) for _ in range(1500)}
    checked = _assert_same_world(wm, raw, delta, sorted(neighbours | sample))
    assert checked > 1000


def test_every_changed_column_is_in_the_delta():
    wm, raw = _world("MayanJungle")
    _edit(wm, seed=11, count=120)
    file_columns, highest = _file_columns(raw)
    assert max(0, 239 - highest) == int(wm.map.source_z_shift)
    markers = {(x, y) for x, y, _z in wm.map.retail_marker_positions}

    changed = set()
    for (x, y), column in file_columns.items():
        if not (40 <= x < 470 and 40 <= y < 470) or (x, y) in markers:
            continue
        if (x * 7 + y * 13) % 5 and (x, y) not in wm.dirty_columns:
            continue  # every dirty column, one in five of the others
        base = _solids(column, z_move=int(wm.map.source_z_shift), fill_to=highest)
        if base - {239} != _server_column(wm, (x, y)) - {239}:
            changed.add((x, y))

    assert changed
    assert changed <= set(wm.dirty_columns)


def test_mismatch_gets_the_whole_map():
    wm, raw = _world("MayanJungle")
    wm.set_block(150, 250, int(wm.map.get_z(150, 250)) - 1, True, 0x112233)

    _reply, stream = _join(_server(wm), _wire_crc(raw) ^ 0x55)

    records = _records(stream)
    assert len(records) == 512 * 512
    assert records[(150, 250)] == bytes(wm.map.serialize_columns([(150, 250)]))[8:]


def test_full_mode_ignores_a_matching_crc():
    wm, raw = _world("MayanJungle")

    _reply, stream = _join(_server(wm, mode="full"), _wire_crc(raw))

    assert len(_records(stream)) == 512 * 512


def test_a_client_without_the_file_never_matches():
    """CRC 0 = "I have no such file"; a map without a file CRC is 0 too."""
    wm, raw = _world("MayanJungle")
    wm.map_file_crc = 0

    _reply, stream = _join(_server(wm), 0)

    assert len(_records(stream)) == 512 * 512


def test_map_creator_keeps_the_full_stream():
    wm, raw = _world("MayanJungle")
    editor = SimpleNamespace(send_pre_validation_map_data=lambda connection: None)

    _reply, stream = _join(_server(wm, game_mode=editor), _wire_crc(raw))

    assert len(_records(stream)) == 512 * 512


def test_config_accepts_auto_and_full_only(tmp_path):
    path = tmp_path / "config.toml"
    for written, read in (("auto", "auto"), ("FULL", "full"), ("delta", "full")):
        path.write_text(textwrap.dedent(f"""
            [game]
            map_sync_mode = "{written}"
        """), encoding="utf-8")
        assert load_config(path).map_sync_mode == read
