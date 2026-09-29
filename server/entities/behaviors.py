"""Per-entity server-side behavior (Phase-2 tickable entity system).

Behavior is attached to a MapEntity by COMPOSITION — the MapEntity stays the
pure wire DTO (its ``to_wire_entity()`` is the single wire-safety choke point)
and an optional EntityBehavior carries the server logic. A behavior instance is
shared across many entities of the same kind (the entity is passed into every
hook), so there is no per-entity allocation in the 60 Hz loop.

Hooks (all optional; the base implementations are inert, so a static entity
needs no behavior at all):
  on_tick(ent, dt, ctx)               — every tick, for alive entities
  on_touch(ent, player, ctx) -> bool  — when a player is within touch_radius
  on_damage(ent, amount, source, ctx) — when routed damage hits (takes_damage)

``ctx`` is an EntityContext (see registry.py), built once per tick.
"""
from __future__ import annotations

import math


_ATTACHMENT_FACE_OFFSETS = {
    0: (0.0, 0.5, 0.5),
    1: (1.0, 0.5, 0.5),
    2: (0.5, 0.0, 0.5),
    3: (0.5, 1.0, 0.5),
    4: (0.5, 0.5, 0.0),
    5: (0.5, 0.5, 1.0),
}


def _attached_face_center(ent):
    """Return the exposed face center named by an attachment packet.

    PlaceDynamite/PlaceC4 coordinates identify the supporting solid voxel,
    not the rendered object center. Starting a blast at that integer voxel
    makes the support block occlude every player ray and is also why a charge
    appears sunk into terrain after a late-join entity snapshot.
    """

    ox, oy, oz = _ATTACHMENT_FACE_OFFSETS.get(
        int(ent.face),
        (0.5, 0.5, 0.5),
    )
    return (float(ent.x) + ox, float(ent.y) + oy, float(ent.z) + oz)


class EntityBehavior:
    """Base behavior — inert. Subclass and override the hooks you need."""

    touch_radius: float = 0.0     # 0 => registry skips the proximity test
    takes_damage: bool = False    # gate for on_damage routing
    hit_radius: float = 0.0       # 0 => not considered by authoritative hitscan
    hit_center_offset = (0.0, 0.0, 0.0)

    def on_tick(self, ent, dt, ctx) -> None:
        pass

    def on_touch(self, ent, player, ctx) -> bool:
        return False

    def on_damage(self, ent, amount, source, ctx) -> None:
        pass

    def on_support_lost(self, ent, ctx) -> None:
        """Remove a passive placed entity whose supporting voxel vanished."""

        _remove_entity(ent, ctx)

    def get_hit_center(self, ent):
        """World-space center used by hitscan and explosion damage."""
        ox, oy, oz = self.hit_center_offset
        return (float(ent.x) + ox, float(ent.y) + oy, float(ent.z) + oz)


class DamageableEntityBehavior(EntityBehavior):
    """Reusable health/despawn lifecycle for placed client entities.

    Health lives on the behavior because ``MapEntity`` is deliberately the
    byte-stable wire DTO.  Each placed damageable receives its own behavior
    instance, so health can never leak between entities.
    """

    takes_damage = True
    hit_radius = 0.75
    hit_center_offset = (0.0, 0.0, 0.5)

    def __init__(self, health: float):
        self.health = max(0.0, float(health))

    def on_damage(self, ent, amount, source, ctx) -> None:
        damage = max(0.0, float(amount))
        if damage <= 0.0 or not ent.alive:
            return
        self.health = max(0.0, self.health - damage)
        if self.health <= 0.0:
            self.on_destroyed(ent, source, ctx)

    def on_destroyed(self, ent, source, ctx) -> None:
        _remove_entity(ent, ctx)


