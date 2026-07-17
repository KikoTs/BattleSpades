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

    def __init__(self, refill, respawn_delay: float = 15.0, sound_id: int = None):
        self.refill = refill
        self.respawn_delay = float(respawn_delay)
        # Client SOUND_ID for the pickup cue (ammo 13 / health 14 / blocks 15).
        self.sound_id = sound_id

    def on_tick(self, ent, dt, ctx) -> None:
        """Keep a map pickup attached to the live voxel column beneath it.

        The retail entity is static after packet 21 creation.  When players
        remove its supporting structure the authoritative server therefore
        has to settle it and emit ``ChangeEntity.SET_POSITION``.  Checks are
        throttled to 10 Hz and only scan a column after the remembered support
        voxel actually disappears.
        """

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
                 block_damage=3.0, blast_radius=3.0, kill_type=13):
        self.thrower_id = int(thrower_id)
        self.fuse = float(fuse)
        self.damage = float(damage)
        self.block_damage = float(block_damage)
        self.blast_radius = float(blast_radius)
        self.crater_radius = 1
        self.kill_type = int(kill_type)
        self.force_destroy = False
        import shared.constants as C
        self.knockback_min = float(getattr(C, "GRAVE_EXPLOSION_KNOCKBACK_MIN", 0.5))
        self.knockback_max = float(getattr(C, "GRAVE_EXPLOSION_KNOCKBACK_MAX", 1.0))
        self._detonate_at = None

    def on_tick(self, ent, dt, ctx) -> None:
        if self._detonate_at is None:
            self._detonate_at = ctx.now + self.fuse
            return
        if ctx.now >= self._detonate_at:
            _detonate_deployable(self, ent, ctx)


class MedpackBehavior(DamageableEntityBehavior):
    """A medic's placed medpack: heals teammates who step on it, for a limited
    number of uses, then despawns.

    The real heal amount/model lives in compiled client code (not in the
    constant catalog) — this uses full-heal-per-touch with 3 uses, flagged for
    live calibration. One instance per placed medpack (it carries use state).
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
        if getattr(player, "health", 0) >= 100:
            return False
        player.heal(self.heal_amount)
        self.uses -= 1
        if self.uses <= 0:
            _remove_entity(ent, ctx)
        return True


class TimedExplosiveBehavior(EntityBehavior):
    """Dynamite / timed charge: detonates a fixed fuse after placement,
    regardless of proximity. Explosion runs through the server's shared blast
    (crater + player damage)."""

    def __init__(self, thrower_id, fuse, damage, block_damage, crater_radius,
                 kill_type, blast_radius=16.0, force_destroy=True,
                 knockback_min=None, knockback_max=None):
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


class ProximityMineBehavior(EntityBehavior):
    """Landmine: arms after a short delay, then detonates when an ENEMY (not
    the placer's team) enters the trigger radius."""

    def __init__(self, thrower_id, team, damage, block_damage, crater_radius,
                 kill_type, trigger_radius=2.5, arm_delay=1.0,
                 blast_radius=16.0, force_destroy=True, detection_layers=3,
                 knockback_min=None, knockback_max=None):
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
        import shared.constants as C
        self.knockback_min = float(
            getattr(C, "LANDMINE_EXPLOSION_KNOCKBACK_MIN", 0.75)
            if knockback_min is None else knockback_min
        )
        self.knockback_max = float(
            getattr(C, "LANDMINE_EXPLOSION_KNOCKBACK_MAX", 0.75)
            if knockback_max is None else knockback_max
        )
        self._armed_at = None

    def on_tick(self, ent, dt, ctx) -> None:
        if self._armed_at is None:
            self._armed_at = ctx.now + self.arm_delay
            return
        if ctx.now < self._armed_at:
            return
        r2 = self.trigger_radius ** 2
        for player in ctx.players:
            if getattr(player, "team", None) == self.team:
                continue  # own team never trips it
            dx = player.x - ent.x
            dy = player.y - ent.y
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
            if (dx * dx + dy * dy) <= r2 and abs(feet_z - ent.z) <= self.detection_layers:
                _detonate_deployable(self, ent, ctx)
                return


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

    def get_hit_center(self, ent):
        # PlaceC4 coordinates name the supporting voxel.  ``face`` selects the
        # exposed face on which the model is centered (client C4Weapon ghost
        # transform, recovered Python source).
        return _attached_face_center(ent)

    def get_explosion_center(self, ent):
        """Detonate from the visible C4 face instead of inside terrain."""

        return _attached_face_center(ent)

    def on_destroyed(self, ent, source, ctx) -> None:
        owner = ctx.server.players.get(ent.player_id) if ctx.server else None
        if owner is not None:
            owner._c4_entity_ids = [
                entity_id for entity_id in
                list(getattr(owner, "_c4_entity_ids", []) or [])
                if int(entity_id) != int(ent.entity_id)
            ]
        super().on_destroyed(ent, source, ctx)


class RadarStationBehavior(DamageableEntityBehavior):
    """Short-lived Scout radar station.

    Visibility is reference-counted by the server so overlapping stations do
    not hide the enemy team when only one of them expires.
    """

    hit_radius = 0.9
    hit_center_offset = (0.5, 0.5, 0.55)

    def __init__(self, team, lifetime=250.0, health=45.0):
        super().__init__(health)
        self.team = int(team)
        self.lifetime = float(lifetime)
        self._expires_at = None

    def on_tick(self, ent, dt, ctx) -> None:
        if self._expires_at is None:
            self._expires_at = ctx.now + self.lifetime
            return
        if ctx.now < self._expires_at:
            return
        self.on_destroyed(ent, None, ctx)

    def on_destroyed(self, ent, source, ctx) -> None:
        if ctx.server is not None:
            ctx.server._radar_station_removed(self.team)
            owner = ctx.server.players.get(ent.player_id)
            if (owner is not None
                    and int(getattr(owner, "_radar_entity_id", -1)) == int(ent.entity_id)):
                owner._radar_entity_id = None
        super().on_destroyed(ent, source, ctx)


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
        )
    if ctx.destroy is not None:
        ctx.destroy(ent.entity_id)
    # One-shot entities must leave the registry as well as the clients.  A
    # dead entry with no respawn_at otherwise leaks forever and eventually
    # exhausts the uint16 entity id space.
    registry = getattr(ctx.server, "entity_registry", None) if ctx.server else None
    if registry is not None:
        registry.remove(ent.entity_id)


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
