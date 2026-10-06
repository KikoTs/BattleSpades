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

from .messages import PerceptionFrame, PlayerSnapshot, Vector3


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
    "classicctf": "cctf",
    "capture_the_flag": "ctf",
    "team_deathmatch": "tdm",
    "teamdeathmatch": "tdm",
    "zombie": "zom",
    "zombies": "zom",
    "multihill": "mh",
    "multi_hill": "mh",
    "demolition": "dem",
    "territory_control": "tc",
    "territorycontrol": "tc",
    "diamond": "dia",
    "diamond_mine": "dia",
    "diamondmine": "dia",
    "occupation": "oc",
    "tutorial": "tut",
}


def _canonical_mode(mode_id: str) -> str:
    # Spaces and hyphens are spelling noise ("Classic CTF", "multi-hill");
    # class-name fallbacks arrive run together ("DiamondMine").
    normalized = "_".join(str(mode_id).strip().lower().replace("-", " ").split())
    return _MODE_ALIASES.get(normalized, normalized)


def canonical_mode_id(mode_id: str) -> str:
    """Return the canonical policy code for any configured/class mode name."""

    return _canonical_mode(mode_id)


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


# The minimap marks an intel carrier only after it has carried this long
# (the mode's exposure timer); until then nobody on the robbed team knows
# where the thief is.
_CTF_MARKER_DELAY = float(getattr(C, "INTEL_MINIMAP_EXPOSURE_TIME", 30))
# A carrier is burdened and cannot sprint: about this many blocks a second.
_CTF_CARRIER_PACE = 6.0


