"""Retail supply-crate air drop (see docs/CRATES_CLASSES_RETAIL.md).

The reference numbers are live measurements of the stock client's ``Crate``
(``aoslib/scenes/main/crate.py`` in ``gameScene.pyd``) taken 2026-09-26 by
creating an AmmoCrate in the air on a running client and sampling its
``world_object`` every 0.05 s.
"""

from __future__ import annotations

from types import SimpleNamespace

import shared.constants as C

from server.entities.behaviors import PickupCrateBehavior
from server.entities.registry import EntityContext, EntityRegistry
from server.map_metadata import MapMetadata
from server.map_resources import (
    CRATE_DROP_START_Z,
    CRATE_FLYBY_ATTENUATION,
    MapResourceService,
)


class _Column:
    """Flat ground whose top solid voxel is ``ground`` (AoS +Z is down)."""

    def __init__(self, ground=215, roof=None):
        self.ground = int(ground)
        self.roof = roof

    def get_solid(self, x, y, z):
        if self.roof is not None and int(z) == int(self.roof):
            return True
        return int(z) >= self.ground


class _Player:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z
        self.team = 0
        self.refills = 0


def _ctx(now, world, players=()):
    created, destroyed = [], []
    ctx = EntityContext(
        dt=1 / 60, now=now, players=list(players), world=world,
        create=created.append, destroy=destroyed.append, move=None,
    )
    ctx._created = created
    ctx._destroyed = destroyed
    return ctx


def _dropping_crate(
    world, *, start_z=1.0, cue=None, ground=215,
    x=128.5, y=252.5,
):
    registry = EntityRegistry()
    behavior = PickupCrateBehavior(
        lambda player: setattr(player, "refills", player.refills + 1),
        respawn_delay=float(C.CRATE_SPAWN_DELAY),
        airdrop=True,
        drop_start_z=start_z,
        drop_cue=cue,
    )
    crate = registry.place(
        int(C.AMMO_CRATE), float(x), float(y), float(ground), behavior=behavior,
    )
    crate.alive = False
    crate.respawn_at = 1000.0
    return registry, crate


def _run_until_landed(registry, crate, world, *, start=1000.0, limit=30.0):
    now = start
    registry.tick(_ctx(now, world))
    track = {}
    while crate.falling and now - start < limit:
        now += 1 / 60
        registry.tick(_ctx(now, world))
        track[round(now - start, 2)] = crate.z
    return now - start, track


def test_respawn_lifts_crate_into_the_sky_and_plays_one_flyby_cue():
    world = _Column(215)
    cues = []
    registry, crate = _dropping_crate(world, cue=cues.append)

    ctx = _ctx(1000.0, world)
    registry.tick(ctx)

    assert crate.alive is True
    assert crate.falling is True
    assert crate.z == 1.0
    assert crate.vel == (0.0, 0.0, 0.0)
    assert ctx._created == [crate]
    assert cues == [crate]
    # The CreateEntity carries the sky position, which is what makes the
    # stock client run its own freefall/parachute animation.
    wire = crate.to_wire_entity()
    assert wire.pos_z == 1.0


def test_fall_matches_the_live_client_crate_to_within_a_frame():
    world = _Column(215)
    registry, crate = _dropping_crate(world, start_z=1.0)

    landed_after, track = _run_until_landed(registry, crate, world)

    # Live client, same column: landed ~5.1 s after creation; 210.88 at
    # 3.84 s (chute open), 212.22 at 4.36 s, 213.24 at 4.87 s (chute gone).
    assert 4.95 <= landed_after <= 5.2
    def at(t):
        return track[min(track, key=lambda key: abs(key - t))]

    assert abs(at(3.84) - 210.88) < 0.5
    assert abs(at(4.36) - 212.22) < 0.2
    assert abs(at(4.87) - 213.24) < 0.2
    assert crate.z == 215.0
    assert crate.terrain_support_z == 215
    assert crate.vel == (0.0, 0.0, 0.0)
    assert crate.parachute_deployed and crate.parachute_removed


