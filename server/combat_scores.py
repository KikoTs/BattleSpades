"""Accepted-damage assist credit and truthful per-round combat awards.

The retail client publishes a 50% assist threshold and +50 points, but does
not contain the dedicated server's damage-history expiry algorithm. The
bounded ten-second window below is an explicit server policy, not RE parity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time

import shared.constants as C
import shared.constants_gamemode as CG
from server.game_constants import MAX_HEALTH, TEAM1, TEAM2

ASSIST_WINDOW_SECONDS = 10.0
_TRANSITION_DEATHS = {C.FORCED_TEAM_CHANGE_KILL, C.TEAM_CHANGE_KILL, C.CLASS_CHANGE_KILL}


@dataclass
class DamageContribution:
    player: object
    team: int
    life: int
    damage: float
    touched: float


@dataclass
class RoundCombatStats:
    awards: dict[int, int] = field(default_factory=dict)

    def increment(self, stat: int) -> None:
        self.awards[stat] = self.awards.get(stat, 0) + 1


def round_stats(player) -> RoundCombatStats:
    state = getattr(player, "round_combat_stats", None)
    if state is None:
        state = player.round_combat_stats = RoundCombatStats()
    return state


def _scoring_active(server) -> bool:
    mode = getattr(server, "mode", None)
    config = getattr(server, "config", None)
    return not (
        bool(getattr(mode, "ended", False))
        or bool(getattr(config, "ugc_runtime", False))
        or str(getattr(config, "default_mode", "")).lower() in {"ugc", "tut", "tutorial"}
    )


def record_damage(server, victim, source, damage: float, *, now: float | None = None) -> None:
    """Remember actual enemy HP removed, never overkill or rejected damage."""
    if (server is None or not _scoring_active(server) or source is None
            or source is victim or source.team == victim.team
            or int(source.team) not in (TEAM1, TEAM2)
            or int(victim.team) not in (TEAM1, TEAM2) or damage <= 0):
        return
    current = time.monotonic() if now is None else now
    contributions = getattr(victim, "damage_contributions", {})
    contributions = {
        key: value for key, value in contributions.items()
        if current - value.touched <= ASSIST_WINDOW_SECONDS
        and server.players.get(key) is value.player
    }
    life = int(getattr(source, "replication_generation", 0))
    previous = contributions.get(int(source.id))
    amount = float(damage)
    if previous is not None and previous.player is source and previous.life == life and previous.team == source.team:
        amount += previous.damage
    contributions[int(source.id)] = DamageContribution(
        source, int(source.team), life, min(float(MAX_HEALTH), amount), current
    )
    victim.damage_contributions = contributions


def record_healing(victim, restored: float) -> None:
    """Healed HP no longer qualifies as damage towards this life’s assist."""
    contributions = getattr(victim, "damage_contributions", {})
    total = sum(value.damage for value in contributions.values())
    if total <= 0 or restored <= 0:
        return
    retained = max(0.0, total - float(restored)) / total
    for value in contributions.values():
        value.damage *= retained


def record_death(server, victim, killer, kill_type: int, *, now: float | None = None) -> None:
    """Consume one life’s contributions before issuing any score packet."""
    contributions = getattr(victim, "damage_contributions", {})
    victim.damage_contributions = {}
    life = int(getattr(victim, "replication_generation", 0))
    if getattr(victim, "_scored_death_generation", None) == life:
        return
    victim._scored_death_generation = life
    if server is None or not _scoring_active(server) or kill_type in _TRANSITION_DEATHS:
        return
    if killer is victim or killer is None:
        round_stats(victim).increment(C.MOST_SUICIDES)
        return
    if (killer.team == victim.team or int(killer.team) not in (TEAM1, TEAM2)
            or int(victim.team) not in (TEAM1, TEAM2)):
        return
    state = round_stats(killer)
    state.increment(C.MOST_KILLS)
    if kill_type == C.HEADSHOT_KILL:
        state.increment(C.MOST_HEADSHOTS)
    elif kill_type == C.MELEE_KILL:
        state.increment(C.MOST_MELEE_KILLS)
    state.awards[C.BIGGEST_KILL_STREAK] = max(
        state.awards.get(C.BIGGEST_KILL_STREAK, 0), int(getattr(killer, "kill_streak", 0))
    )
    current = time.monotonic() if now is None else now
    threshold = float(MAX_HEALTH) * float(CG.GENERIC_ASSIST_PERCENTAGE) / 100.0
    from server.scoreboard import send_player_score

    for player_id, contribution in contributions.items():
        assistant = contribution.player
        if (assistant is killer or server.players.get(player_id) is not assistant
                or assistant.team != killer.team or contribution.team != killer.team
                or contribution.life != int(getattr(assistant, "replication_generation", 0))
                or current - contribution.touched > ASSIST_WINDOW_SECONDS
                or contribution.damage < threshold):
            continue
        assistant.score += int(CG.GENERIC_SCORE_ASSIST)
        round_stats(assistant).increment(C.MOST_ASSISTS)
        send_player_score(server, assistant, reason=int(C.KILL_SCORE_ASSIST_REASON))
