"""Pure, deterministic mode policies executed only in the AI worker.

Policies consume immutable perception messages.  They may use map objectives,
friendly roster state, and mode-sanctioned markers (CTF carriers, VIP crowns,
the Zombie last-survivor marker), but never query authoritative server objects
or infer a hidden enemy from the complete roster snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
from typing import Protocol

import shared.constants as C

from .messages import ObjectiveSnapshot, PerceptionFrame, PlayerSnapshot, Vector3


_ZOMBIE_CLASSES = frozenset({
    int(C.CLASS_ZOMBIE),
    int(C.CLASS_FAST_ZOMBIE),
    int(C.CLASS_JUMP_ZOMBIE),
})


class ModeBotPosture(str, Enum):
    """Combat behavior selected by a bot's current mode role."""

    ASSAULT = "assault"
    BALANCED = "balanced"
    DEFEND = "defend"
    ESCORT = "escort"
    EVASIVE = "evasive"
    SURVIVE = "survive"
    BUILD = "build"
    MINE = "mine"


@dataclass(frozen=True, slots=True)
class ModeBotStrategy:
    """Human-readable winning objective and default style for one mode."""

    code: str
    objective: str
    default_posture: ModeBotPosture


@dataclass(frozen=True, slots=True)
class ModeBotDecision:
    """One phase/role-specific objective and its gameplay behavior."""

    position: Vector3
    role: str
    sprint: bool = True
    arrival_radius: float = 3.0
    # Optional standing order interpreted by BotBrain beyond navigation:
    # ``fortify`` builds a defensible site and ``mine`` excavates nearby safe
    # surface cells for Diamond Mine's authoritative uncover rolls.
    directive: str = ""
    posture: ModeBotPosture = ModeBotPosture.BALANCED
    # Higher commitment makes the objective outrank optional resource trips
    # and stale-contact chasing. Immediate self-defence always remains legal.
    objective_priority: float = 0.5
    # Defensive roles also measure an enemy against their protected objective,
    # rather than only against the bot's current position.
    engagement_radius: float = 160.0
    # Public approach to watch once arrived; independent of the ground waypoint.
    watch_position: Vector3 | None = None


_MODE_STRATEGIES: dict[str, ModeBotStrategy] = {
    "nor": ModeBotStrategy(
        "nor",
        "Advance with the team and control the opposing side of the map.",
        ModeBotPosture.BALANCED,
    ),
    "tdm": ModeBotStrategy(
        "tdm",
        "Win efficient engagements while maintaining squad pressure.",
        ModeBotPosture.ASSAULT,
    ),
    "arena": ModeBotStrategy(
        "arena",
        "Eliminate the enemy team while preserving each irreplaceable life.",
        ModeBotPosture.SURVIVE,
    ),
    "ctf": ModeBotStrategy(
        "ctf",
        "Capture enemy intel, escort carriers, and recover friendly intel.",
        ModeBotPosture.ESCORT,
    ),
    "cctf": ModeBotStrategy(
        "cctf",
        "Capture enemy intel while defending without hidden carrier markers.",
        ModeBotPosture.ESCORT,
    ),
    "zom": ModeBotStrategy(
        "zom",
        "Survivors fortify together; infected breach and convert survivors.",
        ModeBotPosture.SURVIVE,
    ),
    "vip": ModeBotStrategy(
        "vip",
        "Protect the friendly VIP and coordinate attacks on the enemy VIP.",
        ModeBotPosture.ESCORT,
    ),
    "mh": ModeBotStrategy(
        "mh",
        "Capture the active hill, then hold its approaches.",
        ModeBotPosture.DEFEND,
    ),
    "dem": ModeBotStrategy(
        "dem",
        "Fortify the friendly objective and destroy the opposing objective.",
        ModeBotPosture.BUILD,
    ),
    "tc": ModeBotStrategy(
        "tc",
        "Capture connected territories while retaining a defensive line.",
        ModeBotPosture.ASSAULT,
    ),
    "dia": ModeBotStrategy(
        "dia",
        "Mine, collect, escort, and cash in diamonds at active drop-offs.",
        ModeBotPosture.MINE,
    ),
    "oc": ModeBotStrategy(
        "oc",
        "Deliver bombs as Blue; intercept and dispose of them as Green.",
        ModeBotPosture.ESCORT,
    ),
    # These isolated launchers normally have no bots. Explicit passive entries
    # keep admin-added bots from damaging lessons or editor work.
    "tut": ModeBotStrategy(
        "tut",
        "Remain passive so authored player lessons are not disturbed.",
        ModeBotPosture.SURVIVE,
    ),
    "ugc": ModeBotStrategy(
        "ugc",
        "Remain passive while players author and validate map objectives.",
        ModeBotPosture.SURVIVE,
    ),
}

_MODE_ALIASES = {
    "normal": "nor",
    "classic_ctf": "cctf",
    "classic-ctf": "cctf",
    "zombie": "zom",
    "multihill": "mh",
    "multi-hill": "mh",
    "demolition": "dem",
    "territory_control": "tc",
    "territory-control": "tc",
    "diamond": "dia",
    "diamond_mine": "dia",
    "occupation": "oc",
    "tutorial": "tut",
}


def _canonical_mode(mode_id: str) -> str:
    normalized = str(mode_id).strip().lower()
    return _MODE_ALIASES.get(normalized, normalized)


def mode_strategy_for(mode_id: str) -> ModeBotStrategy:
    """Return the declared strategy for every protocol and public mode."""

    return _MODE_STRATEGIES.get(
        _canonical_mode(mode_id),
        _MODE_STRATEGIES["nor"],
    )


class ModeBotPolicy(Protocol):
    """Choose legal mode-supplied navigation knowledge for one observer."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        """Return a bounded decision without reading server-owned objects."""


class PatrolCombatPolicy:
    """Advance toward the opposing side until ordinary perception takes over."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        enemy_anchor = next(
            (
                item for item in frame.objectives
                if item.kind == "team_anchor" and item.team != observer.team
            ),
            None,
        )
        if enemy_anchor is None:
            return None
        assault = _formation_point(
            enemy_anchor.position,
            observer.player_id + observer.team * 31,
            8.0 + float(observer.player_id % 3) * 3.0,
        )
        return ModeBotDecision(
            assault,
            "team_assault_enemy_side",
            sprint=True,
            arrival_radius=5.0,
            posture=ModeBotPosture.BALANCED,
            objective_priority=0.45,
        )


