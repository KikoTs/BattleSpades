"""Late joiners see the exact damage shade live clients show.

Ground truth (stock client, headless IDA 2026-09-26):

* ``shared.common.dim(v, d)`` = ``max(0, v - ((v * int(round(d))) >> 3))``
  (Python 2 ``round``: halves away from zero).
* ``BlockManager.add_damage(x, y, z, d)`` reads the voxel's CURRENT colour
  (a black one is first re-coloured by the native ``map.color_block``),
  creates ``DamagedBlock(get_initial_health, colour)`` on the first hit
  (breaking instead when ``not health > d``), subtracts ``d`` and, if the
  block survives, sets the colour to ``dim`` of the current colour per
  channel: the shade compounds hit by hit.
* ``receive_block_manager_state`` damaged row: ``DamagedBlock(health,
  original)`` and colour = ``dim(original, get_initial_health - health)``:
  the total damage applied once.
* ``color_block`` (PaintBlock 7) only sets the voxel colour.

The client model below implements exactly that; every scenario compares a
client that saw the hits live against a joiner fed the server's reveal.
"""

from __future__ import annotations

import struct

import pytest

from server import block_damage_model as model
from server.combat_runtime import USER_BLOCK_HEALTH
from server.config import ServerConfig
from server.game_constants import DEFAULT_BLOCK_HEALTH
from server.main import BattleSpadesServer
from server.prefab_actions import PrefabActionService
from server.runtime_vxl import ServerVXL
from shared.bytes import ByteReader
from shared.packet import Damage, PaintBlockPacket

BASE = 0x406080


def _client_colour(cell) -> int:
    """Stand-in for colours only the client knows (implicit interior voxels,
    the native ``color_block`` re-colour of a black voxel): deterministic per
    cell, identical on every client, unknown to the server."""

    x, y, z = cell
    return ((x * 37 + z) & 0xFF) << 16 | ((y * 11) & 0xFF) << 8 | 0x5A


class ClientModel:
    """The stock BlockManager damage/colour paths, per the decompile."""

    def __init__(self, colours, scale=1.0):
        self.colours = dict(colours)  # cell -> rgb (0 = client re-colours)
        self.user: dict = {}
        self.damaged: dict = {}
        self.scale = scale

    def initial(self, cell):
        return self.user.get(cell, DEFAULT_BLOCK_HEALTH) * self.scale

    def add_damage(self, cell, damage):
        rgb = self.colours[cell]
        if rgb == 0:
            rgb = _client_colour(cell)
            self.colours[cell] = rgb
        block = self.damaged.get(cell)
        if block is None:
            health = self.initial(cell)
            if not health > damage:
                del self.colours[cell]
                return True
            block = self.damaged[cell] = [health, rgb]
        block[0] -= damage
        if block[0] <= 0:
            del self.colours[cell]
            del self.damaged[cell]
            return True
        self.colours[cell] = model.dim_rgb(rgb, damage)
        return False

    def paint(self, cell, rgb):
        if cell in self.colours:
            self.colours[cell] = rgb

    def receive(self, payload):
        kind = payload[0]
        if kind == 38:
            offset = 1
            (count,) = struct.unpack_from("<i", payload, offset)
            offset += 4
            damaged = []
            for _ in range(count):
                x, y, z, q, b, g, r = struct.unpack_from("<hhhBBBB", payload, offset)
                offset += 10
                damaged.append(((x, y, z), q / 4.0, (r << 16) | (g << 8) | b))
            (count,) = struct.unpack_from("<i", payload, offset)
            offset += 4
            for _ in range(count):
                x, y, z, q = struct.unpack_from("<hhhB", payload, offset)
                offset += 7
                self.user[(x, y, z)] = q / 4.0
            for cell, health, original in damaged:
                self.damaged[cell] = [health, original]
                self.colours[cell] = model.dim_rgb(
                    original, self.initial(cell) - health
                )
        elif kind == PaintBlockPacket.id:
            packet = PaintBlockPacket()
            packet.read(ByteReader(payload[1:]))
            r, g, b = packet.color
            self.paint((packet.x, packet.y, packet.z), (r << 16) | (g << 8) | b)
        elif kind == Damage.id:
            packet = Damage(ByteReader(payload[1:]))
            assert packet.type == 6 and packet.chunk_check == 0
            cell = model.center_cell(packet.position)
            self.add_damage(cell, packet.damage)
        else:  # pragma: no cover - unexpected packet in the reveal
            raise AssertionError(f"unexpected packet {kind}")