class PickupCrateBehavior(EntityBehavior):
    """Ammo/health/block crate: refill a player who steps on it, then despawn
    and schedule a respawn.

    ``refill`` is a ``callable(player)`` supplied at the place() site so this
    module stays free of player/constants imports (keeps it independently
    testable). Refill is unconditional on proximity — matching the original
    ``_check_crate_pickups`` behavior (a full player still consumes the crate).
    """

    # The client's own crate pickup range (CRATE_DISTANCE = 2.5). The old 3.0
    # radius + closely spaced crates let one walk-through consume BOTH the
    # ammo and health crate at once ("everything replenishes everything").
    touch_radius = 2.5
    support_check_interval = 0.10

    # Retail ``Crate`` (client crate.py, gameScene.pyd) air-drop physics,
    # measured on the live client 2026-09-26: a crate created in the air
    # free-falls at 30 blocks/s^2 (0.5 per 1/60 s frame).  Once the ground
    # hit-scan below it is closer than CRATE_PARACHUTE_DEPLOYMENT_HEIGHT (10)
    # the chute opens and every frame the stored velocity is multiplied by
    # CRATE_PARACHUTE_SLOWDOWN (0.75) after the move (terminal 1.5, the crate
    # moves 2.0 blocks/s); below CRATE_PARACHUTE_REMOVAL_HEIGHT (2) the chute
    # is dropped and it free-falls onto the support voxel, where it rests at
    # z == support z.  The server integrates the same fixed-step model so the
    # authoritative pickup position follows the model the players see.
    AIRDROP_GRAVITY = 30.0
    AIRDROP_STEP = 1.0 / 60.0
    AIRDROP_MAX_STEPS_PER_TICK = 30

    def __init__(self, refill, respawn_delay: float = 15.0, sound_id: int = None,
                 *, airdrop: bool = False, drop_start_z: float = 1.0,
                 drop_cue=None):
        self.refill = refill
        self.respawn_delay = float(respawn_delay)
        # Client SOUND_ID for the pickup cue (ammo 13 / health 14 / blocks 15).
        self.sound_id = sound_id
        # Map supply crates respawn by parachute from the sky above their
        # drop point instead of reappearing on the ground.
        self.airdrop = bool(airdrop)
        self.drop_start_z = float(drop_start_z)
        # Optional ``callable(ent)`` for the positioned aircraft fly-by cue.
        self.drop_cue = drop_cue

    # ------------------------------------------------------------------
    # Air drop
    # ------------------------------------------------------------------

    def on_respawn(self, ent, ctx) -> None:
        """Lift a respawning map crate to its drop altitude above home."""

        if not self.airdrop:
            return
        world = getattr(ctx, "world", None)
        if world is None or not callable(getattr(world, "get_solid", None)):
            return
        from shared import constants as C

        home_x, home_y, home_z = (float(value) for value in ent.home)
        x = int(math.floor(home_x))
        y = int(math.floor(home_y))
        if not (0 <= x < int(C.MAP_X) and 0 <= y < int(C.MAP_Y)):
            return
        landing = self._find_support(world, x, y, int(math.floor(home_z)))
        water_limit = int(getattr(C, "Z_ABOVE_WATERPLANE", 238))
        if landing is None or landing > water_limit:
            anchor = getattr(world, "dry_surface_anchor", None)
            if not callable(anchor):
                return
            try:
                ax, ay, az = anchor(home_x, home_y, search=64)
            except TypeError:
                ax, ay, az = anchor(home_x, home_y)
            home_x, home_y = float(ax), float(ay)
            x = int(math.floor(home_x))
            y = int(math.floor(home_y))
            landing = self._find_support(world, x, y, int(math.floor(az)))
            if landing is None or landing > water_limit:
                return
        # Blocks built on the drop point: the crate lands on top of them.
        landing = int(landing)
        while landing > 0 and world.get_solid(x, y, landing - 1):
            landing -= 1
        start_z = self._drop_start(world, x, y, landing)
        ent.x, ent.y, ent.z = home_x, home_y, float(start_z)
        ent.vel = (0.0, 0.0, 0.0)
        ent.falling = float(start_z) < float(landing)
        ent.fall_accumulator = 0.0
        ent.fall_clock = float(getattr(ctx, "now", 0.0))
        ent.parachute_deployed = False
        ent.parachute_removed = False
        ent.terrain_support_z = None
        ent.terrain_offset_z = 0.0
        if not ent.falling:
            ent.z = float(landing)
            ent.terrain_support_z = int(landing)
            return
        if self.drop_cue is not None:
            try:
                self.drop_cue(ent)
            except Exception:  # noqa: BLE001 - a cosmetic cue must not stop the drop
                pass

    def _drop_start(self, world, x: int, y: int, landing: int) -> float:
        """Highest free z over ``landing``: the sky, or just under a roof."""

        top = int(math.floor(self.drop_start_z))
        for z in range(int(landing) - 1, max(-1, top - 1), -1):
            if world.get_solid(x, y, z):
                # The crate model spans about one voxel above its origin, so
                # keep one clear cell between it and the overhang.
                return float(min(landing, z + 2))
        return float(min(landing, self.drop_start_z))

    def _advance_fall(self, ent, dt, ctx) -> None:
        from shared import constants as C

        world = getattr(ctx, "world", None)
        x = int(math.floor(ent.x))
        y = int(math.floor(ent.y))
        landing = None
        if world is not None and callable(getattr(world, "get_solid", None)):
            landing = self._find_support(
                world, x, y, max(0, int(math.floor(ent.z)))
            )
        if landing is None:
            landing = int(C.MAP_Z) - 1
        deploy = float(getattr(C, "CRATE_PARACHUTE_DEPLOYMENT_HEIGHT", 10))
        removal = float(getattr(C, "CRATE_PARACHUTE_REMOVAL_HEIGHT", 2))
        slowdown = float(getattr(C, "CRATE_PARACHUTE_SLOWDOWN", 0.75))
        step = self.AIRDROP_STEP
        vz = float(ent.vel[2])
        z = float(ent.z)
        # Integrate wall time, not ticks: the registry may round-robin skip
        # an overloaded tick, while the retail client keeps its own clock.
        now = float(getattr(ctx, "now", 0.0))
        clock = float(getattr(ent, "fall_clock", 0.0))
        elapsed = now - clock if clock > 0.0 and now >= clock else float(dt)
        ent.fall_clock = now
        ent.fall_accumulator += max(0.0, elapsed)
        steps = 0
        landed = False
        while (ent.fall_accumulator >= step
               and steps < self.AIRDROP_MAX_STEPS_PER_TICK):
            ent.fall_accumulator -= step
            steps += 1
            vz += self.AIRDROP_GRAVITY * step
            z += vz * step
            if z >= landing:
                landed = True
                break
            distance = float(landing) - z
            if not ent.parachute_deployed and distance < deploy:
                ent.parachute_deployed = True
            if (ent.parachute_deployed and not ent.parachute_removed
                    and distance < removal):
                ent.parachute_removed = True
            if ent.parachute_deployed and not ent.parachute_removed:
                vz *= slowdown
        if steps >= self.AIRDROP_MAX_STEPS_PER_TICK:
            # A long server stall must not replay seconds of backlog in one
            # tick; the client keeps falling on its own clock regardless.
            ent.fall_accumulator = 0.0
        if landed:
            ent.z = float(landing)
            ent.vel = (0.0, 0.0, 0.0)
            ent.falling = False
            ent.fall_accumulator = 0.0
            ent.parachute_deployed = True
            ent.parachute_removed = True
            ent.terrain_support_z = int(landing)
            ent.terrain_offset_z = 0.0
            ent.terrain_check_at = float(getattr(ctx, "now", 0.0))
            return
        ent.z = z
        ent.vel = (0.0, 0.0, vz)

    def on_tick(self, ent, dt, ctx) -> None:
        """Keep a map pickup attached to the live voxel column beneath it.

        The retail entity is static after packet 21 creation.  When players
        remove its supporting structure the authoritative server therefore
        has to settle it and emit ``ChangeEntity.SET_POSITION``.  Checks are
        throttled to 10 Hz and only scan a column after the remembered support
        voxel actually disappears.
        """

        if getattr(ent, "falling", False):
            self._advance_fall(ent, dt, ctx)
            return

        from shared import constants as C

        world = getattr(ctx, "world", None)
        if world is None or not callable(getattr(world, "get_solid", None)):
            return
        if ctx.now < float(getattr(ent, "terrain_check_at", 0.0)):
            return
        ent.terrain_check_at = ctx.now + self.support_check_interval

        x = int(math.floor(ent.x))
        y = int(math.floor(ent.y))
        if not (0 <= x < int(C.MAP_X) and 0 <= y < int(C.MAP_Y)):
            return

        support_z = ent.terrain_support_z
        if support_z is None:
            support_z = self._find_support(world, x, y, int(math.floor(ent.z)))
            if support_z is None:
                return
            ent.terrain_support_z = support_z
            ent.terrain_offset_z = float(ent.z) - float(support_z)
            return
        if world.get_solid(x, y, int(support_z)):
            return

        new_support = self._find_support(world, x, y, int(support_z) + 1)
        water_limit = int(getattr(C, "Z_ABOVE_WATERPLANE", 238))
        if new_support is not None and new_support <= water_limit:
            new_position = (
                float(ent.x),
                float(ent.y),
                float(new_support) + float(ent.terrain_offset_z),
            )
        else:
            # A collapsed bridge can leave no dry support in this column. Move
            # the pickup to the nearest dry surface instead of marooning a
            # permanent objective below the water plane.
            anchor = getattr(world, "dry_surface_anchor", None)
            if not callable(anchor):
                return
            try:
                new_position = anchor(ent.x, ent.y, search=64)
            except TypeError:
                new_position = anchor(ent.x, ent.y)
            new_support = int(math.floor(new_position[2]))
            if new_support > water_limit:
                return
            ent.terrain_offset_z = float(new_position[2]) - float(new_support)

        old_position = (float(ent.x), float(ent.y), float(ent.z))
        ent.x, ent.y, ent.z = (float(value) for value in new_position)
        ent.terrain_support_z = int(new_support)
        ent.home = (ent.x, ent.y, ent.z)
        if old_position != ent.home and ctx.move is not None:
            ctx.move(ent)

    @staticmethod
    def _find_support(world, x: int, y: int, start_z: int):
        """Return the first solid at/below ``start_z`` in AoS +Z-down space."""

        from shared import constants as C

        for z in range(max(0, int(start_z)), int(C.MAP_Z)):
            if world.get_solid(x, y, z):
                return z
        return None

    def on_touch(self, ent, player, ctx) -> bool:
        self.refill(player)
        if self.sound_id is not None:
            from server.audio import play_sound_to
            play_sound_to(player, self.sound_id)
        ent.alive = False
        ent.respawn_at = ctx.now + self.respawn_delay
        if ctx.destroy is not None:
            ctx.destroy(ent.entity_id)
        return True


