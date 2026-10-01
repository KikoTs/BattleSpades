"""Server-side lag compensation (target rewind) for hitscan and melee hits.

The server resolves a ShootPacket when it arrives, but the shooter aimed at
the picture its client showed. The stock 1.x client does NOT buffer remote
players for interpolation: ``GameScene.process_packet_world_update`` hands
each remote row to ``Character.set_network_position_and_velocity`` and
``Character.apply_interpolations`` snaps the remote ``world_object`` to that
network position/velocity and keeps simulating it forward (extrapolation).
The remote body the shooter sees at client time ``t`` is therefore the server
state from one downstream trip earlier, and the shot needs one upstream trip
to arrive. Relative to the server tick that processes the shot, the shooter
saw the world roughly one full RTT ago (not RTT/2).

``GameScene.send_shoot_packet`` (gameScene.pyd 0x101702A0, source line 2753)
fills ``shot_on_world_update`` from ``scene.last_world_update``, which
``process_packet_world_update`` stores from the WorldUpdate HEADER loop
(0x10182C93). Our header is ``server.loop_count`` of the tick whose end
state the snapshot carries (``server/replication.py``). Because the client
extrapolates forward from that snapshot, the view is never OLDER than it:
``shot_on_world_update`` is a lower bound on the view tick. The rewind is

    view_age = min(RTT + view_delay, age of the claimed snapshot)
    rewind   = clamp(view_age, 0, min(RTT + extra, max))

so a client claiming an ancient snapshot gains nothing (the RTT term bounds
it) and a genuine one corrects an over-estimated RTT (ENet starts every peer
at a 500 ms default before real samples exist).

Only HITBOXES are rewound: :meth:`RewindContext.body` returns a read-only
view of a target at the rewind tick (position, aim yaw, crouch). The live
Player, its native world object and all physics state are never touched, so
there is nothing to restore. Terrain / line of sight stays on the current
world (terrain history is not kept). Damage, knockback, riot-shield facing
and every other effect keep using the live target.

History is recorded by ``Player.simulate_tick`` (first thing each tick, for
living and dead players, humans and bots), labelled ``server.loop_count - 1``:
the state the previous tick's WorldUpdate published. A shot handled during
the packet drain of tick ``L`` therefore sees the live body as label ``L-1``
(the newest published state) and history for older labels.

Never rewound: across a death/respawn (``replication_generation`` changes),
across a teleport (a per-tick jump above ``TELEPORT_BLOCKS_PER_TICK`` starts a
new epoch), to a sample in which the target was dead, or for bot shooters
(no RTT, their aim is computed on current state).

Config keys (read with getattr, defaults in brackets):

* ``lag_compensation_enabled`` [True]
* ``lag_compensation_max_ms`` [250] absolute rewind cap
* ``lag_compensation_extra_ms`` [50] allowance above the measured RTT
* ``lag_compensation_view_delay_ms`` [0] extra client render delay; the
  retail client extrapolates remotes, so 0 is the measured-contract value
* ``lag_compensation_late_shot_ms`` [400] most extra rewind for a shot that
  arrived late because its datagram was retransmitted (``late_shot_ms``)

See docs/LAG_COMPENSATION.md.
"""

from __future__ import annotations

import contextlib
import math
from typing import Optional

# Ring capacity in ticks (power of two, ~2.1 s at 60 Hz). The default rewind
# (250 ms cap + 400 ms late-shot allowance) is far below this; the slack
# covers tick-rate overrides up to 120 Hz at those defaults.
HISTORY_TICKS = 128
_MASK = HISTORY_TICKS - 1

# Legit bodies never move this far in one tick (sprint ~0.2, terminal fall
# ~1, rocket/explosion knockback well under 3). A larger jump is a teleport:
# no rewind may interpolate across it.
TELEPORT_BLOCKS_PER_TICK = 5.0

DEFAULT_MAX_MS = 250.0
DEFAULT_EXTRA_MS = 50.0
DEFAULT_VIEW_DELAY_MS = 0.0
# Extra rewind for a shot whose reliable datagram was retransmitted (see
# late_shot_ms): about one ENet retransmission time-out at 300 ms ping.
DEFAULT_LATE_SHOT_MS = 400.0
LATE_SEARCH_FRAMES = 8

# Below half a tick there is nothing to rewind.
_MIN_REWIND_TICKS = 0.5

# Sample tuple layout.
_TICK, _X, _Y, _Z, _OX, _OY, _OZ, _CROUCH, _LIVE, _LIFE, _EPOCH = range(11)


class TargetHistory:
    """Fixed ring of per-tick hitbox samples for one player."""

    __slots__ = ("ring", "last", "epoch")

    def __init__(self) -> None:
        self.ring: list = [None] * HISTORY_TICKS
        self.last: Optional[tuple] = None
        self.epoch: int = 0

    def get(self, tick: int) -> Optional[tuple]:
        sample = self.ring[tick & _MASK]
        if sample is None or sample[_TICK] != tick:
            return None
        return sample


