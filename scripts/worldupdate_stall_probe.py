"""Measure WorldUpdate stalls and stale snapshots on a lossy link (headless).

Starts an isolated server, a lossy UDP relay in front of it and three
headless players built from the server's own wire modules:

* the observer joins through the relay and only watches;
* two movers join directly, walk about and keep changing their block
  colour, which the server relays to the observer as reliable packets.

Every reliable packet the relay drops on the way to the observer is a chance
for ENet to hold the snapshot stream behind it. The observer records when
each WorldUpdate carrying other players' rows arrives and its header loop:

* a gap of ``--stall-ms`` or more between two such snapshots is a stall;
* a header loop lower than one already received is a stale snapshot.

    py -3.12 scripts/worldupdate_stall_probe.py --delivery sequenced
    py -3.12 scripts/worldupdate_stall_probe.py --delivery split

No game window is opened. Ports default to 28110 (server) and 28111 (relay).
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import multiprocessing
import random
import select
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import enet  # noqa: E402

from shared import packet as P  # noqa: E402
from shared.bytes import ByteReader  # noqa: E402

PROTOCOL_VERSION = 168
TICK = 1.0 / 60.0
ROW = 56

SERVER_SNIPPET = r"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, {root!r})
from server.config import load_config
from server.logging_runtime import configure_logging
from server.main import BattleSpadesServer
from server.validation import build_validation_config

config = build_validation_config(
    load_config(Path({config!r})), port={port}, map_name={map_name!r},
    mode="tdm",
)
config.worldupdate_delivery = {delivery!r}
config.worldupdate_reorder_guard = {guard!r}
config.log_level = "WARNING"
log_dir = Path({log_dir!r})
log_dir.mkdir(parents=True, exist_ok=True)


COUNTERS = (
    "input_frames_applied", "input_frames_stale", "input_frames_synthesized",
    "input_starved_ticks", "input_frames_reordered", "input_frames_salvaged",
    "input_presses_latched",
)


async def dump_input_counters(server, path):
    # The health log zeroes these every 600 ticks, so sum the increases.
    import json
    totals, previous = {{}}, {{}}
    while True:
        await asyncio.sleep(2.0)
        for player in tuple(server.players.values()):
            name = str(player.name)
            for counter in COUNTERS:
                value = int(getattr(player, counter, 0) or 0)
                before = previous.get((name, counter), 0)
                gained = value if value < before else value - before
                previous[(name, counter)] = value
                row = totals.setdefault(name, {{}})
                row[counter] = row.get(counter, 0) + gained
            totals[name]["reorder_spread_frames"] = int(
                player.input_reorder_spread_frames(server.loop_count)
            )
            totals[name]["ordered_delivery"] = bool(getattr(
                player.connection, "_wu_observer_sequenced", False
            ))
        # Never write inside the event loop: a file write here stalled the
        # server for hundreds of milliseconds and showed up as stalls.
        text = json.dumps(totals)
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: Path(path).write_text(text, encoding="utf-8")
        )


async def main():
    server = BattleSpadesServer(config)
    runtime = configure_logging(config, log_dir)
    dumper = None
    if {stats_path!r}:
        dumper = asyncio.ensure_future(
            dump_input_counters(server, {stats_path!r})
        )
    try:
        await server.start()
    finally:
        if dumper is not None:
            dumper.cancel()
        await server.stop()
        runtime.stop()


asyncio.run(main())
"""