class TeamDeathmatchBotPolicy:
    """Split the team into pressure, support, and cautious regroup roles."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        own_anchor = _objective(frame, "team_anchor", observer.team)
        enemy_anchor = next(
            (
                item for item in frame.objectives
                if item.kind == "team_anchor" and item.team != observer.team
            ),
            None,
        )
        if enemy_anchor is None:
            return None
        teammates = [
            player for player in frame.players
            if player.player_id != observer.player_id
            and player.team == observer.team
            and player.alive
            and player.spawned
        ]
        profile = frame.profile
        caution = float(profile.caution) if profile is not None else 0.5
        teamwork = float(profile.teamwork) if profile is not None else 0.5
        aggression = float(profile.aggression) if profile is not None else 0.6

        recently_wounded = (
            float(observer.last_damage_at) > 0.0
            and 0.0 <= frame.created_at - observer.last_damage_at <= 8.0
        )
        if observer.health <= 45 and teammates and recently_wounded:
            teammate = min(
                teammates,
                key=lambda player: _distance_squared(
                    observer.position, player.position
                ),
            )
            return ModeBotDecision(
                teammate.position,
                "tdm_regroup_wounded",
                sprint=True,
                arrival_radius=4.0,
                posture=ModeBotPosture.SURVIVE,
                objective_priority=0.78,
                engagement_radius=10.0,
            )

        if teamwork >= 0.7 and teammates and observer.player_id % 3 == 0:
            teammate = min(
                teammates,
                key=lambda player: _distance_squared(
                    observer.position, player.position
                ),
            )
            squad_point = _toward(
                teammate.position,
                enemy_anchor.position,
                18.0,
            )
            return ModeBotDecision(
                _formation_point(squad_point, observer.player_id, 4.0),
                "tdm_squad_support",
                sprint=True,
                arrival_radius=3.0,
                posture=ModeBotPosture.ESCORT,
                objective_priority=0.58,
                engagement_radius=34.0,
            )

        if (
            own_anchor is not None
            and caution > 0.75
            and aggression < 0.55
            and observer.player_id % 4 == 0
        ):
            overwatch = _toward(
                own_anchor.position,
                enemy_anchor.position,
                72.0,
            )
            return ModeBotDecision(
                _formation_point(overwatch, observer.player_id, 8.0),
                "tdm_overwatch_lane",
                sprint=False,
                arrival_radius=5.0,
                posture=ModeBotPosture.DEFEND,
                objective_priority=0.52,
                engagement_radius=48.0,
            )

        assault = _formation_point(
            enemy_anchor.position,
            observer.player_id + observer.team * 31,
            8.0 + float(observer.player_id % 3) * 3.0,
        )
        return ModeBotDecision(
            assault,
            "team_assault_enemy_side",
            sprint=True,
            arrival_radius=5.0,
            posture=ModeBotPosture.ASSAULT,
            objective_priority=0.5 + min(0.2, aggression * 0.2),
            engagement_radius=160.0,
        )


class PassiveIsolatedModePolicy:
    """Keep accidental Tutorial/UGC bots from altering authored content."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision:
        return ModeBotDecision(
            observer.position,
            f"{_canonical_mode(frame.mode_id)}_passive",
            sprint=False,
            arrival_radius=1.0,
            posture=ModeBotPosture.SURVIVE,
            objective_priority=1.0,
            engagement_radius=0.0,
        )


