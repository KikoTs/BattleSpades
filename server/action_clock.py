"""Cadence of client actions, measured on the client's own clock.

The server used to time every action (shot, swing, throw, block) by the
moment its packet ARRIVED. Arrival time is the client's cadence plus the
link: jitter moves each packet by tens of milliseconds, and every action
packet is ENet-reliable, so one lost datagram holds all later ones back and
then releases them in the same tick. Measured in the lab
(scripts/anticheat_lab), a legitimate player lost 23 % of his shots at
80 ms +/-30 ms and 31 % at 250 ms to the arrival-time cadence check.

Every action packet carries ``loop_count``: the label of the client frame
that produced it. The stock client advances that label once per 60 Hz
update, so two actions of one tool can never be closer than the tool's
interval in LABELS, whatever the link does to their packets. An action is
therefore admitted when

* its label is at least the interval (in frames, rounded down) after the
  previous admitted action's label, and the label is one the client can have
  reached (not ahead of its own input stream), OR
* its arrival is at least the interval after the previous one (the old rule,
  which still covers clients whose labels cannot be trusted),

and, in both cases, a token bucket in SERVER time still has a token. The
bucket refills at the tool's real rate and holds as many actions as a link
stall can bunch together, so forged labels buy a few early actions once and
never a higher rate.

Nothing here reports or kicks; callers decide what a refusal means.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

TICK_RATE = 60.0
# One tick of arrival jitter against the arrival schedule (the old grace).
ARRIVAL_GRACE = 1.0 / 60.0
# An action frame may lead the newest ClientData label by this much: the
# native client sends its actions one tick after the frame they describe and
# the datagram carrying that frame's ClientData may be the one that was lost.
LABEL_LEAD_FRAMES = 3
# A backward label jump larger than the stock ClockSync dead band is the
# client rewriting its loop clock, not an action from the past.
RELABEL_FRAMES = 10
# Link stall a bucket must absorb when the link's round trip is unknown, and
# its bounds. ENet resends a lost reliable packet after about one round trip
# plus four variances and doubles that on every further loss.
DEFAULT_STALL_SECONDS = 0.35
MIN_STALL_SECONDS = 0.25
MAX_STALL_SECONDS = 1.5
MIN_BUCKET = 2.0
MAX_BUCKET = 32.0


def link_stall_seconds(player) -> float:
    """How long this player's link can hold reliable packets back.

    Two ENet retransmission time-outs (round trip + 4 variances, as ENet
    computes it) cover a packet lost twice in a row; bounded both ways.
    """

    peer = getattr(getattr(player, "connection", None), "peer", None)
    if peer is None:
        return DEFAULT_STALL_SECONDS
    try:
        rtt = float(getattr(peer, "roundTripTime", 0) or 0)
        variance = float(getattr(peer, "roundTripTimeVariance", 0) or 0)
    except (TypeError, ValueError):
        return DEFAULT_STALL_SECONDS
    if not (math.isfinite(rtt) and math.isfinite(variance)) or rtt < 0.0:
        return DEFAULT_STALL_SECONDS
    timeout = (rtt + 4.0 * max(0.0, variance)) / 1000.0
    return max(MIN_STALL_SECONDS, min(MAX_STALL_SECONDS, 2.0 * timeout + 0.1))


def interval_frames(interval: float) -> int:
    """Fewest client frames between two actions ``interval`` seconds apart."""

    return max(1, int(math.floor(float(interval) * TICK_RATE + 1e-6)))


@dataclass
class Lane:
    """Cadence state of one kind of action of one player."""

    label: Optional[int] = None
    interval: float = 0.0
    due: float = 0.0
    tokens: float = -1.0
    token_interval: float = 0.0
    refilled_at: float = 0.0
    admitted: int = 0
    refused: int = 0
    by_label: int = 0


def lanes(player) -> dict:
    existing = getattr(player, "_action_lanes", None)
    if isinstance(existing, dict):
        return existing
    created: dict = {}
    try:
        player._action_lanes = created
    except AttributeError:
        pass
    return created


def reset(player, key=None) -> None:
    """Forget cadence state on a new life, or one lane for an explicit reset."""

    table = lanes(player)
    if key is None:
        table.clear()
    else:
        table.pop(key, None)


def label_plausible(player, label) -> bool:
    """Whether the client can have reached frame ``label`` by now."""

    if label is None:
        return False
    try:
        label = int(label)
    except (TypeError, ValueError, OverflowError):
        return False
    if label < 0:
        return False
    newest = getattr(player, "_input_newest_label", None)
    if newest is None:
        newest = getattr(player, "last_applied_input_loop", None)
    if newest is None:
        return False
    return label <= int(newest) + LABEL_LEAD_FRAMES


def _capacity(player, interval: float) -> float:
    interval = max(1e-3, float(interval))
    return max(MIN_BUCKET, min(
        MAX_BUCKET, 1.0 + math.ceil(link_stall_seconds(player) / interval)
    ))


def peek(player, key, *, label, interval: float, now: float,
         min_frames: Optional[int] = None,
         grace: float = ARRIVAL_GRACE) -> bool:
    """Whether :func:`admit` would accept, without changing anything."""

    return _decide(player, key, label, interval, now, min_frames, grace)[0]


def admit(player, key, *, label, interval: float, now: float,
          min_frames: Optional[int] = None,
          grace: float = ARRIVAL_GRACE) -> bool:
    """Admit one action of lane ``key`` and advance the lane on success."""

    ok, lane, by_label, tokens = _decide(
        player, key, label, interval, now, min_frames, grace
    )
    if lane is None:
        return ok
    lanes(player)[key] = lane
    lane.tokens = tokens
    lane.token_interval = max(0.0, float(interval))
    lane.refilled_at = float(now)
    if not ok:
        lane.refused += 1
        return False
    lane.tokens = max(0.0, tokens - 1.0)
    lane.admitted += 1
    if by_label:
        lane.by_label += 1
    interval = float(interval)
    lane.interval = interval
    due = lane.due
    if due <= 0.0 or now >= due:
        lane.due = float(now) + interval
    else:
        lane.due = due + interval
    if label is not None and label_plausible(player, label):
        lane.label = int(label)
    else:
        lane.label = None
    return True


def _decide(player, key, label, interval, now, min_frames, grace):
    if player is None:
        return True, None, False, 0.0
    table = lanes(player)
    lane = table.get(key)
    if lane is None:
        lane = Lane()
    interval = max(0.0, float(interval))
    now = float(now)
    capacity = _capacity(player, interval) if interval > 0.0 else MAX_BUCKET
    if lane.tokens < 0.0:
        tokens = capacity
    else:
        elapsed = max(0.0, now - lane.refilled_at)
        earned = elapsed / interval if interval > 0.0 else capacity
        # Preserve time credit if the tool's interval changes (weapon swap,
        # minigun spin-up). A fast-tool token is not a whole slow-tool shot.
        previous_interval = lane.token_interval or interval
        carried = lane.tokens * previous_interval / interval if interval > 0.0 else capacity
        tokens = min(capacity, carried + earned)
    arrival_ok = now + float(grace) >= lane.due
    by_label = False
    if not arrival_ok and lane.label is not None and label_plausible(player, label):
        gap = int(label) - int(lane.label)
        # The previous action owns its cooldown. Switching from a slow tool
        # to a faster one must not shorten a cooldown already in progress.
        spacing = lane.interval if lane.interval > 0.0 else interval
        frames = interval_frames(spacing) if min_frames is None else int(min_frames)
        if gap >= frames:
            by_label = True
        elif gap < -RELABEL_FRAMES:
            # The client rewrote its clock backwards between the two actions;
            # its labels say nothing about their spacing. Half the interval
            # of real time is the least a relabel can hide.
            by_label = now + 0.5 * interval >= lane.due
    ok = (arrival_ok or by_label) and tokens + 1e-9 >= 1.0
    return ok, lane, by_label and not arrival_ok, tokens
