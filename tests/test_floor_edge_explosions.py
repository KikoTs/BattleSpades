"""Explosions on the indestructible z=239 bed and at the map edges.

Regression guard for the 2026-09-28 production segfault investigation: the
server died ~2 s after ``DRILL explode at (332.0,285.9,239.0)``.  The crash
was later reproduced without any project native module (a pure-Python
CompactVoxelMap scan segfaults the stock interpreter on the dev host), so it
was not caused by this path; these tests pin that the drill bore, the drill's
lifespan blast, every other projectile warhead, and the placed charges stay
inside 0..511 x 0..511 x 0..238, never break the z=239 bed, and leave the
collapse flood and the MapSync serializers consistent.
"""

import math

import pytest

import shared.constants as C
from server.config import ServerConfig
from server.game_constants import TEAM1
from server.main import BattleSpadesServer
from server.player import Player
from server.projectiles import PROJECTILE_SPECS, DrillContact, Explosion

MAP_EDGE = 511
BED_Z = 239

# The exact production position, the two earlier bed-level drills of that
# session, and bed/edge positions around every map boundary.
CRASH_POSITION = (332.0, 285.9, 239.0)
POSITIONS = (
    CRASH_POSITION,
    (332.4, 308.7, 238.9),
    (330.4, 312.7, 238.7),
    (332.0, 285.9, 239.5),
    (332.0, 285.9, 239.99),
    (332.0, 285.9, 240.5),
    (0.0, 0.0, 239.0),
    (0.4, 256.0, 238.4),
    (511.0, 511.0, 239.0),
    (511.9, 3.0, 238.9),
    (3.0, 511.9, 239.5),
    (-1.0, 256.0, 239.0),
    (512.0, 256.0, 239.0),
)

# Placed charges explode through the native footprint of their damage type.
CHARGES = (
    ("dynamite", "DYNAMITE_DAMAGE", 100.0, 25.0),
    ("c4", "C4_DAMAGE", 100.0, 25.0),
    ("landmine", "LANDMINE_DAMAGE", 100.0, 15.0),
    ("grave", "GRAVE_DAMAGE", 50.0, 10.0),
)


class _Connection:
    def __init__(self):
        self.player = None
        self.in_game = True
        self.sent = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(bytes(data))


def _seabed_server():
    """A flat map with solid seabed blocks (z 231..238) at every probe site."""

    server = BattleSpadesServer(ServerConfig())
    world = server.world_manager
    world.generate_flat_map()
    for px, py, _pz in POSITIONS:
        cx = min(MAP_EDGE, max(0, int(math.floor(px))))
        cy = min(MAP_EDGE, max(0, int(math.floor(py))))
        for x in range(max(0, cx - 4), min(MAP_EDGE, cx + 4) + 1):
            for y in range(max(0, cy - 4), min(MAP_EDGE, cy + 4) + 1):
                for z in range(231, BED_Z):
                    world.set_block(x, y, z, True, 0x405060)
    connection = _Connection()
    connection.server = server
    owner = Player(3, "Driller", TEAM1, C.DRILLGUN_TOOL, connection)
    owner.alive = True
    owner.spawned = True
    connection.player = owner
    server.players[owner.id] = owner
    server.connections[owner.id] = connection
    return server, owner


def _assert_world_sane(server, touched):
    world = server.world_manager
    for x, y in touched:
        assert world.get_solid(x, y, BED_Z), (x, y)
    for (x, y, z) in list(world.block_damage) + list(world.block_health):
        assert 0 <= x <= MAP_EDGE and 0 <= y <= MAP_EDGE and 0 <= z < BED_Z
    for x, y in world.dirty_columns:
        assert 0 <= x <= MAP_EDGE and 0 <= y <= MAP_EDGE
    # Both MapSync serializers must still accept the damaged columns.
    assert world.serialize_dirty_columns_compressed()
    assert sum(1 for _ in world.iter_full_sync_chunks()) > 0


