"""Hit registration under a simulated link (headless, deterministic).

A shooter with ping does not see the server's present. This model replays
what the stock client shows and asks the real server code whether the shot
registers on the body the shooter aimed at.

Timeline (milliseconds, 60 Hz server):

* tick ``k`` publishes the state at ``k * tick`` in a WorldUpdate whose
  header loop is ``k`` (30 Hz); it reaches the shooter one downstream trip
  later, or never;
* the shooter's client snaps a remote body to the newest snapshot it has and
  keeps simulating it forward (stock ``Character.apply_interpolations``), so
  at fire time the body stands at that snapshot's state advanced by the time
  since it arrived. The model advances position, velocity and acceleration
  of the snapshot, which is exact inside a movement segment and wrong across
  a change the snapshot could not know (a turn, a take-off, a landing);
* the shot is a reliable packet: one upstream trip, plus a retransmission
  time-out for every loss;
* the server handles it at the next tick start with the real
  ``lag_compensation.rewind_targets`` and the real hitbox test, using ENet's
  smoothed round trip (modelled with ENet's own update rule).

    py -3.12 scripts/lag_compensation_link.py

prints the table for 50 / 100 / 200 ms, +-20 ms jitter, 0.5 and 2 % loss and
the four movement patterns.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TICK_MS = 1000.0 / 60.0
SNAPSHOT_INTERVAL_TICKS = 2
# The stock client services its socket every 10 ms.
CLIENT_POLL_MS = 10.0
EYE_Z = 59.75
AIM_HEIGHT = 0.8

# Movement in blocks and seconds (z grows downwards). Sprint is the measured
# 7.5 blocks/s; jump and gravity are the native mover's 0.36 impulse and unit
# gravity scaled by 32 blocks per velocity unit.
SPRINT = 7.5
JUMP_SPEED = 11.5
GRAVITY = 32.0
JETPACK_LIFT = 20.0
PARACHUTE_SINK = 3.0


# ---------------------------------------------------------------------------
# Target movement
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    start: float  # seconds
    position: tuple
    velocity: tuple
    acceleration: tuple = (0.0, 0.0, 0.0)


class Path:
    """Piecewise constant-acceleration motion."""

    def __init__(self, segments):
        self.segments = sorted(segments, key=lambda item: item.start)

    def _segment(self, seconds):
        chosen = self.segments[0]
        for segment in self.segments:
            if segment.start <= seconds:
                chosen = segment
            else:
                break
        return chosen

    def state(self, seconds):
        """Return ``(position, velocity, acceleration)`` at ``seconds``."""
        segment = self._segment(seconds)
        elapsed = max(0.0, seconds - segment.start)
        position = tuple(
            p + v * elapsed + 0.5 * a * elapsed * elapsed
            for p, v, a in zip(
                segment.position, segment.velocity, segment.acceleration
            )
        )
        velocity = tuple(
            v + a * elapsed
            for v, a in zip(segment.velocity, segment.acceleration)
        )
        return position, velocity, segment.acceleration


def _chain(pieces, origin):
    """Build a Path from ``(duration, velocity, acceleration)`` pieces."""
    segments = []
    clock = 0.0
    position = origin
    for duration, velocity, acceleration in pieces:
        segments.append(Segment(clock, position, velocity, acceleration))
        position = tuple(
            p + v * duration + 0.5 * a * duration * duration
            for p, v, a in zip(position, velocity, acceleration)
        )
        clock += duration
    return Path(segments)


def strafe_path(seconds, origin=(95.5, 115.5, EYE_Z)):
    pieces = []
    direction = 1.0
    clock = 0.0
    while clock < seconds:
        pieces.append((0.6, (direction * SPRINT, 0.0, 0.0), (0.0, 0.0, 0.0)))
        direction = -direction
        clock += 0.6
    return _chain(pieces, origin)


def jump_path(seconds, origin=(95.5, 115.5, EYE_Z)):
    """Sprint along x, reversing each second, jumping on every landing."""
    flight = 2.0 * JUMP_SPEED / GRAVITY
    pieces = []
    clock = 0.0
    direction = 1.0
    while clock < seconds:
        pieces.append((0.25, (direction * SPRINT, 0.0, 0.0), (0.0, 0.0, 0.0)))
        pieces.append((
            flight,
            (direction * SPRINT, 0.0, -JUMP_SPEED),
            (0.0, 0.0, GRAVITY),
        ))
        clock += 0.25 + flight
        direction = -direction
    return _chain(pieces, origin)


def jetpack_path(seconds, origin=(95.5, 115.5, EYE_Z)):
    """One-second burns with a ballistic arc back to the start height."""
    pieces = []
    clock = 0.0
    direction = 1.0
    burn = 1.0
    rise = JETPACK_LIFT * burn
    height = 0.5 * JETPACK_LIFT * burn * burn
    # Ballistic from upward speed ``rise`` at ``height`` above the start.
    fall = (rise + math.sqrt(rise * rise + 2.0 * GRAVITY * height)) / GRAVITY
    while clock < seconds:
        pieces.append((0.3, (direction * SPRINT, 0.0, 0.0), (0.0, 0.0, 0.0)))
        pieces.append((
            burn, (direction * SPRINT, 0.0, 0.0), (0.0, 0.0, -JETPACK_LIFT)
        ))
        pieces.append((
            fall, (direction * SPRINT, 0.0, -rise), (0.0, 0.0, GRAVITY)
        ))
        clock += 0.3 + burn + fall
        direction = -direction
    return _chain(pieces, origin)


def parachute_path(seconds, origin=(95.5, 115.5, EYE_Z - 30.0)):
    """Free fall, canopy opens, slow drifting descent, repeated."""
    pieces = []
    clock = 0.0
    direction = 1.0
    drop = 0.5
    glide = 1.5
    while clock < seconds:
        pieces.append((drop, (direction * SPRINT, 0.0, 0.0), (0.0, 0.0, GRAVITY)))
        pieces.append((
            glide, (direction * SPRINT * 0.5, 0.0, PARACHUTE_SINK),
            (0.0, 0.0, 0.0),
        ))
        # Climb back (a respawn would be a teleport; keep one continuous life).
        rise_time = 1.0
        fallen = 0.5 * GRAVITY * drop * drop + PARACHUTE_SINK * glide
        pieces.append((
            rise_time, (0.0, 0.0, -fallen / rise_time), (0.0, 0.0, 0.0)
        ))
        clock += drop + glide + rise_time
        direction = -direction
    return _chain(pieces, origin)


PATTERNS = {
    "strafe": strafe_path,
    "jump": jump_path,
    "jetpack": jetpack_path,
    "parachute": parachute_path,
}


# ---------------------------------------------------------------------------
# Link and ENet round trip
# ---------------------------------------------------------------------------


@dataclass
class LinkProfile:
    rtt_ms: float = 100.0
    jitter_ms: float = 20.0  # uniform +-, per datagram and direction
    loss: float = 0.01

    @property
    def one_way_ms(self):
        return self.rtt_ms / 2.0


class Link:
    def __init__(self, profile: LinkProfile, rng: random.Random):
        self.profile = profile
        self.rng = rng

    def transit(self):
        """One-way delay of a datagram in ms, or None when it is lost."""
        if self.profile.loss > 0.0 and self.rng.random() < self.profile.loss:
            return None
        jitter = self.profile.jitter_ms
        extra = self.rng.uniform(-jitter, jitter) if jitter > 0.0 else 0.0
        return max(0.0, self.profile.one_way_ms + extra)


class EnetRoundTrip:
    """ENet's smoothed round trip (enet_protocol_handle_acknowledge)."""

    def __init__(self):
        self.mean = 500  # ENET_PEER_DEFAULT_ROUND_TRIP_TIME
        self.variance = 0

    def sample(self, rtt_ms):
        rtt = max(1, int(round(rtt_ms)))
        self.variance -= self.variance // 4
        if rtt >= self.mean:
            self.mean += (rtt - self.mean) // 8
            self.variance += (rtt - self.mean) // 4
        else:
            self.mean -= (self.mean - rtt) // 8
            self.variance += (self.mean - rtt) // 4

    @property
    def retransmit_ms(self):
        return self.mean + 4 * self.variance


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