def test_parachute_terminal_speed_and_deploy_window():
    world = _Column(213)
    registry, crate = _dropping_crate(world, start_z=100.0, ground=213)
    now = 1000.0
    registry.tick(_ctx(now, world))
    deployed_at = None
    speeds = []
    while crate.falling:
        now += 1 / 60
        registry.tick(_ctx(now, world))
        if crate.parachute_deployed and deployed_at is None:
            deployed_at = crate.z
        if crate.parachute_deployed and not crate.parachute_removed:
            speeds.append(crate.vel[2])
    # Chute opens inside CRATE_PARACHUTE_DEPLOYMENT_HEIGHT of the ground and
    # the stored speed settles at the client's 1.5 blocks/s (0.75 per frame).
    assert 213 - float(C.CRATE_PARACHUTE_DEPLOYMENT_HEIGHT) <= deployed_at < 213
    assert abs(speeds[-1] - 1.5) < 1e-3


def test_drop_point_under_a_roof_starts_just_below_it():
    world = _Column(215, roof=180)
    registry, crate = _dropping_crate(world, start_z=1.0)

    registry.tick(_ctx(1000.0, world))

    assert crate.z == 182.0
    assert crate.falling is True
    _run_until_landed(registry, crate, world, start=1000.0)
    assert crate.z == 180.0


def test_player_built_platform_existing_before_respawn_catches_the_crate():
    class PlayerPlatform(_Column):
        def __init__(self):
            super().__init__(215)
            self.extra = {190}
            self.runtime_solid_cells = {(128, 252, 190)}

        def get_solid(self, x, y, z):
            return int(z) in self.extra or super().get_solid(x, y, z)

    world = PlayerPlatform()
    registry, crate = _dropping_crate(
        world, x=128.375, y=252.625,
    )

    registry.tick(_ctx(1000.0, world))

    # Unlike an authored roof, a runtime platform must remain below the sky
    # start so both client and server can collide with it during the fall.
    assert crate.z == 1.0
    assert (crate.x, crate.y) == (128.375, 252.625)
    _run_until_landed(registry, crate, world, start=1000.0)
    assert crate.z == 190.0
    assert crate.terrain_support_z == 190
    assert crate.home == (128.375, 252.625, 215.0)


def test_a_falling_crate_is_joined_with_its_live_fall_speed():
    world = _Column(215)
    registry, crate = _dropping_crate(world)
    now = 1000.0
    registry.tick(_ctx(now, world))
    for _ in range(60):
        now += 1 / 60
        registry.tick(_ctx(now, world))

    wire = crate.to_wire_entity()
    assert crate.falling is True
    assert 1.0 < wire.pos_z < 215.0
    assert wire.vel_z > 20.0
    assert crate in registry.static_entities()


def test_a_player_can_catch_the_crate_before_it_lands():
    world = _Column(215)
    registry, crate = _dropping_crate(world)
    now = 1000.0
    registry.tick(_ctx(now, world))
    for _ in range(200):
        now += 1 / 60
        registry.tick(_ctx(now, world))
    assert crate.falling is True
    catcher = _Player(crate.x, crate.y, crate.z + 1.0)

    ctx = _ctx(now + 1 / 60, world, players=[catcher])
    registry.tick(ctx)

    assert catcher.refills == 1
    assert crate.alive is False
    assert ctx._destroyed == [crate.entity_id]
    assert crate.respawn_at == now + 1 / 60 + float(C.CRATE_SPAWN_DELAY)


def test_crate_without_airdrop_still_respawns_in_place():
    world = _Column(215)
    registry = EntityRegistry()
    behavior = PickupCrateBehavior(lambda player: None, respawn_delay=25.0)
    crate = registry.place(int(C.AMMO_CRATE), 10.5, 10.5, 215.0, behavior=behavior)
    crate.alive = False
    crate.respawn_at = 1000.0

    registry.tick(_ctx(1000.0, world))

    assert crate.alive is True
    assert crate.falling is False
    assert crate.z == 215.0


# --- map service -------------------------------------------------------------


class _Map:
    source_z_shift = 0
    retail_marker_families = ()

    def get_solid(self, x, y, z):
        return int(z) >= 215


class _World:
    map_name = "Fixture"

    def __init__(self, skybox=None):
        self.map = _Map()
        self.map_metadata = MapMetadata(skybox_name=skybox)

    def get_solid(self, x, y, z):
        return self.map.get_solid(x, y, z)

    def team_base_anchor(self, team):
        return (64.0, 100.0, 215.0) if team == 0 else (448.0, 400.0, 215.0)

    def dry_surface_anchor(self, x, y, search=24):
        return (float(x), float(y), 215.0)


