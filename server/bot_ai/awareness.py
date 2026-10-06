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

from dataclasses import dataclass, field, replace
import math

import shared.constants as C

from .combat_tactics import (
    exposed, find_dead_ground, find_shelter, firing_line, mix, walkable_heading,
)
from .messages import (
    BotActionKind,
    BotIntent,
    BotIntentPriority,
    BotProfile,
    LookIntent,
    MovementAffordance,
    MovementIntent,
    PerceptionFrame,
    PlayerSnapshot,
    StimulusKind,
    Vector3,
)
from .policies import ModeBotDecision, ModeBotPosture
from .stimuli import HEARING_DISTANCE

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
# Counting the enemies in sight costs one ray each; twice a second is as
# often as the answer changes.
_ODDS_SECONDS = 0.45
_ODDS_REACH = 70.0
_MAX_FOES = 6
_ALLY_REACH = 18.0
# Inside this range turning away is death: the fight is settled where it is.
_POINT_BLANK = 7.0
_PRESSURE_SECONDS = 2.5
# A lost fight shows over a couple of seconds, and a sight line that flickers
# for a moment (a step behind a post) does not make it a new fight.
_LOSS_WINDOW_SECONDS = 2.0
_FIGHT_MEMORY_SECONDS = 1.5
_FALL_BACK_SECONDS = 12.0
_FALL_BACK_ARRIVAL = 8.0
# A teammate this close is worth leaving cover for.
_REGROUP_REACH = 60.0
_DEAD_GROUND_SECONDS = 1.5
_DEAD_GROUND_ARRIVAL = 2.5
# How much of a listener's attention each sound takes at point-blank range.
# All of them fade to nothing at the hearing distance; a footstep, the
# quietest, fades fastest.
_LOUDNESS = {
    StimulusKind.EXPLOSION: 1.0,
    StimulusKind.SHOT: 0.8,
    StimulusKind.BLOCK_DESTROYED: 0.5,
    StimulusKind.FOOTSTEP: 0.6,
}
_ALERT_FADE_SECONDS = 6.0
# A place already looked at is not looked at again for every further shot
# from it, unless the noise has come much closer.
_CHECKED_RADIUS = 12.0
_CHECKED_SECONDS = 8.0
# A teammate dying this close is "beside it" when the bot can see the spot;
# at arm's length it needs no sight line to know.
_ALLY_DOWN_REACH = 25.0
_ALLY_DOWN_TOUCH = 10.0
_KILL_SHOT_SECONDS = 2.0
_MAX_SHOTS_HEARD = 6
# Burning blocks set alight whoever comes within this of their centre
# (retail BLOCKFIRE_CHARACTER_SPREAD_RANGE); a lit patch creeps a block or
# two (BLOCKFIRE_SPREAD_RADIUS) while it burns its four seconds.
_FIRE_REACH = float(getattr(C, "BLOCKFIRE_CHARACTER_SPREAD_RANGE", 3.0))
_FIRE_SPREAD = float(getattr(C, "BLOCKFIRE_SPREAD_RADIUS", 2.0))
_FIRE_SECONDS = float(getattr(C, "BLOCKFIRE_MAX_LIFESPAN", 4.0))
_FIRE_PATCH = 4.5
_FIRE_NOTICE = 16.0
_MOLOTOV_TOOL = int(getattr(C, "MOLOTOV_TOOL", 33))
_MOLOTOV_SPEED = float(getattr(C, "MOLOTOV_THROW_SPEED", 40.0))
# The way ahead is checked against every hazard the bot knows of: this far
# at a standstill, and farther by what the body covers before a new stride
# can take effect (a sprint is a block and a half per decision).
_STEER_AHEAD = 4.0
_STEER_SECONDS = 0.6
# Player velocity is in physics units: blocks per second is 32 times that.
_PHYSICS_SCALE = 32.0
_HAZARD_SECONDS = 0.5
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
    # A place too far for a raw stride: the worker routes there, and falls
    # back on ``heading`` while the planner has nothing yet.
    goal: Vector3 | None = None
    look: Vector3 | None = None
    crouch: bool = False
    sprint: bool = False
    priority: BotIntentPriority = BotIntentPriority.COMBAT
    suspect: Vector3 | None = None


@dataclass(frozen=True, slots=True)
class _Hazard:
    """A patch of ground not to walk into: a keep-out circle with an expiry.

    ``reach`` is where it actually hurts; ``radius`` adds the berth this bot
    gives it.
    """

    key: int
    centre: Vector3
    reach: float
    radius: float
    until: float
    kind: str = "fire"


@dataclass(slots=True)
class _Shelter:
    """One run to a spot out of somebody's sight, and whether it got there."""

    spot: Vector3 | None = None
    reached: bool = False
    best: float = math.inf
    progress_at: float = 0.0
    next_search_at: float = 0.0

    def clear(self) -> None:
        self.spot, self.reached = None, False


