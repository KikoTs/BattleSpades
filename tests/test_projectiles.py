"""Projectile engine tests: rocket/drill/snowball contact flight, grenade
bounce regression, sticky anchoring, and spec-table integrity.

Ground truth: docs/CONTENT_TABLES.md + the 2026-07-07 client extraction
(ROCKET 75u/s g*0.05 blast 140/5, ROCKET2 150u/s g*0.025 50/2, DRILL 20u/s
g*1.5 lifespan 3s 50/5 -> destroyed 95/10, SNOWBALL 50u/s g*0.5 10/0).
"""
import asyncio
from types import SimpleNamespace

import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import CreateEntity, Damage, DestroyEntity
from server.config import ServerConfig
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import BotAction, BotActionKind
from server.game_constants import TEAM1
from server.main import BattleSpadesServer
from server.handlers.world import handle_oriented_item
from server.player import Player
from server.projectiles import (
    PROJECTILE_SPECS, DrillContact, ProjectileDeployment, ProjectileEngine,
    BASE_GRAVITY, BOUNCE_DAMP, drill_contact_cells,
)

DT = 1.0 / 60.0


class RecordingConnection:
    def __init__(self, player=None, in_game=True):
        self.player = player
        self.in_game = in_game
        self.sent = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(bytes(data))


class OpenWorld:
    """No blocks anywhere."""
    def get_solid(self, x, y, z):
        return False


class WallWorld:
    """A solid wall plane at x >= wall_x."""
    def __init__(self, wall_x):
        self.wall_x = wall_x

    def get_solid(self, x, y, z):
        return x >= self.wall_x


class FloorWorld:
    """Solid ground at z >= floor_z (AoS z grows downward)."""
    def __init__(self, floor_z):
        self.floor_z = floor_z

    def get_solid(self, x, y, z):
        return z >= self.floor_z


# --- spec table -------------------------------------------------------------

def test_specs_cover_requested_tools():
    for tool in (C.GRENADE_TOOL, C.RPG_TOOL, C.RPG2_TOOL, C.DRILLGUN_TOOL,
                 C.SNOWBLOWER_TOOL, C.STICKY_GRENADE_TOOL, C.CHEMICALBOMB_TOOL,
                 C.GRENADE_LAUNCHER_WEAPON_TOOL, C.MINE_LAUNCHER_TOOL):
        assert int(tool) in PROJECTILE_SPECS, f"tool {tool} missing"


def test_rocket_spec_matches_client_constants():
    s = PROJECTILE_SPECS[int(C.RPG_TOOL)]
    assert s.behavior == "contact"
    assert s.gravity_mult == 0.05
    assert s.damage == 140
    assert s.block_damage == 5
    s2 = PROJECTILE_SPECS[int(C.RPG2_TOOL)]
    assert s2.gravity_mult == 0.025 and s2.damage == 50 and s2.block_damage == 2
    d = PROJECTILE_SPECS[int(C.DRILLGUN_TOOL)]
    assert d.lifespan == 3.0 and d.destroyed_damage == 95
    sb = PROJECTILE_SPECS[int(C.SNOWBLOWER_TOOL)]
    assert sb.damage == 10 and sb.block_damage == 0


def test_late_explosive_specs_match_recovered_client_constants():
    chemical = PROJECTILE_SPECS[int(C.CHEMICALBOMB_TOOL)]
    sticky = PROJECTILE_SPECS[int(C.STICKY_GRENADE_TOOL)]
    launcher = PROJECTILE_SPECS[int(C.GRENADE_LAUNCHER_WEAPON_TOOL)]
    mine = PROJECTILE_SPECS[int(C.MINE_LAUNCHER_TOOL)]

    assert (chemical.behavior, chemical.damage, chemical.block_damage,
            chemical.blast_radius) == ("contact", 50.0, 3.0, 3.0)
    assert (sticky.damage, sticky.block_damage, sticky.blast_radius) == (200.0, 6.0, 5.0)
    assert (launcher.behavior, launcher.damage, launcher.block_damage,
            launcher.blast_radius, launcher.lifespan) == ("contact", 100.0, 6.0, 4.0, 3.0)
    assert (mine.behavior, mine.damage, mine.block_damage,
            mine.blast_radius) == ("deploy", 100.0, 15.0, 6.0)
    assert chemical.entity_type == C.CHEMICALBOMB_ENTITY == 32
    assert launcher.entity_type == C.GRENADE_LAUNCHER_ENTITY == 33
    assert sticky.entity_type == C.STICKY_GRENADE_ENTITY == 34
    assert mine.entity_type == C.PROJECTILE_MINE_ENTITY == 37