class GraveBehavior(EntityBehavior):
    """A player's grave marker.

    The stock client gives graves their own small delayed explosion.  Keeping
    the fuse on the server makes the damage authoritative; destroying the
    entity at the same instant also drives the client's GraveEntity.on_delete
    visual/audio path.
    """

    def __init__(self, thrower_id, fuse=7.0, damage=25.0,
                 block_damage=3.0, blast_radius=3.0, kill_type=13,
                 explosion_center=None):
        self.thrower_id = int(thrower_id)
        self.fuse = float(fuse)
        self.damage = float(damage)
        self.block_damage = float(block_damage)
        self.blast_radius = float(blast_radius)
        self.crater_radius = 1
        self.kill_type = int(kill_type)
        self.force_destroy = False
        self.explosion_center = (
            None
            if explosion_center is None
            else tuple(float(value) for value in explosion_center)
        )
        import shared.constants as C
        self.knockback_min = float(getattr(C, "GRAVE_EXPLOSION_KNOCKBACK_MIN", 0.5))
        self.knockback_max = float(getattr(C, "GRAVE_EXPLOSION_KNOCKBACK_MAX", 1.0))
        self._detonate_at = None

    def get_explosion_center(self, ent):
        """Return the grave's settled blast point without freezing its model.

        GraveEntity performs gravity and collision locally, so its packet must
        retain the airborne death position.  The seven-second authoritative
        blast occurs after that fall has settled and therefore uses the dry
        supporting surface captured by the server at creation time.
        """

        if self.explosion_center is not None:
            return self.explosion_center
        return (ent.x, ent.y, ent.z)

    def on_tick(self, ent, dt, ctx) -> None:
        if self._detonate_at is None:
            self._detonate_at = ctx.now + self.fuse
            return
        if ctx.now >= self._detonate_at:
            _detonate_deployable(self, ent, ctx)