@dataclass(slots=True)
class _Life:
    """Everything one bot life has noticed; dropped on death and map change."""

    identity: tuple[int, int, int]
    seen_at: float = 0.0
    # Shot at by someone who is not in sight.
    hit_at: float = 0.0
    hits: int = 0
    fire_from: Vector3 | None = None
    fire_cause: str = "under_fire"
    fire_noticed_at: float = 0.0
    fire_until: float = 0.0
    shelter: _Shelter = field(default_factory=_Shelter)
    shelter_hits: int = 0
    dash: Vector3 | None = None
    dash_until: float = 0.0
    dashes: int = 0
    look_back_until: float = 0.0
    sighted_id: int = -1
    sighted_at: float = 0.0
    # Outmatched: breaking off a fight that is being lost.
    odds_at: float = 0.0
    foes: tuple[Vector3, ...] = ()
    foes_at: float = 0.0
    nearest_foe: float = math.inf
    health_log: tuple[tuple[float, int], ...] = ()
    outmatched_since: float = 0.0
    outmatched_at: float = 0.0
    retreat: str = ""
    retreat_until: float = 0.0
    retreat_wait: float = 0.0
    retreat_hit_at: float = 0.0
    retreat_rest_until: float = 0.0
    refuge: _Shelter = field(default_factory=_Shelter)
    rally: Vector3 | None = None
    dead_ground: Vector3 | None = None
    dead_ground_at: float = 0.0
    # A look that rides on whatever the body is doing: from/until bound the
    # glance on the move, watch_until the longer look of a bot standing guard.
    glance: Vector3 | None = None
    glance_from: float = 0.0
    glance_until: float = 0.0
    watch_until: float = 0.0
    glance_ready_at: float = 0.0
    # Sounds: the newest one weighed, how wound up the bot is, what it checked.
    heard_at: float = 0.0
    alert: float = 0.0
    alert_at: float = 0.0
    noise: Vector3 | None = None
    noise_at: float = 0.0
    checked: Vector3 | None = None
    checked_at: float = 0.0
    checked_range: float = 0.0
    # Where each enemy last fired from, as heard: the kill feed names a killer,
    # the shot that went with it says roughly where it stood.
    shots: dict[int, tuple[Vector3, float]] = field(default_factory=dict)
    # Ground to keep off: what the bot has seen burning, and where its own
    # Molotov is about to. ``body`` is the snapshot they were judged from.
    hazards: tuple[_Hazard, ...] = ()
    hazards_at: float = 0.0
    body: PlayerSnapshot | None = None
    seen_fires: dict[int, float] = field(default_factory=dict)
    pyre: _Hazard | None = None
    # Which way round each hazard, so the stride does not swap sides mid-way.
    steer_side: dict[int, float] = field(default_factory=dict)


