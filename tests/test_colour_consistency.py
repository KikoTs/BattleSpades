"""Colour-flow harness: builder, observer, late joiner and the server agree.

Every colour-bearing packet the server emits is fed through a small model of
the stock client, whose semantics were measured live on the retail client
(two consoles on one server, 2026-09-26):

* SetColor(11) overwrites ``Character.block_color`` -- for the LOCAL player
  too (the local client then echoes it back as its own SetColor).
* BlockLine(40) / BlockBuild(32) add cells coloured with the SENDER's
  ``block_color`` as the receiver knows it WHEN THE PACKET IS PROCESSED; the
  builder's own echo therefore uses the builder's palette at echo arrival.
* BlockBuildColored(33) adds a cell with its explicit RGB, but is IGNORED on
  an already-solid cell (it cannot recolour).
* PaintBlock(7) recolours an existing solid cell exactly; no-op on air.
* Damage(37) type 6 removes exactly one cell.

The harness drives the real CombatSystem / WorldMutationService /
TerrainRepairService and asserts owner == observer == late joiner == VXL.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import shared.constants as C
from aoslib.vxl import VXL
from aoslib.world import cube_line
from protocol.packet_handler import PacketHandler
from server.colors import pack_rgb, unpack_rgb
from server.combat_runtime import get_combat_system
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.metrics import RuntimeMetrics
from server.player import Player
from server.team import Team
from server.terrain_repair import TerrainRepairService
from server.world_manager import WorldManager
from server.world_mutations import WorldMutationService
from shared.bytes import ByteReader
from shared.packet import (
    BlockBuild,
    BlockBuildColored,
    BlockLine,
    Damage,
    PaintBlockPacket,
    SetColor,
)

MAP_PATH = Path("maps/ArcticBase.vxl")
MAP_BYTES = MAP_PATH.read_bytes() if MAP_PATH.exists() else None
GROUND = 0x7F00FF00
SHELF_Z = 61

pytestmark = pytest.mark.skipif(MAP_BYTES is None, reason="ArcticBase missing")


class ModelClient:
    """The measured colour semantics of one stock client."""

    def __init__(self, local_id, palettes=None, cells=None):
        self.local_id = local_id
        self.palettes = dict(palettes or {})  # player id -> 0xRRGGBB
        self.cells = dict(cells or {})  # (x, y, z) -> 0xRRGGBB (solid)
        self.inbox = []

    def set_local_palette(self, rgb):
        self.palettes[self.local_id] = pack_rgb(rgb)

    def _add(self, cell, rgb):
        if cell not in self.cells:
            self.cells[cell] = pack_rgb(rgb)

    def process(self, data):
        packet_id = data[0]
        reader = ByteReader(data[1:])
        if packet_id == SetColor.id:
            packet = SetColor(reader)
            self.palettes[int(packet.player_id)] = pack_rgb(packet.value)
        elif packet_id == BlockLine.id:
            packet = BlockLine(reader)
            rgb = self.palettes.get(int(packet.player_id), 0x707070)
            for cell in cube_line(
                packet.x1, packet.y1, packet.z1,
                packet.x2, packet.y2, packet.z2,
            ):
                self._add(tuple(int(v) for v in cell), rgb)
        elif packet_id == BlockBuild.id:
            packet = BlockBuild(reader)
            rgb = self.palettes.get(int(packet.player_id), 0x707070)
            self._add((packet.x, packet.y, packet.z), rgb)
        elif packet_id == BlockBuildColored.id:
            packet = BlockBuildColored(reader)
            self._add((packet.x, packet.y, packet.z), packet.color)
        elif packet_id == PaintBlockPacket.id:
            packet = PaintBlockPacket(reader)
            cell = (packet.x, packet.y, packet.z)
            if cell in self.cells:
                self.cells[cell] = pack_rgb(packet.color)
        elif packet_id == Damage.id:
            packet = Damage(reader)
            if int(packet.type) == int(C.WEAPON_DAMAGE):
                cell = tuple(int(v) for v in packet.position)
                self.cells.pop(cell, None)

    def drain(self):
        while self.inbox:
            self.process(self.inbox.pop(0))


class Connection:
    def __init__(self, server, client):
        self.server = server
        self.client = client
        self.player = None
        self.in_game = True

    def send(self, data, reliable=True, prefix=0x30):
        self.client.inbox.append(bytes(data))


class Server:
    def __init__(self):
        self.config = ServerConfig()
        self.config.log_suppress_packets = set()
        self.config.world_mutation_queue_limit = 64
        self.config.world_mutation_batch_limit = 16
        self.config.world_mutation_cell_budget = 64
        self.config.world_mutation_timeout_ticks = 180
        self.config.terrain_repair_delay_ticks = 1
        self.config.terrain_repair_interval_ticks = 1
        self.loop_count = 500
        self.players = {}
        self.connections = {}
        self.metrics = RuntimeMetrics()
        self.world_manager = WorldManager(self.config)
        self.world_manager.map = VXL(-1, MAP_BYTES, len(MAP_BYTES), 2)
        self.world_manager.map_name = MAP_PATH.stem
        self.world_manager._refresh_world()
        top = self.world_manager.map.get_z(100, 100)
        for x in range(94, 107):
            for y in range(94, 107):
                for z in range(0, top):
                    self.world_manager.map.set_point(x, y, z, False, 0)
                self.world_manager.map.set_point(x, y, top, True, GROUND)
        # A floating shelf at z=61 supports z=60 builds next to a builder
        # standing at z=60 (same geometry as test_reversed_combat).
        for x in range(99, 104):
            for y in range(99, 103):
                self.world_manager.map.set_point(x, y, SHELF_Z, True, GROUND)
        self.teams = {
            TEAM1: Team(TEAM1, self.config.team1_name, self.config.team1_color),
            TEAM2: Team(TEAM2, self.config.team2_name, self.config.team2_color),
        }
        self.world_mutations = WorldMutationService(self)
        self.terrain_repair = TerrainRepairService(self)

    def broadcast(self, data, exclude=None, reliable=True, **_kwargs):
        for connection in self.connections.values():
            if exclude is not None and connection.player is exclude:
                continue
            connection.send(data, reliable)


def join(server, player_id, team, position, client):
    connection = Connection(server, client)
    player = Player(player_id, f"P{player_id}", team, C.RIFLE_TOOL, connection)
    connection.player = player
    player.spawn(*position)
    server.players[player_id] = player
    server.connections[player_id] = connection
    return player


@pytest.fixture
def rig():
    server = Server()
    owner_client = ModelClient(0)
    observer_client = ModelClient(1)
    owner = join(server, 0, TEAM1, (100.5, 100.5, 60.0), owner_client)
    observer = join(server, 1, TEAM2, (104.5, 104.5, 60.0), observer_client)
    owner.set_tool(C.BLOCK_TOOL)
    owner.blocks = 50
    owner.last_applied_input_loop = 100
    ground = SHELF_Z
    # Both clients start from the same map and the spawn palettes the server
    # published after CreatePlayer.
    for client in (owner_client, observer_client):
        client.palettes = {0: owner.block_color, 1: observer.block_color}
    return server, owner, owner_client, observer_client, ground


def server_rgb(server, cell):
    assert server.world_manager.get_solid(*cell)
    return pack_rgb(server.world_manager.get_color(*cell))


def assert_agree(server, cells, *clients):
    for cell in cells:
        expected = server_rgb(server, cell)
        for client in clients:
            assert client.cells.get(cell) == expected, (
                f"client {client.local_id} {cell}: "
                f"{client.cells.get(cell)!r:} != server {expected:06X}"
            )


def send_line(server, owner, start, end, loop=103):
    packet = BlockLine()
    packet.loop_count = loop
    packet.player_id = owner.id
    packet.x1, packet.y1, packet.z1 = start
    packet.x2, packet.y2, packet.z2 = end
    asyncio.run(PacketHandler(server).handle(owner, bytes(packet.generate())))


def commit(server, owner, loop=103):
    owner.last_applied_input_loop = loop
    server.world_mutations.commit_ready()


def test_team_spawn_palettes_are_the_advertised_team_colours(rig):
    server, owner, _owner_client, _observer_client, _ground = rig
    observer = server.players[1]
    assert unpack_rgb(owner.block_color) == tuple(server.config.team1_color)
    assert unpack_rgb(observer.block_color) == tuple(server.config.team2_color)


def test_line_built_with_steady_palette_agrees_everywhere(rig):
    server, owner, owner_client, observer_client, ground = rig
    owner.set_color(0xC82828)
    owner_client.set_local_palette(0xC82828)
    cells = [(101, 100, ground - 1), (102, 100, ground - 1)]

    send_line(server, owner, cells[0], cells[-1])
    commit(server, owner)
    owner_client.drain()
    observer_client.drain()

    assert server_rgb(server, cells[0]) == 0xC82828
    assert_agree(server, cells, owner_client, observer_client)


def test_palette_change_before_commit_is_what_everyone_stores(rig):
    """SetColor drained after packet 40 but before its movement-frame commit."""

    server, owner, owner_client, observer_client, ground = rig
    owner.set_color(0x28C828)
    owner_client.set_local_palette(0x28C828)
    cell = (101, 100, ground - 1)

    send_line(server, owner, cell, cell)
    owner.set_color(0x2828C8)  # SetColor sent right after BlockLine
    owner_client.set_local_palette(0x2828C8)
    commit(server, owner)
    owner_client.drain()
    observer_client.drain()

    assert server_rgb(server, cell) == 0x2828C8
    assert_agree(server, [cell], owner_client, observer_client)


def test_palette_change_while_echo_is_in_flight_is_pinned_back(rig):
    """The server cannot know a SetColor still travelling towards it.

    The owner paints its echo with the newer palette; the PaintBlock pin that
    follows the echo restores the committed colour, so all views converge.
    """

    server, owner, owner_client, observer_client, ground = rig
    owner.set_color(0x28C828)
    owner_client.set_local_palette(0x28C828)
    cells = [(101, 100, ground - 1), (101, 101, ground - 1)]

    send_line(server, owner, cells[0], cells[-1])
    commit(server, owner)
    owner_client.set_local_palette(0xE6C814)  # changed before echo arrives
    owner_client.drain()
    observer_client.drain()

    assert server_rgb(server, cells[0]) == 0x28C828
    assert_agree(server, cells, owner_client, observer_client)


def test_single_block_build_does_not_trust_observer_palette_knowledge(rig):
    """Bots (and any id-32 builder) replicate with explicit RGB to observers."""

    server, owner, owner_client, observer_client, ground = rig
    owner.set_color(0x123456)
    owner_client.set_local_palette(0x123456)
    # The observer never saw the throttled SetColor relay.
    observer_client.palettes[owner.id] = 0x707070
    cell = (101, 100, ground - 1)
    packet = BlockBuild()
    packet.loop_count = 103
    packet.player_id = owner.id
    packet.x, packet.y, packet.z = cell
    packet.block_type = 0

    assert get_combat_system(server).handle_block_build(owner, packet)
    commit(server, owner)
    owner_client.drain()
    observer_client.drain()

    assert server_rgb(server, cell) == 0x123456
    assert_agree(server, [cell], owner_client, observer_client)


def test_late_joiner_converges_on_rebuilt_cell_with_new_colour(rig):
    """Snapshot had the cell solid in an old colour; catch-up must recolour."""

    server, owner, owner_client, observer_client, ground = rig
    cell = (101, 100, ground - 1)
    owner.set_color(0xAA0000)
    owner_client.set_local_palette(0xAA0000)
    send_line(server, owner, cell, cell)
    commit(server, owner)
    owner_client.drain()
    observer_client.drain()

    # The joiner's MapSync snapshot is taken here.
    joiner = ModelClient(2, cells={cell: server_rgb(server, cell)})

    # Destroy and rebuild in a new colour before the joiner is revealed.
    server.world_manager.set_block(*cell, False)
    owner.set_color(0x00AA00)
    owner_client.set_local_palette(0x00AA00)
    send_line(server, owner, cell, cell, loop=104)
    commit(server, owner, loop=104)

    # Catch-up replays each journaled cell from its final canonical state.
    for data in server.terrain_repair.canonical_packets(cell, 2):
        joiner.process(data)
    assert joiner.cells[cell] == server_rgb(server, cell) == 0x00AA00


def test_rejected_editor_style_prediction_is_repaired_to_vxl_colour(rig):
    """A client that recoloured a solid cell locally is pinned back."""

    server, _owner, owner_client, observer_client, ground = rig
    cell = (101, 100, ground)
    for client in (owner_client, observer_client):
        client.cells[cell] = server_rgb(server, cell)
    owner_client.cells[cell] = 0xFF00FF  # predicted paint the server refused

    server.terrain_repair.record_cells([cell])
    server.loop_count += 5
    assert server.terrain_repair.tick() == 1
    owner_client.drain()
    observer_client.drain()

    assert_agree(server, [cell], owner_client, observer_client)


def test_canonical_packets_skip_paint_for_implicit_interior(rig):
    """Client-owned interior shading is never overwritten by a column fill."""

    server, _owner, _owner_client, _observer_client, ground = rig
    world = server.world_manager
    x, y = 90, 90
    top = world.map.get_z(x, y)
    interior = None
    for z in range(top + 1, 238):
        if world.get_solid(x, y, z) and not world.map.has_explicit_color(x, y, z):
            interior = (x, y, z)
            break
    if interior is None:
        pytest.skip("no implicit interior voxel in this column")
    packets = server.terrain_repair.canonical_packets(interior, 0)
    assert [data[0] for data in packets] == [BlockBuildColored.id]


def test_colour_helpers_are_one_convention():
    assert pack_rgb((0x12, 0x34, 0x56)) == 0x123456
    assert pack_rgb([0x112, 0x34, 0x56, 0x99]) == 0x123456
    assert pack_rgb(0x80123456) == 0x123456
    assert unpack_rgb(0x7F654321) == (0x65, 0x43, 0x21)
    # The wire codec is the only place that reverses bytes.
    packet = BlockBuildColored()
    packet.color = 0x123456
    assert BlockBuildColored(ByteReader(bytes(packet.generate())[1:])).color == 0x123456
    paint = PaintBlockPacket()
    paint.color = unpack_rgb(0x123456)
    assert tuple(
        PaintBlockPacket(ByteReader(bytes(paint.generate())[1:])).color
    ) == (0x12, 0x34, 0x56)
    set_color = SetColor()
    set_color.value = 0x123456
    assert SetColor(ByteReader(bytes(set_color.generate())[1:])).value == 0x123456
