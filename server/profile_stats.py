"""Authoritative retail profile counters; no network or reward writes.

Counts belong to a player connection and survive respawn. The result bridge
reserves immutable deltas at round end. IDs and units come from
shared.constants and the recovered constants_playerprofile.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

import shared.constants as C


@dataclass
class ProfileStats:
    values: dict[int, list[int]] = field(default_factory=dict)
    remainder: dict[int, float] = field(default_factory=dict)
    shot_serial: int = 0
    hit_serial: int = -1
    last_score: int = 0
    connected_seconds: float = 0.0
    active_seconds: float = 0.0
    idle_seconds: float = 0.0
    afk_seconds: float = 0.0
    activity_seen: bool = False
    human_opponents: int = 0
    bot_opponents: int = 0
    opponent_sample_seconds: float = 0.0
    last_position: tuple | None = None
    last_orientation: tuple | None = None

    def add(self, stat: int, count: int = 1, score: int = 0) -> None:
        if not (0 <= stat <= 250 or 1000 <= stat < 1000 + C.STATS_MAX_WEAPONS
                or 2000 <= stat < 2000 + C.STATS_MAX_WEAPONS
                or 3000 <= stat < 3000 + C.STATS_MAX_WEAPONS):
            raise ValueError("unknown retail profile statistic")
        if count < 0 or score < 0:
            raise ValueError("profile deltas cannot be negative")
        pair = self.values.setdefault(stat, [0, 0])
        pair[0] += count
        pair[1] += score

    def fractional(self, stat: int, amount: float) -> None:
        value = self.remainder.get(stat, 0.0) + amount
        whole = int(value + 1e-9)
        self.remainder[stat] = max(0.0, value - whole)
        if whole:
            self.add(stat, whole)


def tracker(player) -> ProfileStats | None:
    if player is None or bool(getattr(player, "is_bot", False)):
        return None
    server = getattr(getattr(player, "connection", None), "server", None)
    config = getattr(server, "config", None)
    if (bool(getattr(config, "ugc_runtime", False))
            or str(getattr(config, "default_mode", "")).lower() in {"ugc", "tut", "tutorial"}
            or bool(getattr(getattr(server, "mode", None), "ended", False))):
        return None
    state = getattr(player, "profile_stats", None)
    if state is None:
        state = player.profile_stats = ProfileStats()
    return state


def add(player, stat: int, count: int = 1, score: int = 0) -> None:
    state = tracker(player)
    if state is not None and (count or score):
        state.add(int(stat), int(count), int(score))


def shot(player) -> None:
    state = tracker(player)
    tool = int(getattr(player, "tool", -1))
    if state is not None and 0 <= tool < C.STATS_MAX_WEAPONS:
        state.shot_serial += 1
        state.add(1000 + tool)


def hit(player, target) -> None:
    state = tracker(player)
    tool = int(getattr(player, "tool", -1))
    if (state is not None and player is not target
            and player.team != target.team and 0 <= tool < C.STATS_MAX_WEAPONS
            and state.shot_serial > 0 and state.hit_serial != state.shot_serial):
        # A shotgun trigger counts once even if several pellets damage players.
        state.hit_serial = state.shot_serial
        state.add(2000 + tool)


_CLASS_PREFIX = {
    C.CLASS_SOLDIER: "SOLDIER", C.CLASS_SCOUT: "SCOUT",
    C.CLASS_ROCKETEER: "ROCKETEER", C.CLASS_MINER: "MINER",
    C.CLASS_CLASSIC_SOLDIER: "CLASSIC_SOLDIER", C.CLASS_ENGINEER: "ENGINEER",
    C.CLASS_SPECIALIST: "SPECIALIST", C.CLASS_MEDIC: "MEDIC",
    **{value: "GANGSTER" for value in range(C.CLASS_GANGSTER_1, C.CLASS_GANGSTER_VIP_2 + 1)},
}
_TOOL_LABELS = {
    0: "PICKAXE", 1: "KNIFE", 2: "SPADE", 3: "SUPERSPADE", 4: "SPADE",
    6: "RIFLE", 7: "SMG", 8: "MINIGUN", 9: "SHOTGUN", 10: "SHOTGUN2",
    11: "GRENADE", 12: "RPG", 13: "RPG2", 16: "TURRET", 17: "PISTOL",
    18: "SNIPER", 19: "SNIPER2", 20: "LANDMINE", 21: "DYNAMITE",
    29: "SNOWBLOWER", 31: "GRENADE", 32: "APG", 33: "MOLOTOV",
    34: "CROWBAR", 35: "TOMMYGUN", 36: "PISTOL", 49: "RIOTSTICK",
    50: "MACHETE", 52: "RIOTSHIELD", 53: "AUTOSHOTGUN",
    54: "CHEMICALBOMB", 55: "GRENADELAUNCHER", 57: "STICKYGRENADE",
    58: "MINELAUNCHER", 59: "C4", 60: "ASSAULTRIFLE",
    61: "LIGHTMACHINEGUN", 62: "AUTOPISTOL",
}
_KILL_TO_TOOL = {
    C.GRENADE_KILL: 11, C.ROCKET_KILL: 12, C.ROCKET2_KILL: 13,
    C.DRILL_KILL: 14, C.LANDMINE_KILL: 20, C.DYNAMITE_KILL: 21,
    C.ROCKET_TURRET_KILL: 16, C.SNOWBALL_KILL: 29,
    C.CLASSIC_GRENADE_KILL: 31, C.ANTIPERSONNEL_GRENADE_KILL: 32,
    C.MOLOTOV_KILL: 33, C.CHEMICALBOMB_KILL: 54,
    C.GRENADE_LAUNCHER_KILL: 55, C.STICKY_GRENADE_KILL: 57,
    C.MINE_KILL: 58, C.C4_KILL: 59,
}


def death(victim, killer, kill_type: int) -> None:
    if kill_type in {C.FORCED_TEAM_CHANGE_KILL, C.TEAM_CHANGE_KILL, C.CLASS_CHANGE_KILL}:
        return
    add(victim, C.DEATH_SCORE_REASON)
    if killer is victim:
        add(victim, C.SUICIDE_SCORE_REASON)
        return
    if killer is None:
        return
    if killer.team == victim.team:
        add(killer, C.KILL_SCORE_TEAMKILL_REASON)
        return
    add(killer, C.KILL_SCORE_REASON)
    if kill_type == C.HEADSHOT_KILL:
        add(killer, C.KILL_SCORE_HEADSHOT_REASON)
    elif kill_type == C.MELEE_KILL:
        add(killer, C.KILL_SCORE_MELEE_REASON)
    # Delayed explosives retain their weapon identity even after a tool swap.
    tool = _KILL_TO_TOOL.get(kill_type)
    if tool is None and kill_type in {C.WEAPON_KILL, C.HEADSHOT_KILL, C.MELEE_KILL}:
        tool = int(getattr(killer, "tool", -1))
    prefix = _CLASS_PREFIX.get(int(getattr(killer, "class_id", -1)))
    label = _TOOL_LABELS.get(tool)
    if prefix and label:
        stat = getattr(C, f"{prefix}_{label}_KILLS", None)
        if stat is not None:
            add(killer, stat)
    for streak, stat in ((5, C.COMBAT_5INAROW_TOTAL), (10, C.COMBAT_10INAROW_TOTAL),
                         (15, C.COMBAT_15INAROW_TOTAL)):
        if int(getattr(killer, "kill_streak", 0)) == streak:
            add(killer, stat)
    if int(getattr(killer, "class_id", -1)) in {
            C.CLASS_ZOMBIE, C.CLASS_FAST_ZOMBIE, C.CLASS_JUMP_ZOMBIE}:
        add(killer, C.ZOMBIE_HUMANS_KILLED_TOTAL)
        if tool == C.ZOMBIEHAND_TOOL:
            add(killer, C.ZOMBIE_HANDS_KILLS_TOTAL)


def score_changed(player, reason: int) -> None:
    state = tracker(player)
    if state is None:
        return
    current = int(getattr(player, "score", 0))
    delta = max(0, current - state.last_score)
    state.last_score = current
    if not delta:
        return
    # Kill/death counts come from accepted deaths, never scoreboard refreshes.
    state.add(reason, 0 if reason in {1, 2, 3, 4, 6, 220} else 1, delta)
    for aggregate, members in C.SCORE_REASONS_FOR_TOTALS.items():
        if 202 <= aggregate <= 219 and reason in members:
            state.add(int(aggregate), 1, delta)


_MAP_STATS = {
    name.removesuffix("_TIME_SCORE").replace("_", "").lower(): int(value)
    for name, value in vars(C).items() if name.endswith("_TIME_SCORE")
}
_MAP_STATS.update({"ww2": C.HIESVILLE_TIME_SCORE, "chicago": C.CITY_OF_CHICAGO_TIME_SCORE})


def tick(player, server, dt: float) -> None:
    state = tracker(player)
    if state is None or not 0.0 < dt <= 1.0:
        return
    state.connected_seconds += dt
    position = tuple(getattr(player, "position", ()))
    orientation = tuple(getattr(player, "orientation", ()))
    distance = (math.dist(position, state.last_position)
                if len(position) == 3 and state.last_position is not None else 0.0)
    moved = 0.0 < distance <= 2.0
    changed_aim = state.last_orientation is not None and orientation != state.last_orientation
    state.last_position, state.last_orientation = position, orientation
    state.idle_seconds = 0.0 if moved or changed_aim else state.idle_seconds + dt
    state.activity_seen = state.activity_seen or moved or changed_aim
    if not state.activity_seen or state.idle_seconds >= 60.0:
        state.afk_seconds += dt
    if not getattr(player, "alive", False) or int(getattr(player, "team", 0)) not in {2, 3}:
        return
    if state.activity_seen and state.idle_seconds < 60.0:
        state.active_seconds += dt
        state.opponent_sample_seconds -= dt
        if state.opponent_sample_seconds <= 0.0:
            # Population evidence does not need a per-frame O(players²) scan.
            state.opponent_sample_seconds = 1.0
            opponents = [other for other in getattr(server, "players", {}).values()
                         if int(getattr(other, "team", 0)) in {2, 3}
                         and other.team != player.team]
            state.human_opponents = max(state.human_opponents, sum(
                not bool(getattr(other, "is_bot", False)) for other in opponents))
            state.bot_opponents = max(state.bot_opponents, sum(
                bool(getattr(other, "is_bot", False)) for other in opponents))
    if moved and not bool(getattr(player, "airborne", False)):
        state.fractional(C.COMBAT_DISTANCE_RAN_TOTAL, distance)
    map_name = str(getattr(getattr(server, "world_manager", None), "map_name", "")
                   or getattr(server.config, "default_map", ""))
    map_key = "".join(char for char in map_name.lower() if char.isalnum())
    stat = _MAP_STATS.get(map_key)
    if stat is not None:
        state.fractional(stat, dt / 60.0)


def snapshot(player) -> dict[int, list[int]]:
    state = getattr(player, "profile_stats", None)
    return {} if state is None else {key: list(pair) for key, pair in state.values.items()}