def test_projectile_knockback_matches_recovered_client_wrappers():
    grenade = PROJECTILE_SPECS[int(C.GRENADE_TOOL)]
    rocket = PROJECTILE_SPECS[int(C.RPG_TOOL)]
    rocket2 = PROJECTILE_SPECS[int(C.RPG2_TOOL)]
    sticky = PROJECTILE_SPECS[int(C.STICKY_GRENADE_TOOL)]
    drill = PROJECTILE_SPECS[int(C.DRILLGUN_TOOL)]

    assert (grenade.blast_radius, grenade.knockback_min,
            grenade.knockback_max) == (4.0, 0.5, 1.0)
    assert (rocket.blast_radius, rocket.knockback_min,
            rocket.knockback_max) == (6.0, 0.0, 0.25)
    assert (rocket2.self_knockback_min,
            rocket2.self_knockback_max) == (1.0, 1.5)
    assert (sticky.knockback_min, sticky.knockback_max) == (0.75, 0.1)
    assert (drill.destroyed_blast_radius, drill.destroyed_knockback_min,
            drill.destroyed_knockback_max) == (3.5, 0.1, 0.2)


def test_grenade_launcher_never_relays_crashing_use_oriented_packet():
    """The retail client's remote GLGrenade constructor is stale: it passes
    only position, velocity, and value into Entity.initialize(), which needs
    entity_id, team, player, and spawned.  The server must keep GL physics
    authoritative without echoing packet 10 into that client path."""
    server = BattleSpadesServer(ServerConfig())
    shooter = SimpleNamespace(id=1, name="shooter", team=0)
    observer = SimpleNamespace(id=2, name="observer", team=1)
    shooter_connection = RecordingConnection(shooter)
    observer_connection = RecordingConnection(observer)
    server.connections = {1: shooter_connection, 2: observer_connection}

    packet = SimpleNamespace(
        tool=int(C.GRENADE_LAUNCHER_WEAPON_TOOL),
        position=(100.0, 100.0, 30.0),
        velocity=(75.0, 0.0, 0.0),
        value=3.0,
    )
    assert server.spawn_grenade(shooter, packet) is True

    assert len(server.projectile_engine.projectiles) == 1
    assert all(data[0] != 10 for data in observer_connection.sent)
    creates = [
        CreateEntity(ByteReader(data[1:])).entity
        for data in observer_connection.sent
        if data[0] == CreateEntity.id
    ]
    assert len(creates) == 1
    assert creates[0].type == C.GRENADE_LAUNCHER_ENTITY == 33
    assert creates[0].fuse == 3.0


def test_block_cannon_projectile_snapshots_palette_and_source_loop():
    """In-flight shots retain the firing colour even if the palette changes."""
    server = BattleSpadesServer(ServerConfig())
    shooter = SimpleNamespace(
        id=1,
        name="engineer",
        team=0,
        block_color=0x2468AC,
    )
    observer = RecordingConnection(SimpleNamespace(id=2, team=1))
    server.connections = {2: observer}
    packet = SimpleNamespace(
        loop_count=444,
        tool=int(C.SNOWBLOWER_TOOL),
        position=(100.0, 100.0, 30.0),
        velocity=(50.0, 0.0, 0.0),
        value=0.0,
    )

    assert server.spawn_grenade(shooter, packet) is True

    projectile = server.projectile_engine.projectiles[0]
    assert projectile.block_color == 0x2468AC
    assert projectile.source_loop == 444
    create_data = next(
        data for data in observer.sent if data[0] == CreateEntity.id
    )
    entity = CreateEntity(ByteReader(create_data[1:])).entity
    assert entity.type == C.SNOWBALL_ENTITY
    assert entity.color == (0x24, 0x68, 0xAC)