class CTFBotPolicy:
    """Assign capture, escort, recovery, defence, and assault roles."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        classic = str(frame.mode_id).lower() == "cctf"
        prefix = "classic_" if classic else ""
        own_base = _objective(frame, "ctf_base", observer.team)
        own_intel = _objective(frame, "ctf_intel", observer.team)
        enemy_intel = next(
            (
                item for item in frame.objectives
                if item.kind == "ctf_intel" and item.team != observer.team
            ),
            None,
        )

        if observer.carried_entity_id >= 0 and own_base is not None:
            return ModeBotDecision(
                own_base.position,
                f"{prefix}ctf_capture",
                sprint=True,
                arrival_radius=4.0,
                posture=ModeBotPosture.EVASIVE,
                objective_priority=0.98,
                engagement_radius=8.0,
            )

        # Normal CTF publishes the native high-visibility carrier marker.
        # Classic disables its minimap, so do not turn an invisible marker
        # into worker omniscience: Classic defenders hold the base instead.
        if (
            not classic
            and own_intel is not None
            and own_intel.carrier_id >= 0
        ):
            return ModeBotDecision(
                own_intel.position,
                "ctf_intercept_carrier",
                sprint=True,
                arrival_radius=2.5,
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.94,
                engagement_radius=160.0,
            )

        if enemy_intel is not None and enemy_intel.carrier_id >= 0:
            carrier = _friendly_player(frame, observer, enemy_intel.carrier_id)
            if carrier is not None and carrier.player_id != observer.player_id:
                escort = _formation_point(carrier.position, observer.player_id, 4.5)
                return ModeBotDecision(
                    escort,
                    f"{prefix}ctf_escort",
                    sprint=True,
                    arrival_radius=2.5,
                    posture=ModeBotPosture.ESCORT,
                    objective_priority=0.88,
                    engagement_radius=32.0,
                )

        if own_base is not None and self._is_defender(frame, observer, own_base.position):
            return ModeBotDecision(
                self._guard_point(frame, observer, own_base.position, enemy_intel),
                f"{prefix}ctf_defend",
                sprint=False,
                arrival_radius=3.0,
                posture=ModeBotPosture.DEFEND,
                objective_priority=0.74,
                engagement_radius=38.0,
            )

        if (
            enemy_intel is not None
            and enemy_intel.carrier_id < 0
            and (not classic or int(enemy_intel.state) == 0)
        ):
            if own_base is not None and self._should_rally(
                    frame, observer, own_base.position, enemy_intel.position):
                return ModeBotDecision(
                    observer.position,
                    f"{prefix}ctf_rally",
                    sprint=False,
                    arrival_radius=4.0,
                    posture=ModeBotPosture.DEFEND,
                    objective_priority=0.8,
                    engagement_radius=60.0,
                    watch_position=enemy_intel.position,
                )
            return ModeBotDecision(
                enemy_intel.position,
                f"{prefix}ctf_attack_intel",
                sprint=True,
                arrival_radius=2.0,
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.82,
                engagement_radius=90.0,
            )
        return None


    @staticmethod
    def _should_rally(frame: PerceptionFrame, observer: PlayerSnapshot,
                      base: Vector3, goal: Vector3) -> bool:
        """Wait at midfield for a teammate who is about to arrive.

        Lone attackers reached the enemy base one at a time and died there;
        five minutes produced no pickup. The wait is self-limiting: it exists
        only while an ally is coming up from behind within 70 blocks, never
        under fire, and ends the moment anyone is alongside.
        """
        ax, ay = goal[0] - base[0], goal[1] - base[1]
        span = ax * ax + ay * ay
        if span < 160.0 ** 2:
            return False

        def along(position: Vector3) -> float:
            return ((position[0] - base[0]) * ax + (position[1] - base[1]) * ay) / span

        mine = along(observer.position)
        recently_hit = (observer.last_damage_at > 0.0
                        and 0.0 <= frame.created_at - observer.last_damage_at <= 4.0)
        if not 0.38 <= mine <= 0.58 or recently_hit:
            return False
        allies = [player for player in frame.players
                  if player.team == observer.team and player.alive and player.spawned
                  and player.player_id != observer.player_id]
        if any(math.dist(player.position, observer.position) <= 18.0
               or along(player.position) > mine + 0.04 for player in allies):
            return False  # someone is alongside, or the push already left
        return any(0.12 <= along(player.position) < mine
                   and math.dist(player.position, observer.position) <= 70.0
                   for player in allies)

    @staticmethod
    def _is_defender(frame: PerceptionFrame, observer: PlayerSnapshot,
                     base: Vector3) -> bool:
        """Keep the teammates nearest home on guard; everyone else attacks.

        Fixed ``id % 3`` sentries froze four of ten bots for a whole match.
        Proximity rotates the duty by itself: a fresh respawn beside the base
        relieves the previous guard, who then joins the push.
        """
        team = [player for player in frame.players
                if player.team == observer.team and player.alive and player.spawned]
        wanted = 0 if len(team) < 3 else 1 if len(team) < 7 else 2
        if wanted == 0:
            return False
        own = math.dist(observer.position, base)
        closer = sorted(math.dist(player.position, base) for player in team
                        if player.player_id != observer.player_id)
        if len(closer) < wanted:
            return True
        # A relief must be clearly nearer before the duty changes hands, so
        # two bots at similar range do not swap roles every decision.
        return own <= closer[wanted - 1] + 6.0 and own <= 90.0

    @staticmethod
    def _guard_point(frame: PerceptionFrame, observer: PlayerSnapshot, base: Vector3,
                     enemy_intel) -> Vector3:
        """Walk a slow beat around the base, with a forward picket every third leg."""
        beat = int((float(frame.created_at) + observer.player_id * 4.1) // 16.0)
        if beat % 3 == 2 and enemy_intel is not None and enemy_intel.carrier_id < 0:
            return _toward(base, enemy_intel.position, 26.0 + 4.0 * (observer.player_id % 3))
        return _formation_point(base, observer.player_id + beat * 5,
                                7.0 + 4.0 * ((beat + observer.player_id) % 3))


_SURVIVOR_THREAT_RADIUS = 28.0
_SURVIVOR_KITE_RADIUS = 9.0
_ROAM_ARRIVAL_RADIUS = 5.0
_ROAM_NO_PROGRESS_SECONDS = 15.0
_ROAM_MIN_LEG = 15.0


@dataclass(slots=True)
class _RoamLeg:
    """One survivor's current roam destination and its progress evidence."""

    waypoint: Vector3
    chosen_at: float
    deadline: float
    best_distance: float
    progress_at: float
    arrived_at: float | None = None


