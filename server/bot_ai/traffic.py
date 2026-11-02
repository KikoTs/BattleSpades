"""Safe local lane choice and right of way without inventing a route.

Only ordinary walking is adjusted. Every side/back step checks the live full
body footprint; jumps, drops, water, construction and airborne control retain
their original owner. Shared leases are advisory and never lock human input.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from .human_behavior import variation
from .messages import (BotActionKind, BotIntent, BotIntentPriority,
                       MovementAffordance, PerceptionFrame, PlayerSnapshot)


@dataclass(slots=True)
class _Motion:
    life: int
    seen_at: float
    heading: tuple[float, float] | None = None
    turn_until: float = 0.0
    grounded: bool = True
    landed_until: float = 0.0
    yield_until: float = 0.0
    yielding_to: int = -1
    yield_heading: tuple[float, float] = (0., 0.)
    queued_to: int = -1
    queued_since: float = 0.
    reconsider_at: float = 0.
    control_signature: tuple = ()


class Traffic:
    def __init__(self, world: object) -> None:
        self.world = world
        self.motion: dict[tuple[int, int], _Motion] = {}
        self.epoch: tuple[int, int] | None = None
        self.on_blocked = None

    def reset(self) -> None:
        self.motion.clear()
        self.epoch = None

    def forget(self, player_id: int, generation: int) -> None:
        self.motion.pop((player_id, generation), None)

    def _clear(self, actor: PlayerSnapshot, dx: float, dy: float) -> bool:
        solid = getattr(self.world, "solid", None)
        surface = getattr(self.world, "surface", None)
        if not callable(solid) or not callable(surface):
            return False
        floor = int(round(actor.position[2] + 2.25))
        for distance in (.45, 1.0):
            px, py = actor.position[0] + dx * distance, actor.position[1] + dy * distance
            for ox in (-.43, .43):
                for oy in (-.43, .43):
                    x, y = math.floor(px + ox), math.floor(py + oy)
                    if not (0 <= x < 512 and 0 <= y < 512):
                        return False
                    support = surface(x, y, actor.position[2], vertical_span=2, allow_water=False)
                    if support is None or abs(support.support_z - floor) > 1:
                        return False
                    if not solid(x, y, support.support_z) or any(
                            solid(x, y, support.support_z - dz) for dz in (1, 2, 3)):
                        return False
        return True

    @staticmethod
    def _right_of_way(actor: PlayerSnapshot) -> tuple[int, int, int]:
        # Human input always wins; objective carriers next. Stable identity
        # resolves equal bot priority without both repeatedly changing minds.
        return (0 if not actor.is_bot else 1,
                0 if actor.carried_entity_id >= 0 else 1, actor.player_id)

    def adjust(self, frame: PerceptionFrame, intent: BotIntent) -> BotIntent:
        epoch = frame.map_epoch, frame.mode_epoch
        if self.epoch != epoch:
            self.reset()
            self.epoch = epoch
        actor = next((p for p in frame.players if p.player_id == intent.bot_id
                      and p.generation == intent.bot_generation), None)
        if actor is None:
            return intent
        key = actor.player_id, actor.generation
        now = frame.created_at
        state = self.motion.get(key)
        control_signature=(actor.team,actor.class_id,actor.carried_entity_id,actor.can_shoot)
        if state is None or state.life != actor.life_id or state.control_signature != control_signature:
            if len(self.motion) >= 64:
                self.motion.pop(min(self.motion, key=lambda k: self.motion[k].seen_at))
            state = self.motion[key] = _Motion(actor.life_id, now, grounded=actor.grounded)
            state.control_signature=control_signature
        state.seen_at = now
        if actor.grounded and not state.grounded:
            state.landed_until = now + .12
        state.grounded = actor.grounded
        movement = intent.movement
        if (not actor.alive or not actor.grounded or actor.wade
                or intent.action.kind is not BotActionKind.NONE
                or movement.affordance not in {MovementAffordance.WALK, MovementAffordance.CROUCH}
                or movement.jump or movement.crouch or movement.sneak):
            return intent
        length = math.hypot(*movement.direction[:2])
        if length < .1:
            return intent
        if not self._clear(actor, 0., 0.):
            # Unknown/airborne support belongs to the native safety motor,
            # not a speculative "narrow corridor" classification.
            return intent
        dx, dy = movement.direction[0] / length, movement.direction[1] / length
        # Probe once per new decision, never from the 60 Hz gameplay thread.
        side = -1. if variation(actor.player_id, actor.generation, 5) < .5 else 1.
        left, right = (-dy, dx), (dy, -dx)
        left_clear, right_clear = self._clear(actor, *left), self._clear(actor, *right)
        narrow = not left_clear and not right_clear
        if narrow and movement.travel_waypoint is not None:
            wx = movement.travel_waypoint[0] - actor.position[0]
            wy = movement.travel_waypoint[1] - actor.position[1]
            span = math.hypot(wx, wy)
            if span > .1:
                dx, dy = wx / span, wy / span
        neighbors = sorted((p for p in frame.players if p.player_id != actor.player_id
            and p.team == actor.team and p.alive and p.spawned
            and abs(p.position[2] - actor.position[2]) < 2
            and math.dist(p.position[:2], actor.position[:2]) < 2.6),
            key=lambda p: (self._right_of_way(p), math.dist(p.position[:2], actor.position[:2])))
        blocker = None
        for other in neighbors:
            rx, ry = other.position[0] - actor.position[0], other.position[1] - actor.position[1]
            ahead, across = rx * dx + ry * dy, abs(rx * dy - ry * dx)
            if ahead < -.25 or across > .85:
                continue
            approaching = other.orientation[0] * dx + other.orientation[1] * dy < -.25
            human = not other.is_bot
            if (human and ahead < 1.8 or approaching and ahead < 2.4
                    and self._right_of_way(other) < self._right_of_way(actor)):
                blocker = other
                break
            if ahead < 1.05 and not approaching:
                # A queue follows a leader; lateral repulsion is unsafe on a
                # one-block bridge and pushing a stationary ally solves nothing.
                leader_speed = 32.0 * math.hypot(*other.velocity[:2])
                leader_along = (other.velocity[0] * dx + other.velocity[1] * dy) * 32.0
                if leader_speed >= 2.5 and leader_along > 0.5 * leader_speed:
                    # The body in front is itself on its way, our way. Halting
                    # behind it (and again when it had moved a block on) made
                    # a horde of sixteen on one corridor a stop-and-go string
                    # at a third of its speed. Native collision keeps the two
                    # apart; only outrun a leader who is merely walking.
                    state.queued_to = -1
                    if movement.sprint and leader_speed < 7.0:
                        return replace(intent, movement=replace(movement, sprint=False))
                    return intent
                if state.queued_to != other.player_id:
                    state.queued_to, state.queued_since = other.player_id, now
                if now-state.queued_since > 1.:
                    for hx,hy in (left,right):
                        direction = (.6*dx+.8*hx,.6*dy+.8*hy)
                        end = (actor.position[0]+direction[0],actor.position[1]+direction[1])
                        if (self._clear(actor,*direction) and
                                all(math.dist(end,p.position[:2]) >= 1.1 for p in neighbors)):
                            return replace(intent,movement=replace(movement,
                                direction=(*direction,0.),sprint=False,sneak=True),
                                social_cue="yield_detour")
                if now-state.queued_since > 2.5 and now >= state.reconsider_at:
                    state.reconsider_at = now+5
                    if callable(self.on_blocked):
                        self.on_blocked(actor, now)
                return replace(intent, movement=replace(movement, direction=(0., 0., 0.), sprint=False),
                               social_cue="yield_queue")
        if blocker is not None:
            if state.yielding_to != blocker.player_id or now >= state.yield_until:
                candidates = (left, right) if side < 0 else (right, left)
                heading = next((h for h in candidates if self._clear(actor, *h)), None)
                if heading is None and self._clear(actor, -dx, -dy):
                    heading = (-dx, -dy)
                state.yield_heading = heading or (0., 0.)
                state.yielding_to, state.yield_until = blocker.player_id, now + .5
            heading = state.yield_heading
            # A human edit can remove an alcove while its lease is active.
            if heading != (0., 0.) and not self._clear(actor, *heading):
                heading = (0., 0.)
            return replace(intent, movement=replace(movement,
                direction=(*heading, 0.), sprint=False, sneak=heading != (0., 0.)),
                social_cue="yield_human" if not blocker.is_bot else "yield_passage")
        state.yielding_to = -1
        state.queued_to = -1
        peaceful = intent.priority < BotIntentPriority.COMBAT and not (
            intent.look is not None and (intent.look.visible or intent.look.glance))
        if peaceful and state.heading is not None and dx * state.heading[0] + dy * state.heading[1] < -.35:
            state.heading = (dx, dy)
            state.turn_until = now + .12 + variation(*key, 19) * .08
        if state.heading is None:
            state.heading = (dx, dy)
        if peaceful and (now < state.turn_until or now < state.landed_until):
            return replace(intent, movement=replace(movement, direction=(0., 0., 0.), sprint=False))
        state.heading = (dx, dy)
        if peaceful and movement.travel_waypoint is not None:
            remaining = dict(intent.debug_navigation).get("route_remaining", -1)
            distance = math.dist(actor.position[:2], movement.travel_waypoint[:2])
            speed = 32.0 * math.hypot(*actor.velocity[:2])
            if remaining == 1 and speed > 2.5 and distance < min(1.0, .15 + speed * .085):
                return replace(intent, movement=replace(movement, direction=(0., 0., 0.), sprint=False))
        if narrow:
            return replace(intent, movement=replace(movement, direction=(dx, dy, 0.),
                                                    sprint=False, sneak=True))
        if peaceful and movement.travel_source is not None and movement.travel_waypoint is not None:
            route_span = math.dist(movement.travel_source[:2], movement.travel_waypoint[:2])
            if route_span >= 5:
                lateral = .08 * side
                hx, hy = dx - dy * lateral, dy + dx * lateral
                norm = math.hypot(hx, hy)
                if self._clear(actor, hx / norm, hy / norm):
                    return replace(intent, movement=replace(movement,
                        direction=(hx / norm, hy / norm, 0.)))
        return intent
