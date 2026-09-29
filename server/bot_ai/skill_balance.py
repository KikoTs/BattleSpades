"""Dynamic bot difficulty balance.

Bots should neither stomp nor feed the humans they play with. Once per second
(``BattleSpadesServer._run_periodic_services``) this measures how each team's
humans are doing this round and nudges bot aim/reaction toward a fair fight:

* humans losing badly (low K/D, trailing team score): enemy bots soften,
  allied bots sharpen;
* humans stomping: enemy bots sharpen, allied bots relax.

The adjustment is a per-team factor in [-1, 1] that follows its target at a
bounded rate (``skill_balance_rate`` per second) and scales profile fields by
at most ``skill_balance_max_shift``. Every adjusted field stays inside the
configured difficulty band (``[bots] difficulty``; ``mixed`` spans casual to
hard), so a ``normal`` server never gets hard or casual bots. Profiles are
rebuilt from the bot's original profile each time, so nothing drifts.

The adjusted profile replaces ``_RuntimeBot.profile``: the director's motor
(aim noise, turn speed, tracking lag, fire gate) reads it directly and the
worker receives it in the next perception frame (reaction time, recoil).
"""

from __future__ import annotations

from dataclasses import replace
import logging
import math

from server.game_constants import TEAM1, TEAM2

from .profiles import _BANDS

logger = logging.getLogger(__name__)

_PLAYABLE = (TEAM1, TEAM2)

DEFAULT_MAX_SHIFT = 0.35
DEFAULT_RATE = 0.03
DEFAULT_DEADBAND = 0.15
DEFAULT_MIN_EVENTS = 6
# Allied bots move half as far as the humans' opponents.
ALLY_WEIGHT = 0.5
# A team-score gap is judged against at least this many points, so a 1-0
# capture lead is not treated as a rout.
SCORE_GAP_FLOOR = 5.0
KD_WEIGHT = 0.65
_APPLY_EPSILON = 0.01

