"""Statistical anti-cheat DETECTION: per-player suspicion scores for review.

Nothing here kicks or bans. Hard protocol violations are handled (and
kicked) by :mod:`server.anticheat`; this module only turns the aggregates the
validation code already collects into a ranked, explained suspicion report:

* ``headshot_kills`` - headshot share of hitscan kills (profile counters).
* ``headshot_hits``  - headshot share of hitscan hits (combat aggregates).
* ``accuracy``       - per-weapon hit rate above the 99th percentile of the
  HUMAN population (current players plus departed players this session).
* ``aim_snap``       - large aim change within <= 2 input frames landing on
  an enemy head right before a shot (fed by :func:`observe_shot`).
* ``reaction``       - "acquisition time" (see :func:`observe_shot`).
* ``pellet_seed``    - client-chosen shotgun seeds far from uniform.
* ``sustained:<k>``  - log-only violations sustained per minute.

Every check has a minimum sample size and bots are never analysed, never
flagged and never part of a population. :func:`tick` (once per second)
maintains the population archive and every ``[anticheat]
summary_interval_seconds`` logs ONE line per flagged player to the
``anticheat`` logger and appends one JSON line per flagged player to
``[anticheat] report_path`` (size-rotated) for fleet review.

Reaction time: the server does not track line-of-sight per player pair, so
"enemy becomes visible -> first hit" is approximated by the ACQUISITION
time of an engagement: on the first shot aimed at an enemy (not engaged in
the last few seconds, clear line of sight from the shot origin to the head),
walk the input-frame orientation history back to the last frame whose aim
was ``reaction_acquire_deg`` or more away from that enemy's head. The frame
count times the 60 Hz frame length is the time the player needed to bring
the crosshair onto the target. Pre-aimed targets (never that far off within
the history window) produce no sample. The enemy's CURRENT head position is
used for the older frames, which is fine for the short windows involved.
"""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
import time
from collections import Counter, deque
from pathlib import Path

import shared.constants as C

from server import anticheat

logger = logging.getLogger("anticheat")

FRAME_SECONDS = 1.0 / 60.0

# Hitscan tools whose shots are single rays (headshot ratios are meaningful).
_SINGLE_RAY_TOOLS = frozenset({6, 7, 8, 15, 17, 18, 19, 35, 36, 60, 61, 62})
_PELLET_TOOLS = frozenset({9, 10, 53})
HITSCAN_TOOLS = _SINGLE_RAY_TOOLS | _PELLET_TOOLS

# Violation kinds whose sustained rate is interesting (enforced or log-only).
SUSTAINED_KINDS = (
    "shot_origin_drift",
    "aim_direction_mismatch",
    "input_backlog",
    "input_label_ahead",
)

# Defaults for every ``[anticheat]`` key read here (see module docstring).
DEFAULTS = {
    "report_enabled": True,
    "summary_interval_seconds": 60.0,
    "report_path": "logs/anticheat.jsonl",
    "report_max_bytes": 5_000_000,
    "report_backups": 3,
    "flag_min_score": 1.0,
    # Headshot share of hitscan kills.
    "headshot_kill_ratio": 0.60,
    "headshot_kill_min_kills": 30,
    # Headshot share of single-ray hitscan hits.
    "headshot_hit_ratio": 0.55,
    "headshot_hit_min_hits": 60,
    # Per-weapon accuracy vs the human population.
    "accuracy_min_shots": 100,
    "accuracy_min_population": 20,
    "accuracy_percentile": 99.0,
    "accuracy_min_margin": 0.15,
    # Aim snaps.
    "snap_min_deg": 20.0,
    "snap_frames": 2,
    "snap_head_radius": 0.35,
    "snap_margin_deg": 0.75,
    "snap_min_events": 4,
    "snap_min_engaged": 20,
    "snap_ratio": 0.10,
    # Acquisition ("reaction") time.
    "reaction_acquire_deg": 10.0,
    "reaction_history_frames": 60,
    "reaction_min_samples": 15,
    "reaction_median_ms": 90.0,
    "reaction_reengage_seconds": 3.0,
    "engage_cone_deg": 4.0,
    # Pellet seed skew.
    "pellet_seed_min_shots": 64,
    "pellet_seed_top_share": 0.2,
    # Sustained log-only violations.
    "sustained_min_minutes": 5.0,
    "sustained_min_count": 20,
    "sustained_per_minute": 3.0,
}

