"""Retail MapSync must encode colors for solid faces exposed by excavation.

The stock PART_REMOTE decoder (vxl.pyd 0x1002A7D0) stores implicit solidity
without a color-table entry. Finalization shades existing entries; it does
not discover interior surfaces. A collision-only round trip misses holes.
"""

import asyncio
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.runtime_vxl import ServerVXL
from server.world_manager import WorldManager
from server.connection import Connection
from server.config import ServerConfig
from server.main import BattleSpadesServer
from shared.bytes import ByteReader
from shared.packet import Damage, MapSyncChunk, MapSyncEnd


FACES = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
COLOR = 0x80563412


def _world():
    # A plain solid ground slab with only its top colored. The first column
    # references z239, preventing the local-file loader's vertical shift.
    raw = bytes((0, 239, 239, 0)) + struct.pack("<I", COLOR)
    raw += (bytes((0, 60, 60, 0)) + struct.pack("<I", COLOR)) * (512 * 512 - 1)
    wm = WorldManager(SimpleNamespace(maps_path="maps", game_mode="tdm"))
    wm.map_raw_bytes = raw
    wm.map = ServerVXL(None, raw, len(raw))
    return wm


def _decode(stream, wanted):
    """Independent retail record decoder: (solid heights, explicit colors)."""
    decoded = {}
    position = 0
    while position < len(stream):
        xy = struct.unpack_from("<II", stream, position)
        assert xy not in decoded
        position += 8
        solid, colors = set(), {}
        while True:
            words, top, end, _air = stream[position:position + 4]
            count = max(0, end - top + 1)
            color_start = position + 4
            if xy in wanted:
                for i in range(count):
                    colors[top + i] = struct.unpack_from("<I", stream, color_start + 4 * i)[0]
                solid.update(range(top, end + 1))
            if words == 0:
                if xy in wanted:
                    solid.update(range(end + 1, 240))
                    decoded[xy] = (solid, colors)
                position += 4 + 4 * count
                break
            following = position + 4 * words
            bottom_count = words - count - 1
            air = stream[following + 3]
            bottom = air - bottom_count
            assert bottom >= end + 1
            if xy in wanted:
                solid.update(range(end + 1, air))
                for i in range(bottom_count):
                    colors[bottom + i] = struct.unpack_from(
                        "<I", stream, color_start + 4 * (count + i)
                    )[0]
            position = following
    return decoded


def _wire(wm, full):
    snapshot = wm.capture_map_sync(wm.dirty_columns, full=full)
    return zlib.decompress(b"".join(snapshot.build_chunks()))


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("removal", ["single", "line", "blast", "edge"])
def test_excavation_sends_all_exposed_faces_to_late_joiner(full, removal):
    wm = _world()
    if removal == "single":
        removed = {(200, 200, 80)}
        assert wm.set_block(200, 200, 80, False)
    else:
        if removal == "line":
            removed = {(x, 200, 80) for x in range(198, 211)}
        elif removal == "blast":
            removed = {(x, y, z) for x in range(198, 203)
                       for y in range(198, 203) for z in range(78, 83)}
        else:
            removed = {(0, 200, 80), (511, 200, 80), (200, 0, 80), (200, 511, 80)}
        assert set(wm.destroy_blocks(sorted(removed))) == removed
    exposed = {(x + dx, y + dy, z + dz) for x, y, z in removed
               for dx, dy, dz in FACES
               if 0 <= x + dx < 512 and 0 <= y + dy < 512
               and wm.get_solid(x + dx, y + dy, z + dz)}
    wanted = {(x, y) for x, y, _z in exposed | removed}
    client = _decode(_wire(wm, full), wanted)
    for x, y, z in removed:
        assert z not in client[(x, y)][0]
    for x, y, z in exposed:
        assert (x, y) in client, ("missing adjacent column", x, y, z)
        solid, colors = client[(x, y)]
        assert z in solid
        assert z in colors, ("solid but invisible to retail mesher", x, y, z)
        assert colors[z] == wm.get_color(x, y, z) == COLOR
    # Keep remote solid interiors compact; only newly exposed cells need
    # entries, and this operation must not change their collision or color.
    assert not wm.map.has_explicit_color(220, 220, 80)
    assert wm.map.get_color(220, 220, 80) == COLOR