def test_mine_launcher_replaces_flying_visual_with_armed_landmine():
    server = BattleSpadesServer(ServerConfig())
    owner = SimpleNamespace(id=1, name="engineer", team=0)
    observer = SimpleNamespace(id=2, name="observer", team=1)
    owner_connection = RecordingConnection(owner)
    observer_connection = RecordingConnection(observer)
    server.players = {owner.id: owner}
    server.connections = {1: owner_connection, 2: observer_connection}
    packet = SimpleNamespace(
        tool=int(C.MINE_LAUNCHER_TOOL),
        position=(100.0, 100.0, 30.0),
        velocity=(75.0, 0.0, 0.0),
        value=0.0,
    )

    server.spawn_grenade(owner, packet)
    projectile = server.projectile_engine.projectiles[0]
    flight_id = projectile.entity_id
    server._deploy_launched_mine(ProjectileDeployment(projectile))

    assert server.entity_registry.get(flight_id) is None
    assert [entity.type for entity in server.entity_registry.all()] == [
        C.LANDMINE_ENTITY
    ]
    destroyed = [
        DestroyEntity(ByteReader(data[1:])).entity_id
        for data in observer_connection.sent
        if data[0] == DestroyEntity.id
    ]
    assert destroyed == [flight_id]


def test_unknown_tool_not_spawned():
    eng = ProjectileEngine()
    assert eng.spawn(int(C.BLOCK_TOOL), (0, 0, 0), (1, 0, 0), 3.0, 1) is None
    assert eng.projectiles == []


def test_oriented_projectile_requires_exact_held_normalized_tool():
    calls = []
    server = SimpleNamespace(spawn_grenade=lambda player, packet: calls.append(packet.tool))
    player = SimpleNamespace(
        alive=True,
        spawned=True,
        tool=int(C.RPG_TOOL),
        loadout=[int(C.RPG_TOOL)],
        disguised=True,
    )
    forged = SimpleNamespace(tool=int(C.SNOWBLOWER_TOOL))

    asyncio.run(handle_oriented_item(server, player, forged))

    assert calls == []
    assert player.disguised is True

    valid = SimpleNamespace(tool=int(C.RPG_TOOL))
    asyncio.run(handle_oriented_item(server, player, valid))

    assert calls == [int(C.RPG_TOOL)]
    assert player.disguised is False


def test_bot_oriented_action_uses_normal_stock_and_projectile_replication():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    connection = RecordingConnection()
    connection.server = server
    player = Player(0, "RocketBot", TEAM1, C.RPG_TOOL, connection)
    connection.player = player
    player.is_bot = True
    player.class_id = int(C.CLASS_SOLDIER)
    player.loadout = [int(C.RPG_TOOL)]
    player.spawn(100.5, 100.5, 59.75)
    player.set_orientation_vector(1.0, 0.0, 0.0)
    player.set_tool(int(C.RPG_TOOL), raw=True)
    server.players[player.id] = player
    server.connections[player.id] = connection
    server.teams[TEAM1].add_player(player)
    before = player.oriented_stock[int(C.RPG_TOOL)]

    accepted = BotActionGateway(server).execute(
        player,
        BotAction(BotActionKind.ORIENTED, tool_id=int(C.RPG_TOOL)),
    )

    assert accepted is True
    assert player.oriented_stock[int(C.RPG_TOOL)] == before - 1
    assert len(server.projectile_engine.projectiles) == 1
    assert any(entity.type == C.ROCKET_ENTITY for entity in server.entity_registry.all())