class MedpackBehavior(DamageableEntityBehavior):
    """A medic's placed medpack: heals teammates who step on it, for a limited
    number of uses, then despawns.

    DeployableActionService supplies the recovered MEDPACK_HEAL_AMOUNT (25)
    and MEDPACK_USES (3) constants. The generic constructor's full-heal default
    is retained for explicitly constructed custom entities; normal player and
    bot packs always use the service-supplied values. Each pack owns its uses.
    """

    touch_radius = 3.0

    hit_radius = 0.75
    hit_center_offset = (0.0, 0.0, 0.5)

    def __init__(self, team: int, heal_amount: int = 100, uses: int = 3,
                 health: float = 1.0):
        super().__init__(health)
        self.team = int(team)
        self.heal_amount = int(heal_amount)
        self.uses = int(uses)

    def on_touch(self, ent, player, ctx) -> bool:
        if player.team != self.team:
            return False
        if getattr(player, "health", 0) >= getattr(player, "max_health", 100):
            return False
        player.heal(self.heal_amount)
        self.uses -= 1
        if self.uses <= 0:
            _remove_entity(ent, ctx)
        return True


class TimedExplosiveBehavior(DamageableEntityBehavior):
    """Dynamite / timed charge: detonates a fixed fuse after placement,
    regardless of proximity. Explosion runs through the server's shared blast
    (crater + player damage).

    Retail gives the stick DYNAMITE_HEALTH = 1, the same one-point shell as
    LANDMINE_HEALTH, so a bullet, melee hit or a neighbouring blast destroys
    it early.  Like a shot mine it goes off through the normal blast rather
    than vanishing silently (which consequence retail chose is not in any
    client table; the mine precedent is followed).
    """

    hit_radius = 0.65

    def __init__(self, thrower_id, fuse, damage, block_damage, crater_radius,
                 kill_type, blast_radius=16.0, force_destroy=True,
                 knockback_min=None, knockback_max=None, health=None):
        import shared.constants as C
        super().__init__(
            getattr(C, "DYNAMITE_HEALTH", 1.0) if health is None else health
        )
        self.thrower_id = int(thrower_id)
        self.fuse = float(fuse)
        self.damage = float(damage)
        self.block_damage = float(block_damage)
        self.crater_radius = int(crater_radius)
        self.blast_radius = float(blast_radius)
        self.force_destroy = bool(force_destroy)
        self.kill_type = int(kill_type)
        import shared.constants as C
        self.damage_type = int(C.DYNAMITE_DAMAGE)
        self.knockback_min = float(
            getattr(C, "DYNAMITE_EXPLOSION_KNOCKBACK_MIN", 0.1)
            if knockback_min is None else knockback_min
        )
        self.knockback_max = float(
            getattr(C, "DYNAMITE_EXPLOSION_KNOCKBACK_MAX", 0.15)
            if knockback_max is None else knockback_max
        )
        self._detonate_at = None

    def on_tick(self, ent, dt, ctx) -> None:
        if self._detonate_at is None:
            self._detonate_at = ctx.now + self.fuse
            return
        if ctx.now >= self._detonate_at:
            _detonate_deployable(self, ent, ctx)

    def get_explosion_center(self, ent):
        """Use the rendered attachment center, outside its support voxel."""

        return _attached_face_center(ent)

    def get_hit_center(self, ent):
        return _attached_face_center(ent)

    def on_destroyed(self, ent, source, ctx) -> None:
        if source is not None and hasattr(source, "team"):
            self.triggered_by = source
        _detonate_deployable(self, ent, ctx)

    def on_support_lost(self, ent, ctx) -> None:
        _detonate_deployable(self, ent, ctx)


