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


class EntityBehavior:
    """Base behavior — inert. Subclass and override the hooks you need."""

    touch_radius: float = 0.0     # 0 => registry skips the proximity test
    takes_damage: bool = False    # gate for on_damage routing

    def on_tick(self, ent, dt, ctx) -> None:
        pass

    def on_touch(self, ent, player, ctx) -> bool:
        return False

    def on_damage(self, ent, amount, source, ctx) -> None:
        pass


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

    def __init__(self, refill, respawn_delay: float = 15.0):
        self.refill = refill
        self.respawn_delay = float(respawn_delay)

    def on_touch(self, ent, player, ctx) -> bool:
        self.refill(player)
        ent.alive = False
        ent.respawn_at = ctx.now + self.respawn_delay
        if ctx.destroy is not None:
            ctx.destroy(ent.entity_id)
        return True


class GraveBehavior(EntityBehavior):
    """A player's grave marker. Inert: explicitly removed when the player
    respawns (no auto-respawn, no touch). Reserved for a future takes_damage
    model (graves are diggable in some modes)."""


class MedpackBehavior(EntityBehavior):
    """A medic's placed medpack: heals teammates who step on it, for a limited
    number of uses, then despawns.

    The real heal amount/model lives in compiled client code (not in the
    constant catalog) — this uses full-heal-per-touch with 3 uses, flagged for
    live calibration. One instance per placed medpack (it carries use state).
    """

    touch_radius = 3.0

    def __init__(self, team: int, heal_amount: int = 100, uses: int = 3):
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
            ent.alive = False
            if ctx.destroy is not None:
                ctx.destroy(ent.entity_id)
        return True


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