class Relay(multiprocessing.Process):
    """Delay, jitter and loss per datagram, both directions.

    A process of its own: as a thread it shared the interpreter lock with
    the headless players, and when that stalled it read a backlog at one
    instant and jittered packets sent a second apart into random order.
    """

    UP, DOWN = 0, 1

    def __init__(self, listen, target, delay_ms, jitter_ms, loss, seed):
        super().__init__(daemon=True)
        self.listen = listen
        self.target = target
        self.delay = delay_ms / 1000.0
        self.jitter = jitter_ms / 1000.0
        self.loss = loss
        self.seed = seed
        self._impair = multiprocessing.Value("b", 0)
        self._running = multiprocessing.Value("b", 1)
        self._dropped = multiprocessing.Array("i", 2)
        self._forwarded = multiprocessing.Array("i", 2)

    @property
    def impair(self):
        return bool(self._impair.value)

    @impair.setter
    def impair(self, value):
        self._impair.value = 1 if value else 0

    @property
    def running(self):
        return bool(self._running.value)

    @running.setter
    def running(self, value):
        self._running.value = 1 if value else 0

    @property
    def dropped(self):
        return {"up": self._dropped[0], "down": self._dropped[1]}

    @property
    def forwarded(self):
        return {"up": self._forwarded[0], "down": self._forwarded[1]}

    def _schedule(self, direction, sock, data, dest):
        if self.impair:
            if self.rng.random() < self.loss:
                self._dropped[direction] += 1
                return
            extra = self.rng.uniform(-self.jitter, self.jitter)
            at = time.monotonic() + max(0.0, self.delay + extra)
        else:
            at = time.monotonic()
        self.sequence += 1
        self._forwarded[direction] += 1
        heapq.heappush(self.queue, (at, self.sequence, sock, data, dest))

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", self.listen))
        self.up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.up.bind(("127.0.0.1", 0))
        self.rng = random.Random(self.seed)
        self.client = None
        self.queue = []
        self.sequence = 0
        while self.running:
            timeout = 0.005
            if self.queue:
                timeout = max(
                    0.0, min(timeout, self.queue[0][0] - time.monotonic())
                )
            ready, _, _ = select.select([self.sock, self.up], [], [], timeout)
            for sock in ready:
                try:
                    data, address = sock.recvfrom(65535)
                except OSError:
                    continue
                if sock is self.sock:
                    self.client = address
                    self._schedule(self.UP, self.up, data, self.target)
                elif self.client is not None:
                    self._schedule(self.DOWN, self.sock, data, self.client)
            now = time.monotonic()
            while self.queue and self.queue[0][0] <= now:
                _, _, sock, data, dest = heapq.heappop(self.queue)
                try:
                    sock.sendto(data, dest)
                except OSError:
                    pass


def wire(packet) -> bytes:
    return b"\x30" + bytes(packet.generate())


def unchunk(data: bytes, limit: int = 1 << 20) -> bytes:
    """Undo the server's literal-only LZF framing."""
    out = bytearray()
    index = 1
    while index < len(data) and len(out) < limit:
        header = data[index]
        if header >> 5:
            raise ValueError("unexpected LZF back-reference")
        run = (header & 0x1F) + 1
        out += data[index + 1:index + 1 + run]
        index += 1 + run
    return bytes(out)


def initial_info_checksum(body: bytes) -> int:
    reader = ByteReader(body)
    reader.read_uint64()
    reader.read_int()
    reader.read_int()
    for _ in range(7):
        reader.read_string()
    return int(reader.read_int())


