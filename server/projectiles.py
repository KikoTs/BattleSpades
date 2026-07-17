"""Server-authoritative projectile engine.

Generalizes the live-verified grenade sim to every thrown/launched projectile.
Three behaviors, all fed from UseOrientedItem(10) — the client sends its own
predicted position + velocity (already orientation*speed for launchers) and we
rebroadcast the packet so every other client simulates the projectile locally:

  bounce  — grenade family: ballistic arc, wall/floor reflection with x0.36
            damping, explodes when the fuse expires. This is the EXACT math
            verified against the compiled client (decompiled mover
            sub_10011E90) — do not change it.
  contact — rocket family (RPG/RPG2/drill/snowball/molotov): flies straight
            under reduced/increased gravity, explodes on FIRST solid contact.
            Fast movers are sub-stepped so a 150 u/s rocket can't tunnel
            through a wall between ticks (ROCKET_COLLISION_RANGE = 0.5).
  stick   — sticky grenade: on first contact it anchors (velocity zeroed) and
            waits out its fuse.

Ground-truth constants extracted from the original client's constants.py
(docs/CONTENT_TABLES.md + extraction session 2026-07-07):
  ROCKET   75 u/s, gravity x0.05, blast 140, block 5
  ROCKET2 150 u/s, gravity x0.025, blast 50, block 2
  DRILL    20 u/s, gravity x1.5, lifespan 3.0s: contact blast 50/5,
           lifespan-expiry ("destroyed") blast 95/10
  SNOWBALL 50 u/s, gravity x0.5, blast 10, no block damage
Sticky/chemical-bomb blast numbers are NOT in the constant catalog (set in
compiled projectile code) — conservative grenade-family values are used and
flagged for live calibration.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Optional

import shared.constants as C

# Base gravity acceleration in projectile units (world_gravity(1.0) * 30,
# from the decompiled grenade mover). Per-type multipliers scale this.
BASE_GRAVITY = 30.0
MAX_SPEED = 511.98999
BOUNCE_DAMP = 0.36
# Finest collision step for fast contact projectiles (ROCKET_COLLISION_RANGE).
CONTACT_STEP = 0.5
# Sticky grenade: fuse arms when it sticks (client sends value=0; the real
# post-stick delay lives in compiled code — approximate, calibrate live).
STICK_ARM_SECONDS = float(getattr(C, "STICKY_GRENADE_STICK_FUSE", 5.0))
# Failsafe: a fuse-less projectile that never contacts anything dies here.
MAX_FLIGHT_SECONDS = 10.0

# LIVE-MEASURED 2026-07-15 against the retail BlockManager.  Both drill
# handlers delegate to handle_radius_damage(radius=2).  With the ordinary
# in-flight DRILL_DRILLING_BLOCK_DAMAGE=20, a fresh solid volume loses these
# 81 cells exactly (stable across seeds 0, 1, 123, and 255).  This is wider
# than the projectile's one-cell collision trace: the trace chooses the
# centre, while this footprint is the actual boring operation.
@lru_cache(maxsize=8)
def radius_damage_offsets(radius: int) -> tuple[tuple[int, int, int], ...]:
    """Return the retail BlockManager's spherical integer-cell footprint.

    ``handle_radius_damage`` tests voxel centers against ``radius + 0.5``.
    Radius two therefore contains the live-measured 81 Drill/Dynamite/C4
    cells, not the 125 cells in a full 5x5x5 cube.
    """

    radius = max(0, int(radius))
    limit_sq = (float(radius) + 0.5) ** 2
    return tuple(
        (dx, dy, dz)
        for dx in range(-radius, radius + 1)
        for dy in range(-radius, radius + 1)
        for dz in range(-radius, radius + 1)
        if dx * dx + dy * dy + dz * dz < limit_sq
    )


DRILL_CONTACT_OFFSETS = radius_damage_offsets(2)


def drill_contact_cells(block) -> tuple[tuple[int, int, int], ...]:
    """Return the retail Drill Gun's authoritative 81-voxel bore footprint."""

    x, y, z = (int(value) for value in block)
    return tuple((x + dx, y + dy, z + dz)
                 for dx, dy, dz in DRILL_CONTACT_OFFSETS)


def radius_damage_cells(
    center,
    radius: int,
) -> tuple[tuple[int, int, int], ...]:
    """Translate a native radius-damage footprint to canonical VXL cells."""

    x, y, z = (int(value) for value in center)
    return tuple(
        (x + dx, y + dy, z + dz)
        for dx, dy, dz in radius_damage_offsets(radius)
    )