def _touched_columns():
    columns = set()
    for px, py, _pz in POSITIONS:
        cx = min(MAP_EDGE, max(0, int(math.floor(px))))
        cy = min(MAP_EDGE, max(0, int(math.floor(py))))
        for x in range(max(0, cx - 4), min(MAP_EDGE, cx + 4) + 1):
            for y in range(max(0, cy - 4), min(MAP_EDGE, cy + 4) + 1):
                columns.add((x, y))
    return columns


def _spawn(server, owner, tool, position):
    projectile = server.projectile_engine.spawn(
        tool, position, (0.0, 0.0, 20.0), 0.0, owner.id, now=0.0
    )
    spec = projectile.spec
    if spec.entity_type:
        entity = server.entity_registry.place(
            int(spec.entity_type), *position,
            kind="projectile", player_id=owner.id,
        )
        projectile.entity_id = entity.entity_id
    projectile.contact_block = tuple(int(math.floor(v)) for v in position)
    return projectile


def test_drill_bore_and_lifespan_blast_at_the_production_crash_position():
    server, owner = _seabed_server()
    tool = int(C.DRILLGUN_TOOL)
    projectile = _spawn(server, owner, tool, CRASH_POSITION)
    bore_cell = tuple(int(math.floor(v)) for v in CRASH_POSITION)
    assert bore_cell[2] == BED_Z

    # Every drill tick against the bed, then the lifespan (destroyed) blast.
    for _ in range(5):
        server._apply_drill_contact(DrillContact(projectile, bore_cell))
        server._apply_drill_contact(
            DrillContact(projectile, (bore_cell[0], bore_cell[1], BED_Z - 1))
        )
    server._explode_projectile(Explosion(projectile, destroyed=True))
    server.projectile_engine.projectiles.clear()

    world = server.world_manager
    # The 81-cell bore removed the diggable seabed above the bed...
    assert not world.get_solid(332, 285, 238)
    # ...and never the bed itself, anywhere in the footprint.
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            assert world.get_solid(332 + dx, 285 + dy, BED_Z)
    _assert_world_sane(server, _touched_columns())


@pytest.mark.parametrize("position", POSITIONS)
def test_every_projectile_warhead_on_the_bed_and_edges(position):
    server, owner = _seabed_server()
    for tool, spec in sorted(PROJECTILE_SPECS.items()):
        if spec.behavior == "deploy":
            continue
        projectile = _spawn(server, owner, tool, position)
        if spec.name == "drill":
            server._apply_drill_contact(
                DrillContact(projectile, projectile.contact_block)
            )
            server._explode_projectile(Explosion(projectile, destroyed=True))
        else:
            server._explode_projectile(Explosion(projectile))
        server.projectile_engine.projectiles.clear()
    _assert_world_sane(server, _touched_columns())


@pytest.mark.parametrize("position", POSITIONS)
def test_placed_charges_on_the_bed_and_edges(position):
    server, owner = _seabed_server()
    for _name, damage_name, damage, block_damage in CHARGES:
        entity = server.entity_registry.place(
            int(C.DYNAMITE_ENTITY), *position,
            kind="deployable", player_id=owner.id,
        )
        server._apply_blast(
            position[0], position[1], position[2], damage, block_damage, 0,
            owner,
            native_damage_type=int(getattr(C, damage_name)),
            causer_entity_id=entity.entity_id,
            blast_radius=4.0,
        )
        server.entity_registry.remove(entity.entity_id)
    _assert_world_sane(server, _touched_columns())


def test_collapse_flood_treats_the_bed_as_ground_at_every_edge():
    server, _owner = _seabed_server()
    world = server.world_manager
    removed = [
        (0, 0, 238), (511, 511, 238), (0, 511, 238), (511, 0, 238),
        (332, 285, 238), (332, 285, 239), (332, 285, 240), (-1, 0, 238),
        (512, 0, 238), (0, -1, 238),
    ]
    for cell in removed:
        world.destroy_blocks([cell])
    # Seabed columns resting on the bed are grounded: nothing may collapse,
    # and the native and Python floods must agree.
    assert world.find_unsupported_chunks(removed) == []
    from server.world_manager import WorldManager

    python_flood = WorldManager.find_unsupported_chunks.__get__(world)
    world.get_solid = world.get_solid  # instance override forces Python path
    try:
        assert python_flood(removed) == []
    finally:
        del world.get_solid