class _Server:
    def __init__(self, skybox=None):
        self.config = SimpleNamespace(entities_wire_ready=True)
        self.world_manager = _World(skybox)
        self.entity_registry = EntityRegistry()
        self.sent = []
        self.created = []

    def broadcast(self, data, **kwargs):
        self.sent.append(bytes(data))

    def broadcast_create_entity(self, entity):
        self.created.append(entity)

    def broadcast_destroy_entity(self, entity_id):
        pass


def test_map_crates_air_drop_with_the_retail_respawn_delay():
    server = _Server()
    MapResourceService(server).rebuild()

    crates = [e for e in server.entity_registry.all() if e.kind.startswith("map_")]
    assert crates
    for crate in crates:
        assert crate.behavior.airdrop is True
        assert crate.behavior.drop_start_z == CRATE_DROP_START_Z
        assert crate.behavior.respawn_delay == float(C.CRATE_SPAWN_DELAY) == 25.0
        # The round's first crates are already on the ground.
        assert crate.falling is False


def test_flyby_sound_follows_the_map_theme():
    from server.audio import (
        SND_CRATEDROP_FLYBY_POS,
        SND_CRATEDROP_FLYBY_POS_WW,
        SND_CRATEDROP_FLYBY_SPACE_POS,
    )

    assert MapResourceService(_Server()).crate_flyby_sound() == SND_CRATEDROP_FLYBY_POS
    assert MapResourceService(_Server("WW1.txt")).crate_flyby_sound() == (
        SND_CRATEDROP_FLYBY_POS_WW
    )
    assert MapResourceService(_Server("LunarBase.txt")).crate_flyby_sound() == (
        SND_CRATEDROP_FLYBY_SPACE_POS
    )
    assert (SND_CRATEDROP_FLYBY_POS_WW, SND_CRATEDROP_FLYBY_POS,
            SND_CRATEDROP_FLYBY_SPACE_POS) == (24, 25, 26)


def test_respawned_map_crate_broadcasts_a_positioned_flyby():
    from shared.bytes import ByteReader
    from shared.packet import PlaySound

    server = _Server()
    service = MapResourceService(server)
    service.rebuild()
    crate = next(e for e in server.entity_registry.all() if e.kind == "map_ammo")
    crate.alive = False
    crate.respawn_at = 1000.0
    ctx = _ctx(1000.0, server.world_manager)
    ctx.create = server.broadcast_create_entity

    server.entity_registry.tick(ctx)

    assert crate.falling is True and crate.z == CRATE_DROP_START_Z
    assert len(server.sent) == 1
    packet = PlaySound(ByteReader(server.sent[0][1:]))
    assert packet.sound_id == 25
    assert packet.positioned
    assert (packet.x, packet.y) == (crate.x, crate.y)
    assert abs(packet.attenuation - CRATE_FLYBY_ATTENUATION) < 1e-3


def test_blocks_built_on_the_drop_point_become_the_landing_surface():
    class Built(_Column):
        def get_solid(self, x, y, z):
            return 211 <= int(z) or super().get_solid(x, y, z)

    world = Built(215)
    registry, crate = _dropping_crate(world)

    _run_until_landed(registry, crate, world)

    assert crate.z == 211.0
    assert crate.terrain_support_z == 211


def test_water_drop_keeps_exact_xy_and_uses_the_water_surface():
    class WaterColumn:
        def __init__(self):
            self.extra = set()

        def get_solid(self, x, y, z):
            return (
                int(z) == int(C.Z_ABOVE_WATERPLANE) + 1
                or int(z) in self.extra
            )

        def dry_surface_anchor(self, *args, **kwargs):
            raise AssertionError("a water crate must not search sideways for land")

    world = WaterColumn()
    expected_xy = (128.375, 252.625)
    registry, crate = _dropping_crate(
        world, x=expected_xy[0], y=expected_xy[1], ground=215,
    )

    landed_after, _track = _run_until_landed(registry, crate, world)

    assert (crate.x, crate.y) == expected_xy
    assert crate.home[:2] == expected_xy
    assert crate.z == float(C.Z_ABOVE_WATERPLANE) + 1.0
    assert crate.terrain_support_z == int(C.Z_ABOVE_WATERPLANE) + 1

    # Blocks built directly into a water crate lift it out of the entire
    # connected stack while preserving the exact authored horizontal point.
    world.extra.update({236, 237, 238})
    lift_ctx = _ctx(1000.0 + landed_after + 0.11, world)
    moved = []
    lift_ctx.move = moved.append
    registry.tick(lift_ctx)
    assert (crate.x, crate.y, crate.z) == (*expected_xy, 236.0)
    assert crate.home[:2] == expected_xy
    assert moved == [crate]


