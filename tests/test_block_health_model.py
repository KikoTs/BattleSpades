"""One block-health model shared by the server, builders, observers, joiners.

Ground truth (stock client, live 2026-09-26):

* ``BlockManager.handle_damage`` footprints were captured by logging every
  ``add_damage`` call and ``rnd_generator`` draw for all 44 damage types
  (``tests/fixtures/block_damage_footprints_live.json``, seed 77, amount 10,
  13x13x13 solid probe volume at health 50).
* BlockBuild(32) type 0 / BlockLine(40) / prefab cells enter ``user_blocks``
  at 9.0, BlockBuildColored(33) at 3.0, map voxels use 5.0.
* BlockManagerState(38) user rows set ``user_blocks``; damaged rows set
  ``DamagedBlock(remaining, original_colour)`` and darken the voxel.
* PaintBlock(7) recolours a solid voxel without touching its health/damage.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.constants as C
from server import block_damage_model as model
from server.combat_runtime import USER_BLOCK_HEALTH, get_combat_system
from server.config import ServerConfig
from server.main import BattleSpadesServer
from server.prefab_actions import block_state_packets, encode_block_manager_state
from server.runtime_vxl import ServerVXL
from shared.bytes import ByteReader
from shared.packet import BlockBuildColored, Damage

FIXTURE = Path(__file__).parent / "fixtures" / "block_damage_footprints_live.json"


# --------------------------------------------------------------------------
# Footprint model vs the live client capture
# --------------------------------------------------------------------------

def test_model_reproduces_every_captured_client_footprint():
    data = json.loads(FIXTURE.read_text())
    seed, amount, half = data["seed"], data["amount"], data["cube_half"]
    assert data["cells"], "fixture must not be empty"
    for type_name, rows in data["cells"].items():
        predicted = [
            [cell[0], cell[1], cell[2], damage]
            for cell, damage in model.footprint(int(type_name), (0, 0, 0), amount, seed)
            if all(abs(v) <= half for v in cell)
        ]
        assert predicted == rows, f"damage type {type_name}"


@pytest.mark.parametrize(
    "damage_type, radius",
    [(7, 4), (8, 4), (10, 3), (15, 3), (16, 8), (22, 2), (39, 5), (41, 8)],
)
def test_radius_footprints_cover_the_measured_sphere(damage_type, radius):
    cells = model.footprint(damage_type, (100, 100, 100), 7.0, 3)
    assert len(cells) == sum(
        1
        for dx in range(-radius, radius + 1)
        for dy in range(-radius, radius + 1)
        for dz in range(-radius, radius + 1)
        if dx * dx + dy * dy + dz * dz < radius * radius
    )
    # The centre takes the full amount plus up to 2 random extra.
    centre = dict(cells)[(100, 100, 100)]
    assert 7.0 <= centre <= 9.0


def test_centre_and_wire_quantization_match_the_client():
    # Live: position (0.6, 0.4, -0.5) -> cell (1, 0, 0); 0.5 rounds up.
    assert model.center_cell((10.6, 20.4, 29.5)) == (11, 20, 30)
    assert model.center_cell((10.49, 20.5, 30.0)) == (10, 21, 30)
    # Damage.damage is one quarter-unit byte, rounded to nearest.
    assert model.wire_damage(0.7) == 0.75
    assert model.wire_damage(2.99) == 3.0
    assert model.wire_damage(7.5) == 7.5
    assert model.ceil4(8.363) == 8.5


def test_spade_column_and_pickaxe_are_deterministic():
    column = model.footprint(int(C.SPADE_DAMAGE), (5, 5, 5), 5.0, 200)
    assert column == [((5, 5, 4), 5.0), ((5, 5, 5), 5.0), ((5, 5, 6), 5.0)]
    assert model.footprint(int(C.PICKAXE_DAMAGE), (5, 5, 5), 9.0, 9) == [
        ((5, 5, 5), 9.0)
    ]


# --------------------------------------------------------------------------
# WorldManager ledger
# --------------------------------------------------------------------------

@pytest.fixture()
def server():
    instance = BattleSpadesServer(ServerConfig())
    world = instance.world_manager
    world.map = ServerVXL(-1, b"", 0, 2)
    # Probe volumes float in an empty map; collapse is covered elsewhere.
    world.find_unsupported_chunks = lambda _removed: []
    return instance


def _fill(world, cells, *, health=None, color=0x406080):
    for cell in cells:
        assert world.set_block(*cell, True, color, health=health)


def test_paint_keeps_damage_and_health_like_color_block(server):
    world = server.world_manager
    cell = (100, 100, 100)
    _fill(world, [cell], health=USER_BLOCK_HEALTH)
    world.apply_block_damage(*cell, 2.0)
    world.set_block(*cell, True, 0x102030)  # PaintBlock path
    user, damaged = world.block_manager_rows()
    assert user == [(*cell, USER_BLOCK_HEALTH)]
    # The DamagedBlock keeps the colour of the first hit; the paint colour
    # the live clients show undarkened is restated after the row.
    assert damaged == [(*cell, 7.0, (0x40, 0x60, 0x80))]
    assert world.block_shade_rows() == [(*cell, (0x10, 0x20, 0x30))]
    # A new voxel in the same place starts undamaged.
    world.destroy_blocks([cell])
    _fill(world, [cell])
    assert world.block_manager_rows() == ([], [])


def test_base_layer_is_not_damageable(server):
    world = server.world_manager
    cell = (100, 100, model.MAX_DAMAGEABLE_Z + 1)
    _fill(world, [cell])
    assert world.apply_block_damage(*cell, 30.0) == (0.0, False)
    assert world.get_solid(*cell)


def test_block_state_rows_round_trip_through_the_wire_layout():
    data = encode_block_manager_state(
        [(1, 2, 3, 9.0)], [(7, 8, 9, 4.5, (10, 20, 30))]
    )
    # Stock client generate(): damaged row = i16 x,y,z, u8 health*4, B, G, R.
    assert data == (
        struct.pack("<Bi", 38, 1)
        + struct.pack("<hhhBBBB", 7, 8, 9, 18, 30, 20, 10)
        + struct.pack("<i", 1)
        + struct.pack("<hhhB", 1, 2, 3, 36)
        + struct.pack("<i", 0)
    )
    packets = block_state_packets([(i, 0, 0, 9.0) for i in range(5)],
                                  [(9, 9, 9, 1.0, (1, 2, 3))], batch=2)
    # All user rows first: the client darkens a damaged row against the
    # initial health it already holds (live: 4.0/9 before its 9.0 user row
    # darkened like 4.0/5).
    assert packets[-1] == encode_block_manager_state((), [(9, 9, 9, 1.0, (1, 2, 3))])
    assert len(packets) == 4
    assert all(struct.unpack_from("<i", p, 1) == (0,) for p in packets[:-1])


# --------------------------------------------------------------------------
# Live build replication: observers must hold the builder's 9.0
# --------------------------------------------------------------------------

class _Peer:
    def __init__(self, player_id, *, known=()):
        self.player = SimpleNamespace(id=player_id, team=0)
        self.in_game = True
        self.known_entity_ids = set(known)
        self.sent: list[bytes] = []

    def send(self, data, reliable=True):
        self.sent.append(bytes(data))


def _builder(server, player_id=1):
    sent = []
    player = SimpleNamespace(
        id=player_id,
        block_color=0x336699,
        send=lambda data, reliable=True: sent.append(bytes(data)),
        sent=sent,
    )
    return player


def test_single_build_gives_observers_packet_33_then_health_9(server):
    world = server.world_manager
    observer = _Peer(2)
    builder_peer = _Peer(1)
    builder = _builder(server)
    builder_peer.player = builder
    server.connections = {1: builder_peer, 2: observer}
    cell = (100, 100, 100)
    assert world.set_block(*cell, True, 0x336699, health=USER_BLOCK_HEALTH)

    get_combat_system(server)._announce_block_build(builder, cell, 0x336699, 5)

    assert [data[0] for data in observer.sent] == [33, 38]
    assert observer.sent[1] == encode_block_manager_state([(*cell, 9.0)])
    # The builder's own BlockBuild(32) echo already yields 9.0 client-side.
    assert 38 not in [data[0] for data in builder.sent]
    assert 38 not in [data[0] for data in builder_peer.sent]


def test_block_line_commit_records_user_health_and_informs_observers(server):
    world = server.world_manager
    observer = _Peer(2)
    builder = _builder(server)
    builder_peer = _Peer(1)
    builder_peer.player = builder
    server.connections = {1: builder_peer, 2: observer}
    cells = ((100, 100, 100), (101, 100, 100), (102, 100, 100))
    combat = get_combat_system(server)
    combat._build_overlaps_player = lambda *_args: False
    builder.blocks = 10
    builder.add_blocks = lambda n: None

    combat._commit_block_line(builder, 3, (100, 100, 100, 102, 100, 100), cells, 0x336699)

    assert all(world.initial_block_health(*cell) == USER_BLOCK_HEALTH for cell in cells)
    ids = [data[0] for data in observer.sent]
    assert ids.count(33) == 3 and ids.index(38) == ids.index(33) + 3
    assert observer.sent[ids.index(38)] == encode_block_manager_state(
        [(*cell, USER_BLOCK_HEALTH) for cell in cells]
    )


# --------------------------------------------------------------------------
# Explosions: server applies the exact client footprint
# --------------------------------------------------------------------------

def _volume(centre, half):
    x, y, z = centre
    return [
        (x + dx, y + dy, z + dz)
        for dx in range(-half, half + 1)
        for dy in range(-half, half + 1)
        for dz in range(-half, half + 1)
    ]


def _expected_after(cells_health, damage_type, position, amount, seed):
    remaining = dict(cells_health)
    for cell, damage in model.footprint(damage_type, position, amount, seed):
        if cell in remaining:
            remaining[cell] -= damage
    return {cell for cell, health in remaining.items() if health > 0.0}


def test_dynamite_blast_matches_client_footprint_on_built_and_map_cells(server):
    world = server.world_manager
    centre = (200, 200, 150)
    volume = _volume(centre, 4)
    built = {cell for cell in volume if cell[0] >= centre[0]}
    for cell in volume:
        world.set_block(*cell, True, 0x556677,
                        health=USER_BLOCK_HEALTH if cell in built else None)
    knows = _Peer(1, known={77})
    stranger = _Peer(2)
    server.connections = {1: knows, 2: stranger}

    server._apply_blast(
        centre[0] + 0.3, centre[1], centre[2], 100.0, 7.0,
        int(getattr(C.KILL, "DYNAMITE_KILL", 15)), None,
        crater_radius=2, native_damage_type=int(C.DYNAMITE_DAMAGE),
        causer_entity_id=77,
    )

    natives = [Damage(ByteReader(d[1:])) for d in knows.sent if d[0] == 37]
    assert len(natives) == 1
    native = natives[0]
    assert native.type == int(C.DYNAMITE_DAMAGE)
    assert native.damage == 7.0
    health = {cell: (9.0 if cell in built else 5.0) for cell in volume}
    survivors = _expected_after(
        health, native.type, native.position, native.damage, native.seed
    )
    assert {cell for cell in volume if world.get_solid(*cell)} == survivors
    # Built blocks near the centre survive dynamite's 7 (+0..2) far more
    # often than map voxels: at least one partially damaged built cell.
    assert any(cell in built for cell in survivors)
    # A peer that never saw the charge gets the exact outcome instead.
    assert all(d[0] == 37 for d in stranger.sent)
    exact = [Damage(ByteReader(d[1:])) for d in stranger.sent]
    assert all(p.type == int(C.WEAPON_DAMAGE) for p in exact)
    killed = {
        tuple(int(v) for v in p.position) for p in exact if p.damage >= 31.0
    }
    assert killed == set(volume) - survivors
    # Every surviving damaged cell is reported to joiners with its health.
    _user, damaged = world.block_manager_rows()
    for x, y, z, remaining, _colour in damaged:
        assert remaining == pytest.approx(
            health[(x, y, z)] - sum(
                d for c, d in model.footprint(
                    native.type, native.position, native.damage, native.seed
                ) if c == (x, y, z)
            )
        )


def test_grenade_uses_native_radius_packet_and_per_cell_health(server):
    world = server.world_manager
    centre = (300, 300, 150)
    volume = _volume(centre, 3)
    _fill(world, volume, health=USER_BLOCK_HEALTH)
    peer = _Peer(1)
    server.connections = {1: peer}

    server._apply_blast(
        centre[0], centre[1], centre[2], 230.0, 4.0, 3, None,
        crater_radius=1, force_destroy=True,
        terrain_damage_type=int(C.GRENADE_DAMAGE),
    )

    packets = [Damage(ByteReader(d[1:])) for d in peer.sent if d[0] == 37]
    assert len(packets) == 1 and packets[0].type == int(C.GRENADE_DAMAGE)
    # 4 + 2*random never reaches a 9-health built block: nothing breaks,
    # unlike the old forced 27-cell crater.
    assert all(world.get_solid(*cell) for cell in volume)
    assert len(world.block_manager_rows()[1]) == 251


# --------------------------------------------------------------------------
# Terrain repair / catch-up never leave packet 33's 3.0 behind
# --------------------------------------------------------------------------

def test_canonical_repair_pins_health_after_packet_33(server):
    world = server.world_manager
    built = (100, 100, 100)
    plain = (102, 100, 100)
    _fill(world, [built], health=USER_BLOCK_HEALTH)
    _fill(world, [plain])
    world.apply_block_damage(*built, 3.0)

    built_packets = server.terrain_repair.canonical_packets(built, 4)
    plain_packets = server.terrain_repair.canonical_packets(plain, 4)

    assert built_packets[0][0] == BlockBuildColored.id
    assert built_packets[-2:] == [
        encode_block_manager_state([(*built, 9.0)]),
        encode_block_manager_state((), [(*built, 6.0, (0x40, 0x60, 0x80))]),
    ]
    assert plain_packets[-1] == encode_block_manager_state([(*plain, 5.0)])


def test_block_cannon_cells_record_packet_33_health(server):
    world = server.world_manager
    thrower = SimpleNamespace(id=3, name="Cannon", block_color=0x112233)
    cell = (150, 150, 150)
    world.set_block(150, 150, 151, True, 0x000000)  # support
    ex = SimpleNamespace(x=cell[0], y=cell[1], z=cell[2], contact_block=(150, 150, 151),
                         block_color=0x445566, source_loop=4)
    server.connections = {}
    assert server._place_block_cannon_impact(ex, thrower)
    assert world.initial_block_health(*cell) == 3.0