class _PartlyImplicitMap:
    """ServerVXL whose chosen cells hold no explicit colour (interior)."""

    def __init__(self, inner, implicit):
        self._inner = inner
        self._implicit = set(implicit)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def has_explicit_color(self, x, y, z):
        if (int(x), int(y), int(z)) in self._implicit:
            return False
        return self._inner.has_explicit_color(x, y, z)

    def set_point(self, x, y, z, color):
        self._implicit.discard((int(x), int(y), int(z)))
        return self._inner.set_point(x, y, z, color)


class _Joiner:
    def __init__(self, player_id=9):
        self.player = type("P", (), {"id": player_id})()
        self.sent: list[bytes] = []

    def send(self, data, reliable=True):
        self.sent.append(bytes(data))


@pytest.fixture()
def server():
    instance = BattleSpadesServer(ServerConfig())
    world = instance.world_manager
    world.map = ServerVXL(-1, b"", 0, 2)
    world.find_unsupported_chunks = lambda _removed: []
    return instance


# Scenario: (cell, initial colour, recorded health, [("hit", d) | ("paint", rgb)])
SCENARIOS = [
    ((100, 100, 100), BASE, None, [("hit", 1.0)]),
    ((101, 100, 100), BASE, None, [("hit", 1.0), ("hit", 1.0)]),
    ((102, 100, 100), BASE, None, [("hit", 0.75)] * 3),
    ((103, 100, 100), BASE, None, [("hit", 2.0), ("paint", 0x102030)]),
    ((104, 100, 100), BASE, None,
     [("hit", 2.0), ("paint", 0x102030), ("hit", 1.0)]),
    ((105, 100, 100), 0xF0E0D0, USER_BLOCK_HEALTH,
     [("hit", 2.5), ("hit", 2.5), ("hit", 1.5)]),
    ((106, 100, 100), BASE, USER_BLOCK_HEALTH, [("hit", 0.5)] * 5),
    ((107, 100, 100), 0x000000, None, [("hit", 1.0), ("hit", 1.5)]),
    ((108, 100, 100), BASE, None, [("hit", 3.0), ("hit", 1.0)]),
]
IMPLICIT = [
    ((110, 100, 100), [("hit", 0.5), ("hit", 1.5), ("hit", 0.75)]),
    ((111, 100, 100), [("hit", 2.0), ("paint", 0x335577), ("hit", 1.0)]),
    ((112, 100, 100), [("hit", 1.0)]),
]


def _play(server):
    """Apply every scenario to the server and to one live client."""

    world = server.world_manager
    for cell, colour, health, _events in SCENARIOS:
        assert world.set_block(*cell, True, colour, health=health)
    for cell, _events in IMPLICIT:
        assert world.set_block(*cell, True, 0x777777)
    world.map = _PartlyImplicitMap(world.map, [cell for cell, _ in IMPLICIT])

    live_colours = {cell: colour for cell, colour, _h, _e in SCENARIOS}
    live_colours.update({cell: _client_colour(cell) for cell, _ in IMPLICIT})
    live = ClientModel(live_colours)
    for cell, _colour, health, _events in SCENARIOS:
        if health is not None:
            live.user[cell] = health
    for cell, events in [(c, e) for c, _, _, e in SCENARIOS] + IMPLICIT:
        for kind, value in events:
            if kind == "hit":
                assert world.apply_block_damage(*cell, value)[1] is False
                assert live.add_damage(cell, value) is False
            else:
                assert world.set_block(*cell, True, value)
                live.paint(cell, value)
    return live


def _joiner_after_reveal(server):
    """A fresh MapSync client (VXL colours, no damage) fed the reveal."""

    world = server.world_manager
    colours = {}
    for cell, *_rest in SCENARIOS:
        colours[cell] = int(world.get_color(*cell)) & 0xFFFFFF
    for cell, _events in IMPLICIT:
        colours[cell] = (
            int(world.get_color(*cell)) & 0xFFFFFF
            if world.map.has_explicit_color(*cell)
            else _client_colour(cell)
        )
    joiner = ClientModel(colours)
    connection = _Joiner()
    PrefabActionService(server).reveal_to(connection)
    for payload in connection.sent:
        joiner.receive(payload)
    return joiner, connection.sent