@dataclass(frozen=True)
class ProjectileSpec:
    name: str
    behavior: str                 # 'bounce' | 'contact' | 'stick' | 'deploy'
    gravity_mult: float           # x BASE_GRAVITY
    damage: float                 # max player blast damage
    block_damage: float           # per-block damage in the crater cube
    kill_type: int
    damage_type: int
    lifespan: float = 0.0         # >0: explode this many seconds after spawn
    # drill only: bigger blast when the lifespan (not contact) triggers it
    destroyed_damage: float = 0.0
    destroyed_block_damage: float = 0.0
    approximate: bool = False     # numbers not in the client constant catalog
    # Client-rendered flying entity (ENTITY id) — the server spawns a
    # CreateEntity of this type so ALL clients see + simulate the projectile
    # (the firing client does NOT predict rockets locally). 0 = no entity;
    # such projectiles ride the rebroadcast UseOrientedItem instead (grenades,
    # which the client already renders as thrown grenades).
    entity_type: int = 0
    blast_radius: float = 4.0
    knockback_min: float = 0.0
    knockback_max: float = 0.0
    self_knockback_min: Optional[float] = None
    self_knockback_max: Optional[float] = None
    destroyed_blast_radius: float = 0.0
    destroyed_knockback_min: float = 0.0
    destroyed_knockback_max: float = 0.0


def _kill(name: str, default: int) -> int:
    return int(getattr(C.KILL, name, default))


_GRENADE_KILL = _kill("GRENADE_KILL", 3)