# Weight of each triggered check in the score.
WEIGHTS = {
    "headshot_kills": 1.0,
    "headshot_hits": 1.0,
    "accuracy": 1.0,
    "aim_snap": 2.0,
    "reaction": 1.5,
    "pellet_seed": 1.5,
    "sustained": 0.5,
}

_ARCHIVE_LIMIT = 500


def _setting(server, name):
    return anticheat.setting(server, name, DEFAULTS[name])


# ---------------------------------------------------------------------------
# small vector helpers
# ---------------------------------------------------------------------------


def _unit(vector):
    try:
        x, y, z = (float(vector[0]), float(vector[1]), float(vector[2]))
    except (TypeError, ValueError, IndexError):
        return None
    if not all(math.isfinite(c) for c in (x, y, z)):
        return None
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-9:
        return None
    return (x / length, y / length, z / length)


def _angle_deg(a, b) -> float:
    dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def _point(value):
    try:
        point = (float(value[0]), float(value[1]), float(value[2]))
    except (TypeError, ValueError, IndexError):
        return None
    return point if all(math.isfinite(c) for c in point) else None


def _is_bot(player) -> bool:
    return bool(getattr(player, "is_bot", False))


def percentile(values, pct: float) -> float:
    """Linear-interpolated percentile (``pct`` in 0..100)."""

    ordered = sorted(values)
    if not ordered:
        return math.nan
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * max(0.0, min(100.0, pct)) / 100.0
    low = int(math.floor(rank))
    high = min(len(ordered) - 1, low + 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


# ---------------------------------------------------------------------------
# per-shot aim observation (hook from CombatRuntime.handle_shot)
# ---------------------------------------------------------------------------


def aim_stats(player) -> dict:
    """The player's aim-behaviour aggregates (created on first use)."""

    existing = getattr(player, "anticheat_aim", None)
    if isinstance(existing, dict):
        return existing
    created = {
        "shots": 0,
        "engaged": 0,
        "on_head": 0,
        "snaps": 0,
        "snap_examples": deque(maxlen=5),
        "acquisition_ms": deque(maxlen=200),
        "engaged_at": {},
    }
    try:
        player.anticheat_aim = created
    except AttributeError:
        pass
    return created


def observe_shot(server, player, packet=None, *, loop=None, origin=None,
                 direction=None, now=None) -> None:
    """Record aim behaviour for one accepted hitscan trigger pull.

    Call once per accepted ShootPacket (after validation). Cheap: at most
    ``reaction_history_frames`` dictionary lookups plus one line-of-sight
    walk on engaged shots. Never raises.
    """

    try:
        _observe_shot(server, player, packet, loop, origin, direction, now)
    except Exception:  # noqa: BLE001 - detection must never break combat
        logger.debug("anticheat observe_shot failed", exc_info=True)


def _observe_shot(server, player, packet, loop, origin, direction, now):
    if player is None or _is_bot(player):
        return
    tool = int(getattr(player, "tool", -1))
    if tool not in HITSCAN_TOOLS:
        return
    if packet is not None:
        if loop is None:
            loop = getattr(packet, "loop_count", None)
        if origin is None:
            origin = (getattr(packet, "x", None), getattr(packet, "y", None),
                      getattr(packet, "z", None))
        if direction is None:
            direction = (getattr(packet, "ori_x", None),
                         getattr(packet, "ori_y", None),
                         getattr(packet, "ori_z", None))
    origin = _point(origin) if origin is not None else None
    if origin is None:
        origin = _point(getattr(player, "eye", ()))
    aim = _unit(direction) if direction is not None else None
    if aim is None:
        aim = _unit(getattr(player, "orientation", ()))
    if origin is None or aim is None:
        return
    now = time.monotonic() if now is None else float(now)
    stats = aim_stats(player)
    stats["shots"] += 1

    # Nearest enemy head (angularly) to the shot direction.
    best = None
    for target in list(getattr(server, "players", {}).values()):
        if target is player or not getattr(target, "alive", False):
            continue
        if not getattr(target, "spawned", True):
            continue
        if getattr(target, "team", None) == getattr(player, "team", None):
            continue
        head = _point(getattr(target, "eye", ()))
        if head is None:
            continue
        offset = tuple(head[i] - origin[i] for i in range(3))
        distance = math.sqrt(sum(c * c for c in offset))
        if distance < 0.5:
            continue
        head_dir = tuple(c / distance for c in offset)
        angle = _angle_deg(aim, head_dir)
        if best is None or angle < best[0]:
            best = (angle, target, head, head_dir, distance)
    if best is None:
        return
    angle, target, head, head_dir, distance = best
    head_cone = math.degrees(math.atan2(
        float(_setting(server, "snap_head_radius")), distance
    )) + float(_setting(server, "snap_margin_deg"))
    if angle > max(head_cone, float(_setting(server, "engage_cone_deg"))):
        return
    if not _line_of_sight(server, origin, head):
        return
    stats["engaged"] += 1
    on_head = angle <= head_cone
    if on_head:
        stats["on_head"] += 1

    history = getattr(player, "orientation_at_loop", None)

    def aim_at(back: int):
        if not callable(history) or loop is None:
            return None
        try:
            return _unit(history(int(loop) - back) or ())
        except Exception:  # noqa: BLE001
            return None

    # Snap: >= snap_min_deg of aim change within snap_frames, landing on head.
    if on_head:
        snap_min = float(_setting(server, "snap_min_deg"))
        for back in range(1, max(1, int(_setting(server, "snap_frames"))) + 1):
            previous = aim_at(back)
            if previous is None:
                continue
            swing = _angle_deg(previous, aim)
            if swing >= snap_min and _angle_deg(previous, head_dir) >= 0.75 * snap_min:
                stats["snaps"] += 1
                stats["snap_examples"].append({
                    "deg": round(swing, 1),
                    "frames": back,
                    "head_err_deg": round(angle, 2),
                    "dist": round(distance, 1),
                    "target": getattr(target, "id", None),
                })
                break

    # Acquisition time for a fresh engagement.
    engaged_at = stats["engaged_at"]
    target_id = getattr(target, "id", id(target))
    fresh = now - engaged_at.get(target_id, -1e9) >= float(
        _setting(server, "reaction_reengage_seconds")
    )
    engaged_at[target_id] = now
    if len(engaged_at) > 64:
        for key in sorted(engaged_at, key=engaged_at.get)[:-32]:
            del engaged_at[key]
    if not fresh:
        return
    acquire = float(_setting(server, "reaction_acquire_deg"))
    for back in range(1, int(_setting(server, "reaction_history_frames")) + 1):
        previous = aim_at(back)
        if previous is None:
            return  # history gap: no reliable sample
        if _angle_deg(previous, head_dir) >= acquire:
            stats["acquisition_ms"].append(back * FRAME_SECONDS * 1000.0)
            return


def _line_of_sight(server, origin, head) -> bool:
    world = getattr(server, "world_manager", None)
    if world is None:
        return True
    try:
        from server.combat_runtime import segment_clear

        return bool(segment_clear(world, origin, head))
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------------------
# per-player analysis
# ---------------------------------------------------------------------------


def _profile_kills(player):
    """(hitscan_kills, headshot_kills) from profile counters, or None.

    Hitscan kills = kills - melee kills - explosive kills (class/weapon
    counters). Explosive combos without a counter stay in the denominator,
    which only lowers the ratio (conservative).
    """

    state = getattr(player, "profile_stats", None)
    values = getattr(state, "values", None)
    if not isinstance(values, dict):
        return None

    def count(stat):
        pair = values.get(int(stat)) if stat is not None else None
        return int(pair[0]) if pair else 0

    kills = count(C.KILL_SCORE_REASON)
    headshots = count(C.KILL_SCORE_HEADSHOT_REASON)
    melee = count(C.KILL_SCORE_MELEE_REASON)
    explosive = sum(count(stat) for stat in _explosive_kill_stats())
    return max(headshots, kills - melee - explosive), headshots


_EXPLOSIVE_STATS_CACHE: list | None = None


def _explosive_kill_stats() -> list:
    global _EXPLOSIVE_STATS_CACHE
    if _EXPLOSIVE_STATS_CACHE is None:
        stats = set()
        try:
            from server import profile_stats as ps

            labels = {ps._TOOL_LABELS.get(t) for t in ps._KILL_TO_TOOL.values()}
            labels.discard(None)
            for prefix in set(ps._CLASS_PREFIX.values()):
                for label in labels:
                    stat = getattr(C, f"{prefix}_{label}_KILLS", None)
                    if stat is not None:
                        stats.add(int(stat))
        except Exception:  # noqa: BLE001
            pass
        _EXPLOSIVE_STATS_CACHE = sorted(stats)
    return _EXPLOSIVE_STATS_CACHE


def _weapon_accuracies(player, min_shots: int) -> dict:
    stats = getattr(player, "anticheat_stats", None)
    weapons = stats.get("weapons", {}) if isinstance(stats, dict) else {}
    result = {}
    for tool, entry in weapons.items():
        shots = int(entry.get("shots", 0))
        if shots >= min_shots and int(tool) in HITSCAN_TOOLS:
            result[int(tool)] = (int(entry.get("hits", 0)) / shots, shots)
    return result


def _reason(check, weight_key, detail, excess=1.0):
    # Scale modestly with how far past the threshold the value is.
    score = WEIGHTS[weight_key] * max(1.0, min(3.0, excess))
    return {"check": check, "score": round(score, 2), "detail": detail}


def analyze_player(server, player, *, now=None, population=None) -> dict | None:
    """Suspicion report ``{score, flagged, reasons[], ...}``; None for bots."""

    if player is None or _is_bot(player):
        return None
    now = time.monotonic() if now is None else float(now)
    reasons = []
    get = lambda name: _setting(server, name)  # noqa: E731

    # 1. headshot share of hitscan kills
    kills = _profile_kills(player)
    if kills is not None:
        hitscan_kills, headshot_kills = kills
        ratio_limit = float(get("headshot_kill_ratio"))
        if hitscan_kills >= int(get("headshot_kill_min_kills")) and hitscan_kills:
            ratio = headshot_kills / hitscan_kills
            if ratio > ratio_limit:
                reasons.append(_reason(
                    "headshot_kills", "headshot_kills",
                    f"{headshot_kills}/{hitscan_kills} hitscan kills are headshots "
                    f"({ratio:.0%} > {ratio_limit:.0%})",
                    ratio / ratio_limit,
                ))

    stats = getattr(player, "anticheat_stats", None)
    stats = stats if isinstance(stats, dict) else {}

    # 2. headshot share of single-ray hits
    weapons = stats.get("weapons", {}) or {}
    ray_hits = sum(int(w.get("hits", 0)) for t, w in weapons.items()
                   if int(t) in _SINGLE_RAY_TOOLS)
    ray_heads = sum(int(w.get("headshots", 0)) for t, w in weapons.items()
                    if int(t) in _SINGLE_RAY_TOOLS)
    hit_limit = float(get("headshot_hit_ratio"))
    if ray_hits >= int(get("headshot_hit_min_hits")):
        ratio = ray_heads / ray_hits
        if ratio > hit_limit:
            reasons.append(_reason(
                "headshot_hits", "headshot_hits",
                f"{ray_heads}/{ray_hits} hits are headshots ({ratio:.0%} > {hit_limit:.0%})",
                ratio / hit_limit,
            ))

    # 3. per-weapon accuracy vs human population
    if population is None:
        population = population_accuracies(server, exclude=player)
    min_shots = int(get("accuracy_min_shots"))
    min_pop = int(get("accuracy_min_population"))
    pct = float(get("accuracy_percentile"))
    margin = float(get("accuracy_min_margin"))
    for tool, (accuracy, shots) in sorted(_weapon_accuracies(player, min_shots).items()):
        others = [value for owner, value in population.get(tool, ())
                  if owner is not player]
        if len(others) < min_pop:
            continue
        cutoff = percentile(others, pct)
        median = statistics.median(others)
        if accuracy > cutoff and accuracy - median >= margin:
            reasons.append(_reason(
                f"accuracy:{tool}", "accuracy",
                f"tool {tool} accuracy {accuracy:.0%} over {shots} shots > "
                f"p{pct:g} {cutoff:.0%} (median {median:.0%}, n={len(others)})",
                1.0 + (accuracy - cutoff) / max(0.05, margin),
            ))

    # 4. aim snaps and 5. acquisition time
    aim = getattr(player, "anticheat_aim", None)
    if isinstance(aim, dict):
        engaged = int(aim.get("engaged", 0))
        snaps = int(aim.get("snaps", 0))
        if (engaged >= int(get("snap_min_engaged"))
                and snaps >= int(get("snap_min_events"))):
            ratio = snaps / engaged
            limit = float(get("snap_ratio"))
            if ratio >= limit:
                examples = list(aim.get("snap_examples", ()))[-2:]
                reasons.append(_reason(
                    "aim_snap", "aim_snap",
                    f"{snaps} snap-to-head shots of {engaged} engaged "
                    f"({ratio:.0%}); e.g. {examples}",
                    ratio / limit,
                ))
        samples = list(aim.get("acquisition_ms", ()))
        if len(samples) >= int(get("reaction_min_samples")):
            median = statistics.median(samples)
            limit = float(get("reaction_median_ms"))
            if median < limit:
                reasons.append(_reason(
                    "reaction", "reaction",
                    f"median target acquisition {median:.0f} ms over "
                    f"{len(samples)} engagements (< {limit:g} ms)",
                    limit / max(1.0, median),
                ))

    # 6. pellet seed skew
    seeds = stats.get("pellet_seeds")
    if isinstance(seeds, Counter) and seeds:
        total = sum(seeds.values())
        top_seed, top = seeds.most_common(1)[0]
        share_limit = float(get("pellet_seed_top_share"))
        if total >= int(get("pellet_seed_min_shots")) and top >= share_limit * total:
            reasons.append(_reason(
                "pellet_seed", "pellet_seed",
                f"seed {top_seed} used {top}/{total} shotgun shots "
                f"({top / total:.0%}; uniform ~{1 / 256:.1%})",
                (top / total) / share_limit,
            ))

    # 7. sustained violations
    counts = getattr(player, "anticheat_counts", None) or {}
    track = _state(server)["players"].get(_track_key(player))
    minutes = (now - track["first_seen"]) / 60.0 if track else 0.0
    if minutes >= float(get("sustained_min_minutes")):
        per_minute_limit = float(get("sustained_per_minute"))
        for kind in SUSTAINED_KINDS:
            total = int(counts.get(kind, 0)) + int(counts.get(f"{kind}:observed", 0))
            rate = total / minutes
            if total >= int(get("sustained_min_count")) and rate >= per_minute_limit:
                reasons.append(_reason(
                    f"sustained:{kind}", "sustained",
                    f"{total} {kind} in {minutes:.1f} min ({rate:.1f}/min)",
                    rate / per_minute_limit,
                ))

    score = round(sum(r["score"] for r in reasons), 2)
    return {
        "player_id": getattr(player, "id", None),
        "name": getattr(player, "name", ""),
        "score": score,
        "flagged": bool(reasons) and score >= float(get("flag_min_score")),
        "reasons": reasons,
    }


def population_accuracies(server, exclude=None) -> dict:
    """{tool: [(owner, accuracy)]} over humans: current players + archive."""

    min_shots = int(_setting(server, "accuracy_min_shots"))
    result: dict = {}
    current_keys = set()
    for player in list(getattr(server, "players", {}).values()):
        if _is_bot(player):
            continue
        current_keys.add(_track_key(player))
        for tool, (accuracy, _shots) in _weapon_accuracies(player, min_shots).items():
            result.setdefault(tool, []).append((player, accuracy))
    for key, tool, accuracy in _state(server)["archive"]:
        if key in current_keys:
            continue  # a reconnect is already counted live
        result.setdefault(tool, []).append((key, accuracy))
    return result


def analyze_all(server, *, now=None) -> list[dict]:
    """Reports for every connected human, highest score first."""

    population = population_accuracies(server)
    reports = []
    for player in list(getattr(server, "players", {}).values()):
        report = analyze_player(server, player, now=now, population=population)
        if report is not None:
            reports.append(report)
    reports.sort(key=lambda r: (-r["score"], r["player_id"] or 0))
    return reports


def raw_stats(player) -> dict:
    """Raw counters for ``/acstats``."""

    stats = getattr(player, "anticheat_stats", None)
    stats = stats if isinstance(stats, dict) else {}
    aim = getattr(player, "anticheat_aim", None)
    aim = aim if isinstance(aim, dict) else {}
    queue = None
    queue_stats = getattr(player, "input_queue_delay_stats", None)
    if callable(queue_stats):
        try:
            queue = queue_stats()
        except Exception:  # noqa: BLE001
            queue = None
    kills = _profile_kills(player)
    samples = list(aim.get("acquisition_ms", ()))
    return {
        "kills": int(getattr(player, "kills", 0)),
        "deaths": int(getattr(player, "deaths", 0)),
        "hitscan_kills": kills[0] if kills else None,
        "headshot_kills": kills[1] if kills else None,
        "shots": int(stats.get("shots", 0)),
        "hits": int(stats.get("hits", 0)),
        "headshots": int(stats.get("headshots", 0)),
        "pellet_hits": int(stats.get("pellet_hits", 0)),
        "weapons": {int(t): dict(w) for t, w in (stats.get("weapons") or {}).items()},
        "origin_error": dict(stats.get("origin_error") or {}),
        "origin_error_fallback": dict(stats.get("origin_error_fallback") or {}),
        "aim_angle": dict(stats.get("aim_angle") or {}),
        "aim_angle_fallback": dict(stats.get("aim_angle_fallback") or {}),
        "pellet_seed_shots": sum((stats.get("pellet_seeds") or {}).values()),
        "rejected": dict(stats.get("rejected") or {}),
        "violations": dict(getattr(player, "anticheat_counts", None) or {}),
        "input_queue_delay": queue,
        "aim": {
            "shots": int(aim.get("shots", 0)),
            "engaged": int(aim.get("engaged", 0)),
            "on_head": int(aim.get("on_head", 0)),
            "snaps": int(aim.get("snaps", 0)),
            "acquisition_samples": len(samples),
            "acquisition_median_ms": round(statistics.median(samples), 1) if samples else None,
        },
    }


# ---------------------------------------------------------------------------
# periodic summary (tick hook)
# ---------------------------------------------------------------------------


def _track_key(player) -> str:
    connection = getattr(player, "connection", None)
    peer = getattr(connection, "peer", None)
    if peer is not None:
        try:
            from server.bans import address_host

            host = address_host(peer)
            if host and host != "unknown":
                return f"ip:{host}:{getattr(player, 'name', '')}"
        except Exception:  # noqa: BLE001
            pass
    return f"player:{getattr(player, 'id', id(player))}:{getattr(player, 'name', '')}"


def _state(server) -> dict:
    state = getattr(server, "_anticheat_report_state", None)
    if isinstance(state, dict):
        return state
    state = {
        "players": {},   # track key -> {first_seen, player}
        "archive": deque(maxlen=_ARCHIVE_LIMIT),  # (key, tool, accuracy)
        "last_summary": None,
    }
    try:
        server._anticheat_report_state = state
    except AttributeError:
        pass
    return state


def tick(server, now=None) -> list[dict]:
    """Once per second: track players; every summary interval log flags.

    Returns the flagged reports written this call (empty most seconds).
    Never raises.
    """

    try:
        return _tick(server, time.monotonic() if now is None else float(now))
    except Exception:  # noqa: BLE001
        logger.debug("anticheat report tick failed", exc_info=True)
        return []


def _tick(server, now: float) -> list[dict]:
    if not _setting(server, "report_enabled"):
        return []
    state = _state(server)
    tracks = state["players"]
    live = {}
    for player in list(getattr(server, "players", {}).values()):
        if _is_bot(player):
            continue
        key = _track_key(player)
        live[key] = player
        track = tracks.get(key)
        if track is None or track["player"] is not player:
            tracks[key] = {"first_seen": now if track is None else track["first_seen"],
                           "player": player}
    # Departed humans join the accuracy population archive.
    min_shots = int(_setting(server, "accuracy_min_shots"))
    for key in [k for k in tracks if k not in live]:
        departed = tracks.pop(key)["player"]
        for tool, (accuracy, _shots) in _weapon_accuracies(departed, min_shots).items():
            state["archive"].append((key, tool, accuracy))

    interval = max(1.0, float(_setting(server, "summary_interval_seconds")))
    if state["last_summary"] is None:
        state["last_summary"] = now
        return []
    if now - state["last_summary"] < interval:
        return []
    state["last_summary"] = now
    flagged = [r for r in analyze_all(server, now=now) if r["flagged"]]
    for report in flagged:
        logger.warning(
            "anticheat suspect player=%s name=%r score=%.2f reasons=%s",
            report["player_id"], report["name"], report["score"],
            "; ".join(f"{r['check']}: {r['detail']}" for r in report["reasons"]),
        )
    if flagged:
        _append_jsonl(server, flagged)
    return flagged


def _append_jsonl(server, reports) -> None:
    path = Path(str(_setting(server, "report_path")))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path, int(_setting(server, "report_max_bytes")),
                int(_setting(server, "report_backups")))
        config = getattr(server, "config", None)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with path.open("a", encoding="utf-8") as handle:
            for report in reports:
                player = getattr(server, "players", {}).get(report["player_id"])
                record = {
                    "ts": stamp,
                    "server": getattr(config, "name", None),
                    "port": getattr(config, "port", None),
                    "map": getattr(config, "map_name", None),
                    "key": _track_key(player) if player is not None else None,
                    **report,
                    "stats": raw_stats(player) if player is not None else None,
                }
                handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
    except OSError:
        logger.warning("anticheat report: cannot write %s", path, exc_info=True)


def _rotate(path: Path, max_bytes: int, backups: int) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if max_bytes <= 0 or size < max_bytes:
        return
    if backups <= 0:
        path.unlink(missing_ok=True)
        return
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            os.replace(source, path.with_name(f"{path.name}.{index + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))