class ZombieBotPolicy:
    """Separate preparation, survivor, infected, and last-man behavior.

    Survivor roaming keeps a small per-life leg record so a bot walks a leg to
    completion (or gives up on an unreachable one) instead of re-rolling its
    destination on a clock, and so survivors spread out rather than clump.
    """

    def __init__(self) -> None:
        self._epoch: tuple[int, int] | None = None
        self._legs: dict[tuple[int, int, int], _RoamLeg] = {}
        self._recent: dict[tuple[int, int, int], list[Vector3]] = {}

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        phase = str(frame.mode_phase).lower()
        own_anchor = _objective(frame, "team_anchor", observer.team)
        survivor = next(
            (item for item in frame.objectives if item.kind == "last_survivor"),
            None,
        )

        if phase in ("", "waiting", "countdown"):
            if own_anchor is None:
                return None
            if observer.player_id % 3 != 0:
                # Most survivors scout and loot the map before the outbreak
                # instead of all queueing on one fortification site.
                return self._survivor_roam(frame, observer, own_anchor)
            return ModeBotDecision(
                _guard_beat(
                    frame, observer, own_anchor.position,
                    10.0 + float(observer.player_id % 4) * 3.0,
                ),
                "zombie_prepare_fortify",
                sprint=False,
                arrival_radius=5.0,
                directive="fortify",
                posture=ModeBotPosture.BUILD,
                objective_priority=0.92,
                engagement_radius=18.0,
            )

        if survivor is not None and survivor.team != observer.team:
            # This exact location is legal only because ZombieMode publishes
            # the native final-survivor marker to every infected client.
            return ModeBotDecision(
                survivor.position,
                "zombie_hunt_last_survivor",
                sprint=True,
                arrival_radius=1.5,
                posture=ModeBotPosture.ASSAULT,
                objective_priority=1.0,
                engagement_radius=160.0,
            )

        if int(observer.class_id) in _ZOMBIE_CLASSES:
            # Infection exposes the survivor roster as the horde's strategic
            # target set. This is a deliberate mode rule: infected pursue the
            # nearest living survivor even before ordinary weapon FOV/LOS can
            # see them. Combat still requires a fresh LOS sample in BotBrain.
            survivors = [
                player for player in frame.players
                if player.team != observer.team
                and player.alive
                and player.spawned
                and int(player.class_id) not in _ZOMBIE_CLASSES
            ]
            if survivors:
                target = min(
                    survivors,
                    key=lambda player: _distance_squared(
                        observer.position, player.position
                    ),
                )
                return ModeBotDecision(
                    target.position,
                    "zombie_hunt_survivor",
                    sprint=True,
                    arrival_radius=1.25,
                    posture=ModeBotPosture.ASSAULT,
                    objective_priority=0.98,
                    engagement_radius=160.0,
                )

        if survivor is not None and survivor.carrier_id == observer.player_id:
            enemy_anchor = next(
                (
                    item for item in frame.objectives
                    if item.kind == "team_anchor" and item.team != observer.team
                ),
                None,
            )
            escape = _away_from(
                observer.position,
                (
                    enemy_anchor.position
                    if enemy_anchor is not None
                    else (256.0, 256.0, observer.position[2])
                ),
                22.0,
            )
            return ModeBotDecision(
                escape,
                "zombie_last_survivor_escape",
                sprint=True,
                arrival_radius=4.0,
                posture=ModeBotPosture.EVASIVE,
                objective_priority=1.0,
                engagement_radius=7.0,
            )

        if own_anchor is None:
            return None
        if int(observer.class_id) in _ZOMBIE_CLASSES:
            return ModeBotDecision(
                _formation_point(own_anchor.position, observer.player_id, 12.0),
                "zombie_infected_breach",
                sprint=True,
                arrival_radius=5.0,
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.86,
                engagement_radius=160.0,
            )

        threat = self._nearest_threat(frame, observer)
        if threat is not None:
            distance = math.dist(observer.position, threat.position)
            if distance <= _SURVIVOR_KITE_RADIUS:
                # Back away from a zombie in claw range while still allowed to
                # shoot it; melee zombies must not simply walk into the bot.
                return ModeBotDecision(
                    _away_from(observer.position, threat.position, 12.0),
                    "zombie_survivor_kite",
                    sprint=True,
                    arrival_radius=2.0,
                    posture=ModeBotPosture.SURVIVE,
                    objective_priority=0.8,
                    engagement_radius=_SURVIVOR_THREAT_RADIUS,
                )
            # A zombie is close enough to hear: stop roaming and face it.
            return ModeBotDecision(
                _toward(observer.position, threat.position, 3.0),
                "zombie_survivor_hold_line",
                sprint=False,
                arrival_radius=2.0,
                posture=ModeBotPosture.SURVIVE,
                objective_priority=0.72,
                engagement_radius=_SURVIVOR_THREAT_RADIUS + 20.0,
                watch_position=threat.position,
            )
        return self._survivor_roam(frame, observer, own_anchor)

    @staticmethod
    def _nearest_threat(
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> PlayerSnapshot | None:
        """Nearest living zombie within earshot, or None."""
        zombies = [
            player for player in frame.players
            if player.team != observer.team
            and player.alive
            and player.spawned
            and int(player.class_id) in _ZOMBIE_CLASSES
            and math.dist(observer.position, player.position)
            <= _SURVIVOR_THREAT_RADIUS
        ]
        return min(
            zombies,
            key=lambda player: math.dist(observer.position, player.position),
            default=None,
        )

    def _survivor_roam(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        own_anchor: ObjectiveSnapshot,
    ) -> ModeBotDecision:
        """Walk between map points like a player instead of idling at spawn.

        Each survivor keeps its destination until it arrives (then lingers a
        few seconds), stops making progress, or the leg's deadline passes. A
        new destination is chosen away from where other survivors are and are
        heading, so the team spreads over the map. Points are public map
        knowledge: pickups, team anchors, and rings around the survivor base.
        """
        now = float(frame.created_at)
        epoch = (int(frame.map_epoch), int(frame.mode_epoch))
        if epoch != self._epoch:
            self._epoch = epoch
            self._legs.clear()
            self._recent.clear()
        key = (int(observer.player_id), int(observer.generation), int(observer.life_id))
        waypoints = _survivor_waypoints(frame, own_anchor)

        leg = self._legs.get(key)
        if leg is not None and (now < leg.chosen_at or leg.waypoint not in waypoints):
            leg = None
        if leg is not None:
            distance = math.dist(observer.position[:2], leg.waypoint[:2])
            if distance + 2.0 < leg.best_distance:
                leg.best_distance = distance
                leg.progress_at = now
            if distance <= _ROAM_ARRIVAL_RADIUS and leg.arrived_at is None:
                leg.arrived_at = now
            dwell = 3.0 + float(observer.player_id % 4) * 1.5
            if (now >= leg.deadline
                    or (leg.arrived_at is None
                        and now - leg.progress_at > _ROAM_NO_PROGRESS_SECONDS)
                    or (leg.arrived_at is not None and now - leg.arrived_at > dwell)):
                leg = None
        if leg is None:
            leg = self._choose_leg(frame, observer, key, waypoints, now)
            self._legs[key] = leg
            self._forget_stale_legs(frame)

        target = _formation_point(leg.waypoint, observer.player_id, 1.5)
        return ModeBotDecision(
            target,
            "zombie_survivor_roam",
            sprint=math.dist(observer.position, target) > 30.0,
            arrival_radius=_ROAM_ARRIVAL_RADIUS - 1.0,
            posture=ModeBotPosture.BALANCED,
            objective_priority=0.6,
            engagement_radius=80.0,
        )

    def _choose_leg(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        key: tuple[int, int, int],
        waypoints: list[Vector3],
        now: float,
    ) -> _RoamLeg:
        allies = [
            player for player in frame.players
            if player.team == observer.team and player.alive and player.spawned
            and player.player_id != observer.player_id
        ]
        ally_ids = {int(player.player_id) for player in allies}
        crowd = [player.position for player in allies] + [
            leg.waypoint for other, leg in self._legs.items()
            if other[0] in ally_ids
        ]
        recent = self._recent.setdefault(key, [])

        def score(waypoint: Vector3) -> float:
            own = math.dist(observer.position[:2], waypoint[:2])
            spread = min(
                (math.dist(waypoint[:2], point[:2]) for point in crowd),
                default=120.0,
            )
            value = min(spread, 90.0) + min(own, 100.0) * 0.3
            if own < _ROAM_MIN_LEG:
                value -= 60.0
            if waypoint in recent:
                value -= 45.0
            # Stable per-bot tie breaking so equal bots do not pick alike.
            value += ((hash((observer.player_id, waypoint)) & 0xFFFF) % 97) * 0.1
            return value

        waypoint = max(waypoints, key=score)
        recent.append(waypoint)
        del recent[:-4]
        distance = math.dist(observer.position[:2], waypoint[:2])
        return _RoamLeg(
            waypoint=waypoint,
            chosen_at=now,
            deadline=now + 12.0 + distance / 3.0,
            best_distance=distance,
            progress_at=now,
        )

    def _forget_stale_legs(self, frame: PerceptionFrame) -> None:
        if len(self._legs) <= 64:
            return
        live = {(p.player_id, p.generation, p.life_id) for p in frame.players}
        for key in [key for key in self._legs if key not in live]:
            self._legs.pop(key, None)
            self._recent.pop(key, None)


class VIPBotPolicy:
    """Keep an escort while attackers eliminate the VIP, then survivors."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        *,
        retained_role: str = "",
    ) -> ModeBotDecision | None:
        phase = str(frame.mode_phase).lower()
        own_vip = _objective(frame, "vip", observer.team)
        own_anchor = _objective(frame, "team_anchor", observer.team)
        enemy_vip = next(
            (
                item for item in frame.objectives
                if item.kind == "vip" and item.team != observer.team
            ),
            None,
        )
        enemy_anchor = next(
            (
                item for item in frame.objectives
                if item.kind == "team_anchor" and item.team != observer.team
            ),
            None,
        )

        if phase != "active":
            anchor = own_anchor or own_vip
            if anchor is None:
                return None
            return ModeBotDecision(
                _formation_point(anchor.position, observer.player_id, 7.0),
                "vip_form_up",
                sprint=False,
                arrival_radius=4.0,
                posture=ModeBotPosture.DEFEND,
                objective_priority=0.9,
                engagement_radius=24.0,
            )

        if own_vip is not None and observer.player_id == own_vip.carrier_id:
            recently_hurt = (observer.last_damage_at > 0
                            and 0 <= frame.created_at - observer.last_damage_at <= 6)
            retreat = own_anchor.position if own_anchor is not None else own_vip.position
            if recently_hurt and observer.last_damage_source_position is not None:
                # A received hit reveals this position. Do not derive a threat
                # from invisible enemy roster entries.
                retreat = _away_from(observer.position,
                                     observer.last_damage_source_position, 14.0)
            elif not recently_hurt:
                friends = [p for p in frame.players
                           if p.team == observer.team and p.player_id != observer.player_id
                           and p.alive and p.spawned
                           and _distance_squared(p.position, observer.position) <= 40.0 ** 2
                           and (enemy_vip is None or
                                _distance_squared(p.position, enemy_vip.position) >=
                                _distance_squared(observer.position, enemy_vip.position))]
                if friends:
                    retreat = min(friends, key=lambda p: (
                        _distance_squared(observer.position, p.position), p.player_id)).position
            return ModeBotDecision(
                retreat,
                "vip_retreat" if recently_hurt else "vip_rally",
                sprint=recently_hurt,
                arrival_radius=6.0,
                posture=ModeBotPosture.EVASIVE,
                objective_priority=1.0,
                engagement_radius=8.0,
            )

        # Assign from the actual friendly bot roster, not id%3: a small team
        # can otherwise have no attacker at all. Humans receive no assumed
        # orders. Reserve at least one attacker even with a lone non-VIP bot.
        teammates = sorted({p.player_id for p in frame.players
                            if p.team == observer.team and p.is_bot and p.alive and p.spawned
                            and (own_vip is None or p.player_id != own_vip.carrier_id)})
        guard_count = min(max(0, len(teammates) - 1), max(1, len(teammates) // 3))
        guarding = observer.player_id in teammates[:guard_count]
        if retained_role in {"vip_guard_formation", "vip_flank_attack", "vip_mop_up"}:
            guarding = retained_role == "vip_guard_formation"
        if own_vip is not None and guarding:
            return ModeBotDecision(
                # A live character is a grounded route anchor; inventing a
                # ring point at its height can put an escort outside a ledge.
                own_vip.position,
                "vip_guard_formation",
                sprint=True,
                arrival_radius=6.0,
                posture=ModeBotPosture.ESCORT,
                objective_priority=0.94,
                engagement_radius=36.0,
            )

        if enemy_vip is not None:
            role = "vip_sudden_death_assault" if own_vip is None else "vip_flank_attack"
            return ModeBotDecision(
                enemy_vip.position,
                role,
                sprint=own_vip is not None,
                arrival_radius=2.5,
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.84,
                engagement_radius=100.0,
            )

        if enemy_anchor is not None:
            return ModeBotDecision(
                enemy_anchor.position,
                "vip_mop_up",
                sprint=False,
                arrival_radius=7.0,
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.72,
            )
        return None


class ArenaBotPolicy:
    """Regroup wounded players; healthy players retain patrol/combat fallback."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        recently_wounded = (
            float(observer.last_damage_at) > 0.0
            and 0.0 <= frame.created_at - observer.last_damage_at <= 8.0
        )
        if observer.health >= 55 or not recently_wounded:
            assault = _FALLBACK.decide(frame, observer)
            if assault is None:
                return None
            return ModeBotDecision(
                assault.position,
                "arena_elimination_push",
                sprint=True,
                arrival_radius=assault.arrival_radius,
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.62,
                engagement_radius=120.0,
            )
        teammates = [
            player for player in frame.players
            if player.team == observer.team
            and player.player_id != observer.player_id
            and player.alive
        ]
        if not teammates:
            return None
        nearest = min(
            teammates,
            key=lambda player: _distance_squared(observer.position, player.position),
        )
        return ModeBotDecision(
            nearest.position,
            "arena_regroup",
            sprint=False,
            arrival_radius=3.0,
            posture=ModeBotPosture.SURVIVE,
            objective_priority=0.86,
            engagement_radius=12.0,
        )