# Tool id -> spec. The bounce family keeps the legacy verified blast numbers
# (handled by the explosion path in main.py); damage listed for reference.
PROJECTILE_SPECS: dict[int, ProjectileSpec] = {
    int(C.GRENADE_TOOL): ProjectileSpec(
        "grenade", "bounce", 1.0,
        float(getattr(C, "GRENADE_EXPLOSION_DAMAGE", 230.0)),
        float(getattr(C, "GRENADE_EXPLOSION_BLOCK_DAMAGE", 4.0)),
        _GRENADE_KILL, 7,
        blast_radius=4.0,
        knockback_min=float(getattr(C, "GRENADE_EXPLOSION_KNOCKBACK_MIN", 0.5)),
        knockback_max=float(getattr(C, "GRENADE_EXPLOSION_KNOCKBACK_MAX", 1.0))),
    int(getattr(C, "CLASSIC_GRENADE_TOOL", 31)): ProjectileSpec(
        "classic_grenade", "bounce", 1.0,
        float(getattr(C, "CLASSIC_GRENADE_EXPLOSION_DAMAGE", 130.0)),
        float(getattr(C, "CLASSIC_GRENADE_EXPLOSION_BLOCK_DAMAGE", 15.0)),
        _kill("CLASSIC_GRENADE_KILL", 22), 22,
        blast_radius=float(getattr(C, "CLASSIC_GRENADE_EXPLOSION_BLAST_WAVE_RADIUS", 9.0)),
        knockback_min=float(getattr(C, "CLASSIC_GRENADE_EXPLOSION_KNOCKBACK_MIN", 0.1)),
        knockback_max=float(getattr(C, "CLASSIC_GRENADE_EXPLOSION_KNOCKBACK_MAX", 0.1))),
    int(getattr(C, "ANTIPERSONNEL_GRENADE_TOOL", 32)): ProjectileSpec(
        "ap_grenade", "bounce", 1.0,
        float(getattr(C, "ANTIPERSONNEL_GRENADE_EXPLOSION_DAMAGE", 500.0)),
        float(getattr(C, "ANTIPERSONNEL_GRENADE_EXPLOSION_BLOCK_DAMAGE", 0.5)),
        _kill("ANTIPERSONNEL_GRENADE_KILL", 23), 23,
        blast_radius=float(getattr(C, "ANTIPERSONNEL_GRENADE_EXPLOSION_BLAST_WAVE_RADIUS", 6.0)),
        knockback_min=float(getattr(C, "ANTIPERSONNEL_GRENADE_EXPLOSION_KNOCKBACK_MIN", 0.25)),
        knockback_max=float(getattr(C, "ANTIPERSONNEL_GRENADE_EXPLOSION_KNOCKBACK_MAX", 0.5))),
    int(getattr(C, "MOLOTOV_TOOL", 33)): ProjectileSpec(
        "molotov", "contact", 1.0, 50.0, 3.0,
        _kill("MOLOTOV_KILL", 24), 24, approximate=True,
        entity_type=int(getattr(C, "MOLOTOV_ENTITY", 27)),
        blast_radius=4.0,
        knockback_min=float(getattr(C, "MOLOTOV_EXPLOSION_KNOCKBACK_MIN", 0.0)),
        knockback_max=float(getattr(C, "MOLOTOV_EXPLOSION_KNOCKBACK_MAX", 0.1))),
    int(getattr(C, "DYNAMITE_TOOL", 21)): ProjectileSpec(
        "dynamite", "bounce", 1.0, 100.0, 5.0,
        _kill("DYNAMITE_KILL", 15), 16,
        blast_radius=8.0,
        knockback_min=float(getattr(C, "DYNAMITE_EXPLOSION_KNOCKBACK_MIN", 0.1)),
        knockback_max=float(getattr(C, "DYNAMITE_EXPLOSION_KNOCKBACK_MAX", 0.15))),
    int(getattr(C, "LANDMINE_TOOL", 20)): ProjectileSpec(
        "landmine", "bounce", 1.0, 100.0, 3.0,
        _kill("LANDMINE_KILL", 14), 15, approximate=True,
        blast_radius=float(getattr(C, "LANDMINE_EXPLOSION_BLAST_WAVE_RADIUS", 6.0)),
        knockback_min=float(getattr(C, "LANDMINE_EXPLOSION_KNOCKBACK_MIN", 0.75)),
        knockback_max=float(getattr(C, "LANDMINE_EXPLOSION_KNOCKBACK_MAX", 0.75))),
    int(C.RPG_TOOL): ProjectileSpec(
        "rocket", "contact",
        float(getattr(C, "ROCKET_GRAVITY_MULTIPLIER", 0.05)),
        float(getattr(C, "ROCKET_EXPLOSION_DAMAGE", 140)),
        float(getattr(C, "ROCKET_EXPLOSION_BLOCK_DAMAGE", 5)),
        _kill("ROCKET_KILL", 4), 8,
        entity_type=int(getattr(C, "ROCKET_ENTITY", 21)),
        blast_radius=float(getattr(C, "ROCKET_EXPLOSION_BLAST_WAVE_RADIUS", 6.0)),
        knockback_min=float(getattr(C, "ROCKET_EXPLOSION_KNOCKBACK_MIN", 0.0)),
        knockback_max=float(getattr(C, "ROCKET_EXPLOSION_KNOCKBACK_MAX", 0.25))),
    int(C.RPG2_TOOL): ProjectileSpec(
        "rocket2", "contact",
        float(getattr(C, "ROCKET2_GRAVITY_MULTIPLIER", 0.025)),
        float(getattr(C, "ROCKET2_EXPLOSION_DAMAGE", 50)),
        float(getattr(C, "ROCKET2_EXPLOSION_BLOCK_DAMAGE", 2)),
        _kill("ROCKET2_KILL", 5), 9,
        entity_type=int(getattr(C, "ROCKET2_ENTITY", 22)),
        blast_radius=6.0,
        knockback_min=float(getattr(C, "ROCKET2_EXPLOSION_KNOCKBACK_MIN", 0.0)),
        knockback_max=float(getattr(C, "ROCKET2_EXPLOSION_KNOCKBACK_MAX", 0.25)),
        self_knockback_min=float(getattr(C, "ROCKET2_EXPLOSION_SELF_KNOCKBACK_MIN", 1.0)),
        self_knockback_max=float(getattr(C, "ROCKET2_EXPLOSION_SELF_KNOCKBACK_MAX", 1.5))),
    int(C.DRILLGUN_TOOL): ProjectileSpec(
        "drill", "contact",
        float(getattr(C, "DRILL_GRAVITY_MULTIPLIER", 1.5)),
        float(getattr(C, "DRILL_EXPLOSION_DAMAGE", 50)),
        float(getattr(C, "DRILL_EXPLOSION_BLOCK_DAMAGE", 5.0)),
        _kill("DRILL_KILL", 6), 10,
        lifespan=float(getattr(C, "DRILL_LIFESPAN", 3.0)),
        destroyed_damage=float(getattr(C, "DRILL_DESTROYED_EXPLOSION_DAMAGE", 95)),
        destroyed_block_damage=float(getattr(C, "DRILL_DESTROYED_EXPLOSION_BLOCK_DAMAGE", 10.0)),
        entity_type=int(getattr(C, "DRILL_ENTITY", 23)),
        blast_radius=float(getattr(C, "DRILL_EXPLOSION_RADIUS", 3.0)),
        knockback_min=float(getattr(C, "DRILL_EXPLOSION_KNOCKBACK_MIN", 0.01)),
        knockback_max=float(getattr(C, "DRILL_EXPLOSION_KNOCKBACK_MAX", 0.1)),
        destroyed_blast_radius=float(getattr(C, "DRILL_DESTROYED_EXPLOSION_RADIUS", 3.5)),
        destroyed_knockback_min=float(getattr(C, "DRILL_DESTROYED_EXPLOSION_KNOCKBACK_MIN", 0.1)),
        destroyed_knockback_max=float(getattr(C, "DRILL_DESTROYED_EXPLOSION_KNOCKBACK_MAX", 0.2))),
    int(getattr(C, "SNOWBLOWER_TOOL", 29)): ProjectileSpec(
        "snowball", "contact",
        float(getattr(C, "SNOWBALL_GRAVITY_MULTIPLIER", 0.5)),
        float(getattr(C, "SNOWBALL_EXPLOSION_DAMAGE", 10)),
        float(getattr(C, "SNOWBALL_EXPLOSION_BLOCK_DAMAGE", 0)),
        _kill("SNOWBALL_KILL", 21), 20,
        entity_type=int(getattr(C, "SNOWBALL_ENTITY", 24)),
        blast_radius=float(getattr(C, "SNOWBALL_EXPLOSION_RADIUS", 5.0)),
        knockback_min=float(getattr(C, "SNOWBALL_EXPLOSION_KNOCKBACK_MIN", 0.3)),
        knockback_max=float(getattr(C, "SNOWBALL_EXPLOSION_KNOCKBACK_MAX", 0.3))),
    int(getattr(C, "STICKY_GRENADE_TOOL", 57)): ProjectileSpec(
        "sticky_grenade", "stick", 1.0,
        float(getattr(C, "STICKY_GRENADE_EXPLOSION_DAMAGE", 200.0)),
        float(getattr(C, "STICKY_GRENADE_EXPLOSION_BLOCK_DAMAGE", 6.0)),
        _kill("STICKY_GRENADE_KILL", 34), 39,
        entity_type=int(getattr(C, "STICKY_GRENADE_ENTITY", 34)),
        blast_radius=float(getattr(C, "STICKY_GRENADE_EXPLOSION_RADIUS", 5.0)),
        # Recovered wrapper order is intentionally inverted: near=.1, edge=.75.
        knockback_min=0.75, knockback_max=0.1),
    int(getattr(C, "MINE_LAUNCHER_TOOL", 58)): ProjectileSpec(
        "mine_projectile", "deploy", 1.0,
        float(getattr(C, "LANDMINE_EXPLOSION_DAMAGE", 100.0)),
        float(getattr(C, "LANDMINE_EXPLOSION_BLOCK_DAMAGE", 15.0)),
        _kill("MINE_KILL", 35), 40,
        entity_type=int(getattr(C, "PROJECTILE_MINE_ENTITY", 37)),
        blast_radius=float(getattr(C, "LANDMINE_EXPLOSION_BLAST_WAVE_RADIUS", 6.0)),
        knockback_min=float(getattr(C, "LANDMINE_EXPLOSION_KNOCKBACK_MIN", 0.75)),
        knockback_max=float(getattr(C, "LANDMINE_EXPLOSION_KNOCKBACK_MAX", 0.75))),
    int(getattr(C, "CHEMICALBOMB_TOOL", 54)): ProjectileSpec(
        "chemical_bomb", "contact", 1.0,
        float(getattr(C, "CHEMICALBOMB_EXPLOSION_DAMAGE", 50.0)),
        float(getattr(C, "CHEMICALBOMB_EXPLOSION_BLOCK_DAMAGE", 3.0)),
        _kill("CHEMICALBOMB_KILL", 31), 6,
        entity_type=int(getattr(C, "CHEMICALBOMB_ENTITY", 32)),
        blast_radius=float(getattr(C, "CHEMICALBOMB_EXPLOSION_RADIUS", 3.0))),
    int(getattr(C, "GRENADE_LAUNCHER_WEAPON_TOOL", 55)): ProjectileSpec(
        "gl_grenade", "contact", 1.0,
        float(getattr(C, "GRENADE_LAUNCHER_EXPLOSION_DAMAGE", 100.0)),
        float(getattr(C, "GRENADE_LAUNCHER_EXPLOSION_BLOCK_DAMAGE", 6.0)),
        _kill("GRENADE_LAUNCHER_KILL", 32), 37,
        lifespan=float(getattr(C, "GRENADE_LAUNCHER_PROJECTILE_LIFESPAN", 3.0)),
        entity_type=int(getattr(C, "GRENADE_LAUNCHER_ENTITY", 33)),
        blast_radius=float(getattr(C, "GRENADE_LAUNCHER_EXPLOSION_RADIUS", 4.0)),
        knockback_min=0.0, knockback_max=0.25),
}

