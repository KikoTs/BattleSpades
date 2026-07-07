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
from dataclasses import dataclass
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
STICK_ARM_SECONDS = 2.5
# Failsafe: a fuse-less projectile that never contacts anything dies here.
MAX_FLIGHT_SECONDS = 10.0


@dataclass(frozen=True)
class ProjectileSpec:
    name: str
    behavior: str                 # 'bounce' | 'contact' | 'stick'
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


def _kill(name: str, default: int) -> int:
    return int(getattr(C.KILL, name, default))


_GRENADE_KILL = _kill("GRENADE_KILL", 3)

# Tool id -> spec. The bounce family keeps the legacy verified blast numbers
# (handled by the explosion path in main.py); damage listed for reference.
PROJECTILE_SPECS: dict[int, ProjectileSpec] = {
    int(C.GRENADE_TOOL): ProjectileSpec(
        "grenade", "bounce", 1.0, 100.0, 4.0, _GRENADE_KILL, 7),
    int(getattr(C, "CLASSIC_GRENADE_TOOL", 31)): ProjectileSpec(
        "classic_grenade", "bounce", 1.0, 100.0, 4.0,
        _kill("CLASSIC_GRENADE_KILL", 22), 22),
    int(getattr(C, "ANTIPERSONNEL_GRENADE_TOOL", 32)): ProjectileSpec(
        "ap_grenade", "bounce", 1.0, 100.0, 4.0,
        _kill("ANTIPERSONNEL_GRENADE_KILL", 23), 23),
    int(getattr(C, "MOLOTOV_TOOL", 33)): ProjectileSpec(
        "molotov", "contact", 1.0, 50.0, 3.0,
        _kill("MOLOTOV_KILL", 24), 24, approximate=True),
    int(getattr(C, "DYNAMITE_TOOL", 21)): ProjectileSpec(
        "dynamite", "bounce", 1.0, 100.0, 5.0,
        _kill("DYNAMITE_KILL", 15), 16),
    int(getattr(C, "LANDMINE_TOOL", 20)): ProjectileSpec(
        "landmine", "bounce", 1.0, 100.0, 3.0,
        _kill("LANDMINE_KILL", 14), 15, approximate=True),
    int(C.RPG_TOOL): ProjectileSpec(
        "rocket", "contact",
        float(getattr(C, "ROCKET_GRAVITY_MULTIPLIER", 0.05)),
        float(getattr(C, "ROCKET_EXPLOSION_DAMAGE", 140)),
        float(getattr(C, "ROCKET_EXPLOSION_BLOCK_DAMAGE", 5)),
        _kill("ROCKET_KILL", 4), 8),
    int(C.RPG2_TOOL): ProjectileSpec(
        "rocket2", "contact",
        float(getattr(C, "ROCKET2_GRAVITY_MULTIPLIER", 0.025)),
        float(getattr(C, "ROCKET2_EXPLOSION_DAMAGE", 50)),
        float(getattr(C, "ROCKET2_EXPLOSION_BLOCK_DAMAGE", 2)),
        _kill("ROCKET2_KILL", 5), 9),
    int(C.DRILLGUN_TOOL): ProjectileSpec(
        "drill", "contact",
        float(getattr(C, "DRILL_GRAVITY_MULTIPLIER", 1.5)),
        float(getattr(C, "DRILL_EXPLOSION_DAMAGE", 50)),
        float(getattr(C, "DRILL_EXPLOSION_BLOCK_DAMAGE", 5.0)),
        _kill("DRILL_KILL", 6), 10,
        lifespan=float(getattr(C, "DRILL_LIFESPAN", 3.0)),
        destroyed_damage=float(getattr(C, "DRILL_DESTROYED_EXPLOSION_DAMAGE", 95)),
        destroyed_block_damage=float(getattr(C, "DRILL_DESTROYED_EXPLOSION_BLOCK_DAMAGE", 10.0))),
    int(getattr(C, "SNOWBLOWER_TOOL", 29)): ProjectileSpec(
        "snowball", "contact",
        float(getattr(C, "SNOWBALL_GRAVITY_MULTIPLIER", 0.5)),
        float(getattr(C, "SNOWBALL_EXPLOSION_DAMAGE", 10)),
        float(getattr(C, "SNOWBALL_EXPLOSION_BLOCK_DAMAGE", 0)),
        _kill("SNOWBALL_KILL", 21), 20),
    int(getattr(C, "STICKY_GRENADE_TOOL", 57)): ProjectileSpec(
        "sticky_grenade", "stick", 1.0, 100.0, 4.0,
        _kill("STICKY_GRENADE_KILL", 34), 39, approximate=True),
    int(getattr(C, "CHEMICALBOMB_TOOL", 54)): ProjectileSpec(
        "chemical_bomb", "bounce", 1.0, 50.0, 0.0,
        _kill("CHEMICALBOMB_KILL", 31), 6, approximate=True),
}


class Projectile:
    __slots__ = ("spec", "tool", "x", "y", "z", "vx", "vy", "vz",
                 "explode_at", "thrower_id", "stuck", "spawned_at",
                 "lifespan_at")

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


class Explosion:
    """What the engine hands back to the server for damage application."""
    __slots__ = ("x", "y", "z", "thrower_id", "spec", "damage", "block_damage")

    def __init__(self, proj: Projectile, destroyed: bool = False):
        self.x, self.y, self.z = proj.x, proj.y, proj.z
        self.thrower_id = proj.thrower_id
        self.spec = proj.spec
        if destroyed and proj.spec.destroyed_damage > 0.0:
            self.damage = proj.spec.destroyed_damage
            self.block_damage = proj.spec.destroyed_block_damage
        else:
            self.damage = proj.spec.damage
            self.block_damage = proj.spec.block_damage


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
        if spec.behavior == "contact":
            fuse = None
        elif spec.behavior == "stick" and fuse <= 0.0:
            fuse = None
        p = Projectile(spec, tool, pos, vel, fuse, thrower_id, now)
        self.projectiles.append(p)
        return p

    def update(self, dt: float, world, now: Optional[float] = None) -> list[Explosion]:
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
                still.append(p)
                continue

            if p.spec.behavior == "contact":
                if self._advance_contact(p, dt, world):
                    explosions.append(Explosion(p))
                    continue
            else:
                hit = self._advance_bounce(p, dt, world)
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

    def _advance_bounce(self, p: Projectile, dt: float, world) -> bool:
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

        p.x, p.y, p.z = nx, ny, nz
        return hit

    def _advance_contact(self, p: Projectile, dt: float, world) -> bool:
        """Straight flight under scaled gravity; explode on first solid cell.
        Sub-stepped so fast rockets can't tunnel through thin walls (max
        CONTACT_STEP blocks of travel per collision test)."""
        p.vz += BASE_GRAVITY * p.spec.gravity_mult * dt
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
            nx, ny, nz = p.x + sx, p.y + sy, p.z + sz
            if world.get_solid(int(nx), int(ny), int(nz)):
                # Explode AT the cell face we hit — keep the last free position
                # so the crater centers on the wall surface, not inside it.
                return True
            p.x, p.y, p.z = nx, ny, nz
        # Out-of-world safety: kill anything that left the map or fell below
        # the waterline floor so the list can't grow unbounded.
        if not (0.0 <= p.x < 512.0 and 0.0 <= p.y < 512.0 and -64.0 < p.z < 300.0):
            return True
        return False
