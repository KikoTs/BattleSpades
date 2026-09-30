"""Headless join/leave churn for the memory soak (Python 3, no game client).

Every slot runs this cycle for the whole soak::

    connect -> SteamSessionTicket -> InitialInfo -> MapDataValidation ->
    MapSync -> StateData -> NewPlayerConnection -> play -> leave -> wait

"Play" is one ClientData per 60 Hz tick with changing keys and view, plus the
occasional chat line, class change and team change.  A slot that is connected
when the round ends follows the scene reload the way the real client does
(MapEnded -> ClientInMenu -> InitialInfo -> ... -> NewPlayerConnection).

Leaves are mixed on purpose, because each one frees server state through a
different path:

* ``graceful``  - ``peer.disconnect()`` and wait for the acknowledgement;
* ``abrupt``    - ``peer.disconnect_now()`` (one notice, no waiting);
* ``vanish``    - the socket is dropped without a word, so the server only
  notices through the ENet timeout;
* ``prejoin``   - leave during the map transfer, before a Player exists.

Only the server's own wire modules are used (``enet``, ``shared.packet``), so
this runs wherever the server runs.  Output is one JSON line per
``--report-seconds`` with the totals, written to ``--out``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import random
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import enet  # noqa: E402

from shared import packet as P  # noqa: E402
from shared.bytes import ByteReader  # noqa: E402

PROTOCOL_VERSION = 168
TICK = 1.0 / 60.0
TEAMS = (2, 3)
CLASSES = (0, 1, 2, 3, 12, 16, 17)  # soldier, scout, rocketeer, miner, engineer, specialist, medic
CHAT_LINES = ("gg", "nice shot", "push left", "need blocks", "on my way", "defend", "hello")

IDLE, CONNECTING, HANDSHAKE, MAP, SELECT, PLAYING, LEAVING = range(7)
STATE_NAMES = ("idle", "connecting", "handshake", "map", "select", "playing", "leaving")


def wire(packet) -> bytes:
    """Serialize one ``shared.packet`` instance the way a client sends it."""

    return b"\x30" + bytes(packet.generate())


def unchunk(data: bytes, limit: int) -> bytes:
    """Undo the server's literal-only LZF framing for the first ``limit`` bytes.

    The server never emits back-references (``server.util.lzf_compress``), so
    the body is a run of ``length-1`` headers each followed by up to 32 bytes.
    Importing ``server.util`` would load the whole server package into the
    client process, which is why the few lines live here.
    """

    out = bytearray()
    index = 1  # skip the 0x30/0x31 prefix
    size = len(data)
    while index < size and len(out) < limit:
        header = data[index]
        if header >> 5:
            raise ValueError("unexpected LZF back-reference from the server")
        run = (header & 0x1F) + 1
        out += data[index + 1:index + 1 + run]
        index += 1 + run
    return bytes(out)


def initial_info_checksum(body: bytes) -> int:
    """Return ``InitialInfo.checksum`` without parsing the whole packet."""

    reader = ByteReader(body)
    reader.read_uint64()
    reader.read_int()
    reader.read_int()
    for _ in range(7):
        reader.read_string()
    return int(reader.read_int())


class Slot:
    """One repeatedly reconnecting headless player."""

    def __init__(self, index: int, args, rng: random.Random, stats: dict) -> None:
        self.index = index
        self.args = args
        self.rng = rng
        self.stats = stats
        self.host = None
        self.peer = None
        self.state = IDLE
        self.next_action = time.monotonic() + rng.uniform(0.0, args.stagger)
        self.deadline = 0.0
        self.leave_at = 0.0
        self.leave_kind = "graceful"
        self.player_id = -1
        self.cycle = 0
        self.loop_base = None
        self.loop_clock = 0.0
        self.sent_ticks = 0
        self.keys = 0
        self.actions = 0
        self.keys_until = 0.0
        self.yaw = rng.uniform(0.0, math.tau)
        self.pitch = 0.0
        self.turn = 0.0
        self.team = rng.choice(TEAMS)
        self.class_id = rng.choice(CLASSES)
        self.next_chat = 0.0
        self.next_clock = 0.0
        self.alive_seen = False

    # ------------------------------------------------------------------ net
    def send(self, packet, *, unsequenced: bool = False) -> None:
        if self.peer is None:
            return
        flags = enet.PACKET_FLAG_UNSEQUENCED if unsequenced else enet.PACKET_FLAG_RELIABLE
        self.peer.send(0, enet.Packet(wire(packet), flags))

    def close(self) -> None:
        self.peer = None
        self.host = None  # frees the ENet host and its socket
        self.state = IDLE
        self.player_id = -1
        self.loop_base = None

    def begin(self, now: float) -> None:
        self.cycle += 1
        self.host = enet.Host(None, 1, 1, 0, 0)
        self.host.compress_with_range_coder()
        self.peer = self.host.connect(
            enet.Address(self.args.host.encode("ascii"), self.args.port),
            1,
            PROTOCOL_VERSION,
        )
        self.state = CONNECTING
        self.deadline = now + 8.0
        self.alive_seen = False
        self.stats["connect_attempts"] += 1
        roll = self.rng.random()
        if roll < self.args.prejoin_share:
            self.leave_kind = "prejoin"
        elif roll < self.args.prejoin_share + self.args.vanish_share:
            self.leave_kind = "vanish"
        elif roll < self.args.prejoin_share + self.args.vanish_share + self.args.abrupt_share:
            self.leave_kind = "abrupt"
        else:
            self.leave_kind = "graceful"
        if self.rng.random() < self.args.long_share:
            dwell = self.rng.uniform(self.args.long_min, self.args.long_max)
        else:
            dwell = self.rng.uniform(self.args.dwell_min, self.args.dwell_max)
        self.dwell = dwell

    def fail(self, now: float, why: str) -> None:
        self.stats[f"fail_{why}"] = self.stats.get(f"fail_{why}", 0) + 1
        try:
            if self.peer is not None:
                self.peer.disconnect_now()
                self.host.flush()
        except Exception:  # noqa: BLE001
            pass
        self.close()
        self.next_action = now + self.rng.uniform(self.args.gap_min, self.args.gap_max)

    def leave(self, now: float) -> None:
        kind = self.leave_kind
        self.stats[f"leave_{kind}"] = self.stats.get(f"leave_{kind}", 0) + 1
        if kind == "vanish":
            # No notice at all: the server must time the peer out.
            self.close()
            self.next_action = now + self.rng.uniform(self.args.gap_min, self.args.gap_max)
            return
        try:
            if kind == "graceful":
                self.peer.disconnect()
                self.state = LEAVING
                self.deadline = now + 3.0
                return
            self.peer.disconnect_now()
            self.host.flush()
        except Exception:  # noqa: BLE001
            pass
        self.close()
        self.next_action = now + self.rng.uniform(self.args.gap_min, self.args.gap_max)

    # --------------------------------------------------------------- events
    def on_packet(self, data: bytes, now: float) -> None:
        if len(data) < 3:
            return
        self.stats["packets_in"] += 1
        self.stats["bytes_in"] += len(data)
        packet_id = data[2]
        if packet_id not in (114, 45, 28, 0, 52):
            return  # map chunks, world updates, ...: only the cost matters
        try:
            body = unchunk(data, 4096)
        except Exception:  # noqa: BLE001
            self.stats["decode_errors"] += 1
            return
        if not body:
            return
        if packet_id == 114:  # InitialInfo
            try:
                crc = initial_info_checksum(body[1:])
            except Exception:  # noqa: BLE001
                self.stats["decode_errors"] += 1
                crc = 0
            reply = P.MapDataValidation()
            # A wrong CRC forces the full map transfer, which is the path a
            # new player takes and the one that allocates the most.
            reply.crc = (crc ^ 0x5A5A5A5A) & 0x7FFFFFFF if self.args.force_full_map else crc
            self.send(reply)
            self.state = MAP
            self.deadline = now + 60.0
            self.player_id = -1
            self.loop_base = None
            if self.leave_kind == "prejoin":
                self.leave_at = now + self.rng.uniform(0.05, 1.5)
        elif packet_id == 45:  # StateData
            try:
                self.player_id = int(body[1])
            except Exception:  # noqa: BLE001
                self.player_id = 0
            join = P.NewPlayerConnection()
            join.team = self.team
            join.class_id = self.class_id
            join.forced_team = 0
            join.local_language = 0
            join.name = f"{self.args.name}{self.index:02d}"
            self.send(join)
            self.state = SELECT
            self.deadline = now + 20.0
        elif packet_id == 28:  # CreatePlayer
            if len(body) > 1 and int(body[1]) == self.player_id and self.state in (SELECT, PLAYING):
                if self.state == SELECT:
                    self.stats["joins"] += 1
                    self.leave_at = now + self.dwell
                    sync = P.ClockSync()
                    sync.client_time = int(now * 1000.0) & 0x7FFFFFFF
                    sync.server_loop_count = 0
                    self.send(sync, unsequenced=True)
                    self.next_clock = now + 5.0
                    self.next_chat = now + self.rng.uniform(5.0, 40.0)
                self.state = PLAYING
                self.alive_seen = True
        elif packet_id == 0:  # ClockSync reply
            try:
                reply = P.ClockSync()
                reply.read(ByteReader(body[1:]))
                self.loop_base = int(reply.server_loop_count)
                self.loop_clock = now
            except Exception:  # noqa: BLE001
                self.stats["decode_errors"] += 1
        elif packet_id == 52:  # MapEnded: the scene is about to be replaced
            self.stats["map_ended_seen"] += 1
            menu = P.ClientInMenu()
            menu.in_menu = 1
            self.send(menu)
            self.state = HANDSHAKE
            self.deadline = now + 90.0
            self.player_id = -1
            self.loop_base = None

    def pump(self, now: float) -> None:
        host = self.host
        if host is None:
            return
        for _ in range(256):
            try:
                event = host.service(0)
            except Exception:  # noqa: BLE001
                self.fail(now, "service")
                return
            kind = event.type
            if kind == enet.EVENT_TYPE_NONE:
                return
            if kind == enet.EVENT_TYPE_CONNECT:
                self.stats["connects"] += 1
                ticket = P.SteamSessionTicket()
                ticket.ticket = b""
                ticket.ticket_size = 0
                self.send(ticket)
                self.state = HANDSHAKE
                self.deadline = now + 15.0
            elif kind == enet.EVENT_TYPE_DISCONNECT:
                self.stats["server_disconnects" if self.state != LEAVING else "graceful_acks"] += 1
                reason = int(event.data)
                key = f"disconnect_reason_{reason}"
                self.stats[key] = self.stats.get(key, 0) + 1
                self.close()
                self.next_action = now + self.rng.uniform(self.args.gap_min, self.args.gap_max)
                return
            elif kind == enet.EVENT_TYPE_RECEIVE:
                data = bytes(event.packet.data)  # touching .packet frees it
                self.on_packet(data, now)
                if self.host is None:
                    return

    # ----------------------------------------------------------------- play
    def play(self, now: float) -> None:
        if self.loop_base is None:
            if now >= self.next_clock:
                sync = P.ClockSync()
                sync.client_time = int(now * 1000.0) & 0x7FFFFFFF
                sync.server_loop_count = 0
                self.send(sync, unsequenced=True)
                self.next_clock = now + 1.0
            return
        target = self.loop_base + int((now - self.loop_clock) * 60.0)
        if self.sent_ticks == 0 or self.sent_ticks < target - 8:
            self.sent_ticks = target - 1
        rng = self.rng
        while self.sent_ticks < target:
            self.sent_ticks += 1
            if now >= self.keys_until:
                self.keys_until = now + rng.uniform(0.3, 2.5)
                self.keys = rng.choice((0x01, 0x01, 0x81, 0x05, 0x09, 0x02, 0x11, 0x21, 0x00))
                self.actions = rng.choice((0x10, 0x10, 0x11, 0x11, 0x12, 0x14))
                self.turn = rng.uniform(-1.5, 1.5)
                self.pitch = rng.uniform(-0.4, 0.4)
            self.yaw += self.turn * TICK
            cos_pitch = math.cos(self.pitch)
            data = P.ClientData()
            data.loop_count = self.sent_ticks & 0x7FFFFFFF
            data.player_id = self.player_id & 0x7F
            data.tool_id = self.args.tool
            data.o_x = math.cos(self.yaw) * cos_pitch
            data.o_y = math.sin(self.yaw) * cos_pitch
            data.o_z = math.sin(self.pitch)
            data.ooo = 0
            keys = self.keys
            data.up = bool(keys & 0x01)
            data.down = bool(keys & 0x02)
            data.left = bool(keys & 0x04)
            data.right = bool(keys & 0x08)
            data.jump = bool(keys & 0x10)
            data.crouch = bool(keys & 0x20)
            data.sneak = bool(keys & 0x40)
            data.sprint = bool(keys & 0x80)
            actions = self.actions
            data.primary = bool(actions & 0x01)
            data.secondary = bool(actions & 0x02)
            data.zoom = bool(actions & 0x04)
            data.can_pickup = False
            data.can_display_weapon = bool(actions & 0x10)
            data.is_on_fire = False
            data.is_weapon_deployed = False
            data.hover = False
            data.palette_enabled = False
            data.weapon_deployment_yaw = 0.0
            self.send(data, unsequenced=True)
            self.stats["client_data"] += 1
        if now >= self.next_clock:
            sync = P.ClockSync()
            sync.client_time = int(now * 1000.0) & 0x7FFFFFFF
            sync.server_loop_count = 0
            self.send(sync, unsequenced=True)
            self.next_clock = now + 5.0
        if now >= self.next_chat:
            self.next_chat = now + rng.uniform(20.0, 90.0)
            roll = rng.random()
            if roll < 0.6:
                chat = P.ChatMessage()
                chat.player_id = self.player_id
                chat.chat_type = 0
                chat.value = rng.choice(CHAT_LINES)
                self.send(chat)
                self.stats["chat"] += 1
            elif roll < 0.85:
                self.class_id = rng.choice(CLASSES)
                change = P.ChangeClass()
                change.player_id = self.player_id
                change.class_id = self.class_id
                self.send(change)
                self.stats["class_changes"] += 1
            else:
                self.team = 5 - self.team
                change = P.ChangeTeam()
                change.player_id = self.player_id
                change.team = self.team
                self.send(change)
                self.stats["team_changes"] += 1

    def step(self, now: float) -> None:
        if self.state == IDLE:
            if now >= self.next_action:
                try:
                    self.begin(now)
                except Exception:  # noqa: BLE001
                    self.fail(now, "begin")
            return
        self.pump(now)
        if self.host is None:
            return
        if self.state == PLAYING:
            if now >= self.leave_at:
                self.leave(now)
                return
            try:
                self.play(now)
            except Exception:  # noqa: BLE001
                self.stats["play_errors"] += 1
        elif self.state == LEAVING:
            if now >= self.deadline:
                self.stats["graceful_timeouts"] += 1
                self.close()
                self.next_action = now + self.rng.uniform(self.args.gap_min, self.args.gap_max)
        elif self.state == MAP and self.leave_kind == "prejoin" and now >= self.leave_at:
            # Walk away in the middle of the map transfer.
            self.leave_kind = self.rng.choice(("abrupt", "vanish"))
            self.stats["leave_prejoin"] += 1
            self.leave(now)
            self.leave_kind = "prejoin"
        elif now >= self.deadline:
            self.fail(now, STATE_NAMES[self.state])
        try:
            if self.host is not None:
                self.host.flush()
        except Exception:  # noqa: BLE001
            pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=28500)
    parser.add_argument("--slots", type=int, default=6)
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--name", default="Churn")
    parser.add_argument("--tool", type=int, default=2)
    parser.add_argument("--dwell-min", type=float, default=15.0)
    parser.add_argument("--dwell-max", type=float, default=90.0)
    parser.add_argument("--long-share", type=float, default=0.25,
                        help="share of joins that stay long enough to cross a map change")
    parser.add_argument("--long-min", type=float, default=200.0)
    parser.add_argument("--long-max", type=float, default=420.0)
    parser.add_argument("--gap-min", type=float, default=2.0)
    parser.add_argument("--gap-max", type=float, default=20.0)
    parser.add_argument("--stagger", type=float, default=20.0)
    parser.add_argument("--abrupt-share", type=float, default=0.25)
    parser.add_argument("--vanish-share", type=float, default=0.15)
    parser.add_argument("--prejoin-share", type=float, default=0.10)
    parser.add_argument("--force-full-map", action="store_true", default=True)
    parser.add_argument("--matching-crc", dest="force_full_map", action="store_false")
    parser.add_argument("--report-seconds", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument("--out", default="")
    parser.add_argument("--stop-file", default="")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    rng = random.Random(args.seed)
    stats = {
        name: 0 for name in (
            "connect_attempts", "connects", "joins", "server_disconnects",
            "graceful_acks", "graceful_timeouts", "packets_in", "bytes_in",
            "client_data", "chat", "class_changes", "team_changes",
            "decode_errors", "play_errors", "map_ended_seen",
            "leave_graceful", "leave_abrupt", "leave_vanish", "leave_prejoin",
        )
    }
    slots = [Slot(i, args, random.Random(rng.random()), stats) for i in range(args.slots)]
    stopping = False

    def request_stop(*_):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):
            pass
    out = open(args.out, "a", encoding="utf-8") if args.out else None
    started = time.monotonic()
    deadline = started + args.minutes * 60.0
    next_report = started + args.report_seconds
    next_tick = started
    try:
        while not stopping:
            now = time.monotonic()
            if now >= deadline:
                break
            for slot in slots:
                try:
                    slot.step(now)
                except Exception:  # noqa: BLE001
                    stats["play_errors"] += 1
                    slot.fail(now, "step")
            if now >= next_report:
                next_report = now + args.report_seconds
                if args.stop_file and os.path.exists(args.stop_file):
                    break
                row = {
                    "t": round(now - started, 1),
                    "wall": time.time(),
                    "states": [STATE_NAMES[s.state] for s in slots],
                }
                row.update(stats)
                text = json.dumps(row)
                if out is not None:
                    out.write(text + "\n")
                    out.flush()
                else:
                    print(text, flush=True)
            next_tick += TICK
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()
    finally:
        now = time.monotonic()
        for slot in slots:
            if slot.host is not None:
                try:
                    slot.peer.disconnect_now()
                    slot.host.flush()
                except Exception:  # noqa: BLE001
                    pass
                slot.close()
        row = {"t": round(now - started, 1), "wall": time.time(), "final": True}
        row.update(stats)
        text = json.dumps(row)
        if out is not None:
            out.write(text + "\n")
            out.close()
        else:
            print(text, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