@dataclass
class Shot:
    fired_ms: float
    arrival_ms: float
    snapshot: int
    view: tuple
    view_server_ms: float
    retransmissions: int


@dataclass
class Result:
    shots: int = 0
    hits: int = 0
    hits_uncompensated: int = 0
    view_errors: list = field(default_factory=list)
    rewinds_ms: list = field(default_factory=list)
    ideal_ms: list = field(default_factory=list)
    cover_blocks: list = field(default_factory=list)
    retransmitted: int = 0
    retransmitted_hits: int = 0

    def summary(self):
        def mean(values):
            return sum(values) / len(values) if values else 0.0

        def percentile(values, share):
            if not values:
                return 0.0
            ordered = sorted(values)
            return ordered[min(len(ordered) - 1, int(len(ordered) * share))]

        first_try = self.shots - self.retransmitted
        return {
            "shots": self.shots,
            "hit_rate": self.hits / self.shots if self.shots else 0.0,
            "hit_rate_uncompensated": (
                self.hits_uncompensated / self.shots if self.shots else 0.0
            ),
            "hit_rate_first_try": (
                (self.hits - self.retransmitted_hits) / first_try
                if first_try else 0.0
            ),
            "retransmitted": self.retransmitted,
            "retransmitted_hits": self.retransmitted_hits,
            "view_error_mean": mean(self.view_errors),
            "view_error_p95": percentile(self.view_errors, 0.95),
            "view_error_max": max(self.view_errors, default=0.0),
            "rewind_mean_ms": mean(self.rewinds_ms),
            "rewind_max_ms": max(self.rewinds_ms, default=0.0),
            "rewind_error_mean_ms": mean([
                abs(a - b) for a, b in zip(self.rewinds_ms, self.ideal_ms)
            ]),
            "cover_mean_blocks": mean(self.cover_blocks),
            "cover_max_blocks": max(self.cover_blocks, default=0.0),
        }