def test_surface_promotion_preserves_authored_color_and_snapshot_is_immutable():
    wm = _world()
    painted = 0x80887766
    wm.set_block(201, 200, 80, True, painted)
    wm.destroy_blocks([(200, 200, 80)])
    snapshot = wm.capture_map_sync(wm.dirty_columns, full=True)
    revision = snapshot.revision
    # The next edit runs while the already captured payload is compressing.
    wm.destroy_blocks([(201, 200, 80)])
    first = _decode(zlib.decompress(b"".join(snapshot.build_chunks())), {(201, 200)})
    assert first[(201, 200)][1][80] == painted
    assert snapshot.revision == revision < wm.topology_version
    second = _decode(_wire(wm, True), {(201, 200), (202, 200)})
    assert 80 not in second[(201, 200)][0]
    assert second[(202, 200)][1][80] == COLOR


def test_inflight_join_keeps_surface_snapshot_and_journals_later_excavation():
    wm = _world()
    server = BattleSpadesServer(ServerConfig())
    server.world_manager = wm
    server._bind_world_mutation_journal()
    joiner = Connection(SimpleNamespace(address=("127.0.0.1", 40998)), server)
    joiner.player = SimpleNamespace(id=9, team=0)
    sent = []
    joiner.send = lambda data, **kwargs: sent.append(bytes(data))
    server.connections = {9: joiner}
    before, after = (200, 200, 80), (201, 200, 80)
    wm.destroy_blocks([before])
    snapshot = wm.capture_map_sync(wm.dirty_columns, full=True)
    server.mark_map_snapshot_complete(joiner)
    # This edit occurs after capture and before the immutable bytes are
    # compressed. Surface promotion is part of the same synchronous commit.
    observed = []
    wm.subscribe_mutations(lambda *change: observed.append(
        wm.map.has_explicit_color(202, 200, 80)))
    wm.destroy_blocks([after])
    assert observed == [True]
    first = _decode(zlib.decompress(b"".join(snapshot.build_chunks())),
                    {(200, 200), (201, 200)})
    assert 80 not in first[(200, 200)][0]
    assert first[(201, 200)][1][80] == COLOR
    server.replay_map_mutations(joiner)
    removals = [Damage(ByteReader(p[1:])) for p in sent if p[0] == Damage.id]
    # Retail live removal already creates face-neighbor colors. The existing
    # catch-up path must replay the later air cell exactly once, not resend
    # an earlier destruction or publish color promotion as a player build.
    assert [tuple(int(v) for v in p.position) for p in removals] == [after]
    assert all(p.chunk_check == 0 for p in removals)


@pytest.mark.parametrize("name", ["MayanJungle", "20thCenturyTown"])
@pytest.mark.parametrize("full", [False, True])
def test_real_map_fresh_join_receives_excavated_wall_colors(name, full):
    path = Path("maps") / f"{name}.vxl"
    if not path.exists():
        pytest.skip(f"{name}.vxl is not shipped")
    wm = WorldManager(SimpleNamespace(maps_path="maps", game_mode="tdm"))
    assert wm.load_map(name)
    # Select an underground cell with implicit side faces in the actual map;
    # 20thCenturyTown also exercises source z-shift 176 versus world z.
    for x in range(80, 440, 17):
        for y in range(80, 440, 19):
            z = max(wm.map.get_z(x + dx, y + dy) for dx, dy in
                    ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))) + 5
            faces = [(x + dx, y + dy, z + dz) for dx, dy, dz in FACES]
            if z < 237 and all(wm.get_solid(*p) and not wm.map.has_explicit_color(*p)
                              for p in faces):
                break
        else:
            continue
        break
    else:
        pytest.fail("real map has no implicit-interior excavation fixture")
    expected = {p: wm.get_color(*p) & 0xFFFFFF for p in faces}
    assert wm.destroy_blocks([(x, y, z)]) == [(x, y, z)]
    server = SimpleNamespace(
        world_manager=wm, players={}, connections={}, mode=None,
        config=SimpleNamespace(log_suppress_packets=set(),
                               map_sync_mode="full" if full else "auto"),
    )
    connection = Connection(SimpleNamespace(address=("127.0.0.1", 40999)), server)
    sent = []

    async def validation(_packet, timeout):
        crc = zlib.crc32(wm.map_raw_bytes) & 0xFFFFFFFF
        return SimpleNamespace(crc=crc - (1 << 32) if crc >= (1 << 31) else crc)

    connection.wait_for = validation
    connection.send = lambda data, **kwargs: sent.append(bytes(data))
    assert asyncio.run(connection.send_map_data()) is True
    assert sent[-1][0] == MapSyncEnd.id
    wire = zlib.decompress(b"".join(MapSyncChunk(ByteReader(p[1:])).data
                                     for p in sent if p[0] == MapSyncChunk.id))
    client = _decode(wire, {(a, b) for a, b, _c in faces} | {(x, y)})
    assert z not in client[(x, y)][0]
    for (a, b, c), color in expected.items():
        assert c in client[(a, b)][0]
        assert client[(a, b)][1][c] & 0xFFFFFF == color