def test_oriented_inventory_matches_late_weapon_stock_and_snowblower_blocks():
    """Late launcher ammo is one loaded round plus initial reserve; the
    Snowblower is the exception and spends the shared block wallet."""
    player = Player.__new__(Player)
    player.blocks = 2
    Player._reset_equipment_state(player)

    assert player.oriented_stock[int(C.GRENADE_LAUNCHER_WEAPON_TOOL)] == 4
    assert player.oriented_stock[int(C.MINE_LAUNCHER_TOOL)] == 4
    assert player.oriented_stock[int(C.CHEMICALBOMB_TOOL)] == 2
    assert player.oriented_stock[int(C.STICKY_GRENADE_TOOL)] == 2

    assert Player.consume_oriented_item(player, C.SNOWBLOWER_TOOL, now=10.0)
    assert player.blocks == 1
    assert not Player.can_use_oriented_item(player, C.SNOWBLOWER_TOOL, now=10.1)
    assert Player.can_use_oriented_item(player, C.SNOWBLOWER_TOOL, now=10.21)

    assert Player.consume_oriented_item(player, C.MINE_LAUNCHER_TOOL, now=20.0)
    assert player.oriented_stock[int(C.MINE_LAUNCHER_TOOL)] == 3


def test_invalid_oriented_payload_does_not_consume_server_inventory():
    server = BattleSpadesServer(ServerConfig())
    player = Player.__new__(Player)
    player.id = 1
    player.alive = True
    player.spawned = True
    player.tool = int(C.MINE_LAUNCHER_TOOL)
    player.loadout = [int(C.MINE_LAUNCHER_TOOL)]
    player.disguised = True
    player.blocks = 10
    Player._reset_equipment_state(player)
    before = player.oriented_stock[int(C.MINE_LAUNCHER_TOOL)]
    malformed = SimpleNamespace(
        tool=int(C.MINE_LAUNCHER_TOOL),
        position=(float("nan"), 0.0, 0.0),
        velocity=(75.0, 0.0, 0.0),
        value=0.0,
    )

    asyncio.run(handle_oriented_item(server, player, malformed))

    assert player.oriented_stock[int(C.MINE_LAUNCHER_TOOL)] == before
    assert player.disguised is True
    assert server.projectile_engine.projectiles == []


# --- rocket flight ----------------------------------------------------------

def test_rocket_flies_straight_and_hits_wall():
    eng = ProjectileEngine()
    # Fired at 75 u/s along +x toward a wall 30 blocks away.
    eng.spawn(int(C.RPG_TOOL), (100.0, 100.0, 30.0), (75.0, 0.0, 0.0), 0.0, 1, now=0.0)
    world = WallWorld(130)
    explosions = []
    t = 0.0
    for _ in range(120):  # 2 seconds max
        t += DT
        explosions = eng.update(DT, world, now=t)
        if explosions:
            break
    assert len(explosions) == 1
    ex = explosions[0]
    # Exploded AT the wall face (last free position < 130) after ~0.4s.
    assert 128.5 <= ex.x < 130.0
    assert ex.spec.name == "rocket"
    assert ex.damage == 140
    assert eng.projectiles == []  # consumed


def test_rocket_low_gravity_drop():
    """0.05x gravity: after 1s of flight the rocket drops far less than a
    grenade would (30*0.5 = 15 blocks); it should sink ~0.75 blocks."""
    eng = ProjectileEngine()
    eng.spawn(int(C.RPG_TOOL), (100.0, 100.0, 30.0), (75.0, 0.0, 0.0), 0.0, 1, now=0.0)
    world = OpenWorld()
    t = 0.0
    for _ in range(60):
        t += DT
        eng.update(DT, world, now=t)
    p = eng.projectiles[0]
    drop = p.z - 30.0
    assert 0.4 < drop < 1.2, f"rocket dropped {drop}"


def test_rocket_no_tunneling_through_thin_wall():
    """150 u/s RPG2 travels 2.5 blocks/tick — sub-stepping must still catch a
    1-block-thin wall."""
    class ThinWall:
        def get_solid(self, x, y, z):
            return x == 120  # exactly one block column

    eng = ProjectileEngine()
    eng.spawn(int(C.RPG2_TOOL), (110.2, 100.0, 30.0), (150.0, 0.0, 0.0), 0.0, 1, now=0.0)
    explosions = []
    t = 0.0
    for _ in range(30):
        t += DT
        explosions = eng.update(DT, ThinWall(), now=t)
        if explosions:
            break
    assert len(explosions) == 1
    assert explosions[0].x < 120.0