class ProximityMineBehavior(DamageableEntityBehavior):
    """Landmine: arms after a short delay, then detonates when an ENEMY (not
    the placer's team) enters the trigger radius.

    Retail gives the placed entity one health, so weapon/explosion damage
    detonates it instead of leaving an indestructible shell.
    """

    def __init__(self, thrower_id, team, damage, block_damage, crater_radius,
                 kill_type, trigger_radius=2.5, arm_delay=1.0,
                 blast_radius=16.0, force_destroy=True, detection_layers=3,
                 knockback_min=None, knockback_max=None, health=None,
                 vertical_offset=None, damage_type=None):
        import shared.constants as C
        super().__init__(
            getattr(C, "LANDMINE_HEALTH", 1.0)
            if health is None else health
        )
        self.thrower_id = int(thrower_id)
        self.team = int(team)
        self.damage = float(damage)
        self.block_damage = float(block_damage)
        self.crater_radius = int(crater_radius)
        self.blast_radius = float(blast_radius)
        self.force_destroy = bool(force_destroy)
        self.kill_type = int(kill_type)
        self.trigger_radius = float(trigger_radius)
        self.arm_delay = float(arm_delay)
        self.detection_layers = int(detection_layers)
        self.vertical_offset = float(
            getattr(
                C,
                "LANDMINE_EXPLOSION_AND_DETECTION_VERTICAL_OFFSET",
                -0.5,
            )
            if vertical_offset is None else vertical_offset
        )
        self.damage_type = (
            int(getattr(C, "LANDMINE_DAMAGE", 15))
            if damage_type is None else int(damage_type)
        )
        # Mines explicitly detect through three rebuilt layers. Applying the
        # generic grenade LOS gate from inside those layers would detect the
        # player and then discard all HP damage.
        self.ignore_player_los = True
        self.knockback_min = float(
            getattr(C, "LANDMINE_EXPLOSION_KNOCKBACK_MIN", 0.75)
            if knockback_min is None else knockback_min
        )
        self.knockback_max = float(
            getattr(C, "LANDMINE_EXPLOSION_KNOCKBACK_MAX", 0.75)
            if knockback_max is None else knockback_max
        )
        self._armed_at = None

    def _mine_center(self, ent):
        """Return the rendered/explosion center from its placed voxel."""

        return (
            float(ent.x) + 0.5,
            float(ent.y) + 0.5,
            float(ent.z) + self.vertical_offset,
        )

    def get_hit_center(self, ent):
        return self._mine_center(ent)

    def get_explosion_center(self, ent):
        return self._mine_center(ent)

    def on_tick(self, ent, dt, ctx) -> None:
        if self._armed_at is None:
            self._armed_at = ctx.now + self.arm_delay
            return
        if ctx.now < self._armed_at:
            return
        r2 = self.trigger_radius ** 2
        mine_x, mine_y, mine_z = self._mine_center(ent)
        for player in ctx.players:
            if getattr(player, "team", None) == self.team:
                continue  # own team never trips it
            dx = player.x - mine_x
            dy = player.y - mine_y
            # Detection is column/layer based in the stock game: a mine may be
            # re-buried two blocks deep and must still trigger. Compare the
            # player's feet to the mine vertically, but use the 2.5 range in
            # the horizontal plane.
            crouched = bool(getattr(getattr(player, "input", None), "crouch", False))
            try:
                import shared.constants as C
                feet_offset = float(getattr(
                    C,
                    "PLAYER_CROUCHING_POS_ABOVE_GROUND" if crouched
                    else "PLAYER_STANDING_POS_ABOVE_GROUND",
                    1.35 if crouched else 2.25,
                ))
            except Exception:
                feet_offset = 1.35 if crouched else 2.25
            feet_z = player.z + feet_offset
            if (
                (dx * dx + dy * dy) <= r2
                and abs(feet_z - mine_z) <= self.detection_layers
            ):
                _detonate_deployable(self, ent, ctx)
                return

    def on_destroyed(self, ent, source, ctx) -> None:
        """A shot mine explodes through the same authoritative blast path.

        The player who shot/blasted it *instigated* the explosion: when that
        is the owner's teammate, the owner's death (and any teammate harm)
        is theirs for kill credit and grief accounting (server.conduct).
        """

        if source is not None and hasattr(source, "team"):
            self.triggered_by = source
        _detonate_deployable(self, ent, ctx)

    def on_support_lost(self, ent, ctx) -> None:
        # Removing the voxel under a mine is the same stock destruction event
        # as shooting its one-point shell; it must not remain armed in mid-air.
        _detonate_deployable(self, ent, ctx)