def _wrap(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


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


def _holds_ground(observer: PlayerSnapshot, decision: ModeBotDecision | None) -> bool:
    """Roles whose job is to stay in a losing fight.

    A carrier, a bodyguard, a defender standing on the objective it was told
    to hold and an attacker already on top of its objective win or lose the
    round right there.
    """

    if _flight_role(observer, decision):
        return True
    if decision is None or decision.objective_priority < 0.9:
        return False
    if decision.posture is ModeBotPosture.ESCORT:
        return True
    return math.dist(observer.position, decision.position) <= max(
        12.0, 2.0 * float(decision.arrival_radius))


class Awareness:
    """Per-life memory of stimuli and the reactions that follow from them."""

    def __init__(self, world) -> None:
        self.world = world
        self._lives: dict[tuple[int, int], _Life] = {}
        # Burning patches of the newest entity list, shared by every bot
        # whose frame carries that same list.
        self._fire_entities: tuple = ()
        self._fires: tuple[_Hazard, ...] = ()

    def reset(self) -> None:
        self._lives.clear()
        self._fire_entities, self._fires = (), ()

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
        self._watch_hazards(frame, observer, profile, life, now)
        burning = self._leave_fire(observer, life, now)
        if burning is not None:
            return burning
        if int(observer.class_id) in _ZOMBIE_CLASSES:
            return None
        self._listen(frame, observer, profile, life, visible, decision, now)
        hunted = self._under_fire(frame, observer, profile, life, visible, decision, now)
        retreat = self._outmatched(frame, observer, profile, life, visible, decision, now)
        if retreat is not None:
            # One owner: the retreat already answers whoever is shooting.
            life.fire_from = None
            return retreat
        return hunted

    def overlay(self, frame: PerceptionFrame, intent: BotIntent) -> BotIntent:
        """Let a pending glance turn the head while the body carries on."""

        life = self._lives.get((int(intent.bot_id), int(intent.bot_generation)))
        if life is None:
            return intent
        now = float(frame.created_at)
        if life.hazards and now - life.hazards_at <= _HAZARD_SECONDS:
            intent = self._steer(life, intent, now)
        if life.glance is None:
            return intent
        if now >= max(life.glance_until, life.watch_until):
            life.glance = None
            return intent
        if now < life.glance_from or not _head_is_free(intent):
            return intent
        if now >= life.glance_until and math.hypot(*intent.movement.direction[:2]) > 0.1:
            # Only a bot standing its ground keeps watching that long.
            return intent
        return replace(intent, look=LookIntent(life.glance, glance=True))

    # -- fire ------------------------------------------------------------------

    def threw(self, observer: PlayerSnapshot, tool: int, target: Vector3, now: float) -> None:
        """Remember where the bot's own Molotov is about to burn.

        The thrower knows where it aimed before any flame exists; walking on
        along the same line is how gangsters died in their own fire.
        """

        life = self._lives.get((int(observer.player_id), int(observer.generation)))
        if life is None or int(tool) != _MOLOTOV_TOOL:
            return
        flight = math.dist(observer.position, target) / _MOLOTOV_SPEED
        reach = _FIRE_SPREAD + _FIRE_REACH
        life.pyre = _Hazard(-1, tuple(float(value) for value in target), reach, reach + 1.0,
                            now + flight + 1.0 + _FIRE_SECONDS + 1.0)

    def _burning_patches(self, frame: PerceptionFrame) -> tuple[_Hazard, ...]:
        """Group the burning blocks of this frame into patches to walk around."""

        if frame.entities is self._fire_entities:
            return self._fires
        patches: list[list] = []
        for entity in frame.entities:
            if entity.kind != "blockfire" or not entity.alive:
                continue
            for patch in patches:
                if math.dist(patch[0], entity.position) <= _FIRE_PATCH:
                    patch.append(entity)
                    count = len(patch) - 1
                    patch[0] = tuple(
                        (patch[0][axis] * (count - 1) + entity.position[axis]) / count
                        for axis in range(3))
                    break
            else:
                patches.append([entity.position, entity])
        fires = []
        for centre, *members in patches:
            extent = max(math.dist(centre[:2], member.position[:2]) for member in members)
            reach = extent + max(member.blast_radius for member in members)
            fires.append(_Hazard(
                min(member.entity_id for member in members), centre, reach, reach,
                max(member.detonate_at for member in members) + 0.3))
        self._fire_entities, self._fires = frame.entities, tuple(fires)
        return self._fires

    def _watch_hazards(self, frame: PerceptionFrame, observer: PlayerSnapshot,
                       profile: BotProfile, life: _Life, now: float) -> None:
        """Collect the ground this bot knows it must keep off.

        Stimulus: flames in line of sight (or at the bot's feet), and the
        bot's own throw. Fire behind a wall is not known until it is seen.
        A careless player cuts the corner closer than a careful one.
        """

        life.body, life.hazards_at = observer, now
        known = []
        margin = 0.4 + 0.8 * float(profile.skill)
        rays = 0
        for fire in self._burning_patches(frame) if frame.entities else ():
            gap = math.dist(fire.centre[:2], observer.position[:2])
            head = observer.position[2]
            height = fire.centre[2] - min(max(fire.centre[2], head), head + 2.25)
            if gap > fire.radius + _FIRE_NOTICE or now >= fire.until or abs(height) > _FIRE_REACH:
                continue
            seen_at = life.seen_fires.get(fire.key)
            if seen_at is None:
                if gap > fire.radius + 1.0:
                    if rays >= 4:
                        continue
                    rays += 2
                    # The flames stand on top of the block and the smoke
                    # above them: either shows a fire in a dip.
                    x, y, z = fire.centre
                    if not (self.world.has_line_of_sight(observer.eye, (x, y, z - 1.0))
                            or self.world.has_line_of_sight(observer.eye, (x, y, z - 3.5))):
                        continue
                if len(life.seen_fires) >= 16:
                    life.seen_fires.pop(next(iter(life.seen_fires)))
                seen_at = life.seen_fires[fire.key] = now
            # Flames are hard to miss: they register faster than a sound does.
            if now - seen_at >= 0.25 * notice_delay(profile) or gap <= fire.radius + 1.0:
                known.append(replace(fire, radius=fire.radius + margin))
        if life.pyre is not None:
            if now >= life.pyre.until:
                life.pyre = None
            else:
                known.append(life.pyre)
        life.hazards = tuple(known)

    def _leave_fire(self, observer: PlayerSnapshot, life: _Life, now: float) -> Reaction | None:
        """Standing in the flames: out by the shortest way that can be walked."""

        for hazard in life.hazards:
            if hazard.kind != "fire":
                continue
            dx = observer.position[0] - hazard.centre[0]
            dy = observer.position[1] - hazard.centre[1]
            gap = math.hypot(dx, dy)
            if gap >= hazard.reach + 0.2:
                continue
            out = math.atan2(dy, dx) if gap > 1e-3 else mix(
                observer.player_id, observer.life_id, hazard.key) * 2.0 * math.pi
            for turn in (0.0, 0.7, -0.7, 1.4, -1.4):
                heading = (math.cos(out + turn), math.sin(out + turn), 0.0)
                if not observer.grounded or walkable_heading(self.world, observer, heading,
                                                             reach=2.0):
                    return Reaction("fire_escape", heading=heading, sprint=True,
                                    look=_ahead(observer, heading),
                                    priority=BotIntentPriority.SURVIVAL)
        return None

    def _steer(self, life: _Life, intent: BotIntent, now: float) -> BotIntent:
        """Bend the stride round a hazard, or stop short of it.

        The route planner knows nothing of fire. For the four seconds a
        patch burns, the way ahead is checked here: a walk is turned onto the
        tangent that clears the patch, an exact step (a jump, a ledge) that
        would land in it is held back until it has burnt out.
        """

        movement = intent.movement
        length = math.hypot(*movement.direction[:2])
        body = life.body
        if (length <= 0.1 or body is None or intent.priority >= BotIntentPriority.SURVIVAL
                or movement.affordance in {MovementAffordance.SWIM, MovementAffordance.JETPACK,
                                           MovementAffordance.JETPACK_CLIMB}):
            return intent
        dx, dy = movement.direction[0] / length, movement.direction[1] / length
        for hazard in life.hazards:
            to_x = hazard.centre[0] - body.position[0]
            to_y = hazard.centre[1] - body.position[1]
            gap = math.hypot(to_x, to_y)
            along = to_x * dx + to_y * dy
            if gap <= hazard.radius or along <= 0.0 or now >= hazard.until:
                continue  # inside is _leave_fire's business; behind is behind
            reach = min(along, _STEER_AHEAD + _STEER_SECONDS * _PHYSICS_SCALE * math.hypot(
                *body.velocity[:2]))
            miss = math.hypot(to_x - dx * reach, to_y - dy * reach)
            if miss >= hazard.radius:
                continue
            role = f"{intent.debug_role}:avoid_{hazard.kind}"
            if movement.affordance is MovementAffordance.WALK and not movement.jump:
                bearing = math.atan2(to_y, to_x)
                spread = math.asin(min(1.0, hazard.radius / gap)) + 0.15
                side = life.steer_side.get(hazard.key)
                if side is None:
                    # The side that turns the stride least.
                    side = 1.0 if _wrap(math.atan2(dy, dx) - bearing) >= 0.0 else -1.0
                for turn in (side, -side):
                    angle = bearing + turn * spread
                    heading = (math.cos(angle), math.sin(angle), 0.0)
                    if walkable_heading(self.world, body, heading, reach=2.0):
                        if len(life.steer_side) >= 8 and hazard.key not in life.steer_side:
                            life.steer_side.clear()
                        life.steer_side[hazard.key] = turn
                        return replace(
                            intent, debug_role=role,
                            movement=replace(movement, direction=(
                                heading[0] * length, heading[1] * length, 0.0)))
            # No way round that can be walked: wait for it to burn out.
            return replace(intent, debug_role=role, movement=MovementIntent(
                crouch=movement.crouch))
        return intent

    # -- sounds ----------------------------------------------------------------

    def _listen(self, frame: PerceptionFrame, observer: PlayerSnapshot, profile: BotProfile,
                life: _Life, visible: PlayerSnapshot | None,
                decision: ModeBotDecision | None, now: float) -> None:
        """Weigh the sounds of this frame: raise the alert and plan a glance.

        Stimulus: shots, blasts, digging, building and footsteps within the
        game's hearing distance. A sound says roughly where, never who is
        there; a teammate's noise is told apart as on the minimap. The body
        keeps doing what it was doing: sounds only turn the head.
        """

        if life.alert > 0.0:
            life.alert *= math.exp(-max(0.0, now - life.alert_at) / _ALERT_FADE_SECONDS)
        life.alert_at = now
        newest, loudest, fallen = life.heard_at, None, None
        for event in frame.stimuli:
            if event.created_at <= life.heard_at:
                continue
            newest = max(newest, event.created_at)
            if event.kind is StimulusKind.DEATH:
                if event.team == observer.team:
                    fallen = event
                continue
            base = _LOUDNESS.get(event.kind)
            if base is None or event.team == observer.team:
                continue
            if event.kind is StimulusKind.SHOT:
                if len(life.shots) >= _MAX_SHOTS_HEARD and event.source_id not in life.shots:
                    life.shots.pop(min(life.shots, key=lambda key: life.shots[key][1]))
                life.shots[event.source_id] = (event.position, event.created_at)
            distance = math.dist(event.position, observer.position)
            fade = max(0.0, 1.0 - distance / HEARING_DISTANCE)
            loudness = base * (fade * fade if event.kind is StimulusKind.FOOTSTEP else fade)
            if loudest is None or loudness > loudest[0]:
                loudest = (loudness, event, distance)
        life.heard_at = newest
        if fallen is not None and self._ally_down(frame, observer, profile, life, fallen,
                                                  visible, decision, now):
            return
        if loudest is None:
            return
        loudness, event, distance = loudest
        # A fight in hand leaves little attention for anything else; a weak
        # player misses what is not loud, a wound-up one misses less.
        threshold = (0.06 + 0.22 * (1.0 - float(profile.skill))
                     + (0.3 if visible is not None else 0.0) - 0.1 * life.alert)
        if loudness < threshold or self._in_view(frame, observer, event.source_id):
            return
        life.alert = min(1.0, life.alert + loudness)
        life.noise, life.noise_at = event.position, now
        if visible is not None or now < life.glance_ready_at or _flight_role(observer, decision):
            return
        if (life.checked is not None and now - life.checked_at <= _CHECKED_SECONDS
                and math.dist(life.checked, event.position) <= _CHECKED_RADIUS
                and distance > 0.7 * life.checked_range):
            return
        life.checked, life.checked_at, life.checked_range = event.position, now, distance
        dwell = 0.6 + 0.6 * float(profile.caution)
        life.glance = (event.position[0], event.position[1], observer.eye[2])
        life.glance_from = now + 0.5 * notice_delay(profile)
        life.glance_until = life.glance_from + dwell
        life.watch_until = life.glance_from + 3.0 * dwell
        life.glance_ready_at = life.glance_until + 1.0 + 2.5 * (1.0 - float(profile.caution))

    def _ally_down(self, frame: PerceptionFrame, observer: PlayerSnapshot,
                   profile: BotProfile, life: _Life, event, visible: PlayerSnapshot | None,
                   decision: ModeBotDecision | None, now: float) -> bool:
        """A teammate was killed beside the bot: face the shot, and get down.

        Stimulus: the death cry within hearing, the body in sight (or at arm's
        length), the kill feed naming the killer, and the shot heard from that
        killer a moment before. A killer too far to hear leaves only the side
        of the map the enemy is on.
        """

        distance = math.dist(event.position, observer.position)
        if distance > _ALLY_DOWN_REACH or _same_team(frame, observer, event.source_id):
            return False  # too far to be this bot's business, or no enemy did it
        if distance > _ALLY_DOWN_TOUCH and not self.world.has_line_of_sight(
                observer.eye, event.position):
            return False
        life.alert = min(1.0, life.alert + 0.6)
        heard = life.shots.get(event.source_id)
        if heard is not None and 0.0 <= event.created_at - heard[1] <= _KILL_SHOT_SECONDS:
            origin = heard[0]
        else:
            anchor = next((item.position for item in frame.objectives
                           if item.kind == "team_anchor" and item.team != observer.team), None)
            if anchor is None:
                return False
            toward = _unit(anchor[0] - event.position[0], anchor[1] - event.position[1])
            origin = (event.position[0] + toward[0] * 40.0,
                      event.position[1] + toward[1] * 40.0, event.position[2])
        if visible is not None or _flight_role(observer, decision) or life.retreat:
            return True
        delay = notice_delay(profile) * (1.0 - 0.5 * life.alert)
        dwell = 1.0 + 0.6 * float(profile.caution)
        life.glance = (origin[0], origin[1], observer.eye[2])
        life.glance_from = now + delay
        life.glance_until = life.glance_from + dwell
        life.watch_until = life.glance_from + 3.0 * dwell
        life.glance_ready_at = life.glance_until + 1.0
        life.checked, life.checked_at, life.checked_range = origin, now, math.dist(
            origin, observer.position)
        committed = decision is not None and decision.objective_priority >= 0.9
        careful = profile.caution >= 0.45 or profile.skill >= 0.6
        if committed or not careful or (life.fire_from is not None and now < life.fire_until):
            # The objective, a bold temperament or a fight already in hand:
            # the look is all there is time for.
            return True
        # The next shot may be meant for this bot: out of that line.
        life.fire_from, life.fire_cause = origin, "ally_down"
        life.hits = 2
        life.fire_noticed_at = now + delay
        life.look_back_until = life.fire_noticed_at + _LOOK_BACK_SECONDS
        life.fire_until = life.fire_noticed_at + 0.8 * (
            1.4 + 2.2 * float(profile.caution) - 0.8 * float(profile.aggression))
        life.shelter = _Shelter()
        life.shelter_hits = 0
        life.dash = None
        return True

    def _in_view(self, frame: PerceptionFrame, observer: PlayerSnapshot, player_id: int) -> bool:
        """Whether the maker of a sound is someone the bot is looking at."""

        for player in frame.players:
            if player.player_id != player_id:
                continue
            dx, dy = player.eye[0] - observer.eye[0], player.eye[1] - observer.eye[1]
            flat = math.hypot(dx, dy)
            facing = ((observer.orientation[0] * dx + observer.orientation[1] * dy) / flat
                      if flat > 1e-6 else 1.0)
            return (player.alive and facing >= 0.3
                    and self.world.has_line_of_sight(observer.eye, player.eye))
        return False

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
                life.fire_cause = "under_fire"
                if now >= life.fire_until:
                    life.hits = 0
                    # Wound up by what it has been hearing, it understands sooner.
                    life.fire_noticed_at = hit_at + notice_delay(profile) * (
                        1.0 - 0.5 * life.alert)
                    life.look_back_until = life.fire_noticed_at + _LOOK_BACK_SECONDS
                    life.shelter = _Shelter()
                    life.shelter_hits = 0
                    life.dash = None
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
            life.glance, life.glance_from = threat, now
            life.glance_until = life.watch_until = now + 0.8
            return None
        life.glance = None

        shelter = life.shelter
        if shelter.reached and life.hits > life.shelter_hits:
            # Hit again where it thought it was safe: that spot is no cover.
            shelter.clear()
        # A skilled player allows for the shooter stepping sideways and looks
        # farther for something solid; a weak one hides from the one spot the
        # shot came from, within a few strides.
        line = firing_line(observer.position, threat) if profile.skill >= 0.45 else (threat,)
        side = 1.0 if mix(observer.player_id, observer.life_id, life.hits) < 0.5 else -1.0
        state, heading = self._seek(observer, shelter, line, now,
                                    reach=4.5 + 6.5 * float(profile.skill), min_gap=2.5,
                                    side=side)
        # Each run starts with a look at where the shot came from; then the
        # eyes go where the feet are going, as a runner's do.
        startled = now < life.look_back_until
        airborne = not observer.grounded
        if state == "reached":
            if life.shelter_hits <= 0:
                life.shelter_hits = life.hits
            return Reaction(life.fire_cause + "_hold", look=threat, crouch=True)
        if state == "run":
            life.shelter_hits = 0
            gap = math.hypot(shelter.spot[0] - observer.position[0],
                             shelter.spot[1] - observer.position[1])
            return Reaction(life.fire_cause + "_cover", heading=heading,
                            sprint=gap > _SHELTER_BRAKE,
                            look=threat if startled else _ahead(observer, heading))
        if not airborne and (life.dash is None or now >= life.dash_until):
            life.dash = self._dash_heading(observer, threat, life)
            life.dashes += 1
            life.dash_until = now + 0.7 + 0.5 * mix(observer.player_id, life.dashes, 5)
            if life.dashes > 1:
                startled = True
                life.look_back_until = now + _LOOK_BACK_SECONDS
        if life.dash is None:
            return Reaction(life.fire_cause + "_hold", look=threat, crouch=not airborne)
        return Reaction(life.fire_cause + "_evade", heading=life.dash, sprint=True,
                        look=threat if startled else _ahead(observer, life.dash))

    def _seek(self, observer: PlayerSnapshot, shelter: _Shelter, threats, now: float, *,
              reach: float, min_gap: float, side: float) -> tuple[str, Vector3]:
        """Advance one run for cover: ``reached``, ``run`` with a heading, or ``none``.

        Searches are rate limited and never start from mid-air, where ground
        probes mean nothing (a step down a terrace, the hop out of a crouch).
        """

        if shelter.spot is None and observer.grounded and now >= shelter.next_search_at:
            shelter.next_search_at = now + _SHELTER_RETRY_SECONDS
            shelter.spot = find_shelter(self.world, observer, threats, reach=reach,
                                        min_gap=min_gap, side=side)
            shelter.reached = False
            shelter.best = math.inf
            shelter.progress_at = now
        if shelter.spot is None:
            return "none", (0.0, 0.0, 0.0)
        gap = math.hypot(shelter.spot[0] - observer.position[0],
                         shelter.spot[1] - observer.position[1])
        if gap <= _SHELTER_ARRIVAL:
            shelter.reached = True
        if shelter.reached:
            return "reached", (0.0, 0.0, 0.0)
        if gap + 0.3 < shelter.best:
            shelter.best, shelter.progress_at = gap, now
        if now - shelter.progress_at >= _SHELTER_STALL_SECONDS:
            # Something the straight-line check missed is in the way.
            shelter.clear()
            return "none", (0.0, 0.0, 0.0)
        return "run", _unit(shelter.spot[0] - observer.position[0],
                            shelter.spot[1] - observer.position[1])

    # -- outnumbered or nearly dead ------------------------------------------

    def _count_odds(self, frame: PerceptionFrame, observer: PlayerSnapshot,
                    life: _Life, now: float) -> None:
        """Count the enemies this bot can see, and remember where their eyes are.

        Stimulus: enemies in line of sight that are in front of the bot, at
        arm's length, or marked by the damage indicator. Nothing behind a
        wall is counted.
        """

        life.odds_at = now + _ODDS_SECONDS
        life.health_log = tuple(
            entry for entry in life.health_log
            if now - entry[0] <= _LOSS_WINDOW_SECONDS) + ((now, int(observer.health)),)
        candidates = []
        for player in frame.players:
            if (player.team == observer.team or not player.alive or not player.spawned
                    or player.spawn_protected):
                continue
            distance = math.dist(observer.eye, player.eye)
            if distance > _ODDS_REACH:
                continue
            dx, dy = player.eye[0] - observer.eye[0], player.eye[1] - observer.eye[1]
            flat = math.hypot(dx, dy)
            facing = ((observer.orientation[0] * dx + observer.orientation[1] * dy) / flat
                      if flat > 1e-6 else 1.0)
            marked = (observer.last_damage_source_id == player.player_id
                      and 0.0 <= now - observer.last_damage_at <= 3.0)
            if facing >= 0.2 or marked or distance <= 8.0:
                candidates.append((distance, player.eye))
        candidates.sort(key=lambda item: item[0])
        seen = [(distance, eye) for distance, eye in candidates[:_MAX_FOES]
                if self.world.has_line_of_sight(observer.eye, eye)]
        if seen:
            life.foes = tuple(eye for _distance, eye in seen)
            life.foes_at = now
            life.nearest_foe = seen[0][0]
        elif now - life.foes_at > _FIGHT_MEMORY_SECONDS:
            life.foes = ()
            life.nearest_foe = math.inf

    def _outmatched(self, frame: PerceptionFrame, observer: PlayerSnapshot,
                    profile: BotProfile, life: _Life, visible: PlayerSnapshot | None,
                    decision: ModeBotDecision | None, now: float) -> Reaction | None:
        """Break off a fight that is being lost and come back on better terms.

        Out of sight first, then back toward the team; the fight is taken up
        again once there is company or the enemy has lost track. Stimulus:
        the enemies in sight, the bot's own health and how fast it is going.
        """

        if life.retreat:
            return self._retreat(frame, observer, profile, life, visible, now)
        pressed = (observer.last_damage_at > 0.0
                   and 0.0 <= now - observer.last_damage_at <= _PRESSURE_SECONDS
                   and int(observer.last_damage_kind) not in _UNDIRECTED_DAMAGE)
        if not pressed or now < life.retreat_rest_until:
            life.outmatched_since = 0.0
            if not pressed:
                life.health_log = ()
            return None
        if now >= life.odds_at:
            self._count_odds(frame, observer, life, now)
        outmatched = False
        odds = 0
        if life.foes and life.nearest_foe > _POINT_BLANK and not _holds_ground(
                observer, decision):
            odds = len(life.foes) - (self._allies_near(frame, observer) + 1)
            lean = float(profile.caution) - float(profile.aggression)
            outnumbered = odds >= (1 if lean > 0.3 else 3 if lean < -0.4 else 2)
            hurt = observer.health <= 22.0 + 26.0 * float(profile.caution) - 12.0 * float(
                profile.aggression)
            lost = max(health for _at, health in life.health_log) - int(observer.health)
            # Whatever the temperament: at this rate there are seconds left,
            # and fewer when more than one gun is doing it.
            losing = lost >= 20 and observer.health <= (2.5 if odds >= 1 else 1.5) * lost
            outmatched = outnumbered or hurt or losing
        if not outmatched:
            if now - life.outmatched_at > _FIGHT_MEMORY_SECONDS:
                life.outmatched_since = 0.0
            return None
        life.outmatched_at = now
        if life.outmatched_since <= 0.0:
            life.outmatched_since = now
        if now - life.outmatched_since < notice_delay(profile):
            return None
        refuge = _Shelter()
        state, _heading = self._seek(observer, refuge, life.foes, now,
                                     reach=5.0 + 5.5 * float(profile.skill), min_gap=0.0,
                                     side=self._home_side(frame, observer, life))
        life.rally = self._rally_point(frame, observer, life)
        if state == "none" and (odds < 1 or life.rally is None):
            # Hurt in the open, one on one: running only shows a back.
            life.retreat_rest_until = now + 1.5
            return None
        life.refuge = refuge
        life.retreat = "cover" if state != "none" else "fall_back"
        life.retreat_until = now + (4.0 if life.retreat == "cover" else _FALL_BACK_SECONDS)
        life.retreat_hit_at = float(observer.last_damage_at)
        life.dead_ground = None
        life.dead_ground_at = 0.0
        life.outmatched_since = 0.0
        return self._retreat(frame, observer, profile, life, visible, now)

    @staticmethod
    def _allies_near(frame: PerceptionFrame, observer: PlayerSnapshot) -> int:
        """Teammates close enough to be in the same fight (the minimap shows them)."""

        return sum(1 for player in frame.players
                   if player.team == observer.team and player.alive and player.spawned
                   and player.player_id != observer.player_id
                   and math.dist(player.position, observer.position) <= _ALLY_REACH)

    def _retreat(self, frame: PerceptionFrame, observer: PlayerSnapshot, profile: BotProfile,
                 life: _Life, visible: PlayerSnapshot | None, now: float) -> Reaction | None:
        """Carry out one retreat.

        ``cover`` runs to something close, ``fall_back`` crosses open ground
        toward the next fold in the terrain or the team, ``hold`` catches a
        breath out of sight, ``regroup`` walks on to the team.
        """

        if not life.foes:
            return self._end_retreat(life, profile, now)
        foe = life.foes[0]
        side = self._home_side(frame, observer, life)
        hit_at = float(observer.last_damage_at)
        if life.retreat == "regroup" and hit_at > life.retreat_hit_at + 1e-6:
            # Still in somebody's sights: look for cover again.
            life.retreat = "fall_back"
            life.refuge = _Shelter()
        if life.retreat == "fall_back" and life.refuge.spot is None:
            if self._seek(observer, life.refuge, life.foes, now, reach=10.5, min_gap=0.0,
                          side=side)[0] != "none":
                life.retreat = "cover"
                life.retreat_until = now + 4.0
            elif observer.grounded and now >= life.dead_ground_at:
                life.dead_ground_at = now + _DEAD_GROUND_SECONDS
                life.dead_ground = find_dead_ground(self.world, observer, life.foes, side=side)
        if life.retreat == "cover":
            state, heading = self._seek(observer, life.refuge, life.foes, now,
                                        reach=10.5, min_gap=0.0, side=side)
            if state == "run" and now < life.retreat_until:
                gap = math.hypot(life.refuge.spot[0] - observer.position[0],
                                 life.refuge.spot[1] - observer.position[1])
                return Reaction("disengage_cover", heading=heading,
                                sprint=gap > _SHELTER_BRAKE, look=_ahead(observer, heading))
            if state == "reached":
                self._settle(life, profile, hit_at, now)
            else:
                life.refuge.clear()
                life.retreat = "fall_back"
                life.retreat_until = now + _FALL_BACK_SECONDS
        if life.retreat == "hold":
            if visible is not None and math.dist(visible.position, observer.position) <= 14.0:
                # They came round the corner: fight from here.
                return self._end_retreat(life, profile, now)
            hit_here = hit_at > life.retreat_hit_at + 1e-6
            if not hit_here and now < life.retreat_until:
                return Reaction("disengage_hold", look=foe, crouch=True)
            company = self._allies_near(frame, observer) > 0
            life.rally = self._rally_point(frame, observer, life, reach=_REGROUP_REACH)
            if not hit_here and life.rally is not None and not company and (
                    self._way_exposed(observer, life.rally, life.foes)):
                # The way back to the team crosses their sights.
                life.rally = None
            if not hit_here and (company or (life.rally is None and now >= life.retreat_wait)):
                # Breath caught, and company at hand or none to be had.
                return self._end_retreat(life, profile, now)
            if not hit_here and life.rally is None:
                # Nobody to join by a safe way: out of sight is the place to
                # be until someone comes, friend or enemy.
                life.retreat_until = now + 1.0
                return Reaction("disengage_hold", look=foe, crouch=True)
            if hit_here:
                life.rally = self._rally_point(frame, observer, life)
            # On toward the team. Cover is looked for again only if this
            # spot turned out to be none.
            life.retreat = "fall_back" if hit_here else "regroup"
            life.refuge = _Shelter()
            life.dead_ground = None
            life.retreat_hit_at = hit_at
            life.retreat_until = now + _FALL_BACK_SECONDS
        rally = life.rally
        if (rally is None or now >= life.retreat_until
                or math.dist(rally[:2], observer.position[:2]) <= _FALL_BACK_ARRIVAL
                or (life.retreat == "regroup" and self._allies_near(frame, observer))):
            return self._end_retreat(life, profile, now)
        away = _unit(observer.position[0] - foe[0], observer.position[1] - foe[1])
        if life.retreat == "fall_back" and life.dead_ground is not None:
            if math.dist(life.dead_ground[:2], observer.position[:2]) <= _DEAD_GROUND_ARRIVAL:
                self._settle(life, profile, hit_at, now)
                return Reaction("disengage_hold", look=foe, crouch=True)
            return Reaction("fall_back", goal=life.dead_ground, heading=away, sprint=True)
        return Reaction("regroup" if life.retreat == "regroup" else "fall_back",
                        goal=rally, heading=away, sprint=True)

    def _way_exposed(self, observer: PlayerSnapshot, rally: Vector3, foes) -> bool:
        """Whether the first strides toward ``rally`` leave cover again."""

        heading = _unit(rally[0] - observer.position[0], rally[1] - observer.position[1])
        surface = self.world.surface(
            int(math.floor(observer.position[0] + heading[0] * 4.0)),
            int(math.floor(observer.position[1] + heading[1] * 4.0)),
            observer.position[2], vertical_span=2, allow_water=False)
        return surface is None or exposed(self.world, surface.position, foes)

    @staticmethod
    def _settle(life: _Life, profile: BotProfile, hit_at: float, now: float) -> None:
        """Out of sight: stay down for a few seconds before deciding what next."""

        life.retreat = "hold"
        life.retreat_hit_at = hit_at
        life.retreat_until = now + 2.0 + 3.5 * float(profile.caution)
        life.retreat_wait = now + 5.0 + 7.0 * float(profile.caution) - 2.5 * float(
            profile.aggression)

    @staticmethod
    def _end_retreat(life: _Life, profile: BotProfile, now: float) -> None:
        life.retreat = ""
        life.refuge = _Shelter()
        # No second retreat on the heels of the first: the next fight is fought.
        life.retreat_rest_until = now + 5.0 + 6.0 * float(profile.aggression)
        return None

    @staticmethod
    def _rally_point(frame: PerceptionFrame, observer: PlayerSnapshot,
                     life: _Life, reach: float = math.inf) -> Vector3 | None:
        """Where the team is: the nearest teammate out of this fight, else the base.

        Teammates and the base are on every player's minimap. With ``reach``
        only a teammate that close counts.
        """

        foe = life.foes[0] if life.foes else None
        mates = [player.position for player in frame.players
                 if player.team == observer.team and player.alive and player.spawned
                 and player.player_id != observer.player_id
                 and math.dist(player.position, observer.position) > _FALL_BACK_ARRIVAL
                 # Not one standing among the enemy it is running from.
                 and (foe is None or math.dist(player.position, foe) > 12.0)]
        if mates:
            nearest = min(mates, key=lambda position: math.dist(position, observer.position))
            if math.dist(nearest, observer.position) <= reach:
                return nearest
        if reach < math.inf:
            return None
        return next((item.position for item in frame.objectives
                     if item.kind == "team_anchor" and item.team == observer.team), None)

    def _home_side(self, frame: PerceptionFrame, observer: PlayerSnapshot,
                   life: _Life) -> float:
        """Which flank of the line away from the enemy leans toward the team."""

        rally = self._rally_point(frame, observer, life)
        if rally is None or not life.foes:
            return 1.0
        foe = life.foes[0]
        away_x, away_y = observer.position[0] - foe[0], observer.position[1] - foe[1]
        home_x, home_y = rally[0] - observer.position[0], rally[1] - observer.position[1]
        return 1.0 if away_x * home_y - away_y * home_x >= 0.0 else -1.0

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
    a lined-up drop, a swim) need the view where the task has put it. Plain
    walking does not: the keys are worked out from wherever the eyes point.
    """

    movement = intent.movement
    return (intent.action.kind is BotActionKind.NONE
            and intent.priority < BotIntentPriority.SURVIVAL
            and not (intent.look is not None and intent.look.visible)
            and movement.affordance is MovementAffordance.WALK
            and not movement.jump)