class MultiHillBotPolicy:
    """Converge on the live shared objective and fortify friendly control."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        hills = [item for item in frame.objectives if item.kind == "mh_hill"]
        if not hills:
            return _FALLBACK.decide(frame, observer)
        hill = min(
            hills,
            key=lambda item: _distance_squared(observer.position, item.position),
        )
        if hill.team == observer.team and not int(hill.state):
            return ModeBotDecision(
                _formation_point(hill.position, observer.player_id, 4.0),
                "multihill_defend",
                sprint=False,
                arrival_radius=2.5,
                directive="fortify" if observer.player_id % 3 == 0 else "",
                posture=ModeBotPosture.DEFEND,
                objective_priority=0.9,
                engagement_radius=34.0,
            )
        return ModeBotDecision(
            hill.position,
            "multihill_contest" if int(hill.state) else "multihill_claim",
            sprint=True,
            arrival_radius=2.0,
            posture=ModeBotPosture.ASSAULT,
            objective_priority=0.92,
            engagement_radius=72.0,
        )


class DemolitionBotPolicy:
    """Build/defend the friendly base and assault the opposing base."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        own_base = _objective(frame, "dem_base", observer.team)
        enemy_base = next(
            (
                item for item in frame.objectives
                if item.kind == "dem_base" and item.team != observer.team
            ),
            None,
        )
        phase = str(frame.mode_phase).lower()
        if phase in ("waiting", "building"):
            if own_base is None:
                return None
            return ModeBotDecision(
                _formation_point(own_base.position, observer.player_id, 5.0),
                "demolition_build_defences",
                sprint=False,
                arrival_radius=3.0,
                directive="fortify",
                posture=ModeBotPosture.BUILD,
                objective_priority=0.98,
                engagement_radius=18.0,
            )
        if phase == "airstrike" and enemy_base is not None:
            return ModeBotDecision(
                _away_from(observer.position, enemy_base.position, 28.0),
                "demolition_escape_airstrike",
                sprint=True,
                arrival_radius=5.0,
                posture=ModeBotPosture.SURVIVE,
                objective_priority=1.0,
                engagement_radius=6.0,
            )
        if observer.player_id % 4 == 0 and own_base is not None:
            return ModeBotDecision(
                _guard_beat(frame, observer, own_base.position, 6.0),
                "demolition_defend_base",
                sprint=False,
                arrival_radius=3.0,
                directive="fortify" if int(own_base.state) > 0 else "",
                posture=ModeBotPosture.DEFEND,
                objective_priority=0.86,
                engagement_radius=40.0,
            )
        if enemy_base is not None:
            return ModeBotDecision(
                enemy_base.position,
                "demolition_assault_base",
                sprint=True,
                arrival_radius=2.0,
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.9,
                engagement_radius=90.0,
            )
        return None