class RemoteChargeBehavior(DamageableEntityBehavior):
    """Placed C4: inert until its owner sends DetonateC4."""

    hit_radius = 0.65

    def __init__(self, thrower_id, damage=300.0, block_damage=7.0,
                 crater_radius=2, kill_type=36, blast_radius=8.0,
                 health=1.0):
        super().__init__(health)
        self.thrower_id = int(thrower_id)
        self.damage = float(damage)
        self.block_damage = float(block_damage)
        self.crater_radius = int(crater_radius)
        self.kill_type = int(kill_type)
        self.blast_radius = float(blast_radius)
        self.force_destroy = True
        import shared.constants as C
        self.damage_type = int(C.C4_DAMAGE)
        self.knockback_min = float(getattr(C, "C4_EXPLOSION_KNOCKBACK_MIN", 0.1))
        self.knockback_max = float(getattr(C, "C4_EXPLOSION_KNOCKBACK_MAX", 0.15))

    def detonate(self, ent, ctx) -> None:
        if ent.alive:
            _detonate_deployable(self, ent, ctx)

    def _forget_owner(self, ent, ctx) -> None:
        owner = ctx.server.players.get(ent.player_id) if ctx.server else None
        if owner is not None:
            owner._c4_entity_ids = [
                entity_id for entity_id in
                list(getattr(owner, "_c4_entity_ids", []) or [])
                if int(entity_id) != int(ent.entity_id)
            ]

    def get_hit_center(self, ent):
        # PlaceC4 coordinates name the supporting voxel.  ``face`` selects the
        # exposed face on which the model is centered (client C4Weapon ghost
        # transform, recovered Python source).
        return _attached_face_center(ent)

    def get_explosion_center(self, ent):
        """Detonate from the visible C4 face instead of inside terrain."""

        return _attached_face_center(ent)

    def on_destroyed(self, ent, source, ctx) -> None:
        self._forget_owner(ent, ctx)
        super().on_destroyed(ent, source, ctx)

    def on_support_lost(self, ent, ctx) -> None:
        self._forget_owner(ent, ctx)
        self.detonate(ent, ctx)