class Player:
    """One headless player: joins, then sends ClientData every frame."""

    def __init__(self, name, port, team, *, mover, seed):
        self.name = name
        self.mover = mover
        self.team = team
        self.rng = random.Random(seed)
        self.host = enet.Host(None, 1, 1, 0, 0)
        self.host.compress_with_range_coder()
        self.peer = self.host.connect(
            enet.Address(b"127.0.0.1", port), 1, PROTOCOL_VERSION
        )
        self.player_id = -1
        self.playing = False
        self.loop_base = None
        self.loop_clock = 0.0
        self.sent = 0
        self.next_clock = 0.0
        self.next_color = 0.0
        self.yaw = self.rng.uniform(0.0, math.tau)
        self.keys = (True, False, False, False)
        self.keys_until = 0.0
        self.disconnected = None
        # Observer measurements.
        self.snapshots = []  # (arrival seconds, header loop, observer rows)
        self.reliable_seen = 0

    def send(self, packet, *, unsequenced=False):
        flags = (
            enet.PACKET_FLAG_UNSEQUENCED if unsequenced
            else enet.PACKET_FLAG_RELIABLE
        )
        self.peer.send(0, enet.Packet(wire(packet), flags))

    def clock_sync(self, now):
        sync = P.ClockSync()
        sync.client_time = int(now * 1000.0) & 0x7FFFFFFF
        sync.server_loop_count = 0
        self.send(sync, unsequenced=True)

    def on_packet(self, data, now):
        if len(data) < 3:
            return
        packet_id = data[2]
        if packet_id == 2:
            if not self.playing:
                return
            body = unchunk(data)
            loop = int.from_bytes(body[1:5], "little", signed=True)
            count = int.from_bytes(body[5:7], "little")
            others = sum(
                1 for index in range(count)
                if body[7 + index * ROW] != self.player_id
            )
            if others:
                self.snapshots.append((now, loop, others))
            return
        if packet_id == 11:
            self.reliable_seen += 1
            return
        if packet_id not in (114, 45, 28, 0):
            return
        body = unchunk(data, 4096)
        if packet_id == 114:
            reply = P.MapDataValidation()
            reply.crc = initial_info_checksum(body[1:])
            self.send(reply)
        elif packet_id == 45:
            self.player_id = int(body[1])
            join = P.NewPlayerConnection()
            join.team = self.team
            join.class_id = 0
            join.forced_team = 0
            join.local_language = 0
            join.name = self.name
            self.send(join)
        elif packet_id == 28:
            if int(body[1]) == self.player_id and not self.playing:
                self.playing = True
                self.clock_sync(now)
                self.next_clock = now + 1.0
        elif packet_id == 0:
            reply = P.ClockSync()
            reply.read(ByteReader(body[1:]))
            self.loop_base = int(reply.server_loop_count)
            self.loop_clock = now

    def pump(self, now):
        for _ in range(512):
            event = self.host.service(0)
            kind = event.type
            if kind == enet.EVENT_TYPE_NONE:
                return
            if kind == enet.EVENT_TYPE_CONNECT:
                ticket = P.SteamSessionTicket()
                ticket.ticket = b""
                ticket.ticket_size = 0
                self.send(ticket)
            elif kind == enet.EVENT_TYPE_DISCONNECT:
                self.disconnected = int(event.data)
                return
            elif kind == enet.EVENT_TYPE_RECEIVE:
                self.on_packet(bytes(event.packet.data), now)

    def play(self, now):
        if not self.playing:
            return
        if self.loop_base is None:
            if now >= self.next_clock:
                self.clock_sync(now)
                self.next_clock = now + 1.0
            return
        target = self.loop_base + int((now - self.loop_clock) * 60.0)
        if self.sent == 0 or self.sent < target - 8:
            self.sent = target - 1
        while self.sent < target:
            self.sent += 1
            if self.mover and now >= self.keys_until:
                self.keys = tuple(
                    self.rng.random() < 0.5 for _ in range(4)
                )
                self.keys_until = now + self.rng.uniform(0.3, 1.2)
                self.yaw += self.rng.uniform(-1.0, 1.0)
            frame = P.ClientData()
            frame.loop_count = self.sent
            frame.player_id = self.player_id
            frame.o_x = math.cos(self.yaw)
            frame.o_y = math.sin(self.yaw)
            frame.o_z = 0.0
            frame.tool_id = 5 if self.mover else 7
            up, down, left, right = self.keys if self.mover else (False,) * 4
            frame.up, frame.down, frame.left, frame.right = up, down, left, right
            frame.jump = frame.crouch = frame.sneak = frame.sprint = False
            frame.primary = frame.secondary = frame.zoom = False
            frame.can_pickup = frame.is_on_fire = False
            frame.is_weapon_deployed = frame.hover = False
            frame.can_display_weapon = True
            self.send(frame, unsequenced=True)
        if self.mover and now >= self.next_color:
            # Relayed to every observer as a reliable packet (one per 0.1 s
            # per player): steady reliable traffic, like a busy fight.
            color = P.SetColor()
            color.player_id = self.player_id
            color.value = self.rng.randrange(0x1000000)
            self.send(color)
            self.next_color = now + 0.1
        if now >= self.next_clock:
            self.clock_sync(now)
            self.next_clock = now + 1.0
        self.host.flush()


def wait_for_port(port, seconds, server=None):
    """Wait until an ENet connect to ``port`` is accepted."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if server is not None and server.poll() is not None:
            return False
        host = enet.Host(None, 1, 1, 0, 0)
        host.compress_with_range_coder()
        peer = host.connect(
            enet.Address(b"127.0.0.1", port), 1, PROTOCOL_VERSION
        )
        attempt = time.monotonic() + 1.5
        while time.monotonic() < attempt:
            event = host.service(50)
            if event.type == enet.EVENT_TYPE_CONNECT:
                peer.disconnect_now()
                host.flush()
                return True
        del peer, host
    return False


def observer_input_stats(stats_path, since):
    """The observer's input counters as the server summed them."""
    try:
        if stats_path.stat().st_mtime < since:
            return {}
        rows = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rows.get("Observer", {})


