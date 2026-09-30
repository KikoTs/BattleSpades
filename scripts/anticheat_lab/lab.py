"""In-process laboratory: the real server, no sockets, a virtual clock.

``Lab`` builds a real :class:`server.main.BattleSpadesServer`, runs its real
fixed-step tick (``SimulationRuntime.step`` + WorldUpdate broadcast) and
attaches :class:`~anticheat_lab.clientmodel.ClientModel` clients through
:mod:`anticheat_lab.link`. The server never opens a socket, never registers
with a master server and never touches logs on disk.

Time is virtual: one server tick is 1/60 s of lab time however long it takes
to compute, so a minute of play runs in a few seconds and a run is
reproducible from its seed. Server modules read the lab clock through a
proxy of the ``time`` module installed only in the server packages
(``asyncio`` keeps the real clock).
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import math
import sys
import time as _real_time
from collections import Counter, defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import shared.constants as C  # noqa: E402
from server import anticheat  # noqa: E402
from server.config import load_config  # noqa: E402
from server.connection import Connection  # noqa: E402
from server.main import BattleSpadesServer  # noqa: E402
from server.player import Player  # noqa: E402

from . import wire  # noqa: E402
from .clientmodel import ClientEnv, ClientModel  # noqa: E402
from .link import (  # noqa: E402
    RELIABLE, SEQUENCED, UNSEQUENCED, Impairment, Link, NetProfile, PROFILES,
)

TICK = 1.0 / 60.0
_CLOCKED_PACKAGES = ("server", "modes", "protocol", "commands", "plugins")


class VirtualClock:
    """Lab time, exposed to server modules as a stand-in ``time`` module."""

    def __init__(self) -> None:
        self.now = 0.0
        self.monotonic_base = 5000.0
        self.wall_base = 1_790_000_000.0
        self._patched: dict = {}

    # -- the ``time`` API the server uses ------------------------------------
    def monotonic(self) -> float:
        return self.monotonic_base + self.now

    def time(self) -> float:
        return self.wall_base + self.now

    def monotonic_ns(self) -> int:
        return int(self.monotonic() * 1_000_000_000)

    def __getattr__(self, name):
        return getattr(_real_time, name)

    # -- installation ------------------------------------------------------
    def install(self) -> None:
        for name, module in tuple(sys.modules.items()):
            if module is None or name in self._patched:
                continue
            if name.split(".", 1)[0] not in _CLOCKED_PACKAGES:
                continue
            if getattr(module, "time", None) is _real_time:
                self._patched[name] = module
                module.time = self

    def uninstall(self) -> None:
        for module in self._patched.values():
            if getattr(module, "time", None) is self:
                module.time = _real_time
        self._patched.clear()


class _Address:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


class LabPeer:
    """Stands in for an ENet peer on the server side."""

    def __init__(self, lab: "Lab", client: "LabClient", index: int) -> None:
        self.lab = lab
        self.client = client
        self.address = _Address(f"10.77.{index // 250}.{index % 250 + 1}", 40000 + index)
        self.mtu = 1400
        self.packetThrottle = 32
        self.packetThrottleDeceleration = 0
        self.reliableDataInTransit = 0
        self.roundTripTime = max(1, int(round(client.profile.rtt_ms)))
        self.host = None
        self.disconnected: Optional[int] = None
        # Reliable packets are kept until their (virtual) acknowledgement so
        # the server's free-callback sees a realistic acknowledgement time.
        self._unacked: list = []

    def send(self, channel, packet) -> int:
        import enet

        flags = int(getattr(packet, "flags", 0))
        data = bytes(packet.data)
        if flags & enet.PACKET_FLAG_RELIABLE:
            kind = RELIABLE
        elif flags & enet.PACKET_FLAG_UNSEQUENCED:
            kind = UNSEQUENCED
        else:
            kind = SEQUENCED
        now = self.lab.clock.now
        self.client.downlink.send(data, kind, now)
        if kind == RELIABLE:
            self.reliableDataInTransit += len(data)
            heapq.heappush(self._unacked, (
                now + self.client.profile.rtt_ms / 1000.0 + TICK,
                id(packet), len(data), packet,
            ))
        return 0

    def acknowledge(self, now: float) -> None:
        while self._unacked and self._unacked[0][0] <= now:
            _at, _key, size, _packet = heapq.heappop(self._unacked)
            self.reliableDataInTransit = max(0, self.reliableDataInTransit - size)

    def disconnect(self, reason: int = 0) -> None:
        if self.disconnected is None:
            self.disconnected = int(reason)
            self.lab._disconnects.append((self, int(reason)))

    def disconnect_later(self, reason: int = 0) -> None:
        self.disconnect(reason)

    def disconnect_now(self, reason: int = 0) -> None:
        self.disconnect(reason)

    def reset(self) -> None:
        self.disconnect(0)


class LabClient:
    """One client, its two links and its server-side counterpart."""

    def __init__(self, lab: "Lab", model: ClientModel, profile: NetProfile,
                 index: int, *, hz: float, hitches) -> None:
        self.lab = lab
        self.model = model
        self.profile = profile
        self.index = index
        self.hz = float(hz)
        # (every_seconds, stall_seconds): the client stops updating for the
        # stall and then runs the frames it missed (fixed-step catch-up).
        self.hitches = hitches
        self._next_hitch = None if not hitches else hitches[0]
        self.peer = LabPeer(lab, self, index)
        seed = lab.seed * 1000 + index * 2
        self.uplink = Link(Impairment(profile, seed), self._to_server)
        self.downlink = Link(Impairment(profile, seed + 1), self._to_client)
        self.connection: Optional[Connection] = None
        self.player = None
        self.next_frame = 0.0
        self.kicked: Optional[int] = None
        model.transport = self._send

    def _send(self, packet: bytes, kind: str) -> None:
        self.uplink.send(wire.wrap(packet), kind, self.lab.clock.now)

    def _to_server(self, data: bytes, kind: str) -> None:
        self.lab._inbound.append((self, data))

    def _to_client(self, data: bytes, kind: str) -> None:
        self.model.on_datagram(data)

    @property
    def name(self) -> str:
        return self.model.name


class Lab:
    def __init__(
        self,
        *,
        profile: NetProfile | str = "lan",
        map_name: Optional[str] = None,
        mode: str = "tdm",
        anticheat_settings: Optional[dict] = None,
        config_overrides: Optional[dict] = None,
        seed: int = 1,
        court: bool = True,
    ) -> None:
        self.build_court = bool(court)
        self.court = None
        self.profile = PROFILES[profile] if isinstance(profile, str) else profile
        self.map_name = map_name
        self.mode = mode
        self.anticheat_settings = dict(anticheat_settings or {})
        self.config_overrides = dict(config_overrides or {})
        self.seed = int(seed)
        self.clock = VirtualClock()
        self.server: Optional[BattleSpadesServer] = None
        self.clients: list[LabClient] = []
        self.dummies: list = []
        self.blasts: dict = defaultdict(deque)
        self.accepted: dict = defaultdict(Counter)
        self.offered: dict = defaultdict(Counter)
        self.anticheat_lines: list = []
        self.kicks: list = []
        self._inbound: deque = deque()
        self._disconnects: list = []
        self._tasks: set = set()
        self._undo: list = []
        self._log_handler = None
        self._next_tick = TICK
        self.env: Optional[ClientEnv] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _config(self):
        config = load_config(ROOT / "config.toml")
        config.port = 28399
        config.max_players = 32
        config.bot_count = 0
        config.bots.configured = True
        config.bots.enabled = False
        config.default_mode = self.mode
        config.steam.enabled = False
        config.revival.enabled = False
        config.revival.require_identity = False
        config.debug_parity = False
        config.debug_selfrow = False
        config.movement_debug_capture = False
        config.packet_trace = False
        config.log_console = False
        config.auto_balance = False
        if hasattr(config, "plugins_enabled"):
            config.plugins_enabled = False
        for name, value in self.config_overrides.items():
            target = config
            parts = name.split(".")
            for part in parts[:-1]:
                target = getattr(target, part)
            setattr(target, parts[-1], value)
        for name, value in self.anticheat_settings.items():
            setattr(config.anticheat, name, value)
        if self.map_name:
            config.default_map = self.map_name
        return config

    async def start(self) -> None:
        from modes import get_mode_class

        config = self._config()
        server = BattleSpadesServer(config)
        self.server = server
        world = server.world_manager
        if self.map_name:
            world.load_map(config.map_name)
        else:
            from aoslib.vxl import VXL

            world.map = VXL(-1, b"", 0, 2)
            world.map_name = "lab_flat"
            world._refresh_world()
        if self.build_court:
            from .arena import build_court

            self.court = build_court(world)
        self.clock.install()
        mode_class = get_mode_class(config.game_mode)
        server.mode = mode_class(server)
        await server.mode.on_mode_start()
        server.running = True
        self.clock.install()
        self._instrument()
        self.env = ClientEnv(
            world, config, lambda: self.clock.now, blasts=self.blasts,
            teams=server.teams,
        )

    def _instrument(self) -> None:
        server = self.server
        lab = self

        class _Capture(logging.Handler):
            def emit(self, record):  # noqa: D401 - logging API
                try:
                    lab.anticheat_lines.append(
                        (round(lab.clock.now, 3), record.getMessage())
                    )
                except Exception:  # noqa: BLE001
                    pass

        self._log_handler = _Capture(level=logging.INFO)
        logging.getLogger("anticheat").addHandler(self._log_handler)

        original_queue = Player.queue_explosion_impulse

        def queue_explosion_impulse(player, after_input_frames, origin,
                                    blast_radius, knockback_min, knockback_max):
            lab.blasts[int(player.id)].append({
                "origin": tuple(float(v) for v in origin),
                "radius": float(blast_radius),
                "knockback_min": float(knockback_min),
                "knockback_max": float(knockback_max),
                "at": lab.clock.now,
            })
            return original_queue(
                player, after_input_frames, origin, blast_radius,
                knockback_min, knockback_max,
            )

        Player.queue_explosion_impulse = queue_explosion_impulse
        self._undo.append(
            lambda: setattr(Player, "queue_explosion_impulse", original_queue)
        )

        def count(owner, method_name, kind, player_index=0):
            original = getattr(owner, method_name, None)
            if original is None:
                return

            def wrapper(*args, **kwargs):
                player = args[player_index] if len(args) > player_index else None
                key = id(player)
                lab.offered[kind][key] += 1
                result = original(*args, **kwargs)
                if result:
                    lab.accepted[kind][key] += 1
                return result

            setattr(owner, method_name, wrapper)

        # Every class is selectable in the lab (an operator's custom mode):
        # the stock modes never offer the Rocketeer and its two jetpacks.
        from server.handlers import equipment

        original_selectable = equipment.is_class_selectable

        def is_class_selectable(server_, class_id):
            return server_.config is server.config and int(class_id) in C.CLASS_ITEMS \
                or original_selectable(server_, class_id)

        equipment.is_class_selectable = is_class_selectable
        self._undo.append(
            lambda: setattr(equipment, "is_class_selectable", original_selectable)
        )

        count(server.combat, "handle_block_line", "block_line")
        count(server.combat, "handle_weapon_reload", "reload")
        count(server.oriented_actions, "use", "throw")

    async def stop(self) -> None:
        for undo in reversed(self._undo):
            try:
                undo()
            except Exception:  # noqa: BLE001
                pass
        self._undo.clear()
        if self._log_handler is not None:
            logging.getLogger("anticheat").removeHandler(self._log_handler)
            self._log_handler = None
        for task in tuple(self._tasks):
            task.cancel()
        for task in tuple(self._tasks):
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
        self._tasks.clear()
        server = self.server
        if server is not None:
            server.running = False
            server._stopping = True
            for connection in tuple(server.connections.values()):
                connection.retire_for_server_shutdown()
            server.connections.clear()
        self.clock.uninstall()

    # ------------------------------------------------------------------
    # participants
    # ------------------------------------------------------------------

    def add_client(
        self,
        name: str,
        *,
        team: int = 2,
        class_id: int = 0,
        loadout=(),
        prefabs=(),
        shot_phase: int = 0,
        hz: float = 60.0,
        hitches=None,
        profile: NetProfile | str | None = None,
        seed: Optional[int] = None,
    ) -> LabClient:
        index = len(self.clients)
        if isinstance(profile, str):
            profile = PROFILES[profile]
        model = ClientModel(
            self.env, name, team=team, class_id=class_id, loadout=loadout,
            prefabs=prefabs, shot_phase=shot_phase,
            seed=self.seed * 7919 + index if seed is None else seed,
            map_crc=int(getattr(self.server.world_manager, "map_file_crc", 0) or 0),
        )
        client = LabClient(
            self, model, profile or self.profile, index, hz=hz, hitches=hitches,
        )
        client.next_frame = self.clock.now + (index % 7) * 0.0023
        self.clients.append(client)
        self.server._on_connect_sync(client.peer, 168)
        client.connection = self.server.connections.get(client.peer)
        model.hello()
        return client

    def add_dummy(self, name: str, position, *, team: int = 3,
                  class_id: int = 0) -> Player:
        """A server-owned body (a bot without a brain) to shoot at."""

        server = self.server
        player_id = server.get_next_player_id()
        connection = SimpleNamespace(
            server=server, player=None, in_game=False, peer=None,
            send=lambda *a, **k: None, disconnect=lambda *a, **k: None,
            send_snapshot=lambda *a, **k: None,
        )
        player = Player(player_id, name, team, int(C.RIFLE_TOOL), connection)
        player.is_bot = True
        connection.player = player
        from server.class_selection import normalize_server_selection

        player.apply_class_selection(
            normalize_server_selection(server.config, class_id)
        )
        server.players[player_id] = player
        if team in server.teams:
            server.teams[team].add_player(player)
        player.spawn(*position)
        self.dummies.append(player)
        create = getattr(server, "_broadcast_create_player", None)
        if callable(create):
            try:
                create(player, position)
            except Exception:  # noqa: BLE001
                pass
        return player

    # ------------------------------------------------------------------
    # time
    # ------------------------------------------------------------------

    async def run(self, seconds: float) -> None:
        end = self.clock.now + float(seconds)
        while True:
            next_frame = min(
                (c.next_frame for c in self.clients if c.kicked is None),
                default=math.inf,
            )
            upcoming = min(self._next_tick, next_frame)
            if upcoming > end:
                self.clock.now = end
                return
            self.clock.now = upcoming
            if self._next_tick <= upcoming:
                await self._tick()
                self._next_tick += TICK
            for client in self.clients:
                if client.kicked is None and client.next_frame <= upcoming:
                    self._frame(client)

    async def run_until(self, condition, timeout: float = 30.0) -> bool:
        """Advance until ``condition()``; yields to real time for handshakes."""

        deadline = self.clock.now + float(timeout)
        while self.clock.now < deadline:
            if condition():
                return True
            await self.run(0.05)
            await asyncio.sleep(0.002)
        return bool(condition())

    async def join_all(self, timeout: float = 60.0) -> bool:
        return await self.run_until(
            lambda: all(
                c.model.state in ("alive", "dead") or c.kicked is not None
                for c in self.clients
            ),
            timeout,
        )

    def _frame(self, client: LabClient) -> None:
        now = self.clock.now
        client.downlink.pump(now)
        frames = 1
        if client.hitches:
            every, stall = client.hitches
            if client._next_hitch is not None and now >= client._next_hitch:
                client._next_hitch = now + every
                # The client freezes: no frames, no packets. It resumes with
                # the frames it owes (the stock accumulator catches up).
                client.next_frame = now + stall
                client._owed = int(round(stall * client.hz))
                return
            owed = getattr(client, "_owed", 0)
            if owed:
                burst = min(owed, 5)
                client._owed = owed - burst
                frames += burst
        for _ in range(frames):
            client.model.frame()
        client.uplink.flush(now)
        client.next_frame += 1.0 / client.hz
        if client.next_frame < now - 1.0:
            client.next_frame = now

    async def _tick(self) -> None:
        server = self.server
        now = self.clock.now
        for client in self.clients:
            client.peer.acknowledge(now)
            if client.kicked is None:
                client.uplink.pump(now)
        while self._inbound:
            client, data = self._inbound.popleft()
            connection = server.connections.get(client.peer)
            if connection is None:
                continue
            if connection.player is not None:
                server._queue_ingame_packet(connection, data)
            else:
                task = asyncio.create_task(connection.on_receive(data))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        if self._tasks:
            await asyncio.sleep(0)
        server.loop_count += 1
        await server.simulation_runtime.step()
        server.simulation_runtime._guard_sync(
            "world_updates", server._broadcast_world_updates
        )
        for client in self.clients:
            if client.player is None and client.connection is not None:
                client.player = client.connection.player
            client.downlink.flush(now)
        if self._disconnects:
            pending, self._disconnects = self._disconnects, []
            for peer, reason in pending:
                client = peer.client
                client.kicked = reason
                self.kicks.append((round(now, 3), client.name, reason))
                client.model.on_disconnect(reason)
                server._on_disconnect_sync(peer)
        if server.loop_count % 120 == 0:
            self.clock.install()

    # ------------------------------------------------------------------
    # results
    # ------------------------------------------------------------------

    def results(self) -> dict:
        report = {}
        for client in self.clients:
            player = client.player
            model = client.model
            counts = dict(anticheat.counters(player)) if player is not None else {}
            stats = getattr(player, "anticheat_stats", None) or {}
            weapons = {
                int(tool): dict(entry)
                for tool, entry in (stats.get("weapons") or {}).items()
            }
            queue = None
            if player is not None:
                try:
                    queue = player.input_queue_delay_stats()
                except Exception:  # noqa: BLE001
                    queue = None
            report[client.name] = {
                "profile": client.profile.name,
                "kicked": client.kicked,
                "counts": counts,
                "rejected": dict(stats.get("rejected") or {}),
                "origin_error": dict(stats.get("origin_error") or {}),
                "origin_error_fallback": dict(stats.get("origin_error_fallback") or {}),
                "aim_angle": dict(stats.get("aim_angle") or {}),
                "aim_angle_fallback": dict(stats.get("aim_angle_fallback") or {}),
                "weapons": weapons,
                "server_shots": int(stats.get("shots", 0)),
                "input_queue": queue,
                "server_counters": {
                    name: int(getattr(player, name, 0) or 0)
                    for name in (
                        "input_frames_synthesized", "input_frames_catchup",
                        "input_frames_backlog_dropped", "input_frames_overflow",
                        "input_frames_stale", "input_starved_ticks",
                        "input_frames_starvation_steps",
                        "input_frames_rejected_ahead", "rejected_tool_updates",
                    )
                } if player is not None else {},
                "client": {
                    "frames": model.stats.frames,
                    "shots_sent": model.stats.shots_sent,
                    "melee_sent": model.stats.melee_sent,
                    "shots_by_tool": dict(model.stats.shots_by_tool),
                    "throws_sent": model.stats.throws_sent,
                    "blocks_sent": model.stats.blocks_sent,
                    "reloads_sent": model.stats.reloads_sent,
                    "adjusts": model.stats.adjusts,
                    "snaps": model.stats.snaps,
                    "no_history": model.stats.no_history,
                    "max_adjust": round(model.stats.max_adjust, 3),
                    "relabels": model.stats.relabels,
                    "deaths": model.stats.deaths,
                    "spawns": model.stats.spawns,
                    "blasts": model.stats.blasts_applied,
                },
                "accepted": {
                    kind: int(names.get(id(player), 0))
                    for kind, names in self.accepted.items()
                },
                "offered": {
                    kind: int(names.get(id(player), 0))
                    for kind, names in self.offered.items()
                },
                "link": {
                    "up": dict(client.uplink.stats),
                    "down": dict(client.downlink.stats),
                },
            }
        return report