def test_rocket_sweeps_into_player_before_wall():
    """Regression: rockets used to test voxels only and phase through every
    player. The swept body test must catch a target between tick endpoints."""
    eng = ProjectileEngine()
    eng.spawn(
        int(C.RPG2_TOOL), (100.0, 100.0, 30.0), (150.0, 0.0, 0.0),
        0.0, 1, now=0.0,
    )
    target = SimpleNamespace(
        id=2, alive=True, spawned=True,
        x=106.0, y=100.0, z=29.0,
        input=SimpleNamespace(crouch=False),
    )
    explosions = []
    t = 0.0
    for _ in range(10):
        t += DT
        explosions = eng.update(DT, OpenWorld(), now=t, players=[target])
        if explosions:
            break
    assert len(explosions) == 1
    assert explosions[0].x < target.x


def test_contact_projectile_does_not_hit_its_owner_body():
    eng = ProjectileEngine()
    eng.spawn(
        int(C.RPG_TOOL), (100.0, 100.0, 30.0), (75.0, 0.0, 0.0),
        0.0, 1, now=0.0,
    )
    owner = SimpleNamespace(
        id=1, alive=True, spawned=True,
        x=100.0, y=100.0, z=29.0,
        input=SimpleNamespace(crouch=False),
    )
    assert eng.update(DT, OpenWorld(), now=DT, players=[owner]) == []
    assert len(eng.projectiles) == 1


def test_mine_launcher_contact_deploys_instead_of_exploding():
    eng = ProjectileEngine()
    eng.spawn(
        int(C.MINE_LAUNCHER_TOOL),
        (100.0, 100.0, 30.0), (75.0, 0.0, 0.0),
        0.0, 1, now=0.0,
    )
    events = []
    t = 0.0
    for _ in range(30):
        t += DT
        events = eng.update(DT, WallWorld(110), now=t)
        if events:
            break
    assert len(events) == 1
    assert isinstance(events[0], ProjectileDeployment)
    assert events[0].x < 110.0


# --- drill ------------------------------------------------------------------

def test_drill_lifespan_uses_destroyed_blast():
    eng = ProjectileEngine()
    eng.spawn(int(C.DRILLGUN_TOOL), (100.0, 100.0, 30.0), (0.0, 20.0, -45.0), 0.0, 1, now=0.0)
    world = OpenWorld()
    explosions = []
    t = 0.0
    for _ in range(60 * 4):  # lifespan is 3s
        t += DT
        explosions = eng.update(DT, world, now=t)
        if explosions:
            break
    assert len(explosions) == 1
    ex = explosions[0]
    assert ex.damage == 95            # DESTROYED blast, not the contact 50
    assert ex.block_damage == 10.0
    assert 2.9 <= t <= 3.1


def test_drill_contact_damages_block_and_keeps_flying():
    eng = ProjectileEngine()
    eng.spawn(int(C.DRILLGUN_TOOL), (100.0, 100.0, 30.0), (20.0, 0.0, 0.0), 0.0, 1, now=0.0)
    events = []
    t = 0.0
    for _ in range(120):
        t += DT
        events = eng.update(DT, WallWorld(110), now=t)
        if events:
            break
    assert len(events) == 1
    assert isinstance(events[0], DrillContact)
    assert events[0].block[0] == 110
    assert len(eng.projectiles) == 1