# Map Creator subclasses the ordinary Drill and Block Cannon on the client,
# but emits distinct tool/damage/kill ids (47/48 and 33/32 respectively).
# Their flight/entity geometry is otherwise identical.  Keep aliases explicit
# here so packet 10 remains byte-faithful and the native client selects the UGC
# BlockManager handlers instead of silently rejecting an unknown projectile.
PROJECTILE_SPECS[int(getattr(C, "UGC_DRILLGUN_TOOL", 47))] = replace(
    PROJECTILE_SPECS[int(C.DRILLGUN_TOOL)],
    gravity_mult=float(getattr(C, "UGC_DRILL_GRAVITY_MULTIPLIER", 1.5)),
    damage=float(getattr(C, "UGC_DRILL_EXPLOSION_DAMAGE", 50.0)),
    block_damage=float(getattr(C, "UGC_DRILL_EXPLOSION_BLOCK_DAMAGE", 5.0)),
    kill_type=_kill("UGC_DRILL_KILL", 28),
    damage_type=int(getattr(C, "UGC_DRILL_DAMAGE", 33)),
    lifespan=float(getattr(C, "UGC_DRILL_LIFESPAN", 3.0)),
    destroyed_damage=float(
        getattr(C, "UGC_DRILL_DESTROYED_EXPLOSION_DAMAGE", 95.0)
    ),
    destroyed_block_damage=float(
        getattr(C, "UGC_DRILL_DESTROYED_EXPLOSION_BLOCK_DAMAGE", 10.0)
    ),
    blast_radius=float(getattr(C, "UGC_DRILL_EXPLOSION_RADIUS", 3.0)),
    knockback_min=float(getattr(C, "UGC_DRILL_EXPLOSION_KNOCKBACK_MIN", 0.01)),
    knockback_max=float(getattr(C, "UGC_DRILL_EXPLOSION_KNOCKBACK_MAX", 0.1)),
    destroyed_blast_radius=float(
        getattr(C, "UGC_DRILL_DESTROYED_EXPLOSION_RADIUS", 3.5)
    ),
    destroyed_knockback_min=float(
        getattr(C, "UGC_DRILL_DESTROYED_EXPLOSION_KNOCKBACK_MIN", 0.1)
    ),
    destroyed_knockback_max=float(
        getattr(C, "UGC_DRILL_DESTROYED_EXPLOSION_KNOCKBACK_MAX", 0.2)
    ),
)
PROJECTILE_SPECS[int(getattr(C, "UGC_SNOWBLOWER_TOOL", 48))] = replace(
    PROJECTILE_SPECS[int(getattr(C, "SNOWBLOWER_TOOL", 29))],
    gravity_mult=float(getattr(C, "UGC_SNOWBALL_GRAVITY_MULTIPLIER", 0.5)),
    damage=float(getattr(C, "UGC_SNOWBALL_EXPLOSION_DAMAGE", 10.0)),
    block_damage=float(getattr(C, "UGC_SNOWBALL_EXPLOSION_BLOCK_DAMAGE", 0.0)),
    kill_type=_kill("UGC_SNOWBALL_KILL", 29),
    damage_type=int(getattr(C, "UGC_SNOWBALL_DAMAGE", 32)),
    blast_radius=float(getattr(C, "UGC_SNOWBALL_EXPLOSION_RADIUS", 5.0)),
    knockback_min=float(
        getattr(C, "UGC_SNOWBALL_EXPLOSION_KNOCKBACK_MIN", 0.3)
    ),
    knockback_max=float(
        getattr(C, "UGC_SNOWBALL_EXPLOSION_KNOCKBACK_MAX", 0.3)
    ),
)