class TerritoryControlBotPolicy:
    """Push the nearest hostile territory while leaving a defence cadence."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        territories = [
            item for item in frame.objectives if item.kind == "tc_territory"
        ]
        if not territories:
            return _FALLBACK.decide(frame, observer)
        hostile = [item for item in territories if item.team != observer.team]
        friendly = [item for item in territories if item.team == observer.team]
        if observer.player_id % 4 == 0 and friendly:
            base = min(
                friendly,
                key=lambda item: _distance_squared(observer.position, item.position),
            )
            return ModeBotDecision(
                _guard_beat(frame, observer, base.position, 4.0),
                "territory_defend",
                sprint=False,
                arrival_radius=2.5,
                directive="fortify" if not int(base.state) else "",
                posture=ModeBotPosture.DEFEND,
                objective_priority=0.86,
                engagement_radius=34.0,
            )
        target_pool = hostile or friendly
        target = min(
            target_pool,
            key=lambda item: _distance_squared(observer.position, item.position),
        )
        return ModeBotDecision(
            target.position,
            "territory_contest" if int(target.state) else "territory_capture",
            sprint=True,
            arrival_radius=2.0,
            posture=ModeBotPosture.ASSAULT,
            objective_priority=0.92,
            engagement_radius=72.0,
        )


class DiamondMineBotPolicy:
    """Mine-route carriers, escorts, loose diamonds, and drop-off guards."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        dropoffs = [
            item for item in frame.objectives
            if item.kind == "dia_dropoff"
            and item.team in (int(C.TEAM_NEUTRAL), observer.team)
            and int(item.state) > 0
        ]
        diamonds = [
            item for item in frame.objectives if item.kind == "dia_diamond"
        ]
        if observer.carried_entity_id == int(C.DIAMOND_PICKUP) and dropoffs:
            target = min(
                dropoffs,
                key=lambda item: _distance_squared(observer.position, item.position),
            )
            return ModeBotDecision(
                target.position,
                "diamond_cash_in",
                sprint=True,
                arrival_radius=2.0,
                posture=ModeBotPosture.EVASIVE,
                objective_priority=1.0,
                engagement_radius=7.0,
            )
        friendly_carriers = [
            item for item in diamonds
            if item.carrier_id >= 0
            and item.team == observer.team
            and item.carrier_id != observer.player_id
        ]
        if friendly_carriers and observer.player_id % 3 == 0:
            carrier = min(
                friendly_carriers,
                key=lambda item: _distance_squared(observer.position, item.position),
            )
            return ModeBotDecision(
                _formation_point(carrier.position, observer.player_id, 4.0),
                "diamond_escort",
                sprint=True,
                arrival_radius=2.5,
                posture=ModeBotPosture.ESCORT,
                objective_priority=0.92,
                engagement_radius=32.0,
            )
        loose = [item for item in diamonds if item.carrier_id < 0]
        if loose:
            target = min(
                loose,
                key=lambda item: _distance_squared(observer.position, item.position),
            )
            return ModeBotDecision(
                target.position,
                "diamond_collect",
                sprint=True,
                arrival_radius=1.75,
                posture=ModeBotPosture.BALANCED,
                objective_priority=0.9,
                engagement_radius=24.0,
            )
        if dropoffs and observer.player_id % 4 == 0:
            target = min(
                dropoffs,
                key=lambda item: _distance_squared(observer.position, item.position),
            )
            return ModeBotDecision(
                _guard_beat(frame, observer, target.position, 5.0),
                "diamond_guard_dropoff",
                sprint=False,
                arrival_radius=3.0,
                directive="fortify",
                posture=ModeBotPosture.DEFEND,
                objective_priority=0.82,
                engagement_radius=34.0,
            )
        return ModeBotDecision(
            observer.position,
            "diamond_mine_blocks",
            sprint=False,
            arrival_radius=0.5,
            directive="mine",
            posture=ModeBotPosture.MINE,
            objective_priority=0.88,
            engagement_radius=14.0,
        )