def test_live_drill_contact_bores_measured_81_cells_with_compact_packet():
    """Live clients get one native packet; join catch-up gets exact cells."""

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    connection = RecordingConnection()
    connection.server = server
    owner = Player(3, "Miner", TEAM1, C.DRILLGUN_TOOL, connection)
    connection.player = owner
    server.players[owner.id] = owner
    server.connections[owner.id] = connection

    block = (100, 100, 50)
    footprint = set(drill_contact_cells(block))
    assert len(footprint) == 81
    for position in footprint:
        server.world_manager.set_block(*position, True, 0x123456)
    assert all(server.world_manager.get_solid(*pos) for pos in footprint)
    server.world_manager.find_unsupported_chunks = lambda _positions: []

    joining = RecordingConnection(in_game=False)
    joining.map_mutation_watermark = server._map_mutation_sequence
    joining.map_mutation_overflow = False
    server.connections["joining"] = joining

    projectile = server.projectile_engine.spawn(
        int(C.DRILLGUN_TOOL),
        (99.0, 100.0, float(block[2])),
        (20.0, 0.0, 0.0),
        0.0,
        owner.id,
        now=0.0,
    )
    entity = server.entity_registry.place(
        int(C.DRILL_ENTITY),
        99.0,
        100.0,
        float(block[2]),
        kind="projectile",
        player_id=owner.id,
    )
    # Entity id zero is legal and previously fell through ``eid or owner.id``.
    assert entity.entity_id == 0
    projectile.entity_id = entity.entity_id

    server._apply_drill_contact(DrillContact(projectile, block))

    assert all(not server.world_manager.get_solid(*pos) for pos in footprint)
    live_damage = [data for data in connection.sent if data[0] == Damage.id]
    assert len(live_damage) == 1
    damage = Damage(ByteReader(live_damage[0][1:]))
    assert damage.type == int(C.DRILL_DAMAGE)
    assert damage.damage == C.DRILL_DRILLING_BLOCK_DAMAGE
    assert damage.causer_id == entity.entity_id
    assert damage.chunk_check == 1
    assert damage.position == tuple(float(value) for value in block)

    catchup_packets = [
        Damage(ByteReader(data[1:]))
        for _sequence, data in server._map_mutation_journal
    ]
    assert len(catchup_packets) == 81
    assert {packet.type for packet in catchup_packets} == {
        int(C.WEAPON_DAMAGE)
    }
    assert {
        tuple(int(value) for value in packet.position)
        for packet in catchup_packets
    } == footprint


def test_drill_contact_without_live_entity_falls_back_to_exact_safe_cell():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    connection = RecordingConnection()
    connection.server = server
    owner = Player(3, "Miner", TEAM1, C.DRILLGUN_TOOL, connection)
    connection.player = owner
    server.players[owner.id] = owner
    server.connections[owner.id] = connection
    server.world_manager.find_unsupported_chunks = lambda _positions: []

    block = (100, 100, 50)
    server.world_manager.set_block(*block, True, 0x123456)
    projectile = server.projectile_engine.spawn(
        int(C.DRILLGUN_TOOL),
        (99.0, 100.0, 50.0),
        (20.0, 0.0, 0.0),
        0.0,
        owner.id,
        now=0.0,
    )

    server._apply_drill_contact(DrillContact(projectile, block))

    assert not server.world_manager.get_solid(*block)
    damage_packets = [
        Damage(ByteReader(data[1:]))
        for data in connection.sent
        if data[0] == Damage.id
    ]
    assert len(damage_packets) == 1
    assert damage_packets[0].type == int(C.WEAPON_DAMAGE)
    assert damage_packets[0].causer_id == owner.id


def test_drill_explodes_on_player_contact():
    eng = ProjectileEngine()
    eng.spawn(
        int(C.DRILLGUN_TOOL), (100.0, 100.0, 30.0), (20.0, 0.0, 0.0),
        0.0, 1, now=0.0,
    )
    target = SimpleNamespace(
        id=2, alive=True, spawned=True,
        x=105.0, y=100.0, z=29.0,
        input=SimpleNamespace(crouch=False),
    )
    events = []
    t = 0.0
    for _ in range(60):
        t += DT
        events = eng.update(DT, OpenWorld(), now=t, players=[target])
        if events:
            break
    assert len(events) == 1
    assert events[0].damage == 50
    assert eng.projectiles == []


# --- grenade regression -----------------------------------------------------