def _life(player) -> int:
    return int(getattr(player, "replication_generation", 0))


def record_player(player, tick: Optional[int] = None) -> None:
    """Append ``player``'s current hitbox state under label ``tick``.

    Called once per tick per player from ``Player.simulate_tick`` (tick =
    ``server.loop_count - 1``). Never raises: history is best effort and must
    not break simulation.
    """

    try:
        if tick is None:
            tick = player.connection.server.loop_count - 1
        history = getattr(player, "_lag_history", None)
        if history is None:
            history = TargetHistory()
            player._lag_history = history
        x = player.x
        y = player.y
        z = player.z
        live = bool(player.alive and player.spawned)
        life = _life(player)
        last = history.last
        if last is not None and last[_LIFE] == life and last[_TICK] < tick:
            dx = x - last[_X]
            dy = y - last[_Y]
            dz = z - last[_Z]
            limit = TELEPORT_BLOCKS_PER_TICK * (tick - last[_TICK])
            if dx * dx + dy * dy + dz * dz > limit * limit:
                history.epoch += 1
        sample = (
            tick, x, y, z, player.o_x, player.o_y, player.o_z,
            bool(player.hitbox_crouched) if live else False,
            live, life, history.epoch,
        )
        history.ring[tick & _MASK] = sample
        history.last = sample
    except Exception:  # noqa: BLE001 - history must never break a tick
        return


def forget_player(player) -> None:
    """Drop a player's history (e.g. on map change)."""

    try:
        player._lag_history = None
    except AttributeError:
        pass


class RewoundBody:
    """Read-only hitbox view of a target at the rewind tick.

    Exposes what the hitscan code reads (``x/y/z``, ``orientation``,
    ``hitbox_crouched``); everything else (``class_id``, ``input``, ``id``,
    ``team``...) is delegated to the live player.
    """

    __slots__ = ("_target", "x", "y", "z", "o_x", "o_y", "o_z",
                 "hitbox_crouched", "rewound_ticks")

    def __init__(self, target, x, y, z, o_x, o_y, o_z, crouched, ticks):
        self._target = target
        self.x = x
        self.y = y
        self.z = z
        self.o_x = o_x
        self.o_y = o_y
        self.o_z = o_z
        self.hitbox_crouched = crouched
        self.rewound_ticks = ticks

    @property
    def orientation(self):
        return (self.o_x, self.o_y, self.o_z)

    @property
    def position(self):
        return (self.x, self.y, self.z)

    @property
    def target(self):
        return self._target

    def __getattr__(self, name):
        return getattr(self._target, name)


class RewindContext:
    """One shot's rewind decision; hands out rewound hitbox views."""

    __slots__ = ("current_tick", "view_tick", "rewind_ms", "allowed_ms",
                 "rtt_ms", "snapshot_tick", "_bodies")

    def __init__(self, current_tick, view_tick, rewind_ms, allowed_ms,
                 rtt_ms, snapshot_tick):
        self.current_tick = current_tick
        self.view_tick = view_tick
        self.rewind_ms = rewind_ms
        self.allowed_ms = allowed_ms
        self.rtt_ms = rtt_ms
        self.snapshot_tick = snapshot_tick
        self._bodies: dict = {}

    def body(self, target):
        """Return the hitbox to test for ``target`` (the live player when no
        valid rewind exists)."""

        key = id(target)
        cached = self._bodies.get(key)
        if cached is None:
            cached = _rewound_body(target, self.current_tick, self.view_tick)
            self._bodies[key] = cached
        return cached


def _valid(sample, life, epoch) -> bool:
    return (
        sample is not None
        and sample[_LIVE]
        and sample[_LIFE] == life
        and sample[_EPOCH] == epoch
    )


def _live_sample(target, tick, epoch) -> tuple:
    return (
        tick, target.x, target.y, target.z, target.o_x, target.o_y,
        target.o_z, bool(target.hitbox_crouched), True, _life(target), epoch,
    )