class OccupationBotPolicy:
    """Attackers deliver bombs; defenders intercept and clear the blast zone."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        target = next(
            (item for item in frame.objectives if item.kind == "oc_target"),
            None,
        )
        bombs = [item for item in frame.objectives if item.kind == "oc_bomb"]
        if observer.carried_entity_id == int(C.BOMB_PICKUP):
            if observer.team == int(C.TEAM1) and target is not None:
                return ModeBotDecision(
                    target.position,
                    "occupation_deliver_bomb",
                    sprint=True,
                    arrival_radius=2.0,
                    posture=ModeBotPosture.EVASIVE,
                    objective_priority=1.0,
                    engagement_radius=7.0,
                )
            if observer.team == int(C.TEAM2) and target is not None:
                return ModeBotDecision(
                    _away_from(observer.position, target.position, 30.0),
                    "occupation_dispose_bomb",
                    sprint=True,
                    arrival_radius=5.0,
                    posture=ModeBotPosture.SURVIVE,
                    objective_priority=1.0,
                    engagement_radius=6.0,
                )
        loose = [item for item in bombs if item.carrier_id < 0]
        if loose:
            if observer.team == int(C.TEAM1):
                candidates = loose
            else:
                candidates = [item for item in loose if int(item.state)] or loose
            bomb = min(
                candidates,
                key=lambda item: _distance_squared(observer.position, item.position),
            )
            return ModeBotDecision(
                bomb.position,
                "occupation_intercept_live_bomb"
                if observer.team == int(C.TEAM2) and int(bomb.state)
                else "occupation_retrieve_bomb",
                sprint=True,
                arrival_radius=1.75,
                posture=(
                    ModeBotPosture.SURVIVE
                    if observer.team == int(C.TEAM2) and int(bomb.state)
                    else ModeBotPosture.ASSAULT
                ),
                objective_priority=0.96,
                engagement_radius=(
                    10.0
                    if observer.team == int(C.TEAM2) and int(bomb.state)
                    else 56.0
                ),
            )
        if observer.team == int(C.TEAM2) and target is not None:
            return ModeBotDecision(
                _formation_point(target.position, observer.player_id, 6.0),
                "occupation_defend_base",
                sprint=False,
                arrival_radius=3.0,
                directive="fortify" if observer.player_id % 3 == 0 else "",
                posture=ModeBotPosture.DEFEND,
                objective_priority=0.9,
                engagement_radius=40.0,
            )
        fallback = _FALLBACK.decide(frame, observer)
        if fallback is None:
            return None
        return ModeBotDecision(
            fallback.position,
            "occupation_pressure_enemy_side",
            sprint=True,
            arrival_radius=fallback.arrival_radius,
            posture=ModeBotPosture.ASSAULT,
            objective_priority=0.62,
            engagement_radius=120.0,
        )


_FALLBACK = PatrolCombatPolicy()
_TDM_POLICY = TeamDeathmatchBotPolicy()
_PASSIVE_POLICY = PassiveIsolatedModePolicy()
_POLICIES: dict[str, ModeBotPolicy] = {
    "nor": _TDM_POLICY,
    "tdm": _TDM_POLICY,
    "arena": ArenaBotPolicy(),
    "ctf": CTFBotPolicy(),
    "cctf": CTFBotPolicy(),
    "zom": ZombieBotPolicy(),
    "vip": VIPBotPolicy(),
    "mh": MultiHillBotPolicy(),
    "dem": DemolitionBotPolicy(),
    "tc": TerritoryControlBotPolicy(),
    "dia": DiamondMineBotPolicy(),
    "oc": OccupationBotPolicy(),
    "tut": _PASSIVE_POLICY,
    "ugc": _PASSIVE_POLICY,
}
if _POLICIES.keys() != _MODE_STRATEGIES.keys():
    raise RuntimeError(
        "bot policy and strategy registries must cover exactly the same modes"
    )


def objective_decision_for(
    frame: PerceptionFrame,
    observer: PlayerSnapshot,
) -> ModeBotDecision | None:
    """Return the complete role decision for worker navigation/debugging."""

    decision = _POLICIES.get(
        _canonical_mode(frame.mode_id), _FALLBACK
    ).decide(frame, observer)
    return _watch_approach(frame, observer, decision)


def mode_objective_committed(decision: ModeBotDecision | None) -> bool:
    """Mode jobs keep locomotion ownership despite optional team activity.

    A numeric priority ranks urgency within a mode; it is not permission to
    abandon that mode's capture, escort, defence, or elimination job.
    """
    return decision is not None and (
        decision.directive == "mine" or decision.objective_priority >= .9
        or decision.role.startswith(("ctf_", "classic_ctf_", "vip_", "multihill_",
                                     "territory_", "demolition_", "diamond_",
                                     "occupation_", "zombie_", "arena_")))


def _watch_approach(frame: PerceptionFrame, observer: PlayerSnapshot,
                    decision: ModeBotDecision | None) -> ModeBotDecision | None:
    if decision is None or decision.watch_position is not None:
        return decision
    if decision.posture not in {ModeBotPosture.DEFEND, ModeBotPosture.ESCORT,
                                ModeBotPosture.SURVIVE, ModeBotPosture.EVASIVE,
                                ModeBotPosture.BUILD}:
        return decision
    approach = next((item for item in frame.objectives
                     if item.kind == "team_anchor" and item.team != observer.team), None)
    if approach is None:
        approach = next((item for item in frame.objectives
                         if item.kind == "vip" and item.team != observer.team), None)
    return replace(decision, watch_position=approach.position) if approach is not None else decision


@dataclass(slots=True)
class _ModeCommitment:
    signature: tuple[object, ...]
    decision: ModeBotDecision
    role_since: float
    anchor_since: float
    last_seen: float


class ModePolicyMemory:
    """Bounded worker-owned role/anchor hysteresis, reset at authoritative edges."""

    def __init__(self) -> None:
        self.epoch = (-1, -1)
        self._states: dict[tuple[int, int], _ModeCommitment] = {}

    def reset(self) -> None:
        self._states.clear()
        self.epoch = (-1, -1)

    def forget(self, player_id: int, generation: int) -> None:
        self._states.pop((int(player_id), int(generation)), None)

    def decide(self, frame: PerceptionFrame,
               observer: PlayerSnapshot) -> ModeBotDecision | None:
        epoch = (frame.map_epoch, frame.mode_epoch)
        if self.epoch != epoch:
            self.reset()
            self.epoch = epoch
        key = (observer.player_id, observer.generation)
        decision = objective_decision_for(frame, observer)
        if decision is None or not mode_objective_committed(decision):
            self._states.pop(key, None)
            return decision
        now = float(frame.created_at)
        signature = (
            _canonical_mode(frame.mode_id), frame.mode_phase,
            observer.life_id, observer.team, observer.class_id, observer.carried_entity_id,
            observer.last_damage_source_id if decision.role == "vip_retreat" else -1,
            tuple((item.kind, item.team, item.carrier_id, item.state,
                   item.position if item.carrier_id < 0 and item.kind != "vip" else None)
                  for item in frame.objectives),
        )
        previous = self._states.get(key)
        if previous is not None and (previous.signature != signature
                                     or now < previous.last_seen):
            previous = None
        if previous is not None:
            if (_canonical_mode(frame.mode_id) == "vip"
                    and previous.decision.role in {"vip_guard_formation", "vip_flank_attack", "vip_mop_up"}
                    and decision.role in {"vip_guard_formation", "vip_flank_attack", "vip_mop_up"}
                    and now - previous.role_since < 8):
                decision = _watch_approach(frame, observer, VIPBotPolicy().decide(
                    frame, observer, retained_role=previous.decision.role))
                assert decision is not None
            if decision.role == previous.decision.role:
                separation = math.dist(decision.position, previous.decision.position)
                moving_role = ("escort" in decision.role or decision.role in {
                    "vip_guard_formation", "vip_flank_attack", "vip_sudden_death_assault",
                    "vip_rally", "vip_retreat", "ctf_intercept_carrier"})
                # Small motion must not rebuild an escort route every frame.
                # Meaningful carrier movement still moves its escort promptly.
                hold = separation <= 3 and abs(decision.position[2] - previous.decision.position[2]) <= 1
                if not moving_role and now - previous.anchor_since < 8:
                    hold |= (math.dist(observer.position, decision.position) + 6 >=
                             math.dist(observer.position, previous.decision.position))
                if decision.role == "vip_retreat" and now - previous.anchor_since < 4:
                    hold = True
                if hold:
                    decision = replace(decision, position=previous.decision.position)
        role_since = (previous.role_since if previous is not None and
                      previous.decision.role == decision.role else now)
        anchor_since = (previous.anchor_since if previous is not None and
                        previous.decision.position == decision.position else now)
        if key not in self._states and len(self._states) >= 64:
            oldest = min(self._states, key=lambda item: self._states[item].last_seen)
            self._states.pop(oldest)
        self._states[key] = _ModeCommitment(signature, decision, role_since, anchor_since, now)
        return decision


def mode_decision_allows_combat(
    decision: ModeBotDecision | None,
    observer: PlayerSnapshot,
    target: PlayerSnapshot,
    *,
    now: float,
) -> bool:
    """Return whether a visible enemy may interrupt the current objective.

    Assault roles accept the full configured sight range. Carriers and
    survival roles only defend themselves, while guards also respond to an
    enemy entering the protected objective's engagement radius.
    """

    if decision is None:
        return True
    distance_to_bot = math.dist(observer.position, target.position)
    recently_hit = (
        int(observer.last_damage_source_id) == int(target.player_id)
        and float(observer.last_damage_at) > 0.0
        and float(now) - float(observer.last_damage_at) <= 2.0
    )
    if recently_hit or distance_to_bot <= 4.5:
        return True
    radius = max(0.0, float(decision.engagement_radius))
    if decision.posture in {
        ModeBotPosture.DEFEND,
        ModeBotPosture.ESCORT,
        ModeBotPosture.BUILD,
        ModeBotPosture.MINE,
    }:
        return (
            distance_to_bot <= min(radius, 14.0)
            or math.dist(decision.position, target.position) <= radius
        )
    return distance_to_bot <= radius


def objective_goal_for(
    frame: PerceptionFrame,
    observer: PlayerSnapshot,
) -> Vector3 | None:
    """Compatibility view returning only the selected goal position."""

    decision = objective_decision_for(frame, observer)
    return decision.position if decision is not None else None


def _objective(frame: PerceptionFrame, kind: str, team: int):
    return next(
        (item for item in frame.objectives if item.kind == kind and item.team == team),
        None,
    )


def _friendly_player(
    frame: PerceptionFrame,
    observer: PlayerSnapshot,
    player_id: int,
) -> PlayerSnapshot | None:
    return next(
        (
            player for player in frame.players
            if player.player_id == int(player_id)
            and player.team == observer.team
            and player.alive
        ),
        None,
    )


def _guard_beat(frame: PerceptionFrame, observer: PlayerSnapshot, center: Vector3,
                radius: float) -> Vector3:
    """Move a sentry between a few posts instead of freezing it on one cell.

    The post changes every 18 seconds on an identity-staggered clock and stays
    inside the fortification radius of the guarded objective.
    """
    beat = int((float(frame.created_at) + observer.player_id * 5.3) // 18.0)
    return _formation_point(center, observer.player_id + beat * 5,
                            radius + 2.5 * ((beat + observer.player_id) % 3))


def _survivor_waypoints(frame: PerceptionFrame,
                        own_anchor: ObjectiveSnapshot) -> list[Vector3]:
    """Deterministic roam targets built only from public map knowledge."""
    center = own_anchor.position
    points: set[Vector3] = {
        tuple(round(float(value), 1) for value in entity.position)
        for entity in frame.entities
        if entity.alive and not entity.hazardous and entity.owner_id < 0
        and entity.kind != "projectile"
        and math.dist(entity.position, center) <= 160.0
    }
    for item in frame.objectives:
        if item.kind == "team_anchor":
            points.add(item.position)
            if item.team != own_anchor.team:
                points.add(_toward(center, item.position,
                                   math.dist(center, item.position) * 0.5))
    for ring, radius in enumerate((22.0, 38.0, 56.0)):
        for spoke in range(6):
            points.add(_formation_point(center, spoke * 7 + ring * 3, radius))
    return sorted(points)


def _formation_point(position: Vector3, key: int, radius: float) -> Vector3:
    angle = (int(key) * 2.399963229728653) % math.tau
    return (
        min(510.0, max(1.0, position[0] + math.cos(angle) * radius)),
        min(510.0, max(1.0, position[1] + math.sin(angle) * radius)),
        position[2],
    )


def _away_from(position: Vector3, threat: Vector3, distance: float) -> Vector3:
    dx = position[0] - threat[0]
    dy = position[1] - threat[1]
    length = math.hypot(dx, dy)
    if length <= 1e-6:
        dx, dy, length = 1.0, 0.0, 1.0
    return (
        min(510.0, max(1.0, position[0] + dx / length * distance)),
        min(510.0, max(1.0, position[1] + dy / length * distance)),
        position[2],
    )


def _toward(position: Vector3, target: Vector3, distance: float) -> Vector3:
    dx = float(target[0]) - float(position[0])
    dy = float(target[1]) - float(position[1])
    length = math.hypot(dx, dy)
    if length <= 1e-6:
        return position
    step = min(max(0.0, float(distance)), length)
    return (
        min(510.0, max(1.0, position[0] + dx / length * step)),
        min(510.0, max(1.0, position[1] + dy / length * step)),
        position[2],
    )


def _distance_squared(a: Vector3, b: Vector3) -> float:
    return sum((a[index] - b[index]) ** 2 for index in range(3))


__all__ = [
    "ModeBotDecision",
    "ModeBotPosture",
    "ModeBotStrategy",
    "ModePolicyMemory",
    "mode_decision_allows_combat",
    "mode_objective_committed",
    "mode_strategy_for",
    "objective_decision_for",
    "objective_goal_for",
]