class Projectile:
    __slots__ = ("spec", "tool", "x", "y", "z", "vx", "vy", "vz",
                 "explode_at", "thrower_id", "stuck", "spawned_at",
                 "lifespan_at", "entity_id", "contact_block",
                 "attached_player_id", "block_color", "source_loop")

    def __init__(self, spec, tool, pos, vel, fuse, thrower_id, now):
        self.spec = spec
        self.tool = int(tool)
        self.x, self.y, self.z = (float(v) for v in pos)
        self.vx, self.vy, self.vz = (float(v) for v in vel)
        # fuse=None -> no fuse timer (contact projectiles fly until they hit).
        # fuse=0.0 is a REAL immediate detonation (legacy grenade semantics).
        self.explode_at = None if fuse is None else now + max(0.0, fuse)
        self.spawned_at = now
        self.lifespan_at = now + spec.lifespan if spec.lifespan > 0.0 else 0.0
        self.thrower_id = int(thrower_id)
        self.stuck = False
        # ``0`` is a valid uint16 entity id. ``None`` is the only safe
        # sentinel for projectile families rendered without CreateEntity.
        self.entity_id = None
        self.contact_block = None
        self.attached_player_id = None
        # Block Cannon/Snowblower shots use the ordinary block wallet and
        # palette.  Snapshot both values at admission time: changing colour
        # while a projectile is in flight must not recolour its impact block,
        # and the source loop is the only client timeline label for the shot.
        self.block_color = None
        self.source_loop = None