def _rewound_body(target, current_tick: int, view_tick: float):
    history = getattr(target, "_lag_history", None)
    if history is None or history.last is None:
        return target
    if not (target.alive and target.spawned):
        return target
    life = _life(target)
    last = history.last
    epoch = history.epoch
    if last[_LIFE] != life:
        # Respawned since the newest sample: every sample is a previous life.
        return target
    # The live body must be continuous with the newest sample (a teleport
    # after the last record would otherwise interpolate across it).
    gap = max(1, current_tick - last[_TICK])
    dx = target.x - last[_X]
    dy = target.y - last[_Y]
    dz = target.z - last[_Z]
    limit = TELEPORT_BLOCKS_PER_TICK * gap
    if dx * dx + dy * dy + dz * dz > limit * limit:
        return target

    def sample_at(tick):
        if tick >= current_tick:
            return _live_sample(target, current_tick, epoch)
        return history.get(tick)

    base = math.floor(view_tick)
    frac = view_tick - base
    first = sample_at(base)
    second = sample_at(base + 1) if frac > 1e-6 else None
    first_ok = _valid(first, life, epoch)
    second_ok = _valid(second, life, epoch)
    if first_ok and second_ok:
        a, b = first, second
        x = a[_X] + (b[_X] - a[_X]) * frac
        y = a[_Y] + (b[_Y] - a[_Y]) * frac
        z = a[_Z] + (b[_Z] - a[_Z]) * frac
        near = b if frac >= 0.5 else a
        o_x = a[_OX] + (b[_OX] - a[_OX]) * frac
        o_y = a[_OY] + (b[_OY] - a[_OY]) * frac
        o_z = a[_OZ] + (b[_OZ] - a[_OZ]) * frac
        norm = math.sqrt(o_x * o_x + o_y * o_y + o_z * o_z)
        if norm > 1e-6:
            o_x, o_y, o_z = o_x / norm, o_y / norm, o_z / norm
        else:
            o_x, o_y, o_z = near[_OX], near[_OY], near[_OZ]
        crouched = near[_CROUCH]
    elif first_ok or second_ok:
        near = first if first_ok else second
        x, y, z = near[_X], near[_Y], near[_Z]
        o_x, o_y, o_z = near[_OX], near[_OY], near[_OZ]
        crouched = near[_CROUCH]
    else:
        # Dead, previous life, pre-teleport, or evicted at the view tick:
        # the shooter could not have been aiming at this body.
        return target
    return RewoundBody(
        target, x, y, z, o_x, o_y, o_z, crouched,
        float(current_tick) - float(view_tick),
    )


def _setting(server, name: str, default):
    return getattr(getattr(server, "config", None), name, default)