def summarize(observer, started, stall_ms):
    samples = [row for row in observer.snapshots if row[0] >= started]
    gaps = [
        (b[0] - a[0]) * 1000.0 for a, b in zip(samples, samples[1:])
    ]
    newest = None
    stale = 0
    for _at, loop, _rows in samples:
        if newest is not None and loop < newest:
            stale += 1
        newest = loop if newest is None else max(newest, loop)
    ordered = sorted(gaps)

    def percentile(share):
        if not ordered:
            return 0.0
        return ordered[min(len(ordered) - 1, int(len(ordered) * share))]

    return {
        "snapshots": len(samples),
        "stalls": sum(1 for gap in gaps if gap >= stall_ms),
        "stall_ms_total": round(
            sum(gap for gap in gaps if gap >= stall_ms), 1
        ),
        "gap_max_ms": round(max(gaps), 1) if gaps else 0.0,
        "gap_p99_ms": round(percentile(0.99), 1),
        "gap_p50_ms": round(percentile(0.50), 1),
        "stale_snapshots": stale,
        "reliable_packets_seen": observer.reliable_seen,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--delivery", choices=("split", "sequenced"),
                        default="split")
    parser.add_argument("--no-guard", action="store_true")
    parser.add_argument("--port", type=int, default=28110)
    parser.add_argument("--relay-port", type=int, default=28111)
    parser.add_argument("--map", default="ArcticBase")
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--delay-ms", type=float, default=50.0)
    parser.add_argument("--jitter-ms", type=float, default=5.0)
    parser.add_argument("--loss", type=float, default=0.02)
    parser.add_argument("--stall-ms", type=float, default=150.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--log-dir", type=Path, default=ROOT / "tmp" / "stall-probe-logs",
        help="server log directory (kept apart from logs/server.log)",
    )
    parser.add_argument(
        "--input-stats", action="store_true",
        help="also print the server's per-player input counters (tick stats)",
    )
    args = parser.parse_args(argv)
    if args.port == 27015 or args.relay_port == 27015:
        parser.error("27015 is the public server port")

    snippet = SERVER_SNIPPET.format(
        root=str(ROOT), config=str(args.config), port=args.port,
        map_name=args.map, delivery=args.delivery, guard=not args.no_guard,
        log_dir=str(args.log_dir),
        stats_path=(
            str(args.log_dir / "input_counters.json")
            if args.input_stats else ""
        ),
    )
    launched = time.time()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    server = subprocess.Popen(
        [sys.executable, "-X", "faulthandler", "-c", snippet],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    relay = None
    try:
        if not wait_for_port(args.port, 120.0, server):
            detail = ""
            if server.poll() is not None and server.stderr is not None:
                detail = server.stderr.read().decode("utf-8", "replace")[-2000:]
            raise RuntimeError("server did not accept a connection " + detail)
        time.sleep(1.0)
        relay = Relay(
            args.relay_port, ("127.0.0.1", args.port),
            args.delay_ms, args.jitter_ms, args.loss, args.seed,
        )
        relay.start()
        time.sleep(1.0)  # the relay process binds its sockets
        observer = Player(
            "Observer", args.relay_port, 2, mover=False, seed=args.seed
        )
        movers = [
            Player("MoverA", args.port, 2, mover=True, seed=args.seed + 1),
            Player("MoverB", args.port, 3, mover=True, seed=args.seed + 2),
        ]
        players = [observer] + movers
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            for player in players:
                player.pump(now)
                player.play(now)
            if all(p.playing and p.loop_base is not None for p in players):
                break
            if any(p.disconnected is not None for p in players):
                raise RuntimeError("a player was disconnected while joining")
            time.sleep(0.002)
        else:
            raise RuntimeError("players did not reach the game")
        # The join ran on a clean link; measure with the impairment on.
        relay.impair = True
        settle = time.monotonic() + 2.0
        started = settle
        end = settle + args.seconds
        next_frame = time.monotonic()
        while time.monotonic() < end:
            now = time.monotonic()
            for player in players:
                player.pump(now)
            if now >= next_frame:
                for player in players:
                    player.play(now)
                next_frame += TICK
                if next_frame < now - 0.1:
                    next_frame = now
            if any(p.disconnected is not None for p in players):
                raise RuntimeError("a player was disconnected while playing")
            time.sleep(0.001)
        result = summarize(observer, started, args.stall_ms)
        result.update({
            "delivery": args.delivery,
            "reorder_guard": not args.no_guard,
            "seconds": args.seconds,
            "delay_ms": args.delay_ms,
            "jitter_ms": args.jitter_ms,
            "loss": args.loss,
            "stall_threshold_ms": args.stall_ms,
            "relay_dropped": dict(relay.dropped),
            "relay_forwarded": dict(relay.forwarded),
        })
        if args.input_stats:
            result["observer_input"] = observer_input_stats(
                args.log_dir / "input_counters.json", launched
            )
        stale_at = []
        newest = None
        for at, loop, _rows in observer.snapshots:
            if at >= started and newest is not None and loop < newest:
                stale_at.append({
                    "seconds": round(at - started, 3),
                    "loop": loop,
                    "newest": newest,
                })
            newest = loop if newest is None else max(newest, loop)
        result["stale"] = stale_at
        print(json.dumps(result, indent=2))
        if args.out is not None:
            args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 0
    finally:
        if relay is not None:
            relay.running = False
        server.terminate()
        try:
            server.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