class Explosion:
    """What the engine hands back to the server for damage application."""
    __slots__ = ("x", "y", "z", "thrower_id", "spec", "damage", "block_damage",
                 "entity_id", "blast_radius", "knockback_min", "knockback_max",
                 "self_knockback_min", "self_knockback_max", "contact_block",
                 "block_color", "source_loop")

    def __init__(self, proj: Projectile, destroyed: bool = False):
        self.x, self.y, self.z = proj.x, proj.y, proj.z
        self.thrower_id = proj.thrower_id
        self.spec = proj.spec
        self.entity_id = proj.entity_id
        self.contact_block = proj.contact_block
        self.block_color = proj.block_color
        self.source_loop = proj.source_loop
        if destroyed and proj.spec.destroyed_damage > 0.0:
            self.damage = proj.spec.destroyed_damage
            self.block_damage = proj.spec.destroyed_block_damage
            self.blast_radius = (
                proj.spec.destroyed_blast_radius or proj.spec.blast_radius
            )
            self.knockback_min = proj.spec.destroyed_knockback_min
            self.knockback_max = proj.spec.destroyed_knockback_max
        else:
            self.damage = proj.spec.damage
            self.block_damage = proj.spec.block_damage
            self.blast_radius = proj.spec.blast_radius
            self.knockback_min = proj.spec.knockback_min
            self.knockback_max = proj.spec.knockback_max
        self.self_knockback_min = proj.spec.self_knockback_min
        self.self_knockback_max = proj.spec.self_knockback_max


class ProjectileDeployment:
    """A launched mine that contacted terrain and becomes a placed entity."""
    __slots__ = ("x", "y", "z", "thrower_id", "spec", "entity_id")

    def __init__(self, proj: Projectile):
        self.x, self.y, self.z = proj.x, proj.y, proj.z
        self.thrower_id = proj.thrower_id
        self.spec = proj.spec
        self.entity_id = proj.entity_id


class DrillContact:
    """A drill pressed against a solid voxel; it damages it and remains live."""
    __slots__ = ("projectile", "block")

    def __init__(self, projectile: Projectile, block):
        self.projectile = projectile
        self.block = tuple(int(value) for value in block)