def shooter_rtt_ms(player) -> float:
    """Measured round trip (ms) of a human shooter; 0 for bots / unknown.

    ENet's smoothed ``peer.roundTripTime`` (the same value the health log
    prints). There is no application-level ping in the 1.x protocol: the
    client's ClockSync measures RTT locally and never reports it.
    """

    if bool(getattr(player, "is_bot", False)):
        return 0.0
    peer = getattr(getattr(player, "connection", None), "peer", None)
    try:
        rtt = float(getattr(peer, "roundTripTime", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(rtt) or rtt < 0.0:
        return 0.0
    return rtt


def current_tick(server) -> int:
    """Label of the newest published state (see module docstring)."""

    return int(getattr(server, "loop_count", 0)) - 1


def rewind_targets(server, shooter, packet) -> Optional[RewindContext]:
    """Decide how far to rewind target hitboxes for one shot.

    Returns ``None`` when no rewind applies (disabled, bot shooter, no RTT,
    sub-tick rewind); otherwise a :class:`RewindContext`. Logs (log-only)
    claimed snapshots older than the allowed window.
    """

    if not bool(_setting(server, "lag_compensation_enabled", True)):
        return None
    if bool(getattr(shooter, "is_bot", False)):
        return None
    rtt_ms = shooter_rtt_ms(shooter)
    if rtt_ms <= 0.0:
        return None

    tick_seconds = float(getattr(server, "tick_interval", 0.0) or 0.0)
    if tick_seconds <= 0.0:
        tick_seconds = 1.0 / 60.0
    tick_ms = tick_seconds * 1000.0
    now = current_tick(server)

    max_ms = max(0.0, float(_setting(
        server, "lag_compensation_max_ms", DEFAULT_MAX_MS)))
    extra_ms = max(0.0, float(_setting(
        server, "lag_compensation_extra_ms", DEFAULT_EXTRA_MS)))
    view_delay_ms = max(0.0, float(_setting(
        server, "lag_compensation_view_delay_ms", DEFAULT_VIEW_DELAY_MS)))
    allowed_ms = min(rtt_ms + extra_ms, max_ms)
    # A shot whose reliable datagram was lost arrives one ENet retransmission
    # later than the frame that produced it; the unsequenced ClientData of
    # that frame dates it (late_shot_ms). That delay is added to the view
    # age and to the allowance, bounded by lag_compensation_late_shot_ms.
    late_ms = late_shot_ms(server, shooter, packet, tick_ms)
    allowed_ms += late_ms
    # The round trip overstates the view age by about 12 ms (half a tick of
    # wait before the shot is handled plus the client's socket poll inside
    # every acknowledgement). That is deliberate: measured over 10,000 shots
    # per setting (scripts/lag_compensation_link.py), removing it halves the
    # mean position error but registers fewer shots, because the misses are
    # direction changes, where the client's extrapolation overshoots and a
    # slightly older body is the nearer one.
    desired_ms = rtt_ms + view_delay_ms + late_ms

    snapshot = None
    try:
        claimed = int(getattr(packet, "shot_on_world_update", 0) or 0)
    except (TypeError, ValueError):
        claimed = 0
    if claimed > 0:
        if claimed > now + 1:
            _report(server, shooter, "lag_comp_future_snapshot",
                    claimed=claimed, now=now)
        else:
            snapshot = min(claimed, now)
            snapshot_age_ms = (now - snapshot) * tick_ms
            # The client extrapolates forward from the snapshot, so the view
            # is never older than it.
            desired_ms = min(desired_ms, snapshot_age_ms)
            interval = max(1, int(_setting(
                server, "worldupdate_broadcast_interval", 2)))
            slack_ms = (2 * interval + 1) * tick_ms
            if snapshot_age_ms > allowed_ms + slack_ms:
                _report(server, shooter, "lag_comp_stale_snapshot",
                        claimed_ms=round(snapshot_age_ms, 1),
                        allowed_ms=round(allowed_ms, 1),
                        rtt_ms=round(rtt_ms, 1))
    if late_ms > 0.0:
        # Log-only: an honest link shows this at about its loss rate; a
        # client that holds every shot back ("backtrack") shows it always.
        _report(server, shooter, "lag_comp_late_shot",
                late_ms=round(late_ms, 1), rtt_ms=round(rtt_ms, 1))

    rewind_ms = max(0.0, min(desired_ms, allowed_ms))
    if rewind_ms < _MIN_REWIND_TICKS * tick_ms:
        return None
    view_tick = now - rewind_ms / tick_ms
    try:
        shooter.last_lag_rewind_ms = rewind_ms
    except AttributeError:
        pass
    return RewindContext(now, view_tick, rewind_ms, allowed_ms, rtt_ms,
                         snapshot)


def late_shot_ms(server, shooter, packet, tick_ms: float) -> float:
    """How much later than its own client frame an action packet arrived.

    ``Player.label_arrival_tick`` dates every ClientData label by the server
    tick it arrived on. ClientData is unsequenced, so it is never held behind
    a lost reliable packet, while the ShootPacket of the same frame is: when
    that datagram is lost the shot arrives one retransmission time-out late
    (hundreds of ms) and, rewound by the round trip alone, misses the moving
    body the shooter saw. When the shot's own ClientData was lost with it,
    the nearest label that arrived (within ``LATE_SEARCH_FRAMES``) dates it.
    One tick of ordinary drain/phase skew is ignored. Bounded by
    ``lag_compensation_late_shot_ms``; 0 when nothing dates the label.
    """

    cap = max(0.0, float(_setting(
        server, "lag_compensation_late_shot_ms", DEFAULT_LATE_SHOT_MS)))
    if cap <= 0.0:
        return 0.0
    arrival_of = getattr(shooter, "label_arrival_tick", None)
    if not callable(arrival_of):
        return 0.0
    try:
        label = int(getattr(packet, "loop_count", None))
        loop_now = int(getattr(server, "loop_count", 0))
    except (TypeError, ValueError):
        return 0.0
    estimate = None
    for offset in range(LATE_SEARCH_FRAMES + 1):
        for candidate in ((label,) if offset == 0 else (label - offset, label + offset)):
            arrived = arrival_of(candidate)
            if arrived is not None:
                estimate = int(arrived) - (candidate - label)
                break
        if estimate is not None:
            break
    if estimate is None:
        return 0.0
    late_ticks = loop_now - estimate - 1
    if late_ticks <= 0:
        return 0.0
    return min(cap, late_ticks * float(tick_ms))


def _report(server, shooter, kind: str, **detail) -> None:
    try:
        from server import anticheat

        anticheat.report(server, shooter, kind, enforced=False, **detail)
    except Exception:  # noqa: BLE001 - diagnostics must not break combat
        pass


def body_for(owner, target):
    """Hitbox to test for ``target`` under ``owner._lag_rewind`` (if any)."""

    rewind = getattr(owner, "_lag_rewind", None)
    return target if rewind is None else rewind.body(target)


@contextlib.contextmanager
def compensated(owner, server, shooter, packet):
    """Install a rewind on ``owner`` (the CombatRuntime) for one shot.

    ``owner._lag_rewind`` is read by the player-hit loop through
    :func:`body_for`; it is always cleared on exit.
    """

    previous = getattr(owner, "_lag_rewind", None)
    owner._lag_rewind = rewind_targets(server, shooter, packet)
    try:
        yield owner._lag_rewind
    finally:
        owner._lag_rewind = previous
