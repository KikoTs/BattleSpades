"""Pure, deterministic mode policies executed only in the AI worker.

Policies consume immutable perception messages.  They may use map objectives,
friendly roster state, and mode-sanctioned markers (CTF carriers, VIP crowns,
the Zombie last-survivor marker), but never query authoritative server objects
or infer a hidden enemy from the complete roster snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Protocol

import shared.constants as C

from .messages import PerceptionFrame, PlayerSnapshot, Vector3


_ZOMBIE_CLASSES = frozenset({
    int(C.CLASS_ZOMBIE),
    int(C.CLASS_FAST_ZOMBIE),
    int(C.CLASS_JUMP_ZOMBIE),
})


@dataclass(frozen=True, slots=True)
class ModeBotDecision:
    """One phase/role-specific navigation objective for the worker motor."""

    position: Vector3
    role: str
    sprint: bool = True
    arrival_radius: float = 3.0
    # Optional standing order interpreted by BotBrain beyond navigation
    # (currently only "fortify": pick a defensible site and barricade it).
    directive: str = ""


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
                )

        if observer.player_id % 3 == 0 and own_base is not None:
            defence = _formation_point(own_base.position, observer.player_id, 6.0)
            return ModeBotDecision(
                defence,
                f"{prefix}ctf_defend",
                sprint=False,
                arrival_radius=3.0,
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
            )

        if survivor is not None and survivor.team != observer.team:
            # This exact location is legal only because ZombieMode publishes
            # the native final-survivor marker to every infected client.
            return ModeBotDecision(
                survivor.position,
                "zombie_hunt_last_survivor",
                sprint=True,
                arrival_radius=1.5,
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
                enemy_anchor.position if enemy_anchor is not None else (256.0, 256.0, observer.position[2]),
                22.0,
            )
            return ModeBotDecision(
                escape,
                "zombie_last_survivor_escape",
                sprint=True,
                arrival_radius=4.0,
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
            )

        if own_vip is not None and observer.player_id == own_vip.carrier_id:
            retreat = own_anchor.position if own_anchor is not None else own_vip.position
            return ModeBotDecision(
                retreat,
                "vip_retreat",
                sprint=observer.health < 70,
                arrival_radius=6.0,
            )

        if own_vip is not None and (observer.player_id % 3 != 0 or enemy_vip is None):
            guard = _formation_point(own_vip.position, observer.player_id, 5.0)
            return ModeBotDecision(
                guard,
                "vip_guard_formation",
                sprint=True,
                arrival_radius=2.5,
            )

        if enemy_vip is not None:
            flank = _formation_point(enemy_vip.position, observer.player_id + 17, 6.0)
            role = "vip_sudden_death_assault" if own_vip is None else "vip_flank_attack"
            return ModeBotDecision(
                flank,
                role,
                sprint=own_vip is not None,
                arrival_radius=2.5,
            )

        if enemy_anchor is not None:
            return ModeBotDecision(
                enemy_anchor.position,
                "vip_mop_up",
                sprint=False,
                arrival_radius=7.0,
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
            return _FALLBACK.decide(frame, observer)
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
        )


_FALLBACK = PatrolCombatPolicy()
_POLICIES: dict[str, ModeBotPolicy] = {
    "ctf": CTFBotPolicy(),
    "cctf": CTFBotPolicy(),
    "classic_ctf": CTFBotPolicy(),
    "classic-ctf": CTFBotPolicy(),
    "zombie": ZombieBotPolicy(),
    "zom": ZombieBotPolicy(),
    "vip": VIPBotPolicy(),
    "arena": ArenaBotPolicy(),
}


def objective_decision_for(
    frame: PerceptionFrame,
    observer: PlayerSnapshot,
) -> ModeBotDecision | None:
    """Return the complete role decision for worker navigation/debugging."""

    return _POLICIES.get(
        str(frame.mode_id).lower(), _FALLBACK
    ).decide(frame, observer)


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


def _distance_squared(a: Vector3, b: Vector3) -> float:
    return sum((a[index] - b[index]) ** 2 for index in range(3))


__all__ = [
    "ModeBotDecision",
    "objective_decision_for",
    "objective_goal_for",
]