class ProjectileEngine:
    """Owns in-flight projectiles; update() advances them one tick and returns
    the explosions the caller must apply (blast + crater + broadcast)."""

    def __init__(self):
        self.projectiles: list[Projectile] = []

    def spawn(self, tool: int, pos, vel, fuse: float, thrower_id: int,
              now: Optional[float] = None) -> Optional[Projectile]:
        spec = PROJECTILE_SPECS.get(int(tool))
        if spec is None:
            return None
        if now is None:
            now = time.time()
        # Contact projectiles have no client fuse — they fly until they hit
        # (drill also dies at its lifespan). Bounce uses the client fuse.
        # Sticky: the client sends value=0 (measured live 2026-07-07) — the
        # fuse ARMS when it sticks (STICK_ARM_SECONDS), not at throw.
        if spec.behavior in ("contact", "deploy"):
            fuse = None
        elif spec.behavior == "stick" and fuse <= 0.0:
            fuse = None
        p = Projectile(spec, tool, pos, vel, fuse, thrower_id, now)
        self.projectiles.append(p)
        return p

    def spawn_spec(self, spec: ProjectileSpec, pos, vel, thrower_id: int,
                   now: Optional[float] = None) -> Projectile:
        """Spawn a server-owned projectile not directly tied to a player tool.

        Rocket turrets use the stock Rocket entity/flight model but their own
        50/10 warhead, so representing the shot as RPG_TOOL would apply the
        player's 140/5 RPG damage profile.
        """
        if now is None:
            now = time.time()
        p = Projectile(spec, -1, pos, vel, None, thrower_id, now)
        self.projectiles.append(p)
        return p

    def remove_by_thrower(self, thrower_id: int) -> list[Projectile]:
        """Cancel every in-flight projectile owned by one departing player.

        Player ids are reused immediately. Retaining only the numeric id past
        disconnect would transfer collision exclusion, team policy, and kill
        credit to the replacement player. The server owns DestroyEntity for
        the returned visible projectiles; grenade-family entries have no
        entity id and simply disappear from authoritative simulation.
        """
        thrower_id = int(thrower_id)
        removed = [
            projectile
            for projectile in self.projectiles
            if projectile.thrower_id == thrower_id
        ]
        if removed:
            self.projectiles = [
                projectile
                for projectile in self.projectiles
                if projectile.thrower_id != thrower_id
            ]
        return removed

    def update(self, dt: float, world, now: Optional[float] = None,
               players=()) -> list:
        if not self.projectiles:
            return []
        if now is None:
            now = time.time()
        explosions: list[Explosion] = []
        still: list[Projectile] = []
        for p in self.projectiles:
            # Timers first: fuse (bounce/stick) and lifespan (drill).
            if p.explode_at is not None and now >= p.explode_at:
                explosions.append(Explosion(p))
                continue
            if p.lifespan_at and now >= p.lifespan_at:
                explosions.append(Explosion(p, destroyed=True))
                continue

            if p.stuck:
                if p.attached_player_id is not None:
                    target = next(
                        (candidate for candidate in players
                         if int(getattr(candidate, "id", -1)) == p.attached_player_id
                         and getattr(candidate, "alive", False)
                         and getattr(candidate, "spawned", False)),
                        None,
                    )
                    if target is not None:
                        p.x = float(target.x)
                        p.y = float(target.y)
                        p.z = float(target.z) + 1.0
                still.append(p)
                continue

            if p.spec.behavior in ("contact", "deploy"):
                collision_players = players if p.spec.behavior == "contact" else ()
                contact = self._advance_contact(p, dt, world, collision_players)
                if contact:
                    if p.spec.name == "drill" and contact == "world":
                        explosions.append(DrillContact(p, p.contact_block))
                        still.append(p)
                        continue
                    if p.spec.behavior == "deploy":
                        explosions.append(ProjectileDeployment(p))
                    else:
                        explosions.append(Explosion(p))
                    continue
            else:
                hit = self._advance_bounce(
                    p, dt, world, players if p.spec.behavior == "stick" else ()
                )
                if hit and p.spec.behavior == "stick":
                    p.vx = p.vy = p.vz = 0.0
                    p.stuck = True
                    if p.explode_at is None:
                        # Fuse arms on impact (client sends no throw fuse).
                        p.explode_at = now + STICK_ARM_SECONDS
            # Failsafe: nothing fuse-less may outlive MAX_FLIGHT_SECONDS.
            if p.explode_at is None and p.lifespan_at == 0.0 \
                    and (now - p.spawned_at) > MAX_FLIGHT_SECONDS:
                explosions.append(Explosion(p))
                continue
            still.append(p)
        self.projectiles = still
        return explosions

    # -- movers ----------------------------------------------------------

    def _advance_bounce(self, p: Projectile, dt: float, world, players=()) -> bool:
        """EXACT legacy grenade math (verified vs the compiled client):
        gravity 30*dt on vz, displacement vel*dt, axis-separated reflection,
        whole-velocity x0.36 damp on any hit. Returns True if it hit."""
        p.vz += BASE_GRAVITY * p.spec.gravity_mult * dt
        speed = (p.vx ** 2 + p.vy ** 2 + p.vz ** 2) ** 0.5
        if speed > MAX_SPEED:
            k = MAX_SPEED / speed
            p.vx *= k; p.vy *= k; p.vz *= k

        x, y, z = p.x, p.y, p.z
        nx = x + p.vx * dt
        ny = y + p.vy * dt
        nz = z + p.vz * dt

        hit = False
        if world.get_solid(int(nx), int(ny), int(nz)):
            hit = True
            bounced = False
            if world.get_solid(int(nx), int(y), int(z)):
                p.vx = -p.vx; nx = x; bounced = True
            if world.get_solid(int(x), int(ny), int(z)):
                p.vy = -p.vy; ny = y; bounced = True
            if world.get_solid(int(x), int(y), int(nz)):
                p.vz = -p.vz; nz = z; bounced = True
            if not bounced:
                p.vz = -p.vz; nx = x; ny = y; nz = z
            p.vx *= BOUNCE_DAMP
            p.vy *= BOUNCE_DAMP
            p.vz *= BOUNCE_DAMP

        if not hit and players:
            contact = self._first_player_contact(
                (x, y, z), (nx, ny, nz), players, p.thrower_id
            )
            if contact is not None:
                hit_t, target = contact
                nx = x + (nx - x) * hit_t
                ny = y + (ny - y) * hit_t
                nz = z + (nz - z) * hit_t
                p.attached_player_id = int(target.id)
                hit = True

        p.x, p.y, p.z = nx, ny, nz
        return hit

    def _advance_contact(self, p: Projectile, dt: float, world, players=()):
        """Straight flight; explode on the first voxel or player volume.

        Sub-stepped so fast rockets can't tunnel through thin walls (max
        CONTACT_STEP blocks of travel per collision test). Player collision is
        swept over every sub-step too, so a fast RPG2 cannot pass completely
        through a character between two samples.
        """
        p.vz += BASE_GRAVITY * p.spec.gravity_mult * dt
        p.contact_block = None
        speed = (p.vx ** 2 + p.vy ** 2 + p.vz ** 2) ** 0.5
        if speed > MAX_SPEED:
            k = MAX_SPEED / speed
            p.vx *= k; p.vy *= k; p.vz *= k
            speed = MAX_SPEED

        travel = speed * dt
        steps = max(1, int(travel / CONTACT_STEP) + 1)
        sx = p.vx * dt / steps
        sy = p.vy * dt / steps
        sz = p.vz * dt / steps
        for _ in range(steps):
            ox, oy, oz = p.x, p.y, p.z
            nx, ny, nz = p.x + sx, p.y + sy, p.z + sz
            if world.get_solid(int(nx), int(ny), int(nz)):
                p.contact_block = (int(nx), int(ny), int(nz))
                # Explode AT the cell face we hit — keep the last free position
                # so the crater centers on the wall surface, not inside it.
                return "world"
            hit_t = self._first_player_hit(
                (ox, oy, oz), (nx, ny, nz), players, p.thrower_id
            )
            if hit_t is not None:
                # Place the explosion at the entry point of the character's
                # swept volume, rather than behind the target.
                p.x = ox + (nx - ox) * hit_t
                p.y = oy + (ny - oy) * hit_t
                p.z = oz + (nz - oz) * hit_t
                return "player"
            p.x, p.y, p.z = nx, ny, nz
        # Out-of-world safety: kill anything that left the map or fell below
        # the waterline floor so the list can't grow unbounded.
        if not (0.0 <= p.x < 512.0 and 0.0 <= p.y < 512.0 and -64.0 < p.z < 300.0):
            return "bounds"
        return None

    @staticmethod
    def _first_player_hit(start, end, players, thrower_id: int):
        contact = ProjectileEngine._first_player_contact(
            start, end, players, thrower_id
        )
        return None if contact is None else contact[0]

    @staticmethod
    def _first_player_contact(start, end, players, thrower_id: int):
        """Return the earliest segment/AABB entry among live player bodies.

        The stock character collision radius is 0.45. Contact projectiles use
        a 0.5 collision range; expanding the body by both values is the
        Minkowski sum of the body and projectile probe. Z grows downward in
        AoS, from the player's head position to the supporting ground.
        """
        expand = float(getattr(C, "PLAYER_RADIUS", 0.45)) + CONTACT_STEP
        closest = None
        closest_target = None
        for target in players:
            if int(getattr(target, "id", -1)) == int(thrower_id):
                continue
            if not getattr(target, "alive", False) or not getattr(target, "spawned", False):
                continue
            crouched = bool(getattr(getattr(target, "input", None), "crouch", False))
            height = float(getattr(
                C,
                "PLAYER_CROUCHING_HEIGHT" if crouched else "PLAYER_STANDING_HEIGHT",
                1.8 if crouched else 2.7,
            ))
            bounds_min = (
                float(target.x) - expand,
                float(target.y) - expand,
                float(target.z) - CONTACT_STEP,
            )
            bounds_max = (
                float(target.x) + expand,
                float(target.y) + expand,
                float(target.z) + height + CONTACT_STEP,
            )
            entry = ProjectileEngine._segment_aabb_entry(
                start, end, bounds_min, bounds_max
            )
            if entry is not None and (closest is None or entry < closest):
                closest = entry
                closest_target = target
        if closest is None:
            return None
        return closest, closest_target

    @staticmethod
    def _segment_aabb_entry(start, end, bounds_min, bounds_max):
        """Slab intersection for a finite segment; returns entry t in [0,1]."""
        enter, leave = 0.0, 1.0
        for origin, finish, low, high in zip(start, end, bounds_min, bounds_max):
            delta = finish - origin
            if abs(delta) < 1e-12:
                if origin < low or origin > high:
                    return None
                continue
            t0 = (low - origin) / delta
            t1 = (high - origin) / delta
            if t0 > t1:
                t0, t1 = t1, t0
            enter = max(enter, t0)
            leave = min(leave, t1)
            if enter > leave:
                return None
        return enter
