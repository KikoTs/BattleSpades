"""What a bot notices around it, and what it does about it.

Every reaction here starts from something a player standing in the same place
is told by the game: the damage indicator, a sound within hearing distance,
the kill feed, a thing in plain view. Nothing reads a position through a
wall; the stimulus that licenses each reaction is named where it is checked.

The worker asks twice per decision. ``react`` may take the body (run for
cover, fall back, shoot a mine) before combat and navigation are considered.
``overlay`` then adjusts the finished intent without owning it (a glance over
the shoulder while the feet keep walking).

How fast and how well a bot reacts follows its profile: a casual hand needs
a second hit to understand it is being shot at and then runs somewhere poor,
an expert is behind something solid within half a second.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

import shared.constants as C

from .combat_tactics import find_shelter, firing_line, mix, walkable_heading
from .messages import (
    BotActionKind,
    BotIntent,
    BotIntentPriority,
    BotProfile,
    LookIntent,
    MovementAffordance,
    PerceptionFrame,
    PlayerSnapshot,
    Vector3,
)
from .policies import ModeBotDecision, ModeBotPosture

_ZOMBIE_CLASSES = frozenset({
    int(C.CLASS_ZOMBIE),
    int(C.CLASS_FAST_ZOMBIE),
    int(C.CLASS_JUMP_ZOMBIE),
})
# Damage that draws no direction arrow on the victim's screen (the client
# shows a burn flash instead), so it says nothing about where an enemy is.
_UNDIRECTED_DAMAGE = frozenset(
    int(getattr(C, name)) for name in ("BLOCKFIRE_KILL",) if hasattr(C, name))
# A hit older than this is not news any more: the bot was busy (swimming,
# mid-flight) and the moment to react has passed.
_HIT_NEWS_SECONDS = 2.0
# An enemy seen this recently is a duel that lost its sight line for a
# moment, not an unseen shooter: ordinary combat chases the last sighting.
_SIGHTING_SECONDS = 2.5
_SHELTER_RETRY_SECONDS = 0.75
# A sprint covers most of a block between two decisions: brake before the
# spot and accept arriving beside it instead of running through and back.
_SHELTER_ARRIVAL = 1.0
_SHELTER_BRAKE = 3.0
_SHELTER_STALL_SECONDS = 1.5
_LOOK_BACK_SECONDS = 0.35
_MAX_LIVES = 128


@dataclass(frozen=True, slots=True)
class Reaction:
    """One body-owning answer to a stimulus, resolved by the worker.

    ``heading`` is a raw stride for short runs over checked ground. ``suspect``
    hands a place worth checking to the worker's last-seen chase; a reaction
    with an empty ``role`` carries only that and owns nothing.
    """

    role: str = ""
    heading: Vector3 = (0.0, 0.0, 0.0)
    look: Vector3 | None = None
    crouch: bool = False
    sprint: bool = False
    priority: BotIntentPriority = BotIntentPriority.COMBAT
    suspect: Vector3 | None = None


@dataclass(slots=True)
class _Life:
    """Everything one bot life has noticed; dropped on death and map change."""

    identity: tuple[int, int, int]
    seen_at: float = 0.0
    # Shot at by someone who is not in sight.
    hit_at: float = 0.0
    hits: int = 0
    fire_from: Vector3 | None = None
    fire_noticed_at: float = 0.0
    fire_until: float = 0.0
    shelter: Vector3 | None = None
    shelter_hits: int = 0
    shelter_reached: bool = False
    shelter_best: float = math.inf
    shelter_progress_at: float = 0.0
    next_shelter_at: float = 0.0
    dash: Vector3 | None = None
    dash_until: float = 0.0
    dashes: int = 0
    look_back_until: float = 0.0
    sighted_id: int = -1
    sighted_at: float = 0.0
    # A look that rides on whatever the body is doing.
    glance: Vector3 | None = None
    glance_until: float = 0.0


def _unit(dx: float, dy: float) -> Vector3:
    length = math.hypot(dx, dy)
    return (dx / length, dy / length, 0.0) if length > 1e-6 else (0.0, 0.0, 0.0)


def _ahead(observer: PlayerSnapshot, heading: Vector3) -> Vector3:
    """A point at eye height along ``heading``: where a runner looks."""

    return (observer.eye[0] + heading[0] * 6.0, observer.eye[1] + heading[1] * 6.0,
            observer.eye[2])


def notice_delay(profile: BotProfile) -> float:
    """Seconds between a stimulus and the bot understanding it.

    The profile's reaction time is its best case (an enemy already in view).
    Something unexpected takes longer, and much longer for a weak player.
    """

    return float(profile.reaction_time) * (1.0 + 2.0 * (1.0 - float(profile.skill)))


def _flight_role(observer: PlayerSnapshot, decision: ModeBotDecision | None) -> bool:
    """Roles that must keep running whatever happens around them.

    An objective carrier cannot shoot and wins by arriving; a role already
    fleeing (VIP retreat, airstrike escape) has its own destination.
    """

    if observer.carried_entity_id >= 0 or not observer.can_shoot:
        return True
    return decision is not None and decision.objective_priority >= 0.9 and decision.posture in {
        ModeBotPosture.EVASIVE, ModeBotPosture.SURVIVE}


class Awareness:
    """Per-life memory of stimuli and the reactions that follow from them."""

    def __init__(self, world) -> None:
        self.world = world
        self._lives: dict[tuple[int, int], _Life] = {}

    def reset(self) -> None:
        self._lives.clear()

    def forget(self, player_id: int, generation: int) -> None:
        self._lives.pop((int(player_id), int(generation)), None)

    def _life(self, observer: PlayerSnapshot, now: float) -> _Life:
        key = (int(observer.player_id), int(observer.generation))
        identity = (key[0], key[1], int(observer.life_id))
        life = self._lives.get(key)
        if life is None or life.identity != identity:
            if life is None and len(self._lives) >= _MAX_LIVES:
                self._lives.pop(min(self._lives, key=lambda item: self._lives[item].seen_at))
            # Hits taken by a previous life are not this one's news.
            life = self._lives[key] = _Life(identity, hit_at=float(observer.last_damage_at))
        life.seen_at = now
        return life

    def react(self, frame: PerceptionFrame, observer: PlayerSnapshot, profile: BotProfile,
              visible: PlayerSnapshot | None, decision: ModeBotDecision | None,
              now: float) -> Reaction | None:
        """Return the reaction that should own the body this decision, if any."""

        life = self._life(observer, now)
        if int(observer.class_id) in _ZOMBIE_CLASSES:
            return None
        return self._under_fire(frame, observer, profile, life, visible, decision, now)

    def overlay(self, frame: PerceptionFrame, intent: BotIntent) -> BotIntent:
        """Let a pending glance turn the head while the body carries on."""

        life = self._lives.get((int(intent.bot_id), int(intent.bot_generation)))
        if life is None or life.glance is None:
            return intent
        if float(frame.created_at) >= life.glance_until:
            life.glance = None
            return intent
        if not _head_is_free(intent):
            return intent
        return replace(intent, look=LookIntent(life.glance, glance=True))

    # -- shot at by someone who is not in sight ------------------------------

    def _under_fire(self, frame: PerceptionFrame, observer: PlayerSnapshot,
                    profile: BotProfile, life: _Life, visible: PlayerSnapshot | None,
                    decision: ModeBotDecision | None, now: float) -> Reaction | None:
        """Turn to the hit, get out of the line, wait for the shooter to show.

        Stimulus: the damage indicator (``last_damage_*``), which names the
        direction a hit came from whether or not the shooter can be seen.
        """

        if visible is not None:
            life.sighted_id, life.sighted_at = int(visible.player_id), now
        hit_at = float(observer.last_damage_at)
        if hit_at > life.hit_at + 1e-6:
            life.hit_at = hit_at
            source = observer.last_damage_source_position
            attacker = int(observer.last_damage_source_id)
            dueling = (attacker == life.sighted_id
                       and now - life.sighted_at <= _SIGHTING_SECONDS)
            if (source is not None and attacker != int(observer.player_id) and not dueling
                    and int(observer.last_damage_kind) not in _UNDIRECTED_DAMAGE
                    and 0.0 <= now - hit_at <= _HIT_NEWS_SECONDS
                    and not _same_team(frame, observer, attacker)):
                if now >= life.fire_until:
                    life.hits = 0
                    life.fire_noticed_at = hit_at + notice_delay(profile)
                    life.look_back_until = life.fire_noticed_at + _LOOK_BACK_SECONDS
                    life.shelter = None
                    life.shelter_reached = False
                    life.dash = None
                    life.next_shelter_at = 0.0
                life.hits += 1
                life.fire_from = tuple(float(value) for value in source)
                committed = decision is not None and decision.objective_priority >= 0.9
                # Counted from the moment it sinks in: a slow player is not
                # done reacting before having started.
                life.fire_until = max(hit_at, life.fire_noticed_at) + (
                    0.6 if committed else 1.0) * (
                    1.4 + 2.2 * float(profile.caution) - 0.8 * float(profile.aggression))
        threat = life.fire_from
        if threat is None:
            return None
        if now >= life.fire_until:
            # Nothing more came. The bold go and look; the rest carry on.
            life.fire_from = None
            life.glance = None
            hunts = profile.aggression >= 0.45 or profile.skill >= 0.6
            return Reaction(suspect=threat) if hunts and visible is None else None
        if now < life.fire_noticed_at or visible is not None:
            # A shooter in sight is ordinary combat's business.
            return None
        if _flight_role(observer, decision):
            return None
        eager = profile.skill >= 0.35 or profile.caution >= 0.7
        if life.hits < (1 if eager else 2):
            # A weak player's first hit is only a look over the shoulder.
            life.glance, life.glance_until = threat, now + 0.8
            return None
        life.glance = None

        if life.shelter is not None and life.shelter_reached and life.hits > life.shelter_hits:
            # Hit again where it thought it was safe: that spot is no cover.
            life.shelter = None
            life.shelter_reached = False
        # Ground probes mean nothing from mid-air (a step down a terrace, the
        # hop out of a crouch): keep the stride in hand and choose on landing.
        airborne = not observer.grounded
        if life.shelter is None and not airborne and now >= life.next_shelter_at:
            life.next_shelter_at = now + _SHELTER_RETRY_SECONDS
            # A skilled player allows for the shooter stepping sideways and
            # looks farther for something solid; a weak one hides from the
            # one spot the shot came from, within a few strides.
            line = (firing_line(observer.position, threat) if profile.skill >= 0.45
                    else (threat,))
            side = 1.0 if mix(observer.player_id, observer.life_id, life.hits) < 0.5 else -1.0
            life.shelter = find_shelter(
                self.world, observer, line, reach=4.5 + 6.5 * float(profile.skill),
                min_gap=2.5, side=side)
            life.shelter_hits = life.hits
            life.shelter_reached = False
            life.shelter_best = math.inf
            life.shelter_progress_at = now
        # Each run starts with a look at where the shot came from; then the
        # eyes go where the feet are going, as a runner's do.
        startled = now < life.look_back_until
        if life.shelter is not None:
            gap = math.hypot(life.shelter[0] - observer.position[0],
                             life.shelter[1] - observer.position[1])
            if gap <= _SHELTER_ARRIVAL:
                life.shelter_reached = True
                life.shelter_hits = life.hits
            if life.shelter_reached:
                return Reaction("under_fire_hold", look=threat, crouch=True)
            if gap + 0.3 < life.shelter_best:
                life.shelter_best, life.shelter_progress_at = gap, now
            if now - life.shelter_progress_at < _SHELTER_STALL_SECONDS:
                heading = _unit(life.shelter[0] - observer.position[0],
                                life.shelter[1] - observer.position[1])
                return Reaction("under_fire_cover", heading=heading,
                                sprint=gap > _SHELTER_BRAKE,
                                look=threat if startled else _ahead(observer, heading))
            # Something the straight-line check missed is in the way.
            life.shelter = None
        if not airborne and (life.dash is None or now >= life.dash_until):
            life.dash = self._dash_heading(observer, threat, life)
            life.dashes += 1
            life.dash_until = now + 0.7 + 0.5 * mix(observer.player_id, life.dashes, 5)
            if life.dashes > 1:
                startled = True
                life.look_back_until = now + _LOOK_BACK_SECONDS
        if life.dash is None:
            return Reaction("under_fire_hold", look=threat, crouch=not airborne)
        return Reaction("under_fire_evade", heading=life.dash, sprint=True,
                        look=threat if startled else _ahead(observer, life.dash))

    def _dash_heading(self, observer: PlayerSnapshot, threat: Vector3,
                      life: _Life) -> Vector3 | None:
        """Open ground: run away from the shot and to one side, alternating."""

        away = math.atan2(observer.position[1] - threat[1], observer.position[0] - threat[0])
        first = 1.0 if mix(observer.player_id, observer.life_id, 9) < 0.5 else -1.0
        side = first if life.dashes % 2 == 0 else -first
        for turn in (0.9, -0.9, 0.35, -0.35, 1.5, -1.5):
            angle = away + turn * side
            heading = (math.cos(angle), math.sin(angle), 0.0)
            if walkable_heading(self.world, observer, heading, reach=3.0):
                return heading
        return None


def _same_team(frame: PerceptionFrame, observer: PlayerSnapshot, player_id: int) -> bool:
    """A teammate's stray shot is not an enemy to hide from."""

    return any(player.player_id == player_id and player.team == observer.team
               for player in frame.players)


def _head_is_free(intent: BotIntent) -> bool:
    """Whether the eyes can leave the work in hand for a moment.

    Aiming at a visible enemy, digging, building and any exact step (a jump,
    a ledge run-off, a swim) need the view where the task has put it.
    """

    movement = intent.movement
    return (intent.action.kind is BotActionKind.NONE
            and intent.priority < BotIntentPriority.SURVIVAL
            and not (intent.look is not None and intent.look.visible)
            and movement.affordance is MovementAffordance.WALK
            and not movement.jump and movement.walk_drop <= 1)