class RadarStationBehavior(DamageableEntityBehavior):
    """Short-lived Scout radar station.

    Detection is client-side: the stock Minimap asks every RadarStationEntity
    of the viewer's team ``can_detect_player`` (250 blocks,
    ``C.RADAR_STATION_RANGE``) for each enemy. The server only owns the
    entity's lifetime, health and one-station-per-owner rule.

    Retail values: 45 s lifetime (``C.RADAR_STATION_LIFETIME``, sent as the
    packet-21 fuse the client counts down) and 45 health.
    """

    hit_radius = 0.9
    hit_center_offset = (0.5, 0.5, 0.55)

    def __init__(self, team, lifetime=45.0, health=45.0):
        super().__init__(health)
        self.team = int(team)
        self.lifetime = float(lifetime)
        self._expires_at = None

    def on_tick(self, ent, dt, ctx) -> None:
        if self._expires_at is None:
            self._expires_at = ctx.now + self.lifetime
            return
        if ctx.now < self._expires_at:
            # Keep the wire fuse at the remaining lifetime so a late joiner's
            # CreateEntity replay counts down from the live value, not 45.
            ent.fuse = max(0.0, float(self._expires_at - ctx.now))
            return
        self.on_destroyed(ent, None, ctx)

    def on_destroyed(self, ent, source, ctx) -> None:
        if not ent.alive:
            # Already torn down (expiry, damage, replacement or support loss
            # may race in one tick); never release the team count twice.
            return
        if ctx.server is not None:
            ctx.server._radar_station_removed(self.team)
            owner = ctx.server.players.get(ent.player_id)
            owner_entity_id = (
                getattr(owner, "_radar_entity_id", None)
                if owner is not None
                else None
            )
            if (
                owner_entity_id is not None
                and int(owner_entity_id) == int(ent.entity_id)
            ):
                owner._radar_entity_id = None
        super().on_destroyed(ent, source, ctx)

    def on_support_lost(self, ent, ctx) -> None:
        # Radar has no destructive action; use its normal teardown so team
        # visibility and the owner's one-live-station slot are released too.
        self.on_destroyed(ent, None, ctx)


