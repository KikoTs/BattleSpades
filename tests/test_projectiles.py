"""Projectile engine tests: rocket/drill/snowball contact flight, grenade
bounce regression, sticky anchoring, and spec-table integrity.

Ground truth: docs/CONTENT_TABLES.md + the 2026-07-07 client extraction
(ROCKET 75u/s g*0.05 blast 140/5, ROCKET2 150u/s g*0.025 50/2, DRILL 20u/s
g*1.5 lifespan 3s 50/5 -> destroyed 95/10, SNOWBALL 50u/s g*0.5 10/0).
"""
from types import SimpleNamespace

import shared.constants as C
from server.projectiles import (
    PROJECTILE_SPECS, DrillContact, ProjectileDeployment, ProjectileEngine,
    BASE_GRAVITY, BOUNCE_DAMP,
)

DT = 1.0 / 60.0


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
            mine.blast_radius) == ("deploy", 100.0, 15.0, 3.0)


def test_unknown_tool_not_spawned():
    eng = ProjectileEngine()
    assert eng.spawn(int(C.BLOCK_TOOL), (0, 0, 0), (1, 0, 0), 3.0, 1) is None
    assert eng.projectiles == []


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
