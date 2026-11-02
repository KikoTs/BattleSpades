"""Bounded, interruptible social attention on top of competent locomotion.

This controller never creates combat actions, routes or movement directions.
It uses visible partners, mode events, audible cues and confirmed construction.
The ordinary safety/aim motor remains authoritative over every suggestion.

Every look it adds is a ``glance``: the route, movement keys, posture, tool
and actions keep their original owner, and the director turns the head with
its relaxed, bounded gaze dynamics. Every timing is drawn from the bot's own
identity, so no two bots move in step.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math

import shared.constants as C

from .messages import (
    BotActionKind, BotIntent, BotIntentPriority, LookIntent, MovementAffordance,
    ObjectiveSnapshot, PerceptionFrame, PlayerSnapshot, StimulusKind, Vector3,
)

_MAX_LIVES = 64
_MAX_PARTNERS = 8
_PARTNER_RANGE = 7.0
_PRECISE = frozenset({MovementAffordance.JUMP, MovementAffordance.DROP,
                     MovementAffordance.SWIM, MovementAffordance.JETPACK,
                     MovementAffordance.BREACH, MovementAffordance.BUILD_STEP,
                     MovementAffordance.BUILD_BRIDGE, MovementAffordance.PLACE_PREFAB})
_ZOMBIE_CLASSES = frozenset({
    int(C.CLASS_ZOMBIE), int(C.CLASS_FAST_ZOMBIE), int(C.CLASS_JUMP_ZOMBIE),
})
# An enemy this close is a fight (or claw business for the infected, who
# cannot shoot); one farther away is only a sight. A zombie that sees its
# prey on a roof twenty blocks up is at work, not in combat.
_FOE_BUSY_RANGE = 35.0
_CLAW_BUSY_RANGE = 6.0
_WORK_KINDS = frozenset({BotActionKind.PLACE_PREFAB.value, BotActionKind.BUILD.value,
                         BotActionKind.BUILD_LINE.value})
_THREAT_SOUNDS = frozenset({StimulusKind.SHOT, StimulusKind.EXPLOSION, StimulusKind.DEATH})
# Zombie order roles whose eyes stay on the work while walking (hunt 0,
# dig_root 2, climb 3, tunnel 5); surround 4 and flank 1 glance about.
_ROAMING_ORDER_STATES = frozenset({1, 4, 6})


def variation(*values: int) -> float:
    """Stable temperament/event variation without consuming combat randomness."""
    value = 2166136261
    for item in values:
        value = ((value ^ int(item)) * 16777619) & 0xffffffff
        value ^= value >> 13
    return value / 0xffffffff


def temperament(*values: int) -> float:
    """A well-mixed stable draw in [0, 1) for per-bot rhythm parameters.

    ``variation`` mixes weakly: neighbouring ids, or the same identity with
    different salts, come out nearly equal, which put three of sixteen bots
    in step. Scan width, period and phase need draws that are independent of
    each other and of the next bot's.
    """
    value = 0x9E3779B97F4A7C15
    for item in values:
        value = ((value ^ (int(item) & 0xffffffffffffffff)) * 0xBF58476D1CE4E5B9) & 0xffffffffffffffff
        value ^= value >> 31
        value = (value * 0x94D049BB133111EB) & 0xffffffffffffffff
        value ^= value >> 29
    return (value >> 11) / float(1 << 53)


@dataclass(slots=True)
class _Partner:
    seen_at: float
    crouching: bool = False
    ready_at: float = 0.0
    attention_at: float = -math.inf
    encounters: int = 0


@dataclass(slots=True)
class _Gesture:
    kind: str
    target: Vector3
    starts_at: float
    ends_at: float
    crouch: bool = False


@dataclass(slots=True)
class _RoamingLook:
    target: Vector3
    heading: tuple[float, float]
    ends_at: float


@dataclass(slots=True)
class _Life:
    identity: tuple[int, int, int]
    team: int
    seen_at: float
    partners: dict[tuple[int, int], _Partner] = field(default_factory=dict)
    gesture: _Gesture | None = None
    next_idle_at: float = 0.0
    idle_target: Vector3 | None = None
    last_objective: float = -math.inf
    next_event_at: float = 0.0
    next_roam_at: float = 0.0
    roam_sequence: int = 0
    roaming_look: _RoamingLook | None = None
    # When the feet last came to rest; infinity while walking.
    still_since: float = math.inf
    # Audible threats already weighed, and the next moment one may turn the head.
    heard_at: float = -math.inf
    next_sound_at: float = 0.0
    # Teammates' placements already noticed (player id -> action time).
    work_seen: dict[int, float] = field(default_factory=dict)
    next_work_at: float = 0.0
    next_order_glance_at: float = 0.0


class HumanBehavior:
    """Worker-local memory with per-partner and squad-wide interaction limits."""

    def __init__(self, world: object) -> None:
        self.world = world
        self.lives: dict[tuple[int, int], _Life] = {}
        self.epoch: tuple[int, int] | None = None
        self.team_reply_ready: dict[tuple[int, int, int], float] = {}
        self.completions: dict[tuple[int, int, int], tuple[str, Vector3, float]] = {}

    def reset(self) -> None:
        self.lives.clear()
        self.team_reply_ready.clear()
        self.completions.clear()
        self.epoch = None

    def forget(self, player_id: int, generation: int) -> None:
        key = int(player_id), int(generation)
        self.lives.pop(key, None)
        for identity in tuple(self.completions):
            if identity[:2] == key:
                self.completions.pop(identity, None)

    def completed(self, identity: tuple[int, int, int], kind: str,
                  position: Vector3, now: float) -> None:
        """Called only after the construction controller confirms world state."""
        if kind in {"bridge", "breach", "rampart", "schematic", "cover", "outpost"}:
            if len(self.completions) >= _MAX_LIVES:
                oldest = min(self.completions, key=lambda k: self.completions[k][2])
                self.completions.pop(oldest, None)
            self.completions[identity] = kind, position, now

    def _life(self, actor: PlayerSnapshot, now: float) -> _Life:
        key = actor.player_id, actor.generation
        identity = key + (actor.life_id,)
        life = self.lives.get(key)
        if life is None or life.identity != identity or life.team != actor.team:
            # Keep recent partner cooldowns within the round, never a pending
            # gesture belonging to the previous body/team.
            partners = life.partners if life and life.team == actor.team else {}
            if len(self.lives) >= _MAX_LIVES and key not in self.lives:
                oldest = min(self.lives, key=lambda k: self.lives[k].seen_at)
                self.lives.pop(oldest, None)
            life = self.lives[key] = _Life(identity, actor.team, now, partners,
                next_idle_at=now + 1.2 + variation(*identity, 11) * 2.5,
                next_roam_at=now + 1.3 + variation(*identity, 71) * 2.7,
                heard_at=now)
        life.seen_at = now
        life.partners = {key: partner for key, partner in life.partners.items()
                         if now - partner.seen_at <= 45.0}
        return life

    def _visible(self, actor: PlayerSnapshot, target: Vector3) -> bool:
        dx, dy = target[0] - actor.eye[0], target[1] - actor.eye[1]
        distance = math.hypot(dx, dy)
        facing = math.hypot(*actor.orientation[:2])
        if (distance > 1 and facing > .01
                and (dx * actor.orientation[0] + dy * actor.orientation[1]) / (distance * facing) < -.15):
            return False
        reader = getattr(self.world, "has_line_of_sight", None)
        return bool(callable(reader) and reader(actor.eye, target))

    def _facing(self, actor: PlayerSnapshot, target: Vector3) -> float:
        """Cosine between the horizontal facing and the bearing to ``target``."""
        dx, dy = target[0] - actor.eye[0], target[1] - actor.eye[1]
        distance = math.hypot(dx, dy)
        facing = math.hypot(*actor.orientation[:2])
        if distance <= 1e-6 or facing <= .01:
            return 1.0
        return (dx * actor.orientation[0] + dy * actor.orientation[1]) / (distance * facing)

    def _safe_posture(self, actor: PlayerSnapshot) -> bool:
        if actor.wade or not actor.grounded:
            return False
        solid = getattr(self.world, "solid", None)
        if not callable(solid):
            return False
        floor = int(round(actor.position[2] + 2.25))
        return all(solid(math.floor(actor.position[0] + dx),
                         math.floor(actor.position[1] + dy), floor)
                   for dx in (-.35, .35) for dy in (-.35, .35))

    def _busy(self, frame: PerceptionFrame, actor: PlayerSnapshot,
              intent: BotIntent) -> bool:
        now = frame.created_at
        if (not actor.alive or not actor.spawned or actor.reloading
                or not actor.grounded
                or actor.carried_entity_id >= 0 or not actor.can_shoot
                or intent.action.kind is not BotActionKind.NONE
                or intent.priority >= BotIntentPriority.COMBAT
                or intent.movement.affordance in _PRECISE
                or intent.movement.jump
                or intent.look is not None and (intent.look.visible or intent.look.glance)
                or 0 <= now - actor.last_damage_at < 2.0):
            return True
        if frame.mode_phase.casefold() in {"resetting", "ended", "retiring", "intermission"}:
            return True
        if any(entity.hazardous and entity.alive and
               math.dist(actor.position, entity.position) < max(8.0, entity.blast_radius + 3)
               for entity in frame.entities):
            return True
        zombie = int(actor.class_id) in _ZOMBIE_CLASSES
        # Gunfire nearby is a fight for anyone who can shoot back. The
        # infected only turn their heads to it (see _sound_beat).
        if not zombie and any(cue.team != actor.team and cue.kind in {
                StimulusKind.SHOT, StimulusKind.EXPLOSION, StimulusKind.DEATH}
               and cue.expires_at > now and math.dist(actor.position, cue.position) < 22
               for cue in frame.stimuli):
            return True
        # At most two extra short LOS checks; never react to hidden positions.
        reach = _CLAW_BUSY_RANGE if zombie else _FOE_BUSY_RANGE
        foes = sorted((p for p in frame.players if p.team != actor.team and
                       p.alive and p.spawned and math.dist(p.position, actor.position) < reach),
                      key=lambda p: math.dist(p.position, actor.position))[:2]
        return any(self._visible(actor, p.eye) for p in foes)

    def overlay(self, frame: PerceptionFrame, intent: BotIntent) -> BotIntent:
        """Add a short visible reaction without taking a route or tool away."""
        epoch = frame.map_epoch, frame.mode_epoch
        if self.epoch != epoch:
            self.reset()
            self.epoch = epoch
        actor = next((p for p in frame.players if p.player_id == intent.bot_id
                      and p.generation == intent.bot_generation), None)
        if actor is None or not actor.is_bot:
            return intent
        now = float(frame.created_at)
        life = self._life(actor, now)
        moving = math.hypot(*intent.movement.direction[:2]) > .1
        if moving:
            life.still_since = math.inf
        elif life.still_since > now:
            life.still_since = now
        if not frame.social_interactions or self._busy(frame, actor, intent):
            life.gesture = None
            life.roaming_look = None
            return intent
        # A glance is free only on plain travel. Working, carrying and live
        # infected assault orders always keep their movement and posture.
        order = next((o for o in frame.objectives if o.kind == "zombie_order"
                      and o.carrier_id == actor.player_id), None)
        urgent = intent.priority >= BotIntentPriority.TRAVERSAL or order is not None
        gesture = life.gesture
        if gesture is not None and now >= gesture.ends_at:
            life.gesture = gesture = None
        if (gesture is None and intent.social_cue in {"yield_human", "yield_passage"}
                and not urgent and now >= life.next_event_at):
            friend = min((p for p in frame.players if p.player_id != actor.player_id
                and p.team == actor.team and p.alive and p.spawned
                and math.dist(p.position, actor.position) < 4
                and self._visible(actor, p.eye)),
                key=lambda p: math.dist(p.position, actor.position), default=None)
            phase = variation(*life.identity, int(now / 12), 85)
            life.next_event_at = now + 15 + 10 * phase
            if friend is not None and phase < .45:
                life.gesture = gesture = _Gesture("yield_ack", friend.eye, now,
                    now + .3 + .2 * phase)
        if gesture is None:
            self._observe_partners(frame, actor, life, intent, moving or urgent)
            gesture = life.gesture
        completed = self.completions.pop(life.identity, None)
        if completed and gesture is None and not urgent and now >= life.next_event_at:
            kind, point, at = completed
            if 0 <= now - at <= 2 and self._visible(actor, point):
                life.gesture = gesture = _Gesture("completed_" + kind, point, now,
                    now + .45 + variation(*life.identity, int(now), 17) * .25)
                life.next_event_at = now + 12
        if gesture is None and not moving and not urgent:
            for cue in frame.stimuli:
                if (cue.kind is StimulusKind.OBJECTIVE and cue.team == actor.team
                        and life.last_objective < cue.created_at <= now
                        and cue.expires_at > now and now >= life.next_event_at
                        and math.dist(actor.position, cue.position) <= 24
                        and self._visible(actor, cue.position)):
                    life.last_objective = cue.created_at
                    life.next_event_at = now + 20
                    life.gesture = gesture = _Gesture("objective_ack", cue.position,
                        now + variation(*life.identity, 31) * .2, now + .65)
                    break
        if gesture is None:
            self._attention_beats(frame, actor, life, intent, order)
            gesture = life.gesture
        if gesture is not None and gesture.starts_at <= now < gesture.ends_at:
            life.roaming_look = None
            movement = intent.movement
            if gesture.crouch and not moving and not urgent and self._safe_posture(actor):
                movement = replace(movement, crouch=True, sprint=False)
            return replace(intent, movement=movement,
                look=LookIntent(gesture.target, glance=True), social_cue=gesture.kind)
        roaming = self._roaming_attention(frame, actor, life, intent)
        if roaming is not None:
            return roaming
        standing = self._standing_attention(frame, actor, life, intent, moving)
        return intent if standing is None else standing

    def _attention_beats(self, frame: PerceptionFrame, actor: PlayerSnapshot,
                         life: _Life, intent: BotIntent,
                         order: ObjectiveSnapshot | None) -> None:
        """The brief looks a working bot still spares, each with its own timing.

        A shot heard, a teammate's placement just completed, the ring spot it
        has been given: each turns the head for well under a second and then
        the eyes go back to the work. Never during a swing, placement, jump or
        precise step (``_busy`` ran first), and never two at once.
        """
        now = frame.created_at
        if self._sound_beat(frame, actor, life, now):
            return
        if self._work_beat(frame, actor, life, now):
            return
        if order is not None:
            self._order_beat(actor, life, intent, order, now)

    def _sound_beat(self, frame: PerceptionFrame, actor: PlayerSnapshot,
                    life: _Life, now: float) -> bool:
        # Everyone else hears through the awareness layer, which also owns
        # the body's answer to fire. The infected have no gun to answer with;
        # a shot or a blast only turns their head for a moment.
        if int(actor.class_id) not in _ZOMBIE_CLASSES:
            return False
        newest, chosen = life.heard_at, None
        for cue in frame.stimuli:
            if (cue.kind in _THREAT_SOUNDS and cue.team != actor.team
                    and life.heard_at < cue.created_at <= now and cue.expires_at > now):
                newest = max(newest, cue.created_at)
                distance = math.dist(actor.position, cue.position)
                if distance <= 48. and (chosen is None or distance < chosen[0]):
                    chosen = distance, cue
        life.heard_at = newest
        if chosen is None or now < life.next_sound_at:
            return False
        cue = chosen[1]
        if self._facing(actor, cue.position) > math.cos(math.radians(25.)):
            return False  # already looking that way
        phase = temperament(*life.identity, int(cue.created_at * 1000) & 0xffffffff, 53)
        # Reaction time differs from one bot to the next by more than it
        # does within one, so a squad hearing the same shot does not turn as one.
        delay = .1 + .6 * phase
        life.next_sound_at = now + 5. + 6. * temperament(*life.identity, int(now), 59)
        life.gesture = _Gesture("sound_glance",
            (cue.position[0], cue.position[1], actor.eye[2]),
            now + delay, now + delay + .5 + .4 * phase)
        return True

    def _work_beat(self, frame: PerceptionFrame, actor: PlayerSnapshot,
                   life: _Life, now: float) -> bool:
        probes = 0
        for mate in frame.players:
            if (mate.player_id == actor.player_id or mate.team != actor.team
                    or not mate.alive or not mate.spawned
                    or mate.last_action_kind not in _WORK_KINDS
                    or not mate.last_action_accepted
                    or not 0. <= now - mate.last_action_at <= 1.2
                    or math.dist(mate.position, actor.position) > 16.):
                continue
            if life.work_seen.get(mate.player_id) == mate.last_action_at:
                continue
            if len(life.work_seen) >= _MAX_PARTNERS:
                life.work_seen.pop(min(life.work_seen, key=life.work_seen.get))
            life.work_seen[mate.player_id] = mate.last_action_at
            if now < life.next_work_at or probes >= 3:
                continue
            point = mate.last_action_position
            point = (tuple(math.floor(v) + .5 for v in point) if point is not None
                     else mate.eye)
            probes += 1
            if not self._visible(actor, point):
                continue
            phase = temperament(*life.identity, mate.player_id,
                              int(mate.last_action_at * 100) & 0xffffffff, 61)
            delay = .1 + .5 * phase
            life.next_work_at = now + 6. + 8. * phase
            life.gesture = _Gesture("teammate_work", point, now + delay,
                                    now + delay + .45 + .4 * phase)
            return True
        return False

    def _order_beat(self, actor: PlayerSnapshot, life: _Life, intent: BotIntent,
                    order: ObjectiveSnapshot, now: float) -> bool:
        """A waiting zombie checks the spot it was given now and then."""
        if (math.hypot(*intent.movement.direction[:2]) > .1 or now < life.next_order_glance_at
                or math.hypot(order.position[0] - actor.position[0],
                              order.position[1] - actor.position[1]) < 3.):
            return False
        phase = temperament(*life.identity, int(now / 4), 67)
        life.next_order_glance_at = now + 7. + 7. * phase
        point = (order.position[0], order.position[1], order.position[2] - 1.)
        if not self._visible(actor, point):
            return False
        life.gesture = _Gesture("order_glance", point, now + .1 + .2 * phase,
                                now + .7 + .5 * phase)
        return True

    def _standing_attention(self, frame: PerceptionFrame, actor: PlayerSnapshot,
                            life: _Life, intent: BotIntent, moving: bool) -> BotIntent | None:
        """Nobody waiting stands like a statue.

        With a point to watch (a lane, a survivor on a roof, an approach) the
        eyes breathe slowly around it: a few degrees either side at a pace of
        several seconds, each bot with its own width, period and phase. With
        nothing to watch, an occasional look at some visible spot nearby.
        Walking, swimming, airborne bodies and anything above ordinary
        traversal priority are left alone.
        """
        now = frame.created_at
        movement = intent.movement
        if (moving or not actor.grounded or actor.wade or life.still_since > now
                or movement.affordance not in (MovementAffordance.WALK, MovementAffordance.CROUCH)
                or intent.priority > BotIntentPriority.TRAVERSAL):
            return None
        held = now - life.still_since
        look = intent.look
        if look is not None:
            if look.glance:
                return None
            start = .9 + 1.4 * temperament(*life.identity, 101)
            if held < start:
                return None
            dx, dy = look.target[0] - actor.eye[0], look.target[1] - actor.eye[1]
            flat = math.hypot(dx, dy)
            if flat < 1.5:
                return None
            amplitude = math.radians(7. + 9. * temperament(*life.identity, 103))
            period = 5.5 + 3.5 * temperament(*life.identity, 107)
            phase = 2. * math.pi * temperament(*life.identity, 109)
            vertical = math.radians(1.5 + 2. * temperament(*life.identity, 113))
            # Eased in over the first moment so the pan leaves the target
            # smoothly instead of starting mid-swing.
            elapsed = held - start
            ramp = min(1., elapsed / 1.5)
            yaw = (math.atan2(dy, dx)
                   + amplitude * ramp * math.sin(2. * math.pi * elapsed / period + phase))
            tilt = vertical * ramp * math.sin(2. * math.pi * elapsed / (period * .61) + 1.7 * phase)
            reach = max(6., min(30., flat))
            rise = (look.target[2] - actor.eye[2]) * reach / flat
            point = (actor.eye[0] + math.cos(yaw) * reach,
                     actor.eye[1] + math.sin(yaw) * reach,
                     actor.eye[2] + rise + math.tan(tilt) * reach)
            return replace(intent, look=LookIntent(point, glance=True), social_cue="standing_scan")
        if held < 1.2:
            return None
        if now >= life.next_idle_at:
            phase = temperament(*life.identity, int(now / 3), frame.strategy_seed)
            yaw = math.atan2(actor.orientation[1], actor.orientation[0]) + (phase - .5) * 1.8
            point = (actor.eye[0] + math.cos(yaw) * 9,
                     actor.eye[1] + math.sin(yaw) * 9,
                     actor.eye[2] + (temperament(actor.player_id, int(now), 9) - .5) * 1.2)
            life.idle_target = point if self._visible(actor, point) else None
            life.next_idle_at = now + 1.8 + 2.7 * phase
        if life.idle_target is not None:
            return replace(intent, look=LookIntent(life.idle_target, glance=True), social_cue="idle_look")
        return None

    def _roaming_attention(self, frame: PerceptionFrame, actor: PlayerSnapshot,
                           life: _Life, intent: BotIntent) -> BotIntent | None:
        """Check a visible side opening briefly, then watch the route again.

        A steady aim motor alone made ordinary travel look rigid. These are
        occasional, held world-space looks, not noise on every decision. The
        route, movement keys, posture and actions keep their original owner.
        """
        movement = intent.movement
        waypoint = movement.travel_waypoint
        source = movement.travel_source
        length = math.hypot(*movement.direction[:2])
        eligible = (
            length > .1 and actor.grounded and not actor.wade
            and movement.affordance is MovementAffordance.WALK
            and not (movement.sneak or movement.crouch)
            and movement.travel_arrival_radius >= .25
            and waypoint is not None and source is not None
            and math.hypot(waypoint[0] - actor.position[0],
                           waypoint[1] - actor.position[1]) >= 2.5
            and abs(waypoint[2] - actor.position[2]) <= .75
            # A zombie digging, climbing or running at its prey keeps its
            # eyes on the work; one pacing the waiting ring or walking round
            # to its side (surround 4, flank 1) glances about like anyone.
            and not any(o.kind == "zombie_order" and o.carrier_id == actor.player_id
                        and int(o.state) not in _ROAMING_ORDER_STATES
                        for o in frame.objectives)
        )
        now = frame.created_at
        if not eligible:
            life.roaming_look = None
            life.next_roam_at = max(life.next_roam_at, now + .8)
            return None
        heading = movement.direction[0] / length, movement.direction[1] / length
        glance = life.roaming_look
        if glance is not None:
            dx, dy = glance.target[0] - actor.eye[0], glance.target[1] - actor.eye[1]
            distance = math.hypot(dx, dy)
            # Do not keep looking back at a passed point, down at a previous
            # floor, or sideways while the route takes a different corner.
            if (now >= glance.ends_at or distance < 3.
                    or sum(a*b for a, b in zip(heading, glance.heading)) < math.cos(math.radians(25))
                    or (dx*heading[0] + dy*heading[1]) < distance * math.cos(math.radians(55))
                    or abs(glance.target[2] - actor.eye[2]) > 1.5
                    or not self._safe_posture(actor)):
                life.roaming_look = None
                return None
            return replace(intent, look=LookIntent(glance.target, glance=True),
                           social_cue="roam_check")
        if now < life.next_roam_at:
            return None
        life.roam_sequence += 1
        event = (*life.identity, frame.strategy_seed, life.roam_sequence)
        phase = variation(*event, 73)
        life.next_roam_at = now + 3.2 + 3.8 * phase
        if not self._safe_posture(actor):
            return None
        # The native route already owns the ground ahead. Require full support
        # two blocks ahead as well before spending attention off its centre.
        floor = int(round(actor.position[2] + 2.25))
        solid = self.world.solid
        if not all(solid(math.floor(actor.position[0] + heading[0]*2 + dx),
                         math.floor(actor.position[1] + heading[1]*2 + dy), floor)
                   for dx in (-.35, .35) for dy in (-.35, .35)):
            return None
        side = -1. if variation(*event, 79) < .5 else 1.
        angle = math.radians((18. if movement.sprint else 25.) + 15.*phase)
        distance = 12. + 4.*variation(*event, 83)
        yaw = math.atan2(heading[1], heading[0])
        height = actor.eye[2] + (variation(*event, 89) - .5)*1.2
        # An opening on one side may be a wall on the other. Bounded probes
        # choose visible space; no hidden player or objective is consulted.
        for offset in (side*angle, -side*angle, side*angle*.55):
            point = (actor.eye[0] + math.cos(yaw + offset)*distance,
                     actor.eye[1] + math.sin(yaw + offset)*distance, height)
            if self._visible(actor, point):
                # The relaxed head takes most of half a second to get there;
                # the look is held long enough to actually see something.
                life.roaming_look = _RoamingLook(point, heading,
                    now + .65 + .35*variation(*event, 97))
                return replace(intent, look=LookIntent(point, glance=True),
                               social_cue="roam_check")
        return None

    def _observe_partners(self, frame: PerceptionFrame, actor: PlayerSnapshot,
                          life: _Life, intent: BotIntent, glance_only: bool) -> None:
        now = frame.created_at
        if now < life.next_event_at:
            return
        partners = sorted((p for p in frame.players if p.player_id != actor.player_id
                           and p.team == actor.team and not p.is_bot and p.alive and p.spawned
                           and math.dist(p.position, actor.position) <= _PARTNER_RANGE),
                          key=lambda p: (math.dist(p.position, actor.position), p.player_id))[:3]
        for human in partners:
            if not self._visible(actor, human.eye):
                continue
            key = human.player_id, human.generation
            previous = life.partners.get(key)
            if previous is None:
                if len(life.partners) >= _MAX_PARTNERS:
                    oldest = min(life.partners, key=lambda k: life.partners[k].seen_at)
                    life.partners.pop(oldest, None)
                previous = life.partners[key] = _Partner(now)
            was_crouching = previous.crouching
            previous.crouching, previous.seen_at = human.crouching, now
            attention = max((s.created_at for s in frame.stimuli
                if s.kind is StimulusKind.FRIENDLY_ATTENTION and s.source_id == human.player_id
                and s.target_id == actor.player_id and s.team == actor.team
                and s.target_life_id in (-1, actor.life_id)
                and s.expires_at > now and s.created_at > previous.attention_at), default=-math.inf)
            kind = ""
            if attention > -math.inf:
                previous.attention_at = attention
                kind = "friendly_hit_ack"
            elif human.crouching and not was_crouching and variation(
                    *life.identity, human.player_id, int(now / 10)) < .78:
                kind = "crouch_greeting"
            elif previous.encounters == 0 and math.hypot(*human.velocity[:2]) > .03:
                kind = "notice_ally"
            event_target = human.eye
            if not kind and now >= life.next_event_at:
                # Follow a visible ally's attention only toward an event the
                # observer can also see. An ally's aim is never a wall hack.
                for event in frame.stimuli[:16]:
                    if event.expires_at <= now or event.created_at > now or now-event.created_at > 1:
                        continue
                    vector = tuple(event.position[a] - human.eye[a] for a in range(3))
                    distance = math.sqrt(sum(v*v for v in vector))
                    alignment = sum(vector[a] * human.orientation[a] for a in range(3))
                    if (1 < distance < 24 and alignment / distance > .94
                            and self._visible(actor, event.position)):
                        kind, event_target = "follow_ally_attention", event.position
                        life.next_event_at = now + 10
                        break
            if not kind or now < previous.ready_at:
                continue
            group_key = actor.team, human.player_id, human.generation
            if now < self.team_reply_ready.get(group_key, 0):
                continue
            self.team_reply_ready[group_key] = now + 10
            self.team_reply_ready = {k: t for k, t in self.team_reply_ready.items() if t > now}
            phase = variation(*life.identity, human.player_id, int(now), 41)
            delay = .12 + .18 * phase
            previous.encounters = min(8, previous.encounters + 1)
            previous.ready_at = now + 12 + 10 * phase
            life.next_event_at = now + 10
            life.gesture = _Gesture(kind, event_target, now + delay,
                now + delay + .35 + .25 * phase,
                crouch=kind == "crouch_greeting" and not glance_only)
            return