def _detonate_deployable(behavior, ent, ctx) -> None:
    """Shared detonation for timed/proximity deployables: run the server blast,
    despawn the entity, and tell clients (removes the model + FX)."""
    thrower = ctx.server.players.get(behavior.thrower_id) if ctx.server else None
    # Mark dead before applying blast damage so the source charge cannot route
    # its own explosion back through on_damage and recursively destroy itself.
    ent.alive = False
    if ctx.server is not None:
        get_center = getattr(behavior, "get_explosion_center", None)
        if callable(get_center):
            gx, gy, gz = get_center(ent)
        else:
            gx, gy, gz = ent.x, ent.y, ent.z
        from server.conduct import blast_instigator

        trigger = getattr(behavior, "triggered_by", None)
        with blast_instigator(
            ctx.server, trigger if trigger is not None else thrower
        ):
            _apply_deployable_blast(behavior, ent, ctx, thrower, gx, gy, gz)
    if ctx.destroy is not None:
        ctx.destroy(ent.entity_id)
    # One-shot entities must leave the registry as well as the clients.  A
    # dead entry with no respawn_at otherwise leaks forever and eventually
    # exhausts the uint16 entity id space.
    registry = getattr(ctx.server, "entity_registry", None) if ctx.server else None
    if registry is not None:
        registry.remove(ent.entity_id)


def _apply_deployable_blast(behavior, ent, ctx, thrower, gx, gy, gz) -> None:
    """Run the shared server blast for one deployable."""
    ctx.server._apply_blast(
        gx, gy, gz, behavior.damage, behavior.block_damage,
        behavior.kill_type, thrower,
        crater_radius=behavior.crater_radius,
        force_destroy=getattr(behavior, "force_destroy", True),
        blast_radius=getattr(behavior, "blast_radius", 16.0),
        knockback_min=getattr(behavior, "knockback_min", 0.0),
        knockback_max=getattr(behavior, "knockback_max", 0.0),
        native_damage_type=getattr(behavior, "damage_type", None),
        causer_entity_id=int(ent.entity_id),
        ignore_player_los=bool(
            getattr(behavior, "ignore_player_los", False)
        ),
    )


def _remove_entity(ent, ctx) -> None:
    """Idempotently remove a one-shot placed entity from server and clients."""
    if not ent.alive:
        return
    ent.alive = False
    if ctx.destroy is not None:
        ctx.destroy(ent.entity_id)
    registry = getattr(ctx.server, "entity_registry", None) if ctx.server else None
    if registry is not None:
        registry.remove(ent.entity_id)


class IntelBehavior(EntityBehavior):
    """CTF intel/flag pickup — a thin adapter over the mode's existing intel
    state. On touch it hands off to ``mode.pick_up_intel(player, ent)``.

    NOTE: not yet wired into ctf.py. The INTEL_PICKUP wire entity must be
    verified against the compiled client first (a bad type/state crashes it,
    same class as the historic pickup=0xFF bug). The class exists so the CTF
    wiring is a one-liner once verified live.
    """

    touch_radius = 3.0

    def __init__(self, mode):
        self.mode = mode

    def on_touch(self, ent, player, ctx) -> bool:
        handler = getattr(self.mode, "pick_up_intel", None)
        if handler is None:
            return False
        return bool(handler(player, ent))