_FIELDS = (
    "skill", "reaction_time", "tracking_delay", "turn_speed",
    "turn_acceleration", "aim_noise",
)
_BAND_ATTR = {
    "skill": "skill",
    "reaction_time": "reaction",
    "tracking_delay": "tracking",
    "turn_speed": "turn_speed",
    "turn_acceleration": "turn_acceleration",
    "aim_noise": "aim_noise",
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def band_limits(difficulties) -> dict[str, tuple[float, float]]:
    """Union of the named difficulty bands, per profile field."""
    names = [name for name in difficulties if name in _BANDS] or list(_BANDS)
    limits = {}
    for field_name in _FIELDS:
        attr = _BAND_ATTR[field_name]
        lows = [getattr(_BANDS[name], attr)[0] for name in names]
        highs = [getattr(_BANDS[name], attr)[1] for name in names]
        limits[field_name] = (min(lows), max(highs))
    return limits


def scale_profile(profile, strength: float, limits: dict[str, tuple[float, float]]):
    """Return ``profile`` sharpened (``strength`` > 0) or softened (< 0).

    ``strength`` is the already-bounded shift (factor x max_shift, so at most
    about +-0.35). Every field is clamped into ``limits`` but a field that
    already lies outside them (an admin-chosen difficulty) is never pushed
    further out.
    """
    k = float(strength)
    if abs(k) < 1e-9:
        return profile

    def bounded(name: str, original: float, value: float) -> float:
        low, high = limits[name]
        low, high = min(low, original), max(high, original)
        return _clamp(value, low, high)

    skill = bounded("skill", profile.skill, profile.skill + 0.4 * k)
    return replace(
        profile,
        skill=skill,
        reaction_time=bounded(
            "reaction_time", profile.reaction_time, profile.reaction_time * (1.0 - 0.8 * k)
        ),
        tracking_delay=bounded(
            "tracking_delay", profile.tracking_delay, profile.tracking_delay * (1.0 - k)
        ),
        turn_speed=bounded(
            "turn_speed", profile.turn_speed, profile.turn_speed * (1.0 + 0.5 * k)
        ),
        turn_acceleration=bounded(
            "turn_acceleration",
            profile.turn_acceleration,
            profile.turn_acceleration * (1.0 + 0.5 * k),
        ),
        aim_noise=bounded("aim_noise", profile.aim_noise, profile.aim_noise * (1.0 - k)),
        recoil_control=_clamp(0.30 + skill * 0.65, 0.0, 1.0),
    )


def human_edge(server, team: int, *, min_events: int = DEFAULT_MIN_EVENTS,
               deadband: float = DEFAULT_DEADBAND) -> float:
    """How well ``team``'s humans are doing this round, in [-1, 1].

    Combines the humans' K/D (log scale: 4:1 is +1, 1:4 is -1) with the
    team-score gap, scaled by confidence (few kills/deaths say little) and
    passed through a dead band so an ordinary close game changes nothing.
    """
    kills = deaths = 0
    for player in getattr(server, "players", {}).values():
        if bool(getattr(player, "is_bot", False)):
            continue
        if int(getattr(player, "team", -1)) != team:
            continue
        kills += max(0, int(getattr(player, "kills", 0) or 0))
        deaths += max(0, int(getattr(player, "deaths", 0) or 0))
    events = kills + deaths
    if events <= 0:
        return 0.0
    kd_edge = _clamp(math.log2((kills + 1.0) / (deaths + 1.0)) / 2.0, -1.0, 1.0)
    teams = getattr(server, "teams", {}) or {}
    other = TEAM2 if team == TEAM1 else TEAM1
    try:
        own = float(getattr(teams.get(team), "score", 0) or 0)
        theirs = float(getattr(teams.get(other), "score", 0) or 0)
    except (TypeError, ValueError):
        own = theirs = 0.0
    gap = _clamp((own - theirs) / max(SCORE_GAP_FLOOR, own + theirs), -1.0, 1.0)
    edge = KD_WEIGHT * kd_edge + (1.0 - KD_WEIGHT) * gap
    edge *= min(1.0, events / float(max(1, min_events)))
    magnitude = abs(edge)
    if magnitude <= deadband:
        return 0.0
    return math.copysign((magnitude - deadband) / (1.0 - deadband), edge)


class BotSkillBalancer:
    """Per-team bounded, smoothed bot difficulty adjustment."""

    def __init__(self, server) -> None:
        self.server = server
        self.factors: dict[int, float] = {TEAM1: 0.0, TEAM2: 0.0}
        self.targets: dict[int, float] = {TEAM1: 0.0, TEAM2: 0.0}
        self._base: dict[tuple[int, int], object] = {}
        self._applied: dict[tuple[int, int], tuple[int, float]] = {}
        self._last_update: float | None = None

    def _bots_config(self):
        config = getattr(self.server, "config", None)
        return getattr(config, "bots", config)

    def _setting(self, name: str, default: float) -> float:
        value = getattr(self._bots_config(), name, None)
        if value is None:
            return float(default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def enabled(self) -> bool:
        return bool(getattr(self._bots_config(), "skill_balance", True))

    def compute_targets(self) -> dict[int, float]:
        """Target factor for each team's bots (positive = sharper)."""
        server = self.server
        humans = {TEAM1: 0, TEAM2: 0}
        for player in getattr(server, "players", {}).values():
            if bool(getattr(player, "is_bot", False)):
                continue
            team = int(getattr(player, "team", -1))
            if team in humans:
                humans[team] += 1
        total = humans[TEAM1] + humans[TEAM2]
        targets = {TEAM1: 0.0, TEAM2: 0.0}
        mode = getattr(server, "mode", None)
        if total <= 0 or mode is None or bool(getattr(mode, "ended", False)):
            return targets
        min_events = int(self._setting("skill_balance_min_events", DEFAULT_MIN_EVENTS))
        deadband = _clamp(self._setting("skill_balance_deadband", DEFAULT_DEADBAND), 0.0, 0.95)
        for team in _PLAYABLE:
            if humans[team] <= 0:
                continue
            weight = humans[team] / float(total)
            edge = human_edge(server, team, min_events=min_events, deadband=deadband)
            other = TEAM2 if team == TEAM1 else TEAM1
            targets[other] += weight * edge
            targets[team] -= weight * edge * ALLY_WEIGHT
        return {team: _clamp(value, -1.0, 1.0) for team, value in targets.items()}

    def update(self, now: float) -> None:
        director = getattr(self.server, "bots", None)
        runtimes = getattr(director, "_runtime", None)
        previous = self._last_update
        self._last_update = float(now)
        if not isinstance(runtimes, dict):
            return
        dt = 1.0 if previous is None else _clamp(float(now) - previous, 0.0, 5.0)
        self.targets = (
            self.compute_targets() if self.enabled() else {TEAM1: 0.0, TEAM2: 0.0}
        )
        rate = max(0.0, self._setting("skill_balance_rate", DEFAULT_RATE))
        step = rate * dt
        for team in _PLAYABLE:
            current = self.factors[team]
            target = self.targets[team]
            if target > current:
                self.factors[team] = min(target, current + step)
            else:
                self.factors[team] = max(target, current - step)
        self._apply(runtimes)

    def _apply(self, runtimes: dict) -> None:
        max_shift = _clamp(self._setting("skill_balance_max_shift", DEFAULT_MAX_SHIFT), 0.0, 0.9)
        configured = str(getattr(self._bots_config(), "difficulty", "mixed")).lower()
        live: set[tuple[int, int]] = set()
        for player_id, runtime in tuple(runtimes.items()):
            player = getattr(runtime, "player", None)
            key = (int(player_id), int(getattr(runtime, "generation", 0)))
            live.add(key)
            base = self._base.get(key)
            if base is None:
                base = self._base[key] = runtime.profile
            team = int(getattr(player, "team", -1))
            factor = self.factors.get(team, 0.0)
            applied = self._applied.get(key)
            if applied is not None and applied[0] == team and abs(applied[1] - factor) < _APPLY_EPSILON:
                continue
            difficulties = (
                (configured, str(getattr(base, "difficulty", configured)))
                if configured in _BANDS else tuple(_BANDS)
            )
            try:
                runtime.profile = scale_profile(
                    base, factor * max_shift, band_limits(difficulties)
                )
            except Exception:  # noqa: BLE001 - keep the previous profile
                logger.debug("bot skill scaling failed for %s", player_id, exc_info=True)
                continue
            self._applied[key] = (team, factor)
        for key in [key for key in self._base if key not in live]:
            self._base.pop(key, None)
            self._applied.pop(key, None)

    def base_profile(self, player_id: int, generation: int):
        return self._base.get((int(player_id), int(generation)))


__all__ = ["BotSkillBalancer", "band_limits", "human_edge", "scale_profile"]