class _Connection:
    def __init__(self, server):
        self.server = server
        self.player = None
        self.in_game = True
        self.sent = []
        self.reserved_player_id = None
        self.peer = None

    def send(self, data, reliable=True, prefix=0x30, **_kwargs):
        self.sent.append((data, reliable))

    def on_disconnect(self):
        pass


def build_world():
    """Real server, real players: shooter at rest, target to be moved."""
    import shared.constants as C
    from server.config import ServerConfig
    from server.game_constants import TEAM1, TEAM2
    from server.main import BattleSpadesServer
    from server.player import Player

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()

    def add(player_id, team, position):
        connection = _Connection(server)
        player = Player(
            player_id, "LC%d" % player_id, team, C.RIFLE_TOOL, connection
        )
        connection.player = player
        player.is_bot = False
        player.class_id = int(C.CLASS_SOLDIER)
        player.loadout = [C.RIFLE_TOOL]
        player.spawn(*position)
        player.set_tool(C.RIFLE_TOOL, raw=True)
        player.spawned_at = time.monotonic() - 60.0
        server.players[player.id] = player
        server.connections[player.id] = connection
        server.teams[team].add_player(player)
        return player

    shooter = add(0, TEAM1, (100.5, 100.5, EYE_Z))
    target = add(1, TEAM2, (95.5, 115.5, EYE_Z))
    shooter.connection.peer = SimpleNamespace(roundTripTime=0)
    return server, shooter, target


def _aim(origin, point):
    delta = (
        point[0] - origin[0],
        point[1] - origin[1],
        point[2] + AIM_HEIGHT - origin[2],
    )
    length = math.sqrt(sum(value * value for value in delta))
    return tuple(value / length for value in delta)


def _packet(shooter, direction, snapshot):
    from shared.packet import ShootPacket

    packet = ShootPacket()
    packet.loop_count = 1
    packet.shooter_id = shooter.id
    packet.shot_on_world_update = int(snapshot)
    packet.x, packet.y, packet.z = shooter.eye
    packet.ori_x, packet.ori_y, packet.ori_z = direction
    packet.damage = 0.0
    packet.penetration = 0
    packet.affect_shooter = 0
    packet.secondary = 0
    packet.seed = 0
    return packet