def test_platform_built_during_fall_catches_the_crate():
    class MutableColumn(_Column):
        def __init__(self, ground):
            super().__init__(ground)
            self.extra = set()

        def get_solid(self, x, y, z):
            return int(z) in self.extra or super().get_solid(x, y, z)

    world = MutableColumn(215)
    registry, crate = _dropping_crate(world)
    registry.tick(_ctx(1000.0, world))
    assert crate.terrain_support_z == 215

    now = 1000.0
    for _ in range(60):
        now += 1 / 60
        registry.tick(_ctx(now, world))
    assert 1.0 < crate.z < 190.0

    # The platform appears after CreateEntity, while the crate is in motion.
    world.extra.add(190)
    moved = []
    positions = []
    while crate.falling and now < 1030.0:
        now += 1 / 60
        tick_ctx = _ctx(now, world)
        tick_ctx.move = moved.append
        registry.tick(tick_ctx)
        positions.append(crate.z)

    assert crate.falling is False
    assert max(positions) == 190.0
    assert crate.z == 190.0
    assert crate.terrain_support_z == 190
    assert (crate.x, crate.y) == crate.home[:2]
    assert moved == [crate]


def test_stack_built_during_fall_cannot_entomb_the_crate():
    class MutableColumn(_Column):
        def __init__(self, ground):
            super().__init__(ground)
            self.extra = set()

        def get_solid(self, x, y, z):
            return int(z) in self.extra or super().get_solid(x, y, z)

    world = MutableColumn(215)
    registry, crate = _dropping_crate(world)
    registry.tick(_ctx(1000.0, world))
    assert crate.terrain_support_z == 215
    world.extra.update(range(211, 215))

    now = 1000.0
    moved = []
    positions = []
    while crate.falling and now < 1030.0:
        now += 1 / 60
        tick_ctx = _ctx(now, world)
        tick_ctx.move = moved.append
        registry.tick(tick_ctx)
        positions.append(crate.z)

    assert crate.falling is False
    assert max(positions) == 211.0
    assert (crate.x, crate.y) == crate.home[:2]
    assert crate.z == 211.0
    assert crate.terrain_support_z == 211
    assert moved == [crate]


def test_block_behind_falling_crate_lifts_it_as_soon_as_it_lands():
    class MutableColumn(_Column):
        def __init__(self):
            super().__init__(215)
            self.extra = set()

        def get_solid(self, x, y, z):
            return int(z) in self.extra or super().get_solid(x, y, z)

    world = MutableColumn()
    registry, crate = _dropping_crate(world)
    now = 1000.0
    registry.tick(_ctx(now, world))
    while crate.z <= 150.0:
        now += 1 / 60
        registry.tick(_ctx(now, world))

    world.extra.add(100)  # already behind the +Z-down direction of travel
    before = crate.z
    now += 1 / 60
    registry.tick(_ctx(now, world))
    assert crate.falling is True
    assert crate.z > before  # the airborne crate does not reverse its fall
    _run_until_landed(registry, crate, world, start=now)

    assert crate.z == 100.0
    assert crate.terrain_support_z == 100


def test_next_respawn_recalculates_z_after_the_crate_is_picked_up():
    class MutableColumn(_Column):
        def __init__(self, ground):
            super().__init__(ground)
            self.extra = set()

        def get_solid(self, x, y, z):
            return int(z) in self.extra or super().get_solid(x, y, z)

    world = MutableColumn(215)
    registry, crate = _dropping_crate(world)
    first_fall, _track = _run_until_landed(registry, crate, world)
    assert crate.z == 215.0

    pickup_at = 1000.0 + first_fall + 1 / 60
    player = _Player(crate.x, crate.y, crate.z)
    registry.tick(_ctx(pickup_at, world, players=[player]))
    assert crate.alive is False
    assert player.refills == 1

    # Terrain changed after pickup may affect the next life, but only its Z.
    world.extra.update(range(210, 215))
    respawn_at = crate.respawn_at
    registry.tick(_ctx(respawn_at, world))
    assert crate.falling is True
    assert crate.terrain_support_z == 210
    assert (crate.x, crate.y) == crate.home[:2]

    _run_until_landed(registry, crate, world, start=respawn_at)
    assert (crate.x, crate.y) == crate.home[:2]
    assert crate.z == 210.0
