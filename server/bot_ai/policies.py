"""Pure, deterministic mode policies executed only in the AI worker.

Policies consume immutable perception messages.  They may use map objectives,
friendly roster state, and mode-sanctioned markers (CTF carriers, VIP crowns,
the Zombie last-survivor marker), but never query authoritative server objects
or infer a hidden enemy from the complete roster snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
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

        if observer.health <= 45 and teammates:
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

        if observer.player_id % 3 == 0 and own_base is not None:
            defence = _formation_point(own_base.position, observer.player_id, 6.0)
            return ModeBotDecision(
                defence,
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


class ZombieBotPolicy:
    """Separate preparation, survivor, infected, and last-man behavior."""

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

        if own_anchor is not None:
            fallback = _formation_point(
                own_anchor.position,
                observer.player_id,
                12.0,
            )
            role = (
                "zombie_survivor_regroup"
                if survivor is None or survivor.team == observer.team
                else "zombie_infected_breach"
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


class VIPBotPolicy:
    """Protect VIPs in formation and flank the opposing marked VIP."""

    def decide(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
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
            retreat = own_anchor.position if own_anchor is not None else own_vip.position
            return ModeBotDecision(
                retreat,
                "vip_retreat",
                sprint=observer.health < 70,
                arrival_radius=6.0,
                posture=ModeBotPosture.EVASIVE,
                objective_priority=1.0,
                engagement_radius=8.0,
            )

        if own_vip is not None and (observer.player_id % 3 != 0 or enemy_vip is None):
            guard = _formation_point(own_vip.position, observer.player_id, 5.0)
            return ModeBotDecision(
                guard,
                "vip_guard_formation",
                sprint=True,
                arrival_radius=2.5,
                posture=ModeBotPosture.ESCORT,
                objective_priority=0.94,
                engagement_radius=36.0,
            )

        if enemy_vip is not None:
            flank = _formation_point(enemy_vip.position, observer.player_id + 17, 6.0)
            role = "vip_sudden_death_assault" if own_vip is None else "vip_flank_attack"
            return ModeBotDecision(
                flank,
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
        if observer.health >= 55:
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
                _formation_point(own_base.position, observer.player_id, 6.0),
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
                _formation_point(base.position, observer.player_id, 4.0),
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
                _formation_point(target.position, observer.player_id, 5.0),
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

    return _POLICIES.get(
        _canonical_mode(frame.mode_id), _FALLBACK
    ).decide(frame, observer)


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
    "mode_decision_allows_combat",
    "mode_strategy_for",
    "objective_decision_for",
    "objective_goal_for",
]