def measure(
    world,
    pattern: str,
    profile: LinkProfile,
    *,
    seconds: float = 20.0,
    seed: int = 1,
    shot_every_ms: float = 140.0,
    snapshot_interval: int = SNAPSHOT_INTERVAL_TICKS,
    forged_snapshot_age: int | None = None,
    warmup_seconds: float = 3.0,
) -> Result:
    """Fire at a moving target over ``profile`` and score registration."""
    from server import lag_compensation as lc

    server, shooter, target = world
    rng = random.Random(seed)
    link = Link(profile, rng)
    round_trip = EnetRoundTrip()
    path = PATTERNS[pattern](seconds + warmup_seconds + 2.0)
    ticks = int((seconds + warmup_seconds) * 1000.0 / TICK_MS)
    lc.forget_player(target)
    target.replication_generation = int(
        getattr(target, "replication_generation", 0)
    ) + 1

    # Snapshots: (label, arrival ms), in label order.
    snapshots = []
    for label in range(0, ticks + 1, max(1, snapshot_interval)):
        transit = link.transit()
        if transit is not None:
            snapshots.append((label, label * TICK_MS + transit))

    # ENet round trip samples: steady reliable traffic, each acknowledged
    # after down + client poll + up.
    samples = []
    clock = 0.0
    while clock < ticks * TICK_MS:
        down, up = link.transit(), link.transit()
        if down is not None and up is not None:
            poll = rng.uniform(0.0, CLIENT_POLL_MS)
            samples.append((clock + down + poll + up, down + poll + up))
        clock += 100.0
    samples.sort()

    # Shots, fired at what the client shows.
    shots = []
    clock = warmup_seconds * 1000.0
    end = ticks * TICK_MS - 400.0
    newest_index = -1
    while clock < end:
        clock += shot_every_ms * rng.uniform(0.6, 1.4)
        best = None
        for label, arrival in snapshots:
            if arrival <= clock and (best is None or label > best[0]):
                best = (label, arrival)
            if label * TICK_MS > clock:
                break
        if best is None:
            continue
        label, arrival = best
        position, velocity, acceleration = path.state(label * TICK_MS / 1000.0)
        elapsed = (clock - arrival) / 1000.0
        view = tuple(
            p + v * elapsed + 0.5 * a * elapsed * elapsed
            for p, v, a in zip(position, velocity, acceleration)
        )
        estimate = round_trip_at(samples, clock)
        retransmissions = 0
        sent = clock
        transit = link.transit()
        while transit is None and retransmissions < 5:
            retransmissions += 1
            sent += estimate.retransmit_ms * (2 ** (retransmissions - 1))
            transit = link.transit()
        if transit is None:
            continue
        shots.append(Shot(
            fired_ms=clock,
            arrival_ms=sent + transit,
            snapshot=label,
            view=view,
            view_server_ms=label * TICK_MS + (clock - arrival),
            retransmissions=retransmissions,
        ))
    shots.sort(key=lambda shot: shot.arrival_ms)

    result = Result()
    pending = 0
    start = path.state(0.0)[0]
    target.set_position(*start)
    for tick in range(1, ticks + 1):
        server.loop_count = tick
        # Packet drain of tick ``tick``: the live body is label tick - 1.
        while pending < len(shots) and shots[pending].arrival_ms <= tick * TICK_MS:
            shot = shots[pending]
            pending += 1
            estimate = round_trip_at(samples, tick * TICK_MS)
            shooter.connection.peer.roundTripTime = estimate.mean
            claimed = shot.snapshot
            if forged_snapshot_age is not None:
                claimed = max(1, tick - 1 - forged_snapshot_age)
            direction = _aim(shooter.eye, shot.view)
            packet = _packet(shooter, direction, claimed)
            context = lc.rewind_targets(server, shooter, packet)
            body = target if context is None else context.body(target)
            hit = server.combat._ray_hits_target(
                shooter.eye, direction, 128.0, body
            ) is not None
            live_hit = server.combat._ray_hits_target(
                shooter.eye, direction, 128.0, target
            ) is not None
            result.shots += 1
            result.hits += int(hit)
            result.hits_uncompensated += int(live_hit)
            if shot.retransmissions:
                result.retransmitted += 1
                result.retransmitted_hits += int(hit)
            result.view_errors.append(math.dist(
                (body.x, body.y, body.z), shot.view
            ))
            rewind = 0.0 if context is None else float(context.rewind_ms)
            result.rewinds_ms.append(rewind)
            result.ideal_ms.append(
                (tick - 1) * TICK_MS - shot.view_server_ms
            )
            result.cover_blocks.append(math.dist(
                (body.x, body.y, body.z), (target.x, target.y, target.z)
            ))
        lc.record_player(target)
        position, velocity, _acceleration = path.state(tick * TICK_MS / 1000.0)
        target.set_position(*position)
        speed = math.hypot(velocity[0], velocity[1])
        if speed > 1e-6:
            target.set_orientation_vector(
                velocity[0] / speed, velocity[1] / speed, 0.0
            )
    return result


def round_trip_at(samples, clock_ms) -> EnetRoundTrip:
    """ENet's estimate after every acknowledgement received by ``clock_ms``."""
    estimate = EnetRoundTrip()
    for arrival, rtt in samples:
        if arrival > clock_ms:
            break
        estimate.sample(rtt)
    return estimate


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--jitter-ms", type=float, default=20.0)
    args = parser.parse_args(argv)

    world = build_world()
    header = (
        "pattern", "rtt", "loss", "shots", "hit", "first try", "no comp",
        "view err", "p95", "rewind", "off by", "cover max",
    )
    print("%-10s %4s %5s %5s %6s %9s %7s %8s %6s %7s %7s %9s" % header)
    for pattern in PATTERNS:
        for rtt in (50.0, 100.0, 200.0):
            for loss in (0.005, 0.02):
                profile = LinkProfile(rtt, args.jitter_ms, loss)
                summary = measure(
                    world, pattern, profile,
                    seconds=args.seconds, seed=args.seed,
                ).summary()
                print(
                    "%-10s %4d %5.1f %5d %5.1f%% %8.1f%% %6.1f%% %8.3f %6.3f "
                    "%6.1f %6.1f %9.2f" % (
                        pattern, rtt, loss * 100.0, summary["shots"],
                        summary["hit_rate"] * 100.0,
                        summary["hit_rate_first_try"] * 100.0,
                        summary["hit_rate_uncompensated"] * 100.0,
                        summary["view_error_mean"],
                        summary["view_error_p95"],
                        summary["rewind_mean_ms"],
                        summary["rewind_error_mean_ms"],
                        summary["cover_max_blocks"],
                    )
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
