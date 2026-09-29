"""Per-player anti-cheat accounting shared by every validation point.

Validation code calls :func:`report` whenever client data fails a check. The
call is cheap (a counter bump); logging is rate-limited per player and kind so
a flood of forged packets never floods the log. :func:`protocol_violation`
is for data the stock retail client can never produce (NaN floats, packets
it never sends): it kicks with ``ERROR_KICK_HACKING`` when
``[anticheat] kick_on_protocol_violation`` is on.

Checks that could misfire on a legitimate client under packet loss consult
:func:`enforcing` and stay log-only until their ``enforce_*`` switch is set.
"""

from __future__ import annotations

import logging
import time
from collections import Counter

import shared.constants as C

logger = logging.getLogger("anticheat")

# One log line per (player, kind) at most this often; the counters keep the
# exact totals in between.
_LOG_INTERVAL_SECONDS = 10.0


def _config(server):
    return getattr(getattr(server, "config", None), "anticheat", None)


def enforcing(server, switch: str) -> bool:
    """Whether ``[anticheat] <switch>`` (an ``enforce_*`` flag) is on."""

    config = _config(server)
    return bool(getattr(config, switch, False)) if config is not None else False


def setting(server, name: str, default):
    """An ``[anticheat]`` value with a fallback for minimal test servers."""

    config = _config(server)
    return getattr(config, name, default) if config is not None else default


def counters(player) -> Counter:
    """The player's per-kind violation counts (created on first use)."""

    existing = getattr(player, "anticheat_counts", None)
    if isinstance(existing, Counter):
        return existing
    created = Counter()
    try:
        player.anticheat_counts = created
    except AttributeError:
        pass
    return created


def report(server, player, kind: str, *, enforced: bool = True, **detail) -> None:
    """Count one failed check and log it (rate-limited).

    ``enforced`` records whether the action was rejected (True) or only
    observed in log-only mode (False); both are counted separately so a
    log-only session shows exactly what enforcement would have rejected.
    """

    if player is None:
        return
    key = kind if enforced else f"{kind}:observed"
    counts = counters(player)
    counts[key] += 1
    now = time.monotonic()
    stamps = getattr(player, "_anticheat_logged_at", None)
    if not isinstance(stamps, dict):
        stamps = {}
        try:
            player._anticheat_logged_at = stamps
        except AttributeError:
            return
    if now - stamps.get(key, -1e9) < _LOG_INTERVAL_SECONDS:
        return
    stamps[key] = now
    details = " ".join(f"{name}={value}" for name, value in sorted(detail.items()))
    logger.warning(
        "anticheat %s player=%s name=%r count=%d %s%s",
        key,
        getattr(player, "id", "?"),
        getattr(player, "name", ""),
        counts[key],
        "" if enforced else "(log-only) ",
        details,
    )


def protocol_violation(server, player, kind: str, **detail) -> bool:
    """Data the stock client cannot send. Returns True when the player was kicked."""

    report(server, player, f"protocol:{kind}", **detail)
    if player is None or getattr(player, "is_bot", False):
        return False
    if not setting(server, "kick_on_protocol_violation", True):
        return False
    logger.warning(
        "anticheat kick player=%s name=%r reason=%s",
        getattr(player, "id", "?"), getattr(player, "name", ""), kind,
    )
    disconnect = getattr(player, "disconnect", None)
    if callable(disconnect):
        disconnect(int(C.DISCONNECT.ERROR_KICK_HACKING))
        return True
    return False


def summary(player) -> dict[str, int]:
    """A copy of the player's counters for admin reports."""

    return dict(counters(player))
