"""Accepted-damage assist credit and truthful per-round combat awards.

The retail client publishes a 50% assist threshold and +50 points, but does
not contain the dedicated server's damage-history expiry algorithm. The
window is the stock server-only ``PLAYER_INTERACTION_EXPIRY_SECONDS`` (A100,
5.0 s): no stock client pyc/pyd reads it, and the stock explosion manager is
handed a ``player_interacts`` tracker, so it is the retail server's
interaction (damage-history) expiry (rules audit 2026-09-27 #7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time

import shared.constants as C
import shared.constants_gamemode as CG
from server.game_constants import MAX_HEALTH, TEAM1, TEAM2

ASSIST_WINDOW_SECONDS = float(getattr(C, "PLAYER_INTERACTION_EXPIRY_SECONDS", 5.0))
_TRANSITION_DEATHS = {C.FORCED_TEAM_CHANGE_KILL, C.TEAM_CHANGE_KILL, C.CLASS_CHANGE_KILL}
_OBJECTIVE_DEATHS = {C.AIRSTRIKE_KILL, C.BOMB_KILL}


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
    # Fractional accumulators behind the integer award values (distance in
    # blocks, seconds airborne / on fire, shots fired) and the last sampled
    # position.
    measures: dict = field(default_factory=dict)
    last_position: tuple | None = None
    last_airstrike_survived_at: float = -1e9

    def increment(self, stat: int, count: int = 1) -> None:
        self.awards[stat] = self.awards.get(stat, 0) + int(count)

    def raise_to(self, stat: int, value: int) -> None:
        """Keep the round maximum of a "biggest/longest/highest" award."""
        if int(value) > self.awards.get(stat, 0):
            self.awards[stat] = int(value)

    def accumulate(self, stat: int, amount: float) -> None:
        total = self.measures.get(stat, 0.0) + float(amount)
        self.measures[stat] = total
        self.awards[stat] = int(total)


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


def _generic_assist_active(server) -> bool:
    """GENERIC_SCORE_ASSIST belongs to the generic combat economy.

    Zombie (its own ZOM_* scores) and any mode opting out of the generic
    kill score never pay it; the MOST_ASSISTS award still counts.
    """
    mode = getattr(server, "mode", None)
    if mode is not None and not bool(getattr(mode, "generic_scoring_enabled", True)):
        return False
    mode_code = str(getattr(getattr(server, "config", None), "default_mode", "")).lower()
    return mode_code not in {"zom", "zombie"}


def _zombie_team(server) -> int | None:
    """The infected team id while Zombie mode runs, else ``None``."""
    mode = getattr(server, "mode", None)
    code = str(getattr(mode, "mode_code", "") or "").lower() if mode is not None else ""
    if not code:
        code = str(getattr(getattr(server, "config", None), "default_mode", "")).lower()
    if code not in {"zom", "zombie"} and type(mode).__name__ != "ZombieMode":
        return None
    try:
        from modes.zombie import ZOMBIE_TEAM
    except Exception:  # noqa: BLE001 - awards must never break a death
        return None
    return int(ZOMBIE_TEAM)


# --- round-award evidence (GameStats rows, 2026-09-28) ----------------------
# The 30 retail GAME_STAT_TYPES are all rendered by the stock client; only
# the server chose which three to show (NOOF_GAME_STATS_TO_SHOW is
# server-only).  The thresholds below are inferred from the award names.
LOW_HEALTH_KILL_HP = 20          # MOST_KILLS_AT_LOW_HEALTH: killer at <= 20 HP
SNIPER_TOOLS = frozenset((int(C.SNIPER_TOOL), int(C.SNIPER2_TOOL)))
AIRSTRIKE_SURVIVE_DEDUPE_SECONDS = 3.0   # one strike = five shells
FEWEST_SHOTS_MIN_KILLS = 3       # FEWEST_SHOTS_FIRED needs a real round
SHOTS_MEASURE = "shots"


def record_damage_taken(server, victim, source, damage: float, kill_type: int,
                        *, now: float | None = None) -> None:
    """MOST_DAMAGE_TAKEN / MOST_HEADSHOTS_RECEIVED / MOST_AIRSTRIKES_SURVIVED."""
    if server is None or not _scoring_active(server) or damage <= 0:
        return
    if not _playable(victim):
        return
    state = round_stats(victim)
    state.increment(C.MOST_DAMAGE_TAKEN, int(round(float(damage))))
    if (int(kill_type) == int(C.HEADSHOT_KILL) and source is not None
            and source is not victim
            and getattr(source, "team", None) != getattr(victim, "team", None)):
        state.increment(C.MOST_HEADSHOTS_RECEIVED)
    if int(kill_type) == int(C.AIRSTRIKE_KILL) and int(getattr(victim, "health", 0)) > 0:
        current = time.monotonic() if now is None else now
        if current - state.last_airstrike_survived_at >= AIRSTRIKE_SURVIVE_DEDUPE_SECONDS:
            state.increment(C.MOST_AIRSTRIKES_SURVIVED)
        state.last_airstrike_survived_at = current


def record_shot(server, player) -> None:
    """Shots fired this round (FEWEST_SHOTS_FIRED)."""
    if server is None or not _scoring_active(server) or not _playable(player):
        return
    measures = round_stats(player).measures
    measures[SHOTS_MEASURE] = measures.get(SHOTS_MEASURE, 0.0) + 1.0


def record_blocks_placed(server, player, cells) -> None:
    """MOST_BLOCKS_PLACED and HIGHEST_BLOCK (VXL z grows downward)."""
    cells = tuple(cells or ())
    if not cells or server is None or not _scoring_active(server) or not _playable(player):
        return
    state = round_stats(player)
    state.increment(C.MOST_BLOCKS_PLACED, len(cells))
    top = int(C.MAP_Z) - 1
    state.raise_to(C.HIGHEST_BLOCK, max(top - int(cell[2]) for cell in cells))


def record_blocks_destroyed(server, player, count: int, chunks=()) -> None:
    """MOST_BLOCKS_DESTROYED and BIGGEST_COLLAPSING_OBJECT."""
    if server is None or not _scoring_active(server) or not _playable(player):
        return
    state = round_stats(player)
    if int(count) > 0:
        state.increment(C.MOST_BLOCKS_DESTROYED, int(count))
    for chunk in chunks or ():
        state.raise_to(C.BIGGEST_COLLAPSING_OBJECT, len(chunk))


def record_crate(server, player, award: int) -> None:
    """MOST_HEALTH/AMMO/BLOCK_CRATES_COLLECTED."""
    if server is None or not _scoring_active(server) or not _playable(player):
        return
    round_stats(player).increment(int(award))


def round_tick(player, server, dt: float) -> None:
    """MOST_DISTANCE_RAN, MOST_TIME_IN_AIR and MOST_TIME_ON_FIRE (bots too)."""
    if server is None or not 0.0 < dt <= 1.0 or not _scoring_active(server):
        return
    state = round_stats(player)
    position = position_of(player)
    last, state.last_position = state.last_position, position
    if not bool(getattr(player, "alive", False)) or not _playable(player):
        return
    if bool(getattr(player, "airborne", False)):
        state.accumulate(C.MOST_TIME_IN_AIR, dt)
    elif position is not None and last is not None:
        step = sum((a - b) ** 2 for a, b in zip(position, last)) ** 0.5
        if 0.0 < step <= 2.0:  # teleports/respawns are not running
            state.accumulate(C.MOST_DISTANCE_RAN, step)
    if bool(getattr(player, "on_fire", False)):
        state.accumulate(C.MOST_TIME_ON_FIRE, dt)


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


def record_death(server, victim, killer, kill_type: int, *, now: float | None = None,
                 domination: bool = False) -> None:
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
        # Same rule as BaseMode.apply_generic_death_penalty: objective blasts
        # (bomb/airstrike) and unattributed world deaths are not suicides.
        if kill_type not in _OBJECTIVE_DEATHS and (
            killer is victim or kill_type == C.FALL_KILL
        ):
            round_stats(victim).increment(C.MOST_SUICIDES)
        return
    if (killer.team == victim.team or int(killer.team) not in (TEAM1, TEAM2)
            or int(victim.team) not in (TEAM1, TEAM2)):
        return
    state = round_stats(killer)
    # Zombie: an infected player's kill is a brain eaten, the retail
    # zombie-side award; survivors keep the ordinary MOST_KILLS.
    if _zombie_team(server) == int(killer.team):
        state.increment(C.MOST_BRAINS_EATEN)
    else:
        state.increment(C.MOST_KILLS)
    if kill_type == C.HEADSHOT_KILL:
        state.increment(C.MOST_HEADSHOTS)
    elif kill_type == C.MELEE_KILL:
        state.increment(C.MOST_MELEE_KILLS)
    state.awards[C.BIGGEST_KILL_STREAK] = max(
        state.awards.get(C.BIGGEST_KILL_STREAK, 0), int(getattr(killer, "kill_streak", 0))
    )
    try:
        killer_hp = int(getattr(killer, "health", MAX_HEALTH))
    except (TypeError, ValueError):
        killer_hp = MAX_HEALTH
    if 0 < killer_hp <= LOW_HEALTH_KILL_HP:
        state.increment(C.MOST_KILLS_AT_LOW_HEALTH)
    first, second = position_of(killer), position_of(victim)
    if first is not None and second is not None and kill_type != C.MELEE_KILL:
        state.raise_to(C.LONGEST_RANGED_KILL, int(
            sum((a - b) ** 2 for a, b in zip(first, second)) ** 0.5
        ))
    try:
        victim_tool = int(getattr(victim, "tool", -1))
    except (TypeError, ValueError):
        victim_tool = -1
    if victim_tool in SNIPER_TOOLS:
        state.increment(C.MOST_SNIPERS_KILLED)
    if domination:
        state.increment(C.MOST_DOMINATIONS)
        round_stats(victim).increment(C.MOST_DOMINATED)
    current = time.monotonic() if now is None else now
    threshold = float(MAX_HEALTH) * float(CG.GENERIC_ASSIST_PERCENTAGE) / 100.0
    # Kill steal: the killer's own recent damage is below the assist share
    # while a teammate's reached it (inferred from the award name).
    own = contributions.get(int(getattr(killer, "id", -1)))
    own_damage = (
        float(own.damage) if own is not None and own.player is killer
        and current - own.touched <= ASSIST_WINDOW_SECONDS else 0.0
    )
    if own_damage < threshold and any(
        contribution.player is not killer
        and contribution.team == killer.team
        and current - contribution.touched <= ASSIST_WINDOW_SECONDS
        and contribution.damage >= threshold
        for contribution in contributions.values()
    ):
        state.increment(C.MOST_KILL_STEALS)
    from server.scoreboard import send_player_score

    assist_scores = _generic_assist_active(server)
    for player_id, contribution in contributions.items():
        assistant = contribution.player
        if (assistant is killer or server.players.get(player_id) is not assistant
                or assistant.team != killer.team or contribution.team != killer.team
                or contribution.life != int(getattr(assistant, "replication_generation", 0))
                or current - contribution.touched > ASSIST_WINDOW_SECONDS
                or contribution.damage < threshold):
            continue
        round_stats(assistant).increment(C.MOST_ASSISTS)
        if not assist_scores:
            continue
        assistant.score += int(CG.GENERIC_SCORE_ASSIST)
        send_player_score(server, assistant, reason=int(C.KILL_SCORE_ASSIST_REASON))


# ---------------------------------------------------------------------------
# Retail objective score events (shared by every objective mode)
# ---------------------------------------------------------------------------
#
# Amounts, radii and intervals are the retail client constants
# (shared/constants_gamemode.py *_SCORE_*, *_THREAT_RADIUS, *_ESCORT_RADIUS,
# *_CARRY_INTERVAL); the popup text is SCORE_REASON_CODES[reason]. The
# client ships no server-side trigger code, so the kill-event geometry is
# inferred from the reason names and their retail strings ("Close to Flag",
# "Flag Assault", "Flag Carrier Defend", "Flag Intercept", ...):
#
# * intercept       - kill the enemy carrying an objective.
# * carrier_defend  - kill an enemy within *_CARRIER_THREAT_RADIUS of a
#                     living carrier of your team (not the carrier itself).
# * defend          - kill an enemy within *_THREAT_RADIUS of an objective
#                     your team protects.
# * assault         - kill while YOU are within *_THREAT_RADIUS of an
#                     objective you attack ("Close to ...").
# * assault_enemy   - kill a defender standing within *_THREAT_RADIUS of the
#                     objective you attack, from further away.
# * distract        - die to an enemy while within the escort radius of your
#                     own living carrier: you drew the fire meant for it.
#
# A kill earns at most one kill event (priority in that order).

KILL_EVENT_PRIORITY = (
    "intercept", "carrier_defend", "defend", "assault", "assault_enemy",
)
# Round-award counters credited alongside an event (GameStats rows).
_EVENT_AWARDS = {
    "carrier_defend": C.MOST_DEFENDS,
    "defend": C.MOST_DEFENDS,
    "distract": C.MOST_DISTRACTIONS,
}


def position_of(entity) -> tuple[float, float, float] | None:
    """(x, y, z) of a player-like object or a 3-sequence; None if unknown."""
    if entity is None:
        return None
    if isinstance(entity, (tuple, list)):
        if len(entity) < 3:
            return None
        try:
            return float(entity[0]), float(entity[1]), float(entity[2])
        except (TypeError, ValueError):
            return None
    try:
        return float(entity.x), float(entity.y), float(entity.z)
    except (AttributeError, TypeError, ValueError):
        return None


def within(a, b, radius: float) -> bool:
    """3D distance test between two players/positions."""
    first, second = position_of(a), position_of(b)
    if first is None or second is None:
        return False
    return sum((p - q) ** 2 for p, q in zip(first, second)) <= float(radius) ** 2


def _playable(player) -> bool:
    try:
        return int(getattr(player, "team", -1)) in (TEAM1, TEAM2)
    except (TypeError, ValueError):
        return False


def owns_slot(server, player) -> bool:
    """``player`` still owns its id in the live roster (departed ids skip)."""
    if player is None:
        return False
    players = getattr(server, "players", None)
    if not isinstance(players, dict):
        return True
    try:
        return players.get(int(getattr(player, "id", -1))) is player
    except (TypeError, ValueError):
        return False


def objective_scoring_open(server, mode=None) -> bool:
    """False once the round ended, while the mode is being replaced, and in
    tutorial/UGC (no score economy)."""
    if mode is not None and (bool(getattr(mode, "ended", False))
                             or bool(getattr(mode, "retiring", False))):
        return False
    current = getattr(server, "mode", None)
    return _scoring_active(server) and not bool(getattr(current, "retiring", False))


def award_score_event(server, player, amount: int, reason: int, *,
                      award: int | None = None, mode=None) -> bool:
    """Pay one retail personal score event.

    Sends SetScore(PLAYER) with ``reason`` (the client's score popup) to the
    peers that know the id; ``score_changed`` records the profile stat and
    rolls the reason into its COM_* commendation / *_TOTAL_SCORE leaderboard
    aggregates. ``award`` also counts a GameStats round-award statistic.
    Skips ended/retiring rounds, departed or re-used ids and spectators.
    Bots are eligible (their profile tracker is a no-op). ``mode`` supplies
    ended/retiring flags when ``server.mode`` is not that mode.
    Returns True when paid.
    """
    if player is None or int(amount) <= 0 or not _playable(player):
        return False
    if not objective_scoring_open(server, mode) or not owns_slot(server, player):
        return False
    player.score = int(getattr(player, "score", 0) or 0) + int(amount)
    from server.scoreboard import send_player_score

    send_player_score(server, player, reason=int(reason))
    if award is not None:
        round_stats(player).increment(int(award))
    return True


def award_kill_event(server, player, event: str | None, amounts: dict,
                     reasons: dict, *, mode=None) -> bool:
    """Pay ``event`` (a KILL_EVENT_PRIORITY name or ``"distract"``) from a
    mode's amount/reason tables, crediting its round award."""
    if event is None or event not in amounts or event not in reasons:
        return False
    return award_score_event(
        server, player, int(amounts[event]), int(reasons[event]),
        award=_EVENT_AWARDS.get(event), mode=mode,
    )


def eligible_kill(killer, victim, kill_type: int) -> bool:
    """A real cross-team kill between two playable players."""
    if killer is None or victim is None or killer is victim:
        return False
    try:
        if int(kill_type) in _TRANSITION_DEATHS:
            return False
    except (TypeError, ValueError):
        return False
    if not (_playable(killer) and _playable(victim)):
        return False
    return int(killer.team) != int(victim.team)


def classify_objective_kill(killer, victim, *, victim_carrying: bool = False,
                            killer_team_carriers=(), defend_points=(),
                            attack_points=(), carrier_threat_radius: float = 0.0,
                            threat_radius: float = 0.0) -> str | None:
    """Pick the one objective event this kill earns (or None).

    ``victim_carrying``: the victim held an objective the killer's team
    wants back or away (evaluate BEFORE the death drops it).
    ``killer_team_carriers``: carriers on the killer's team.
    ``defend_points``: positions the killer's team protects.
    ``attack_points``: positions the killer's team attacks.
    """
    if victim_carrying:
        return "intercept"
    for carrier in killer_team_carriers:
        if carrier is killer or carrier is victim or not bool(getattr(carrier, "alive", True)):
            continue
        if within(victim, carrier, carrier_threat_radius):
            return "carrier_defend"
    for point in defend_points:
        if within(victim, point, threat_radius):
            return "defend"
    for point in attack_points:
        if within(killer, point, threat_radius):
            return "assault"
    for point in attack_points:
        if within(victim, point, threat_radius):
            return "assault_enemy"
    return None


def is_distraction(victim, killer, kill_type: int, victim_team_carriers,
                   radius: float) -> bool:
    """``victim`` died to an enemy while near its team's living carrier."""
    if not eligible_kill(killer, victim, kill_type):
        return False
    return any(
        carrier is not victim and bool(getattr(carrier, "alive", True))
        and within(victim, carrier, radius)
        for carrier in victim_team_carriers
    )


class EscortTracker:
    """Escort membership with the retail radius + hysteresis pair.

    A teammate starts escorting inside ``radius`` of the carrier and keeps
    escorting until it is beyond ``radius + hysteresis``. Membership is keyed
    by object identity, so a joiner reusing a departed id inherits nothing.
    """

    def __init__(self, radius: float, hysteresis: float = 0.0) -> None:
        self.radius = float(radius)
        self.hysteresis = max(0.0, float(hysteresis))
        self._members: dict[int, tuple[object, dict[int, object]]] = {}

    def reset(self) -> None:
        self._members.clear()

    def forget_carrier(self, carrier) -> None:
        self._members.pop(id(carrier), None)

    def escorts(self, carrier, candidates) -> list:
        """Update and return the living teammates escorting ``carrier``."""
        entry = self._members.get(id(carrier))
        previous = entry[1] if entry is not None and entry[0] is carrier else {}
        current: dict[int, object] = {}
        for other in candidates:
            if (other is carrier or not bool(getattr(other, "alive", False))
                    or not bool(getattr(other, "spawned", True))
                    or getattr(other, "team", None) != getattr(carrier, "team", None)):
                continue
            limit = self.radius
            if previous.get(id(other)) is other:
                limit += self.hysteresis
            if within(other, carrier, limit):
                current[id(other)] = other
        self._members[id(carrier)] = (carrier, current)
        return list(current.values())


def record_profile_total(player, stat: int, count: int = 1) -> None:
    """Count a retail *_TOTAL profile stat and roll it into each COM_*
    commendation aggregate listing it (DIA_STEAL_TOTAL -> COM_DIA_STEAL,
    OCC_LASTMAN_TOTAL -> COM_OCC_SURVIVAL). Score reasons roll up through
    ``score_changed`` instead."""
    from server import profile_stats

    profile_stats.add(player, int(stat), int(count))
    for aggregate, members in C.SCORE_REASONS_FOR_TOTALS.items():
        if 202 <= int(aggregate) <= 219 and int(stat) in members:
            profile_stats.add(player, int(aggregate), int(count))


# ---------------------------------------------------------------------------
# Teabag (retail TEABAG_* constants)
# ---------------------------------------------------------------------------

# The grave/corpse sits on the ground under the dead body's recorded
# position; allow a standing player's height difference.
TEABAG_VERTICAL_TOLERANCE = 3.0


def _classic_corpses(server) -> bool:
    mode = getattr(server, "mode", None)
    if str(getattr(mode, "mode_code", "") or "").lower() == "cctf":
        return True
    return str(getattr(mode, "death_representation", "grave") or "grave") != "grave"


def _teabag_points_enabled(server) -> bool:
    config = getattr(server, "config", None)
    if config is None:
        return False
    try:
        from server.game_rules import get_rules

        return bool(get_rules(config).enabled("RULE_POINTS_FROM_TEABAGGING"))
    except Exception:  # noqa: BLE001 - a rules fault must not break input
        return False


def record_teabag_crouch(server, player, *, now: float | None = None) -> bool:
    """Feed one crouch PRESS edge; True when it completed a teabag.

    Retail: TEABAG_CROUCH_COUNT (3) crouches, each within
    TEABAG_TIME_THRESHOLD (0.5 s) of the previous one, within
    TEABAG_MAX_DISTANCE (1.75 blocks, horizontal) of a dead enemy's
    grave/corpse. Each enemy death is teabagged once per player. The profile
    stat (COMBAT_TEABAG_TOTAL, COMBAT_TEABAG_CLASSIC_TOTAL under Classic
    corpses) and the MOST_TEABAGS round award always count; the
    NORMAL/CLASSIC_SCORE_TEABAG points (2, TEABAG_SCORE_REASON) are paid
    only when RULE_POINTS_FROM_TEABAGGING is on (retail default off).
    """
    if (server is None or player is None or not bool(getattr(player, "alive", False))
            or not _playable(player) or not objective_scoring_open(server)):
        return False
    current = time.monotonic() if now is None else float(now)
    presses = list(getattr(player, "_teabag_crouch_times", ()) or ())
    if presses and current - presses[-1] > float(C.TEABAG_TIME_THRESHOLD):
        presses = []
    presses.append(current)
    presses = presses[-int(C.TEABAG_CROUCH_COUNT):]
    player._teabag_crouch_times = presses
    if len(presses) < int(C.TEABAG_CROUCH_COUNT):
        return False
    origin = position_of(player)
    if origin is None:
        return False
    done = getattr(player, "_teabagged_deaths", None)
    if not isinstance(done, set):
        done = player._teabagged_deaths = set()
    for victim in tuple(getattr(server, "players", {}).values()):
        if (victim is player or bool(getattr(victim, "alive", True))
                or not _playable(victim) or int(victim.team) == int(player.team)):
            continue
        death_time = float(getattr(victim, "death_time", 0.0) or 0.0)
        key = (int(victim.id), death_time)
        if death_time <= 0.0 or key in done:
            continue
        spot = position_of(victim)
        if spot is None:
            continue
        horizontal = ((spot[0] - origin[0]) ** 2 + (spot[1] - origin[1]) ** 2) ** 0.5
        if (horizontal > float(C.TEABAG_MAX_DISTANCE)
                or abs(spot[2] - origin[2]) > TEABAG_VERTICAL_TOLERANCE):
            continue
        done.add(key)
        player._teabag_crouch_times = []
        classic = _classic_corpses(server)
        from server import profile_stats

        profile_stats.add(
            player, C.COMBAT_TEABAG_CLASSIC_TOTAL if classic else C.COMBAT_TEABAG_TOTAL
        )
        round_stats(player).increment(C.MOST_TEABAGS)
        if _teabag_points_enabled(server):
            amount = CG.CLASSIC_SCORE_TEABAG if classic else CG.NORMAL_SCORE_TEABAG
            award_score_event(server, player, int(amount), int(C.TEABAG_SCORE_REASON))
        return True
    return False