class CTFBotPolicy:
    """Split a team between carrying, escorting, intercepting, guarding and raiding.

    Every teammate reads the same roster and runs the same allocation, so the
    team agrees on who does what without talking: one job is filled nearest
    first, then the next from whoever is left.

    ===================  ==================================================
    both intels at home  home guard (1; 2 from seven players), the rest raid
    we carry theirs      home guard, two close escorts, the rest cover the walk
    they carry ours      about half intercept (three at most), the rest raid
    both carried         two escorts, everyone else intercepts
    ours lies dropped    the two nearest hold it; nobody guards an empty base
    ===================  ==================================================

    Teams under three players keep no guard. Classic has no minimap and so
    no carrier marker: nobody hunts a thief there, the raid on the enemy
    base (where the thief has to score) goes on instead.
    """

    def __init__(self) -> None:
        # Per intel: where it last lay (None when first met in a thief's
        # hands), the carrier and when the carry began. The thief's own
        # position is never stored: it is hidden until the marker appears.
        self._tracks: dict[tuple[int, int, int], list] = {}

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        classic = _canonical_mode(frame.mode_id) == "cctf"
        prefix = "classic_" if classic else ""
        own_base = _objective(frame, "ctf_base", observer.team)
        own_intel = _objective(frame, "ctf_intel", observer.team)
        enemy_base = next(
            (item for item in frame.objectives
             if item.kind == "ctf_base" and item.team != observer.team), None)
        enemy_intel = next(
            (
                item for item in frame.objectives
                if item.kind == "ctf_intel" and item.team != observer.team
            ),
            None,
        )
        team = [player for player in frame.players
                if player.team == observer.team and player.alive and player.spawned]
        thief = own_intel is not None and own_intel.carrier_id >= 0 and not classic
        track = (self._track(frame, own_intel)
                 if own_intel is not None and not classic else None)

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

        carrier = (
            _friendly_player(frame, observer, enemy_intel.carrier_id)
            if enemy_intel is not None and enemy_intel.carrier_id >= 0 else None
        )
        if carrier is not None and carrier.player_id == observer.player_id:
            carrier = None
        me = observer.player_id
        free = [player for player in team
                if carrier is None or player.player_id != carrier.player_id]
        if all(player.player_id != me for player in free):
            free.append(observer)

        own_home = (own_intel is not None and own_intel.carrier_id < 0
                    and int(own_intel.state) == 0)
        if own_base is not None and own_home:
            # An intel at home is the only thing worth guarding there; once
            # it is gone the guard joins the hunt or the raid.
            guards = self._nearest(free, own_base.position,
                                   0 if len(team) < 3 else 1 if len(team) < 7 else 2,
                                   within=90.0)
            if me in guards:
                return ModeBotDecision(
                    self._guard_point(frame, observer, own_base.position, enemy_intel),
                    f"{prefix}ctf_defend",
                    sprint=False,
                    arrival_radius=3.0,
                    posture=ModeBotPosture.DEFEND,
                    objective_priority=0.74,
                    engagement_radius=38.0,
                )
            free = [player for player in free if player.player_id not in guards]

        if (
            own_intel is not None
            and own_intel.carrier_id < 0
            and int(own_intel.state) == 1
            and self._visible_drop(classic, observer, own_intel.position)
        ):
            # A dropped friendly intel lies in the open: the nearest two
            # teammates guard it (touch-return where the server allows it)
            # instead of letting the enemy walk back and re-take it.
            holders = self._nearest(free, own_intel.position, 2)
            if me in holders:
                return ModeBotDecision(
                    own_intel.position,
                    f"{prefix}ctf_recover_intel",
                    sprint=True,
                    arrival_radius=1.5,
                    posture=ModeBotPosture.ASSAULT,
                    objective_priority=0.9,
                    engagement_radius=60.0,
                )
            free = [player for player in free if player.player_id not in holders]

        if carrier is not None:
            close = self._nearest(free, carrier.position,
                                  # With both intels on the move the hunt
                                  # needs bodies too.
                                  2 if thief or len(free) <= 5 else 3)
            if me in close:
                return ModeBotDecision(
                    _formation_point(carrier.position, observer.player_id, 4.5),
                    f"{prefix}ctf_escort",
                    sprint=True,
                    arrival_radius=2.5,
                    posture=ModeBotPosture.ESCORT,
                    objective_priority=0.9,
                    # Whoever can hit the carrier is this escort's business,
                    # and rifles reach well past the formation.
                    engagement_radius=60.0,
                )
            free = [player for player in free if player.player_id not in close]

        if thief:
            point = self._thief_estimate(frame, own_intel, enemy_base, track)
            if point is not None:
                hunters = free if carrier is not None or enemy_intel is None else [
                    # A raider already at the enemy intel stays on it: taking
                    # it answers the theft, and the thief must come that way.
                    player for player in free
                    if math.dist(player.position, enemy_intel.position) > 60.0]
                wanted = (len(hunters) if carrier is not None
                          else min(3, max(1, (len(hunters) + 1) // 2)))
                if me in self._nearest(hunters, point, wanted):
                    return ModeBotDecision(
                        point,
                        "ctf_intercept_carrier",
                        sprint=True,
                        arrival_radius=2.5,
                        posture=ModeBotPosture.ASSAULT,
                        objective_priority=0.94,
                        engagement_radius=160.0,
                    )

        if carrier is not None:
            # Everyone not needed elsewhere covers the walk home: a teammate
            # coming from home clears the road ahead, the others hold off the
            # pursuit from the side the intel was taken on.
            ahead = (own_base is not None
                     and math.dist(observer.position, own_base.position)
                     < math.dist(carrier.position, own_base.position))
            toward = own_base if ahead else enemy_base
            return ModeBotDecision(
                (_toward(carrier.position, toward.position, 16.0 + 4.0 * (me % 3))
                 if toward is not None else carrier.position),
                f"{prefix}ctf_escort_cover",
                sprint=True,
                arrival_radius=4.0,
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.88,
                engagement_radius=90.0,
            )

        # The enemy intel only ever leaves its home in a teammate's hands, so
        # where it lies is where that teammate fell: the whole team knows the
        # spot without a minimap. Limiting Classic to drops within sight left
        # every far bot with no order for the rest of the match (Classic
        # never returns a dropped intel).
        if enemy_intel is not None and enemy_intel.carrier_id < 0:
            if own_base is not None and (
                self._should_rally(frame, observer, own_base.position, enemy_intel.position)
                or int(enemy_intel.state) == 0
                and self._awaits_wave(frame, observer, free, enemy_intel.position)
            ):
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
        if enemy_intel is not None and enemy_intel.carrier_id != observer.player_id:
            # A carrier the roster did not describe: its marker is still the
            # teammate to close up on.
            return ModeBotDecision(
                enemy_intel.position,
                f"{prefix}ctf_escort",
                sprint=True,
                arrival_radius=4.5,
                posture=ModeBotPosture.ESCORT,
                objective_priority=0.88,
                engagement_radius=60.0,
            )
        # No intel published (its snapshot failed): fight on like any team.
        return _FALLBACK.decide(frame, observer)

    # Perception range: our own dropped Classic intel farther than this is
    # off screen with no minimap marker to reveal it.
    _CLASSIC_DROP_SIGHT = 160.0

    @classmethod
    def _visible_drop(cls, classic: bool, observer: PlayerSnapshot,
                      position: Vector3) -> bool:
        return not classic or math.dist(observer.position, position) <= cls._CLASSIC_DROP_SIGHT

    @staticmethod
    def _nearest(pool, point: Vector3, count: int, *,
                 within: float = math.inf) -> tuple[int, ...]:
        """Ids of the ``count`` players of ``pool`` nearest ``point`` (id breaks ties)."""

        if count <= 0 or not pool:
            return ()
        ranked = sorted((math.dist(player.position, point), player.player_id)
                        for player in pool)
        return tuple(player_id for distance, player_id in ranked[:count]
                     if distance <= within)

    def _track(self, frame: PerceptionFrame, intel) -> list:
        """Remember where ``intel`` last lay and since when it is carried."""

        key = (frame.map_epoch, frame.mode_epoch, intel.team)
        now = float(frame.created_at)
        track = self._tracks.get(key)
        if track is None:
            if len(self._tracks) >= 8:
                self._tracks.clear()
            track = self._tracks[key] = [None, -1, now]
        if intel.carrier_id < 0:
            # The minimap shows an intel on the ground wherever it lies.
            track[0] = intel.position
            track[1] = -1
        elif track[1] != intel.carrier_id or now < track[2]:
            track[1] = intel.carrier_id
            track[2] = now
        return track

    @staticmethod
    def _thief_estimate(frame: PerceptionFrame, intel, enemy_base, track) -> Vector3 | None:
        """Where the team may look for the enemy carrying its intel.

        Marked on the minimap: the marker. Before that a player only knows
        where the intel was taken and that the thief is walking it to the
        enemy base, so the hunt follows that line at a carrier's pace and
        ends on the base it has to reach.
        """

        ground, _carrier_id, since = track
        elapsed = max(0.0, float(frame.created_at) - float(since))
        if elapsed >= _CTF_MARKER_DELAY:
            return intel.position
        if enemy_base is None:
            return None
        if ground is None:
            return enemy_base.position
        return _toward(ground, enemy_base.position, _CTF_CARRIER_PACE * elapsed)

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

    # The last stop before a guarded intel: raiders arriving inside this band
    # of distance from it go in together, at the latest every wave period.
    _WAVE_BAND = (60.0, 85.0)
    _WAVE_PERIOD = 20.0
    _WAVE_WINDOW = 5.0

    @classmethod
    def _awaits_wave(cls, frame: PerceptionFrame, observer: PlayerSnapshot,
                     raiders, goal: Vector3) -> bool:
        """Hold just outside the enemy base until the raid goes in as a group.

        A carrier is unarmed and cannot sprint, and raiders arrived strung
        out (the second teammate 77 blocks behind at the median pickup), so
        the grab was a solo act that ended six seconds later. The wait cannot
        last: three raiders go at once, anyone follows a push already inside,
        and the team's shared clock sends whoever is there every period.
        """

        near, far = cls._WAVE_BAND
        own = math.dist(observer.position, goal)
        if not near <= own <= far:
            return False
        if (observer.last_damage_at > 0.0
                and 0.0 <= frame.created_at - observer.last_damage_at <= 4.0):
            return False
        if (float(frame.created_at) + 7.0 * observer.team) % cls._WAVE_PERIOD < cls._WAVE_WINDOW:
            return False
        beside = 0
        for player in raiders:
            if player.player_id == observer.player_id:
                continue
            distance = math.dist(player.position, goal)
            if distance < near:
                return False  # the push already left
            beside += distance <= far
        return beside < 2

    @staticmethod
    def _guard_point(frame: PerceptionFrame, observer: PlayerSnapshot, base: Vector3,
                     enemy_intel) -> Vector3:
        """Walk a slow beat around the base, with a forward picket every third leg."""
        beat = int((float(frame.created_at) + observer.player_id * 4.1) // 16.0)
        if beat % 3 == 2 and enemy_intel is not None and enemy_intel.carrier_id < 0:
            return _toward(base, enemy_intel.position, 26.0 + 4.0 * (observer.player_id % 3))
        return _formation_point(base, observer.player_id + beat * 5,
                                7.0 + 4.0 * ((beat + observer.player_id) % 3))


# ``zombie_order.state`` role codes (server/bot_ai/horde_strategy.ROLE_CODES).
_HORDE_ROLES = {
    0: "hunt",
    1: "flank",
    2: "dig_root",
    3: "climb",
    4: "surround",
    5: "tunnel",
}


class ZombieBotPolicy:
    """Separate preparation, survivor, infected, and last-man behavior.

    Survivors play like humans do in the retail mode: they leave the spawn for
    the team's elected refuge (``zombie_refuge`` objective, a high flat spot
    chosen by the director for the whole team), fortify it, hold it together
    and fall back to a new refuge when it is breached. Infected hunt.
    """

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        phase = str(frame.mode_phase).lower()
        own_anchor = _objective(frame, "team_anchor", observer.team)
        refuge = _objective(frame, "zombie_refuge", observer.team)
        enemy_anchor = next(
            (
                item for item in frame.objectives
                if item.kind == "team_anchor" and item.team != observer.team
            ),
            None,
        )
        survivor = next(
            (item for item in frame.objectives if item.kind == "last_survivor"),
            None,
        )
        infected = int(observer.class_id) in _ZOMBIE_CLASSES
        if phase in ("", "waiting", "countdown"):
            if refuge is not None and not infected:
                return self._refuge_decision(
                    frame, observer, refuge, enemy_anchor, "zombie_prepare_refuge"
                )
            if own_anchor is None:
                return None
            preparation = _formation_point(
                own_anchor.position,
                observer.player_id,
                10.0 + float(observer.player_id % 4) * 3.0,
            )
            return ModeBotDecision(
                preparation,
                "zombie_prepare_fortify",
                sprint=False,
                arrival_radius=5.0,
                directive="fortify",
                posture=ModeBotPosture.BUILD,
                objective_priority=0.92,
                engagement_radius=18.0,
            )
        if infected and phase == "active":
            ordered = self._horde_order_decision(frame, observer)
            if ordered is not None:
                return ordered
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
        if infected:
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
            nearest_zombie = self._nearest_enemy(frame, observer)
            if (
                refuge is not None
                and math.dist(observer.position, refuge.position) < 48.0
                and (
                    nearest_zombie is None
                    or math.dist(refuge.position, nearest_zombie.position)
                    > math.dist(observer.position, nearest_zombie.position) + 4.0
                )
            ):
                # The last human runs for the high ground, not into the open.
                return ModeBotDecision(
                    refuge.position,
                    "zombie_last_survivor_escape",
                    sprint=True,
                    arrival_radius=1.5,
                    posture=ModeBotPosture.EVASIVE,
                    objective_priority=1.0,
                    engagement_radius=7.0,
                )
            escape = _away_from(
                observer.position,
                (
                    nearest_zombie.position
                    if nearest_zombie is not None
                    else enemy_anchor.position
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
        if refuge is not None and not infected:
            return self._refuge_decision(
                frame, observer, refuge, enemy_anchor, "zombie_survivor_refuge"
            )
        # With nobody left in sight the horde sweeps the survivors' side of
        # the map rather than milling around its own spawn.
        sweep = enemy_anchor if infected and enemy_anchor is not None else own_anchor
        if sweep is not None:
            fallback = _formation_point(
                sweep.position,
                observer.player_id,
                12.0,
            )
            # Key on what the observer IS: an infected bot with no survivor
            # in sight must breach, never pick up a survivor's block job.
            role = (
                "zombie_infected_breach"
                if infected
                or (survivor is not None and survivor.team != observer.team)
                else "zombie_survivor_regroup"
            )
            return ModeBotDecision(
                fallback,
                role,
                sprint=role.endswith("breach"),
                arrival_radius=5.0,
                # Regrouping survivors keep fortifying; infected never do.
                directive="fortify" if role == "zombie_survivor_regroup" else "",
                posture=(
                    ModeBotPosture.BUILD
                    if role == "zombie_survivor_regroup"
                    else ModeBotPosture.ASSAULT
                ),
                objective_priority=0.86,
                engagement_radius=(
                    28.0 if role == "zombie_survivor_regroup" else 160.0
                ),
            )
        return None

    @staticmethod
    def _horde_order_decision(
        frame: PerceptionFrame, observer: PlayerSnapshot
    ) -> ModeBotDecision | None:
        """Follow the horde coordinator's order (``zombie_order`` objective).

        The director's ``HordeCoordinator`` spreads infected bots over the
        survivors and, under an elevated survivor, assigns collapse diggers,
        climbers and a surrounding ring instead of a pile. Siege roles only
        fight what is within claw reach (4.5 blocks): a survivor shooting
        from a platform must not pull diggers off the cut.
        """

        order = next(
            (
                item for item in frame.objectives
                if item.kind == "zombie_order"
                and item.carrier_id == observer.player_id
            ),
            None,
        )
        if order is None:
            return None
        role = _HORDE_ROLES.get(int(order.state))
        if role is None:
            return None
        target = next(
            (
                player for player in frame.players
                if player.player_id == order.attacker and player.alive
                and player.spawned
            ),
            None,
        )
        target_position = target.position if target is not None else order.position
        if role == "hunt":
            # The order's own position is the prey, or the next stretch of a
            # known way in to it (stairs round the back, a ramp, a door).
            via = math.dist(order.position, target_position) > 2.5
            return ModeBotDecision(
                order.position if via else target_position,
                "zombie_hunt_survivor",
                sprint=True,
                arrival_radius=1.25,
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.98,
                engagement_radius=160.0,
                watch_position=target_position if via else None,
            )
        if role == "flank":
            return ModeBotDecision(
                order.position,
                "zombie_hunt_flank",
                sprint=True,
                arrival_radius=2.5,
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.97,
                # A flanker that sees its prey up close takes it.
                engagement_radius=14.0,
                watch_position=target_position,
            )
        if role in ("dig_root", "tunnel"):
            return ModeBotDecision(
                order.position,
                "zombie_siege_dig" if role == "dig_root" else "zombie_hunt_tunnel",
                sprint=True,
                arrival_radius=1.5,
                directive="siege",
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.99,
                engagement_radius=4.5,
                watch_position=target_position,
            )
        if role == "climb":
            return ModeBotDecision(
                target_position,
                "zombie_siege_climb",
                sprint=True,
                arrival_radius=1.5,
                directive="climb",
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.98,
                engagement_radius=4.5,
                watch_position=target_position,
            )
        return ModeBotDecision(
            order.position,
            "zombie_siege_surround",
            sprint=math.dist(observer.position, order.position) > 12.0,
            arrival_radius=2.0,
            posture=ModeBotPosture.ASSAULT,
            objective_priority=0.96,
            engagement_radius=4.5,
            watch_position=target_position,
        )

    @staticmethod
    def _nearest_enemy(
        frame: PerceptionFrame, observer: PlayerSnapshot
    ) -> PlayerSnapshot | None:
        enemies = [
            player for player in frame.players
            if player.team != observer.team and player.alive and player.spawned
        ]
        if not enemies:
            return None
        return min(
            enemies,
            key=lambda player: _distance_squared(observer.position, player.position),
        )

    def _refuge_decision(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        refuge,
        enemy_anchor,
        role: str,
    ) -> ModeBotDecision:
        """Spread the squad over the refuge top and watch the approach."""

        # Inside the squad's rampart ring (four blocks out), clear of the
        # wall columns so builders never have to work around a body.
        spot = _formation_point(
            refuge.position,
            observer.player_id,
            1.0 + float(observer.player_id % 3) * 0.75,
        )
        spot = (spot[0], spot[1], float(refuge.position[2]))
        nearest = self._nearest_enemy(frame, observer)
        watch = (
            nearest.position
            if nearest is not None
            else enemy_anchor.position
            if enemy_anchor is not None
            else None
        )
        return ModeBotDecision(
            spot,
            role,
            sprint=True,
            arrival_radius=2.0,
            directive="fortify",
            posture=ModeBotPosture.BUILD,
            objective_priority=0.86,
            engagement_radius=28.0,
            watch_position=watch,
        )


# Strikes on the enemy VIP "arrive" only on top of it. With the old 2.5-block
# radius two hunters separated by a single wall (a VIP shelter, a building
# side) both counted as arrived, lost sight of each other and stood still;
# the tight radius keeps the route (and its wall breach) going until combat
# sees the boss.
_VIP_STRIKE_ARRIVAL = 1.0
_VIP_STRIKE_ROLES = frozenset({"vip_flank_attack", "vip_sudden_death_assault",
                               "vip_lone_hunt"})


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
                return _FALLBACK.decide(frame, observer)
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
            alone = not any(p.team == observer.team and p.player_id != observer.player_id
                            for p in frame.players)
            if alone:
                # Nobody else on this team can win the round. Holding behind
                # an empty team deadlocked bots-only games (1v1: both bosses
                # sheltered at home forever), so a lone VIP fights: it hunts
                # the enemy boss through its public crown marker, or mops up
                # once that boss is dead.
                if enemy_vip is not None:
                    return ModeBotDecision(
                        enemy_vip.position,
                        "vip_lone_hunt",
                        sprint=True,
                        arrival_radius=_VIP_STRIKE_ARRIVAL,
                        posture=ModeBotPosture.ASSAULT,
                        objective_priority=0.95,
                        engagement_radius=100.0,
                    )
                if enemy_anchor is not None:
                    return _vip_mop_up(enemy_anchor)
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
            if recently_hurt:
                return ModeBotDecision(
                    retreat,
                    "vip_retreat",
                    sprint=True,
                    arrival_radius=6.0,
                    posture=ModeBotPosture.EVASIVE,
                    objective_priority=1.0,
                    engagement_radius=8.0,
                )
            # The VIP stays behind its team and holds: it defends itself
            # against anyone who closes in, and asks the construction layer
            # for a shelter where it stands (``vip_shelter`` directive).
            return ModeBotDecision(
                retreat,
                "vip_rally",
                sprint=False,
                arrival_radius=6.0,
                directive="vip_shelter",
                posture=ModeBotPosture.DEFEND,
                objective_priority=1.0,
                engagement_radius=20.0,
            )

        # Assign from the actual friendly bot roster, not id%3: a small team
        # can otherwise have no attacker at all. Humans receive no assumed
        # orders. Reserve at least one attacker even with a lone non-VIP bot.
        teammates = sorted({p.player_id for p in frame.players
                            if p.team == observer.team and p.is_bot and p.alive and p.spawned
                            and (own_vip is None or p.player_id != own_vip.carrier_id)})
        # Exactly one bodyguard (when the team has a second bot to attack):
        # every other bot goes for the enemy VIP. The lowest id is a stable
        # designation, so the guard does not change as positions move.
        guard_count = min(max(0, len(teammates) - 1), 1)
        guarding = observer.player_id in teammates[:guard_count]
        if retained_role in {"vip_flank_attack", "vip_mop_up"} and guarding:
            # Hysteresis only toward attacking: a retained attacker that was
            # just designated keeps attacking for the commitment window, a
            # guard that lost the designation leaves at once. Brief roster
            # churn can leave the VIP unguarded for a moment, never with two
            # bodyguards.
            guarding = False
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
                arrival_radius=_VIP_STRIKE_ARRIVAL,
                posture=ModeBotPosture.ASSAULT,
                # The enemy VIP is the win condition: nothing optional
                # (supplies, construction, formations) outranks the push.
                objective_priority=0.95,
                engagement_radius=100.0,
            )

        if enemy_anchor is not None:
            return _vip_mop_up(enemy_anchor)
        return None


def _vip_mop_up(enemy_anchor) -> ModeBotDecision:
    """TDM-style hunt for a VIP-less enemy team (the worker searches the
    enemy side after arriving instead of standing on the anchor)."""
    return ModeBotDecision(
        enemy_anchor.position,
        "vip_mop_up",
        sprint=False,
        arrival_radius=7.0,
        posture=ModeBotPosture.ASSAULT,
        objective_priority=0.72,
    )


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
        teammates = [
            player for player in frame.players
            if player.team == observer.team
            and player.player_id != observer.player_id
            and player.alive
        ]
        # The last one standing has nobody to fall back on: it keeps hunting
        # instead of waiting out the round where it was hit.
        if observer.health >= 55 or not recently_wounded or not teammates:
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
    """Claim hostile hills, guard owned ones, and clear out before the strike.

    Every hill that rotates out is airstruck (five shells, 5 blocks apart,
    6-block blasts). The rotation timer is on the native HUD, so bots may
    leave in time and skip a hill that will expire before they arrive.
    """

    # Shell pattern reach plus blast radius, with a margin.
    _STRIKE_CLEARANCE = 5.0 + float(getattr(C, "AIRSTRIKE_EXPLOSION_RADIUS", 6)) + 5.0
    _LEAVE_SECONDS = 7.0
    _SKIP_SECONDS = 14.0

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> ModeBotDecision | None:
        hills = [item for item in frame.objectives if item.kind == "mh_hill"]
        if not hills:
            return _FALLBACK.decide(frame, observer)

        def xy(item) -> float:
            return math.hypot(observer.position[0] - item.position[0],
                              observer.position[1] - item.position[1])

        doomed = [item for item in hills
                  if 0.0 <= float(item.expires_in) <= self._LEAVE_SECONDS
                  and xy(item) <= self._STRIKE_CLEARANCE]
        if doomed:
            hill = min(doomed, key=xy)
            return ModeBotDecision(
                _away_from(observer.position, hill.position,
                           self._STRIKE_CLEARANCE + 8.0 - xy(hill)),
                "multihill_evade_airstrike",
                sprint=True,
                arrival_radius=3.0,
                posture=ModeBotPosture.SURVIVE,
                objective_priority=1.0,
                engagement_radius=8.0,
            )
        live = [item for item in hills
                if not 0.0 <= float(item.expires_in) <= self._SKIP_SECONDS]
        if not live:
            # Every hill is about to rotate out: hold outside the strike and
            # be ready for the next activation instead of running into it.
            return ModeBotDecision(
                observer.position,
                "multihill_await_rotation",
                sprint=False,
                arrival_radius=3.0,
                posture=ModeBotPosture.BALANCED,
                objective_priority=0.5,
                engagement_radius=90.0,
            )
        owned = [item for item in live
                 if item.team == observer.team and not int(item.state)]
        targets = [item for item in live
                   if item.team != observer.team or int(item.state)]
        guard_hill = min(owned, key=xy) if owned else None
        if guard_hill is not None and (
            not targets or self._is_guard(frame, observer, guard_hill.position)
        ):
            return ModeBotDecision(
                _formation_point(guard_hill.position, observer.player_id, 4.0),
                "multihill_defend",
                sprint=False,
                arrival_radius=2.5,
                directive="fortify" if observer.player_id % 3 == 0 else "",
                posture=ModeBotPosture.DEFEND,
                objective_priority=0.9,
                engagement_radius=34.0,
            )
        hill = min(targets, key=xy)
        return ModeBotDecision(
            hill.position,
            "multihill_contest" if int(hill.state) else "multihill_claim",
            sprint=True,
            arrival_radius=2.0,
            posture=ModeBotPosture.ASSAULT,
            objective_priority=0.92,
            engagement_radius=72.0,
        )

    @staticmethod
    def _is_guard(frame: PerceptionFrame, observer: PlayerSnapshot,
                  hill: Vector3) -> bool:
        """The one or two teammates nearest an owned hill hold it."""

        team = [player for player in frame.players
                if player.team == observer.team and player.alive and player.spawned]
        wanted = 1 if len(team) < 5 else 2
        own = (math.dist(observer.position, hill), observer.player_id)
        closer = sum(1 for player in team if player.player_id != observer.player_id
                     and (math.dist(player.position, hill), player.player_id) < own)
        return closer < wanted


class DemolitionBotPolicy:
    """Build/defend and repair the friendly base; tear down the enemy's.

    The win condition is the enemy base's objective blocks, so attackers get
    the ``demolish`` directive (dig/melee blocks inside the enemy base) and
    defenders at a damaged base the ``repair`` directive (re-place destroyed
    objective blocks). Both read the public base-health cell hints of the
    ``dem_base`` objective.
    """

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
                return _FALLBACK.decide(frame, observer)
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
        if phase == "airstrike":
            # The strike falls on the base that was destroyed, whichever
            # team owns it; the winners may be standing right on it.
            bases = [item for item in frame.objectives if item.kind == "dem_base"]
            doomed = next((item for item in bases if int(item.state) >= 100), None)
            if doomed is None and bases:
                doomed = max(bases, key=lambda item: int(item.state))
            if doomed is not None:
                return ModeBotDecision(
                    _away_from(observer.position, doomed.position, 28.0),
                    "demolition_escape_airstrike",
                    sprint=True,
                    arrival_radius=5.0,
                    posture=ModeBotPosture.SURVIVE,
                    objective_priority=1.0,
                    engagement_radius=6.0,
                )
        can_build = int(C.BLOCK_TOOL) in {int(tool) for tool in observer.loadout} and (
            int(observer.blocks) > 0)
        if (
            own_base is not None
            and own_base.repair_cells
            and can_build
            and self._is_repairer(frame, observer, own_base)
        ):
            cell = _nearest_cell(observer.position, own_base.repair_cells)
            return ModeBotDecision(
                # Stand on the block under the hole, within placing reach.
                _standing_on(cell[0], cell[1], cell[2] + 1),
                "demolition_repair_base",
                sprint=True,
                arrival_radius=3.0,
                directive="repair",
                posture=ModeBotPosture.BUILD,
                objective_priority=0.9,
                engagement_radius=40.0,
            )
        if _passive_slot(frame, observer, 4) and own_base is not None:
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
            target = (
                _standing_on(*_nearest_cell(observer.position, enemy_base.cells))
                if enemy_base.cells else enemy_base.position
            )
            # A sapper at the base keeps digging through distant fire; only
            # a close enemy (or one that just hit it) pulls it into a duel.
            # From midfield it fights like any assault.
            at_base = math.dist(observer.position, target) <= 10.0
            return ModeBotDecision(
                target,
                "demolition_assault_base",
                sprint=True,
                arrival_radius=2.0,
                directive="demolish",
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.9,
                engagement_radius=12.0 if at_base else 90.0,
            )
        # No enemy base published (its snapshot failed): fight on like any team.
        return _FALLBACK.decide(frame, observer)

    @staticmethod
    def _is_repairer(frame: PerceptionFrame, observer: PlayerSnapshot, base) -> bool:
        """The base guard always repairs; heavier damage recalls more hands.

        Up to a quarter of the team per 25% of damage (at least one bot)
        breaks off, nearest first, so the assault never stops entirely.
        """

        team = [player for player in frame.players
                if player.team == observer.team and player.alive and player.spawned
                and player.is_bot]
        if _team_slot(frame, observer) % 4 == 0:
            return True
        damage = max(0, min(100, int(base.state)))
        wanted = max(1, min(len(team) // 2, (len(team) * damage) // 100))
        own = (math.dist(observer.position, base.position), observer.player_id)
        closer = sum(1 for player in team if player.player_id != observer.player_id
                     and (math.dist(player.position, base.position), player.player_id) < own)
        return closer < wanted


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
        # The capture bar shows an enemy draining a friendly base even when
        # no defender stands in it (the base is then "uncontested").
        besieged = [item for item in friendly
                    if int(item.attacker) >= 0 and int(item.attacker) != observer.team]
        if besieged and (
            _team_slot(frame, observer) % 4 == 0
            # A second responder when it is already within reach.
            or _team_slot(frame, observer) % 4 == 1
            and min(math.dist(observer.position, item.position) for item in besieged) <= 90.0
        ):
            base = min(
                besieged,
                key=lambda item: _distance_squared(observer.position, item.position),
            )
            return ModeBotDecision(
                base.position,
                "territory_relieve_siege",
                sprint=True,
                arrival_radius=2.0,
                posture=ModeBotPosture.ASSAULT,
                objective_priority=0.95,
                engagement_radius=72.0,
            )
        if _passive_slot(frame, observer, 4) and friendly:
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
        if friendly_carriers and _passive_slot(frame, observer, 3):
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
        if dropoffs and _passive_slot(frame, observer, 4):
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
    """Attackers bring bombs in as a group; defenders intercept and dispose of them.

    A carrier is unarmed and cannot sprint, so the attack travels with it:
    two close escorts and everyone else as a vanguard between the bomb and
    the base, where the defenders come from. A bomb that falls is lit; its
    fuse (a public rule) decides whether a teammate can still run it in or
    everybody clears the blast.

    A defender carrying a bomb runs it to ONE disposal point, chosen when it
    picked the bomb up (away from the target), where the mode drops it. The
    point used to be recomputed from the moving carrier every decision, which
    chased the goal to the map edge and hoarded the bomb out of play.
    """

    _DISPOSE_EXTRA = 26.0
    _DISPOSE_MIN = 44.0
    # Armed bombs this close to the target will score unless carried off.
    _INTERCEPT_RADIUS = 36.0
    # A loose, unarmed bomb is only worth denying with an attacker this close.
    _DENY_RADIUS = 24.0
    _FUSE = float(getattr(C, "BOMB_EXPLOSION_FUSE", 10.0))
    # Blast radius plus a few steps: nobody waits inside this of a lit bomb.
    _BLAST_CLEARANCE = float(getattr(C, "BOMB_EXPLOSION_RADIUS", 7.0)) + 6.0
    # A dropped bomb cannot be picked up again for this long.
    _PICKUP_LOCK = float(getattr(C, "NO_PICKUP_AFTER_DROP_TIME", 2.5))
    # Blocks a second: a free bot sprinting over ordinary ground, and a
    # burdened carrier (no sprint).
    _RUN_PACE = 9.0
    _CARRY_PACE = 5.5
    # The mode releases a defender's bomb this far outside the target.
    _DISPOSAL_DISTANCE = 20.0

    def __init__(self) -> None:
        self._dispose: dict[tuple[int, int, int], Vector3] = {}
        # Round-scoped memory of what every player can see or hear: when each
        # live bomb was lit and where fresh bombs appear.
        self._epoch: tuple[int, int] = (-1, -1)
        self._lit: dict[tuple, float] = {}
        self._spawn: Vector3 | None = None

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
        self._observe(frame, bombs)
        attacker = observer.team == int(C.TEAM1)
        carry_key = (observer.player_id, observer.generation, observer.life_id)
        if observer.carried_entity_id == int(C.BOMB_PICKUP):
            if attacker and target is not None:
                if self._lets_escort_lead(frame, observer, target):
                    return ModeBotDecision(
                        observer.position,
                        "occupation_follow_escort",
                        sprint=False,
                        arrival_radius=2.0,
                        posture=ModeBotPosture.EVASIVE,
                        objective_priority=1.0,
                        engagement_radius=7.0,
                        watch_position=target.position,
                    )
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
                point = self._dispose.get(carry_key)
                if point is None:
                    if len(self._dispose) >= 128:
                        self._dispose.clear()
                    here = math.hypot(observer.position[0] - target.position[0],
                                      observer.position[1] - target.position[1])
                    point = _away_from(
                        observer.position, target.position,
                        max(self._DISPOSE_EXTRA, self._DISPOSE_MIN - here),
                    )
                    self._dispose[carry_key] = point
                return ModeBotDecision(
                    point,
                    "occupation_dispose_bomb",
                    sprint=True,
                    arrival_radius=4.0,
                    posture=ModeBotPosture.SURVIVE,
                    objective_priority=1.0,
                    engagement_radius=6.0,
                )
        self._dispose.pop(carry_key, None)

        enemies = [player for player in frame.players
                   if player.team != observer.team and player.alive and player.spawned]
        allies = [player for player in frame.players
                  if player.team == observer.team and player.alive and player.spawned]
        carried = [item for item in bombs if item.carrier_id >= 0]
        loose = [item for item in bombs if item.carrier_id < 0]
        lit = [item for item in loose if int(item.state)]

        if attacker:
            friendly_carrier = next(
                (item for item in carried if item.team == observer.team
                 and item.carrier_id != observer.player_id), None)
            if friendly_carrier is not None:
                rank = self._rank(observer, allies, friendly_carrier.position,
                                  exclude=friendly_carrier.carrier_id)
                if rank < 2:
                    return ModeBotDecision(
                        # A few steps ahead of the carrier on either side:
                        # between it and the guns it is walking toward.
                        self._beside_ahead(friendly_carrier.position, target.position,
                                           -1.0 if rank == 0 else 1.0)
                        if target is not None else
                        _formation_point(friendly_carrier.position, observer.player_id, 4.5),
                        "occupation_escort_carrier",
                        sprint=True,
                        arrival_radius=2.5,
                        posture=ModeBotPosture.ESCORT,
                        objective_priority=0.92,
                        # Whoever can hit the carrier is this escort's
                        # business, and rifles reach well past the formation.
                        engagement_radius=60.0,
                    )
                if target is not None:
                    # The defenders come out of the base at the carrier:
                    # everyone else walks ahead of it and meets them first.
                    return ModeBotDecision(
                        _toward(friendly_carrier.position, target.position,
                                22.0 + 4.0 * (observer.player_id % 3)),
                        "occupation_escort_vanguard",
                        sprint=True,
                        arrival_radius=4.0,
                        posture=ModeBotPosture.ASSAULT,
                        objective_priority=0.9,
                        engagement_radius=90.0,
                    )
            for bomb in sorted(lit, key=lambda item: _distance_squared(
                    observer.position, item.position)):
                left = self._fuse_left(frame, bomb)
                planted = target is not None and self._footprint_distance(
                    target, bomb.position) <= 0.0
                if (not planted and target is not None
                        and self._nearest_few(observer, allies, bomb.position, 1)
                        and self._can_carry(observer, bomb, left, self._footprint_distance(
                            target, bomb.position) + 2.0)):
                    # Close enough to finish the run before it goes off.
                    return ModeBotDecision(
                        bomb.position,
                        "occupation_retrieve_bomb",
                        sprint=True,
                        arrival_radius=1.75,
                        posture=ModeBotPosture.ASSAULT,
                        objective_priority=0.96,
                        engagement_radius=56.0,
                    )
                gap = math.dist(observer.position, bomb.position)
                if planted and gap <= 60.0:
                    # Planted: keep the defenders off it from outside the blast.
                    keep = self._BLAST_CLEARANCE + 2.0
                    return ModeBotDecision(
                        _toward(bomb.position, observer.position, keep) if gap >= keep
                        else _away_from(observer.position, bomb.position, keep - gap),
                        "occupation_cover_plant",
                        sprint=True,
                        arrival_radius=3.0,
                        posture=ModeBotPosture.ASSAULT,
                        objective_priority=0.94,
                        engagement_radius=60.0,
                    )
                if gap <= self._BLAST_CLEARANCE:
                    return self._clear_blast(observer, bomb)
            fresh = [item for item in loose if not int(item.state)]
            if fresh:
                bomb = min(fresh, key=lambda item: _distance_squared(
                    observer.position, item.position))
                return ModeBotDecision(
                    bomb.position,
                    "occupation_retrieve_bomb",
                    sprint=True,
                    arrival_radius=1.75,
                    posture=ModeBotPosture.ASSAULT,
                    objective_priority=0.96,
                    engagement_radius=56.0,
                )
            hostile_carrier = next(
                (item for item in carried if item.team != observer.team), None)
            if hostile_carrier is not None:
                # A defender is walking the bomb away: kill the carrier so
                # it drops, then bring it back in.
                return ModeBotDecision(
                    hostile_carrier.position,
                    "occupation_hunt_carrier",
                    sprint=True,
                    arrival_radius=2.5,
                    posture=ModeBotPosture.ASSAULT,
                    objective_priority=0.92,
                    engagement_radius=100.0,
                )
            if self._spawn is not None:
                # No bomb to carry yet: gather where the last one appeared
                # instead of running at the base without one.
                return ModeBotDecision(
                    _formation_point(self._spawn, observer.player_id, 7.0),
                    "occupation_await_bomb",
                    sprint=True,
                    arrival_radius=4.0,
                    posture=ModeBotPosture.BALANCED,
                    objective_priority=0.8,
                    engagement_radius=90.0,
                )
        else:
            live = [item for item in lit if target is not None
                    and math.dist(item.position, target.position) <= self._INTERCEPT_RADIUS]
            for bomb in sorted(live, key=lambda item: _distance_squared(
                    observer.position, item.position)):
                # One defender carries it off, and only with fuse to spare;
                # the rest stay out of the blast.
                if (self._nearest_few(observer, allies, bomb.position, 1)
                        and self._can_carry(
                            observer, bomb, self._fuse_left(frame, bomb),
                            max(0.0, self._DISPOSAL_DISTANCE
                                - self._footprint_distance(target, bomb.position)))):
                    return ModeBotDecision(
                        bomb.position,
                        "occupation_intercept_live_bomb",
                        sprint=True,
                        arrival_radius=1.75,
                        posture=ModeBotPosture.SURVIVE,
                        objective_priority=0.96,
                        engagement_radius=10.0,
                    )
            for bomb in lit:
                if math.dist(observer.position, bomb.position) <= self._BLAST_CLEARANCE:
                    return self._clear_blast(observer, bomb)
            hostile_carrier = next(
                (item for item in carried if item.team != observer.team), None)
            if (hostile_carrier is not None
                    and _team_slot(frame, observer) % 3 != 0
                    and math.dist(observer.position, hostile_carrier.position) <= 140.0):
                return ModeBotDecision(
                    hostile_carrier.position,
                    "occupation_hunt_carrier",
                    sprint=True,
                    arrival_radius=2.5,
                    posture=ModeBotPosture.ASSAULT,
                    objective_priority=0.94,
                    engagement_radius=100.0,
                )
            contested = [
                item for item in loose if not int(item.state)
                and any(math.dist(enemy.position, item.position) <= self._DENY_RADIUS
                        for enemy in enemies)
                and self._nearest_few(observer, allies, item.position, 1)
            ]
            if contested:
                bomb = min(contested, key=lambda item: _distance_squared(
                    observer.position, item.position))
                return ModeBotDecision(
                    bomb.position,
                    "occupation_deny_bomb",
                    sprint=True,
                    arrival_radius=1.75,
                    posture=ModeBotPosture.ASSAULT,
                    objective_priority=0.94,
                    engagement_radius=56.0,
                )
            if target is not None:
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

    def _observe(self, frame: PerceptionFrame, bombs) -> None:
        """Note when each bomb was lit and where a fresh one lies."""

        epoch = (frame.map_epoch, frame.mode_epoch)
        if epoch != self._epoch:
            self._epoch = epoch
            self._lit = {}
            self._spawn = None
        now = float(frame.created_at)
        seen = {}
        for bomb in bombs:
            if int(bomb.state):
                seen[self._bomb_key(bomb)] = bomb
            elif bomb.carrier_id < 0:
                self._spawn = bomb.position
        gone = sorted((since, key) for key, since in self._lit.items() if key not in seen)
        for key in seen:
            if key not in self._lit:
                # A lit bomb changing hands keeps its fuse.
                self._lit[key] = gone.pop(0)[0] if gone else now
            elif self._lit[key] > now:
                self._lit[key] = now
        for key in [key for key in self._lit if key not in seen]:
            del self._lit[key]

    @staticmethod
    def _bomb_key(bomb) -> tuple:
        if bomb.carrier_id >= 0:
            return ("carried", int(bomb.carrier_id))
        return ("ground", round(bomb.position[0]), round(bomb.position[1]))

    def _fuse_left(self, frame: PerceptionFrame, bomb) -> float:
        since = self._lit.get(self._bomb_key(bomb), float(frame.created_at))
        return self._FUSE - (float(frame.created_at) - since)

    def _can_carry(self, observer: PlayerSnapshot, bomb, left: float,
                   distance: float) -> bool:
        """Whether the observer can fetch a lit bomb and walk it ``distance``."""

        reach = math.dist(observer.position, bomb.position) / self._RUN_PACE
        lock = self._PICKUP_LOCK - (self._FUSE - left)
        return max(reach, lock) + distance / self._CARRY_PACE + 1.0 <= left

    @staticmethod
    def _footprint_distance(target, position: Vector3) -> float:
        """Horizontal blocks from ``position`` to the target footprint (0 inside)."""

        if len(target.bounds) >= 4:
            x0, x1, y0, y1 = target.bounds[:4]
            dx = max(x0 - position[0], 0.0, position[0] - x1)
            dy = max(y0 - position[1], 0.0, position[1] - y1)
            return math.hypot(dx, dy)
        return max(0.0, math.hypot(position[0] - target.position[0],
                                   position[1] - target.position[1]) - 8.0)

    def _clear_blast(self, observer: PlayerSnapshot, bomb) -> ModeBotDecision:
        return ModeBotDecision(
            _away_from(observer.position, bomb.position, self._BLAST_CLEARANCE + 4.0),
            "occupation_clear_blast",
            sprint=True,
            arrival_radius=3.0,
            posture=ModeBotPosture.SURVIVE,
            objective_priority=1.0,
            engagement_radius=6.0,
        )

    # The carrier follows: it walks while a teammate within this range is at
    # least a couple of steps nearer the base than it is, and waits for
    # teammates who are on their way from no farther than the support range.
    _LEAD_RANGE = 45.0
    _LEAD_STEPS = 2.5
    _SUPPORT_RANGE = 120.0
    # Inside this distance of the base it just goes for the plant.
    _FINAL_RUN = 28.0

    @classmethod
    def _lets_escort_lead(cls, frame: PerceptionFrame, observer: PlayerSnapshot,
                          target) -> bool:
        """Whether the bomb carrier waits for a teammate to go in front.

        Unarmed and unable to sprint, the carrier used to walk point: nine
        in ten died with no teammate even five blocks ahead of them, about
        75 blocks short of the base and with three defenders in range. It
        now walks behind whoever is nearby and waits for teammates who are
        on their way. The wait ends when someone is in front, when nobody
        is close enough to come, under fire, on the final run, and in any
        case for six seconds of every twenty, so a teammate that cannot get
        ahead never parks the bomb.
        """

        own = cls._footprint_distance(target, observer.position)
        if own <= cls._FINAL_RUN:
            return False
        now = float(frame.created_at)
        if observer.last_damage_at > 0.0 and 0.0 <= now - observer.last_damage_at <= 3.0:
            return False
        if now % 20.0 < 6.0:
            return False
        coming = False
        for player in frame.players:
            if (player.team != observer.team or not player.alive or not player.spawned
                    or player.player_id == observer.player_id):
                continue
            gap = math.dist(player.position, observer.position)
            if gap > cls._SUPPORT_RANGE:
                continue
            if (gap <= cls._LEAD_RANGE and cls._footprint_distance(
                    target, player.position) <= own - cls._LEAD_STEPS):
                return False
            coming = True
        return coming

    @staticmethod
    def _beside_ahead(carrier: Vector3, target: Vector3, side: float) -> Vector3:
        dx, dy = target[0] - carrier[0], target[1] - carrier[1]
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            return carrier
        ux, uy = dx / length, dy / length
        return (
            min(510.0, max(1.0, carrier[0] + ux * 8.0 - uy * 4.0 * side)),
            min(510.0, max(1.0, carrier[1] + uy * 8.0 + ux * 4.0 * side)),
            carrier[2],
        )

    @staticmethod
    def _rank(observer: PlayerSnapshot, allies, position: Vector3, *,
              exclude: int = -1) -> int:
        """How many free allies are nearer ``position`` than the observer."""

        own = (math.dist(observer.position, position), observer.player_id)
        return sum(1 for player in allies
                   if player.player_id not in (observer.player_id, exclude)
                   and player.carried_entity_id < 0
                   and (math.dist(player.position, position), player.player_id) < own)

    @classmethod
    def _nearest_few(cls, observer: PlayerSnapshot, allies, position: Vector3,
                     count: int, *, exclude: int = -1) -> bool:
        return cls._rank(observer, allies, position, exclude=exclude) < count


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


def standing_order_for(frame: PerceptionFrame, observer: PlayerSnapshot) -> ModeBotDecision:
    """The order of a bot whose mode policy had none: it never stands idle.

    A policy with nothing to say (objectives not published yet, a state it
    does not cover) used to leave the worker without a goal, and the bot
    stood where it was until the state changed. Push on the enemy side when
    the map names one, otherwise walk a beat around home. A frame that names
    no place at all gives nowhere to walk to: the bot holds its ground with
    a named order (wandering from the spot walked a bot that had just
    climbed out of London's river back toward it).
    """

    push = _FALLBACK.decide(frame, observer)
    if push is not None:
        return push
    home = _objective(frame, "team_anchor", observer.team)
    if home is None:
        return ModeBotDecision(
            observer.position,
            "hold_no_objective",
            sprint=False,
            arrival_radius=3.0,
            posture=ModeBotPosture.BALANCED,
            objective_priority=0.3,
        )
    return ModeBotDecision(
        _guard_beat(frame, observer, home.position, 8.0),
        "patrol_no_objective",
        sprint=False,
        arrival_radius=3.0,
        posture=ModeBotPosture.BALANCED,
        objective_priority=0.3,
    )


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


# Roles whose goal is a live player (or a point derived from one, such as a
# flight away from the nearest zombie). Freezing their anchor for eight
# seconds sent hunters to where the survivor used to be. Wounded regroups
# (arena_regroup, tdm_regroup_wounded) deliberately stay anchored: the role
# itself only lasts the 8 s after a hit, and re-routing after a teammate's
# every step walked wounded London arena bots into the river (map matrix).
# Horde siege roles follow the coordinator's live site/ring assignment.
_LIVE_TARGET_ROLE_WORDS = ("escort", "hunt", "escape", "evade", "intercept", "siege")
_LIVE_TARGET_ROLES = frozenset({
    "vip_guard_formation", "vip_flank_attack", "vip_sudden_death_assault",
    "vip_rally", "vip_retreat", "tdm_squad_support",
})


def _tracks_live_position(role: str) -> bool:
    return role in _LIVE_TARGET_ROLES or any(word in role for word in _LIVE_TARGET_ROLE_WORDS)


# A raider that stops to wait finishes at least this much of the wait, and
# one that has just set off does not stop again for a few seconds: the
# conditions of a rally (who is alongside, who is ahead) flicker as teammates
# move, and a third of all waits used to last half a second.
_RALLY_MIN_HOLD = 1.5
_RALLY_REARM = 3.0


def _steady_rally(previous: "_ModeCommitment", decision: ModeBotDecision,
                  observer: PlayerSnapshot, now: float) -> ModeBotDecision:
    before = previous.decision
    waited = before.role.endswith("ctf_rally")
    if waited == decision.role.endswith("ctf_rally"):
        return decision
    age = now - previous.role_since
    if waited:
        under_fire = (observer.last_damage_at > 0.0
                      and 0.0 <= now - observer.last_damage_at <= 4.0)
        return before if age < _RALLY_MIN_HOLD and not under_fire else decision
    if before.role.endswith("ctf_attack_intel") and age < _RALLY_REARM:
        return before
    return decision


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
               observer: PlayerSnapshot) -> ModeBotDecision:
        epoch = (frame.map_epoch, frame.mode_epoch)
        if self.epoch != epoch:
            self.reset()
            self.epoch = epoch
        key = (observer.player_id, observer.generation)
        decision = objective_decision_for(frame, observer)
        if decision is None:
            decision = standing_order_for(frame, observer)
        if not mode_objective_committed(decision):
            self._states.pop(key, None)
            return decision
        now = float(frame.created_at)
        signature = (
            _canonical_mode(frame.mode_id), frame.mode_phase,
            observer.life_id, observer.team, observer.class_id, observer.carried_entity_id,
            observer.last_damage_source_id if decision.role == "vip_retreat" else -1,
            tuple((item.kind, item.team, item.carrier_id, item.state,
                   item.position if item.carrier_id < 0 and item.kind != "vip" else None)
                  for item in frame.objectives
                  # Another zombie's horde order is not this bot's business.
                  if item.kind != "zombie_order"
                  or item.carrier_id == observer.player_id),
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
            decision = _steady_rally(previous, decision, observer, now)
            if decision.role == previous.decision.role:
                separation = math.dist(decision.position, previous.decision.position)
                moving_role = _tracks_live_position(decision.role)
                # Small motion must not rebuild an escort route every frame.
                # Meaningful carrier movement still moves its escort promptly.
                hold = separation <= 3 and abs(decision.position[2] - previous.decision.position[2]) <= 1
                if not moving_role and now - previous.anchor_since < 8:
                    hold |= (math.dist(observer.position, decision.position) + 6 >=
                             math.dist(observer.position, previous.decision.position))
                if decision.role == "vip_retreat" and now - previous.anchor_since < 4:
                    hold = True
                if (hold and decision.role in _VIP_STRIKE_ROLES
                        and math.dist(observer.position, previous.decision.position)
                        <= decision.arrival_radius + 0.5):
                    # Reached the held point but the boss is not there: chase
                    # its current position instead of idling on a stale one.
                    hold = False
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


def _team_slot(frame: PerceptionFrame, observer: PlayerSnapshot) -> int:
    """The observer's index among its team's bots, ordered by player id.

    Role splits used ``player_id % n``, but bots join the two teams
    alternately, so even ids all landed on one team: Demolition gave Blue
    three base guards and Green none, and Territory Control the same.
    """

    ids = sorted({player.player_id for player in frame.players
                  if player.team == observer.team and player.is_bot}
                 | {observer.player_id})
    return ids.index(observer.player_id)


def _team_bot_count(frame: PerceptionFrame, observer: PlayerSnapshot) -> int:
    return len({player.player_id for player in frame.players
                if player.team == observer.team and player.is_bot}
               | {observer.player_id})


def _passive_slot(frame: PerceptionFrame, observer: PlayerSnapshot, n: int) -> bool:
    """Whether this bot takes a team's 1-in-``n`` passive role (guard/escort).

    A bot alone on its team always plays the objective: a lone Diamond or
    Demolition bot parked as a guard would never mine or attack.
    """

    return (_team_bot_count(frame, observer) >= 2
            and _team_slot(frame, observer) % n == 0)


def _nearest_cell(position: Vector3, cells) -> tuple[int, int, int]:
    return min(
        cells,
        key=lambda cell: (
            (cell[0] + 0.5 - position[0]) ** 2
            + (cell[1] + 0.5 - position[1]) ** 2
            + (cell[2] + 0.5 - position[2]) ** 2,
            tuple(cell),
        ),
    )


def _standing_on(x: int, y: int, support_z: int) -> Vector3:
    """Player position (eye-level anchor) standing on voxel ``support_z``."""

    return (float(x) + 0.5, float(y) + 0.5, float(support_z) - 2.25)


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
    "standing_order_for",
]