def test_grenade_bounces_and_explodes_on_fuse():
    """Legacy math regression: a grenade dropped on a floor bounces (damped)
    and explodes when the fuse expires — never on contact."""
    eng = ProjectileEngine()
    eng.spawn(int(C.GRENADE_TOOL), (100.0, 100.0, 58.0), (0.0, 0.0, 5.0), 2.0, 1, now=0.0)
    world = FloorWorld(60)
    explosions = []
    bounced_up = False
    t = 0.0
    for _ in range(240):
        t += DT
        explosions = eng.update(DT, world, now=t)
        if eng.projectiles and eng.projectiles[0].vz < 0:
            bounced_up = True  # velocity reflected upward (negative z = up)
        if explosions:
            break
    assert bounced_up, "grenade never bounced"
    assert len(explosions) == 1
    assert 1.9 <= t <= 2.1  # fuse-timed, not contact
    assert explosions[0].spec.name == "grenade"


def test_zero_fuse_grenade_explodes_immediately():
    eng = ProjectileEngine()
    eng.spawn(int(C.GRENADE_TOOL), (100.0, 100.0, 30.0), (0.0, 0.0, 0.0), 0.0, 1, now=0.0)
    explosions = eng.update(DT, OpenWorld(), now=0.01)
    assert len(explosions) == 1


# --- sticky -----------------------------------------------------------------

def test_sticky_zero_fuse_arms_on_stick():
    """The real client sends value=0 for stickies (measured live 2026-07-07):
    the fuse must arm at IMPACT, not at throw — and never instantly."""
    from server.projectiles import STICK_ARM_SECONDS
    eng = ProjectileEngine()
    eng.spawn(int(C.STICKY_GRENADE_TOOL), (100.0, 100.0, 30.0), (40.0, 0.0, 0.0), 0.0, 1, now=0.0)
    world = WallWorld(105)
    t = 0.0
    stuck_at = None
    explosions = []
    for _ in range(600):
        t += DT
        explosions = eng.update(DT, world, now=t)
        if explosions:
            break
        if eng.projectiles[0].stuck and stuck_at is None:
            stuck_at = t
    assert stuck_at is not None and stuck_at > 0.05   # did NOT explode at throw
    assert len(explosions) == 1
    assert abs((t - stuck_at) - STICK_ARM_SECONDS) < 0.1


def test_sticky_anchors_on_contact_then_fuse_fires():
    eng = ProjectileEngine()
    eng.spawn(int(C.STICKY_GRENADE_TOOL), (100.0, 100.0, 30.0), (40.0, 0.0, 0.0), 2.0, 1, now=0.0)
    world = WallWorld(105)
    t = 0.0
    stuck_pos = None
    explosions = []
    for _ in range(240):
        t += DT
        explosions = eng.update(DT, world, now=t)
        if explosions:
            break
        p = eng.projectiles[0]
        if p.stuck and stuck_pos is None:
            stuck_pos = (p.x, p.y, p.z)
        elif p.stuck:
            assert (p.x, p.y, p.z) == stuck_pos  # anchored, not sliding
    assert stuck_pos is not None, "sticky never stuck"
    assert len(explosions) == 1
    assert 1.9 <= t <= 2.1


def test_sticky_attaches_to_and_follows_player_until_stock_fuse():
    eng = ProjectileEngine()
    eng.spawn(
        int(C.STICKY_GRENADE_TOOL),
        (100.0, 100.0, 30.0), (40.0, 0.0, 0.0),
        0.0, 1, now=0.0,
    )
    target = SimpleNamespace(
        id=2, alive=True, spawned=True,
        x=105.0, y=100.0, z=29.0,
        input=SimpleNamespace(crouch=False),
    )
    t = 0.0
    stuck_at = None
    events = []
    for _ in range(60 * 7):
        t += DT
        events = eng.update(DT, OpenWorld(), now=t, players=[target])
        if eng.projectiles and eng.projectiles[0].attached_player_id == target.id:
            if stuck_at is None:
                stuck_at = t
                target.x = 110.0
            elif not events:
                assert abs(eng.projectiles[0].x - target.x) < 1e-6
        if events:
            break

    assert stuck_at is not None
    assert len(events) == 1
    assert events[0].spec.name == "sticky_grenade"
    assert abs(events[0].x - target.x) < 1e-6
    assert abs((t - stuck_at) - C.STICKY_GRENADE_STICK_FUSE) < 0.1