def test_dim_is_the_decompiled_formula():
    assert model.dim(200, 1.0) == 175
    assert model.dim(200, 0.25) == 200  # round(0.25) == 0
    assert model.dim(200, 0.5) == 175  # Python 2 round(0.5) == 1
    assert model.dim(200, 2.5) == 125  # round(2.5) == 3, not 2
    assert model.dim(10, 9.0) == 0  # clamped
    assert model.dim(7, 1.0) == 7  # (7 * 1) >> 3 == 0
    assert model.dim_rgb(0x406080, 2.0) == 0x304860
    # Compounding differs from one shot: why joiners looked wrong.
    twice = model.dim_rgb(model.dim_rgb(0xC08040, 1.0), 1.0)
    assert twice != model.dim_rgb(0xC08040, 2.0)


def test_late_joiner_matches_live_shade_and_health(server):
    live = _play(server)
    joiner, _sent = _joiner_after_reveal(server)
    cells = [c for c, *_ in SCENARIOS] + [c for c, _ in IMPLICIT]
    for cell in cells:
        assert joiner.colours[cell] == live.colours[cell], cell
        assert joiner.damaged[cell][0] == pytest.approx(live.damaged[cell][0]), cell
    # Where the server knows it, the DamagedBlock original colour agrees too.
    for cell, *_ in SCENARIOS:
        assert joiner.damaged[cell][1] == live.damaged[cell][1], cell


def test_single_hit_cells_need_no_extra_packets(server):
    world = server.world_manager
    cell = (100, 100, 100)
    world.set_block(*cell, True, BASE)
    world.apply_block_damage(*cell, 1.5)
    connection = _Joiner()
    PrefabActionService(server).reveal_to(connection)
    assert [payload[0] for payload in connection.sent] == [38]
    assert world.block_shade_rows() == []
    assert world.block_hit_replays() == []


def test_reveal_orders_rows_then_shades_then_replays(server):
    _play(server)
    _joiner, sent = _joiner_after_reveal(server)
    kinds = [payload[0] for payload in sent]
    first_paint = kinds.index(PaintBlockPacket.id)
    first_damage = kinds.index(Damage.id)
    assert set(kinds[:first_paint]) == {38}
    assert set(kinds[first_paint:first_damage]) == {PaintBlockPacket.id}
    assert set(kinds[first_damage:]) == {Damage.id}


def test_repair_of_a_live_client_keeps_its_live_shade(server):
    live = _play(server)
    before = {
        cell: (live.colours[cell], list(live.damaged[cell]))
        for cell, *_ in SCENARIOS
    }
    for cell, *_ in SCENARIOS:
        for payload in server.terrain_repair.canonical_health_packets(cell):
            live.receive(payload)
    for cell, (colour, block) in before.items():
        assert live.colours[cell] == colour, cell
        assert live.damaged[cell][0] == pytest.approx(block[0]), cell


def test_shade_state_follows_the_cell_lifecycle(server):
    world = server.world_manager
    cell = (100, 100, 100)
    world.set_block(*cell, True, BASE)
    world.apply_block_damage(*cell, 1.0)
    world.apply_block_damage(*cell, 1.0)
    assert cell in world.block_shade
    world.destroy_blocks([cell])
    assert cell not in world.block_shade and cell not in world.block_damage
    world.set_block(*cell, True, BASE)
    world.apply_block_damage(*cell, 1.0)
    # A rebuilt voxel starts from its own colour, not the old shade.
    assert world.block_shade[cell] == (BASE << 24) | model.dim_rgb(BASE, 1.0)
    world.set_block(*cell, False)
    assert cell not in world.block_shade


def test_hit_history_is_bounded_and_falls_back_to_health(server):
    world = server.world_manager
    cell = (100, 100, 100)
    world.set_block(*cell, True, 0x000000, health=60.0)
    for _ in range(world.MAX_REPLAY_HITS + 1):
        world.apply_block_damage(*cell, 0.25)
    assert world.block_hits[cell] is None
    assert world.block_hit_replays() == []
    user, damaged = world.block_manager_rows(replay_hits=True)
    remaining = 60.0 - 0.25 * (world.MAX_REPLAY_HITS + 1)
    # Health stays exact through the pre-existing health-only row.
    assert user == [(*cell, remaining)] and damaged == []
