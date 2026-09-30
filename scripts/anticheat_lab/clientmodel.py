"""A legitimate protocol-168 client, headless.

What is modelled (sources in brackets):

* One update per frame at ``hz`` (60): one loop label, one movement step, one
  unsequenced ClientData; ClockSync every 60 loops, relabel only outside the
  +/-10 loop dead band using the half round trip [stock gameScene.pyd, see
  BattleSpadesClient protocol168_clock.hpp].
* Movement prediction with the SAME native mover and class profile the
  server uses (``aoslib.world.Player``), composed per frame exactly as the
  server composes a consumed input frame (locomotion/aim of the previous
  packet, crouch of the current one). Without network impairment the client
  and the server therefore agree to the last bit, which is the calibrated
  retail state (0 ADJUST on LAN, docs/NETCODE_RECONCILIATION.md).
* Everything the retail owner learns from the server arrives with the
  network delay and is applied on arrival: jetpack/parachute state (self
  row action bit 0x04 / state bit 0x01), explosion knockback (Damage 37),
  spawns, deaths, teleports.
* Retail reconciliation: history row with the self row's exact stamp,
  squared distance > 16 -> SNAP (history wiped), > 0.01 -> ADJUST (take the
  server state, replay the buffered frames), else nothing.
* Weapon cadence, magazines and reloads from the stock weapon table; every
  packet except ClientData/ClockSync is reliable.

What is NOT modelled: rendering, audio, the jump-frame position restore of
the unpatched stock pyd (the shipped pyd has it patched out), terrain
prediction lag (the client reads the server's terrain directly).

``shot_phase`` selects which frame an action packet describes:
0 = label L with the position after frame L's movement as the server defines
it; 1 = label L with the position one frame later (the native client sends
its actions after the tick that follows the ClientData label).
"""

from __future__ import annotations

import math
import random
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable, Optional

import shared.constants as C
from server.class_selection import normalize_server_selection
from server.explosions import explosion_impulse
from server.game_constants import WEAPON_CATALOG, WEAPON_PROFILES
from server.player import Player

from . import wire
from .link import RELIABLE, UNSEQUENCED

FRAME = 1.0 / 60.0
POSITION_RESET_TOLERANCE_SQ = 16.0
POSITION_TOLERANCE_SQ = 0.010000000000000002
MAX_CLOCK_SYNC_DIFFERENCE = 10
CLOCK_SYNC_RATE = 60
MAX_PING_MS = 10000
HISTORY_LIMIT = 128

JETPACK_IDS = (66, 67, 68, 69)
PARACHUTE_ID = int(C.A370)
# Damage(37) types whose stock handler pushes the local character
# (server/main.py STOCK_EXPLOSION_DAMAGE_TYPES).
EXPLOSION_TYPES = frozenset((
    7, 8, 9, 10, 11, 12, 13, 15, 16, 18, 19, 20, 21, 22, 23, 24,
    30, 33, 37, 38, 39, 40, 41,
))

# Pull-out time before a freshly selected tool acts. The stock client plays
# a pull-out animation; half a second is on the slow (honest) side.
TOOL_SWITCH_DELAY = 0.5

MELEE_TOOLS = frozenset(
    tool for tool, profile in WEAPON_CATALOG.items() if profile.is_melee
)
GUN_TOOLS = frozenset(WEAPON_PROFILES)


@dataclass
class Intent:
    """What the scripted human does this frame."""

    up: bool = False
    down: bool = False
    left: bool = False
    right: bool = False
    jump: bool = False
    crouch: bool = False
    sneak: bool = False
    sprint: bool = False
    primary: bool = False
    secondary: bool = False
    zoom: bool = False
    hover: bool = False
    aim: tuple = (1.0, 0.0, 0.0)
    tool: Optional[int] = None

    def flags(self) -> tuple:
        return (self.up, self.down, self.left, self.right, self.jump,
                self.crouch, self.sneak, self.sprint)


@dataclass
class _Frame:
    """One simulated frame: the inputs used and the state it produced."""

    label: int
    flags: tuple
    orientation: tuple
    hover: bool
    jet: bool
    chute: bool
    position: tuple = (0.0, 0.0, 0.0)
    velocity: tuple = (0.0, 0.0, 0.0)


@dataclass
class _Gun:
    clip: int = 0
    reserve: int = 0
    cooldown: float = 0.0
    reload_left: float = 0.0
    # Character.update_alive starts a requested reload only once the
    # weapon_shoot animation (length = shoot_interval) has finished.
    reload_pending: bool = False
    burst_left: int = 0
    burst_cooldown: float = 0.0
    minigun_interval: float = 0.3


@dataclass
class _Launcher:
    """A thrown or launched tool: count, optional clip, cadence."""

    clip: int = 0
    clip_size: int = 0
    reserve: int = 0
    cooldown: float = 0.0
    reload_left: float = 0.0
    reload_pending: bool = False
    reload_time: float = 0.0
    interval: float = 0.5
    clip_reload: bool = False


@dataclass
class ClientStats:
    frames: int = 0
    client_data_sent: int = 0
    shots_sent: int = 0
    melee_sent: int = 0
    throws_sent: int = 0
    blocks_sent: int = 0
    prefabs_sent: int = 0
    deployables_sent: int = 0
    reloads_sent: int = 0
    adjusts: int = 0
    snaps: int = 0
    no_history: int = 0
    relabels: int = 0
    deaths: int = 0
    spawns: int = 0
    max_adjust: float = 0.0
    blasts_applied: int = 0
    shots_by_tool: dict = field(default_factory=dict)


class ClientEnv:
    """What a client needs from its surroundings."""

    def __init__(self, world_manager, config, now: Callable[[], float],
                 blasts: Optional[dict] = None, teams=None) -> None:
        self.world_manager = world_manager
        self.config = config
        self.now = now
        # (rounded x, y, z, damage type) -> explosion parameters, filled by
        # the lab at the moment the server resolves a blast (the stock client
        # derives the same numbers from its own explosion tables).
        self.blasts = blasts if blasts is not None else {}
        self.teams = teams or {}


class ClientModel:
    def __init__(
        self,
        env: ClientEnv,
        name: str,
        *,
        team: int = 2,
        class_id: int = 0,
        loadout=(),
        prefabs=(),
        seed: int = 1,
        shot_phase: int = 0,
        map_crc: int = 0,
    ) -> None:
        self.env = env
        self.name = name
        self.team = int(team)
        self.class_id = int(class_id)
        self.wanted_loadout = tuple(int(t) for t in loadout)
        self.wanted_prefabs = tuple(prefabs)
        self.shot_phase = 1 if shot_phase else 0
        self.map_crc = int(map_crc)
        self.rng = random.Random(seed)
        self.transport: Optional[Callable[[bytes, str], None]] = None

        self.state = "connecting"   # handshake / selecting / alive / dead
        self.player_id: Optional[int] = None
        self.loop = 0
        self.world_loop = 0
        self.hp = 100
        self.loadout: list[int] = []
        self.prefabs: list[str] = []
        self.tool = int(C.RIFLE_TOOL)
        self.intent = Intent()
        self._previous = Intent()
        self._sent_orientation = (1.0, 0.0, 0.0)
        self.script = None
        self.stats = ClientStats()

        self.helper: Optional[Player] = None
        self.history: "OrderedDict[int, _Frame]" = OrderedDict()
        self.net_position = None
        self.net_velocity = None
        self.net_stamp: Optional[int] = None
        self._net_fresh = False
        self.jet_advertised = False
        self.chute_advertised = False
        self.jetpack_fuel = 100.0
        self.roster: dict[int, dict] = {}
        self._pending_blasts: deque = deque()
        self.guns: dict[int, _Gun] = {}
        self.launchers: dict[int, _Launcher] = {}
        self.deployables: dict[int, list] = {}
        self.oriented_cooldown: dict[int, float] = {}
        self.melee_cooldown: dict[int, float] = {}
        self.block_cooldown = 0.0
        self.blocks = 0
        self.switch_delay = 0.0
        self._clock_frames = 0
        self._queued_actions: deque = deque()
        self._deferred: list = []
        self.disconnected: Optional[int] = None
        self.log: list = []
        # Cheats patch these hooks; a legitimate client leaves them alone.
        self.mutate_client_data = None
        self.mutate_action = None
        self.cooldown_scale = 1.0
        self.ignore_ammo = False

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def send(self, packet: bytes) -> None:
        if self.transport is None or not packet:
            return
        kind = UNSEQUENCED if packet[0] in wire.UNSEQUENCED_IDS else RELIABLE
        self.transport(packet, kind)

    def hello(self) -> None:
        """First packet after the ENet connect: the ticket-less hello."""

        self.state = "handshake"
        self.send(wire.steam_ticket())

    def on_datagram(self, datagram: bytes) -> None:
        try:
            packet = wire.unwrap(datagram)
        except Exception:  # noqa: BLE001
            return
        self.on_packet(packet)

    def on_disconnect(self, reason: int) -> None:
        self.disconnected = int(reason)
        self.state = "disconnected"

    # ------------------------------------------------------------------
    # inbound packets
    # ------------------------------------------------------------------

    def on_packet(self, packet: bytes) -> None:
        packet_id, parsed = wire.decode(packet)
        if packet_id is None:
            return
        if packet_id == 114:        # InitialInfo: answer with our map CRC
            self.send(wire.map_validation(self.map_crc))
            return
        if packet_id == 45 and parsed is not None:   # StateData
            self.player_id = int(parsed.player_id)
            if self.state in ("handshake", "connecting"):
                self.state = "selecting"
                self._join()
            return
        if parsed is None:
            return
        if packet_id == 0:
            self._on_clock_sync(parsed)
        elif packet_id == 2:
            self._on_world_update(parsed)
        elif packet_id == 28:
            self._on_create_player(parsed)
        elif packet_id == 46:
            self._on_kill(parsed)
        elif packet_id == 5:
            self.hp = int(parsed.hp)
        elif packet_id == 37:
            self._on_damage(parsed)
        elif packet_id == 69:
            if int(parsed.player_id) == self.player_id:
                self._on_restock(int(parsed.type))

    def _join(self) -> None:
        self.send(wire.set_class_loadout(
            player_id=self.player_id or 0, class_id=self.class_id,
            loadout=self.wanted_loadout, prefabs=self.wanted_prefabs,
        ))
        self.send(wire.new_player(
            name=self.name, team=self.team, class_id=self.class_id,
        ))

    def _on_clock_sync(self, packet) -> None:
        now_ms = int(self.env.now() * 1000.0) % MAX_PING_MS
        ping = now_ms - int(packet.client_time)
        if ping < 0:
            ping += MAX_PING_MS
        latency = ping * 0.001 * 0.5
        estimate = int(packet.server_loop_count) + int(latency * 60)
        if abs(estimate - self.loop) > MAX_CLOCK_SYNC_DIFFERENCE:
            # Never reuse a label already sent: the server drops it as stale.
            self.loop = max(self.loop, estimate)
            self.stats.relabels += 1

    def _on_create_player(self, packet) -> None:
        player_id = int(packet.player_id)
        self.roster[player_id] = {
            "team": int(packet.team), "alive": True,
            "position": (float(packet.x), float(packet.y), float(packet.z)),
            "crouch": False,
        }
        if player_id != self.player_id:
            return
        self.team = int(packet.team)
        self.class_id = int(packet.class_id)
        self.loadout = [int(t) for t in packet.loadout]
        self.prefabs = [str(p) for p in packet.prefabs]
        self._spawn((float(packet.x), float(packet.y), float(packet.z)))

    def _on_kill(self, packet) -> None:
        victim = int(packet.player_id)
        if victim in self.roster:
            self.roster[victim]["alive"] = False
        if victim == self.player_id and self.state == "alive":
            self.state = "dead"
            self.stats.deaths += 1
            self.history.clear()

    def _on_world_update(self, packet) -> None:
        self.world_loop = int(packet.loop_count)
        for player_id, row in packet.player_updates.items():
            position, _orientation, velocity = row[0], row[1], row[2]
            if int(player_id) == self.player_id:
                stamp = int(row[4])
                action = int(row[7])
                state = int(row[8])
                self.jet_advertised = bool(action & 0x04)
                self.chute_advertised = bool(state & 0x01)
                if len(row) > 10:
                    self.jetpack_fuel = float(row[10])
                # set_network_position_and_velocity dedupes equal stamps.
                if stamp != self.net_stamp:
                    self.net_stamp = stamp
                    self.net_position = tuple(float(v) for v in position)
                    self.net_velocity = tuple(float(v) for v in velocity)
                    self._net_fresh = True
                continue
            entry = self.roster.setdefault(
                int(player_id), {"team": -1, "alive": True}
            )
            entry["alive"] = True
            entry["position"] = tuple(float(v) for v in position)
            entry["crouch"] = bool(int(row[6]) & 0x20)

    def _on_damage(self, packet) -> None:
        if self.state != "alive" or int(packet.type) not in EXPLOSION_TYPES:
            return
        try:
            position = tuple(float(v) for v in packet.position)
        except (TypeError, ValueError):
            return
        # The stock client derives the push from its own explosion tables;
        # the lab hands over the same numbers the server used for this body.
        for record in self.env.blasts.get(self.player_id, ()):
            if record.get("used"):
                continue
            if math.dist(record["origin"], position) <= 1.0:
                record["used"] = True
                self._pending_blasts.append(record)
                break

    # ------------------------------------------------------------------
    # life
    # ------------------------------------------------------------------

    def _facade(self):
        env = self.env
        client = self

        class _Players(dict):
            def values(self_inner):  # noqa: N805 - dict API
                return client._collision_proxies()

            def __bool__(self_inner):  # noqa: N805
                return True

        server = SimpleNamespace(
            world_manager=env.world_manager,
            config=env.config,
            players=_Players(),
            teams=env.teams,
            loop_count=0,
            tick_rate=60,
            mode=None,
        )
        return SimpleNamespace(
            server=server, player=None, in_game=True,
            send=lambda *a, **k: None, disconnect=lambda *a, **k: None,
        )

    def _collision_proxies(self):
        proxies = []
        for player_id, entry in self.roster.items():
            if player_id == self.player_id or not entry.get("alive"):
                continue
            position = entry.get("position")
            if position is None:
                continue
            height = 1.8 if entry.get("crouch") else 2.7
            proxies.append(SimpleNamespace(
                alive=True, spawned=True, team=entry.get("team", -1),
                x=position[0], y=position[1], z=position[2],
                _current_height=lambda h=height: h,
            ))
        return proxies

    def _spawn(self, position) -> None:
        selection = normalize_server_selection(
            self.env.config, self.class_id, self.loadout, self.prefabs,
        )
        connection = self._facade()
        helper = Player(
            int(self.player_id or 0), self.name, self.team,
            int(C.RIFLE_TOOL), connection,
        )
        connection.player = helper
        helper._award_teabag_point = lambda: None
        helper.apply_class_selection(selection)
        # The server's committed loadout is the truth (CreatePlayer).
        helper.loadout = list(self.loadout)
        helper.spawn(*position)
        self.helper = helper
        self.history.clear()
        self.net_stamp = None
        self._net_fresh = False
        self.jet_advertised = False
        self.chute_advertised = False
        self._pending_blasts.clear()
        self._previous = Intent(aim=self.intent.aim)
        self.state = "alive"
        self.hp = 100
        self.stats.spawns += 1
        self.blocks = int(getattr(helper, "blocks", 0))
        self._restock()
        guns = [t for t in self.loadout if t in GUN_TOOLS]
        self.tool = guns[0] if guns else (self.loadout[0] if self.loadout else 0)
        self.intent.tool = None

    def _on_restock(self, kind: int) -> None:
        """Restock(69): 0 = the per-life reset, AMMO_CRATE tops up, 5 = blocks."""

        if kind == 0:
            self._restock()
        elif kind == int(getattr(C, "AMMO_CRATE", 3)):
            for tool, gun in self.guns.items():
                profile = WEAPON_PROFILES[tool]
                amount = int(getattr(profile, "restock_amount", -1))
                if amount < 0:
                    amount = int(profile.reserve_ammo)
                gun.reserve = min(gun.reserve + amount, int(profile.reserve_ammo))
        elif kind == 5 and self.helper is not None:
            self.blocks = int(self.helper._block_wallet_max())

    def _restock(self) -> None:
        from server.deployable_inventory import STOCK_RULES
        from server.player import ORIENTED_STOCK_AMMO

        self.guns = {}
        self.launchers = {}
        self.deployables = {}
        for tool in self.loadout:
            tool = int(tool)
            profile = WEAPON_PROFILES.get(tool)
            if profile is not None:
                initial = int(getattr(profile, "initial_reserve", -1))
                self.guns[tool] = _Gun(
                    clip=int(profile.clip_size),
                    reserve=int(profile.reserve_ammo) if initial < 0 else initial,
                    minigun_interval=0.3,
                )
                continue
            stock = ORIENTED_STOCK_AMMO.get(tool)
            catalog = WEAPON_CATALOG.get(tool)
            if stock is not None and catalog is not None:
                _mag_max, mag_initial, reserve_max, reserve_initial, _r = stock
                has_clip = (
                    tool in Player._RELOADING_LAUNCHERS
                    and float(catalog.reload_time or 0.0) > 0.0
                )
                self.launchers[tool] = _Launcher(
                    clip=int(mag_initial),
                    clip_size=int(catalog.clip_size or 0) if has_clip else 0,
                    reserve=int(reserve_initial) if reserve_max else 0,
                    reload_time=float(catalog.reload_time or 0.0) if has_clip else 0.0,
                    interval=float(catalog.fire_interval or 0.5),
                    clip_reload=tool in Player._CLIP_RELOAD_LAUNCHERS,
                )
                continue
            rule = STOCK_RULES.get(tool)
            if rule is not None:
                # [stock, cooldown, interval]
                self.deployables[tool] = [int(rule.initial), 0.0, float(rule.interval)]
        if int(C.ROCKET_TURRET_TOOL) in self.loadout:
            self.deployables[int(C.ROCKET_TURRET_TOOL)] = [
                int(getattr(C, "ROCKET_TURRET_INITIAL_STOCK", 2)), 0.0,
                float(C.ROCKET_TURRET_SHOOT_INTERVAL),
            ]
        if int(getattr(C, "SNOWBLOWER_TOOL", 29)) in self.loadout:
            catalog = WEAPON_CATALOG.get(int(C.SNOWBLOWER_TOOL))
            self.launchers[int(C.SNOWBLOWER_TOOL)] = _Launcher(
                clip=1 << 30, interval=float(catalog.fire_interval or 0.2),
            )

    @property
    def alive(self) -> bool:
        return self.state == "alive" and self.helper is not None

    @property
    def position(self) -> tuple:
        helper = self.helper
        return (float(helper.x), float(helper.y), float(helper.z))

    @property
    def velocity(self) -> tuple:
        helper = self.helper
        return (float(helper.vx), float(helper.vy), float(helper.vz))

    @property
    def airborne(self) -> bool:
        return bool(self.helper.airborne)

    @property
    def jetpack_id(self) -> int:
        return int(getattr(self.helper, "jetpack_id", 0) or 0)

    @property
    def parachute_id(self) -> int:
        return int(getattr(self.helper, "parachute_id", 0) or 0)

    # ------------------------------------------------------------------
    # one frame
    # ------------------------------------------------------------------

    def frame(self) -> None:
        """One client update. The transport delivers inbound packets first."""

        self.stats.frames += 1
        if self.state in ("connecting", "handshake", "selecting", "disconnected"):
            return
        self._clock_frames += 1
        if self._clock_frames >= CLOCK_SYNC_RATE:
            self._clock_frames = 0
            self.send(wire.clock_sync(int(self.env.now() * 1000.0) % MAX_PING_MS))

        if not self.alive:
            # Retail keeps streaming ClientData on the death screen.
            self._advance_script()
            self._send_client_data(neutral=True)
            self.loop += 1
            return

        self._reconcile()
        self._advance_script()
        intent = self.intent
        if intent.tool is not None and int(intent.tool) != self.tool:
            if int(intent.tool) in self.loadout:
                self.tool = int(intent.tool)
                self._on_tool_changed()
        aim = wire.normalize(intent.aim)

        previous = self._previous
        flags = list(previous.flags())
        flags[5] = bool(intent.crouch)
        frame = _Frame(
            label=self.loop,
            flags=tuple(flags),
            orientation=self._sent_orientation,
            hover=bool(previous.hover),
            jet=self._jet_physics(),
            chute=bool(self.chute_advertised),
        )
        self._apply_blasts()
        self._step(frame)
        frame.position = self.position
        frame.velocity = self.velocity
        self.history[frame.label] = frame
        while len(self.history) > HISTORY_LIMIT:
            self.history.popitem(last=False)

        self._send_client_data()
        self._flush_deferred()
        self._weapons(aim)
        self._run_actions(aim)

        self._previous = Intent(**{**intent.__dict__, "aim": aim})
        self._sent_orientation = wire.quantized_orientation(aim)
        self.loop += 1

    def _advance_script(self) -> None:
        if self.script is None:
            return
        try:
            next(self.script)
        except StopIteration:
            self.script = None

    def _jet_physics(self) -> bool:
        return bool(self.jet_advertised and self.jetpack_id in JETPACK_IDS)

    def _step(self, frame: _Frame) -> None:
        helper = self.helper
        helper.set_orientation_vector(*frame.orientation)
        helper.update_input(*frame.flags)
        helper.input.hover = bool(frame.hover)
        helper._jetpack_physics_active = bool(frame.jet)
        helper.jetpack_active = bool(frame.jet)
        helper._parachute_physics_active = bool(frame.chute)
        helper.parachute_active = bool(frame.chute)
        collisions = helper._build_player_collision_positions()
        helper._apply_input_state_to_world(
            trigger_jump=False, collisions=collisions
        )
        helper._world_object.update(FRAME, collisions)
        helper._sync_cached_vectors()

    def _set_state(self, position, velocity) -> None:
        world_object = self.helper._world_object
        world_object.set_position(*position)
        world_object.set_velocity(*velocity)
        self.helper._sync_cached_vectors()

    def _apply_blasts(self) -> None:
        while self._pending_blasts:
            blast = self._pending_blasts.popleft()
            impulse = explosion_impulse(
                blast["origin"], self.position, blast["radius"],
                blast["knockback_min"], blast["knockback_max"],
                crouched=bool(self.helper.input.crouch),
            )
            if impulse is None:
                continue
            vx, vy, vz = self.velocity
            self.helper._world_object.set_velocity(
                vx + impulse[0], vy + impulse[1], vz + impulse[2]
            )
            self.helper._sync_cached_vectors()
            self.stats.blasts_applied += 1

    def _reconcile(self) -> None:
        if not self._net_fresh or self.net_position is None:
            return
        self._net_fresh = False
        row = self.history.get(int(self.net_stamp))
        if row is None:
            # apply_player_network_correction: a label that is not in the
            # movement history means world_object.set_position(network
            # position); the velocity is kept.
            self.stats.no_history += 1
            if self.history:
                drift = math.dist(self.net_position, self.position)
                self.helper._world_object.set_position(*self.net_position)
                self.helper._sync_cached_vectors()
                self.log.append(("rollback", self.loop, drift))
            return
        distance_sq = sum(
            (a - b) ** 2 for a, b in zip(self.net_position, row.position)
        )
        if distance_sq > POSITION_RESET_TOLERANCE_SQ:
            self._set_state(self.net_position, self.net_velocity)
            self.history.clear()
            self.stats.snaps += 1
            self.log.append(("snap", self.loop, math.sqrt(distance_sq)))
            return
        if distance_sq <= POSITION_TOLERANCE_SQ:
            return
        self.stats.adjusts += 1
        self.stats.max_adjust = max(self.stats.max_adjust, math.sqrt(distance_sq))
        self._set_state(self.net_position, self.net_velocity)
        row.position = self.net_position
        row.velocity = self.net_velocity
        # The stock client keeps the corrected label and everything newer.
        for label in [label for label in self.history if label < row.label]:
            del self.history[label]
        for label, frame in self.history.items():
            if label <= row.label:
                continue
            self._step(frame)
            frame.position = self.position
            frame.velocity = self.velocity

    # ------------------------------------------------------------------
    # outbound
    # ------------------------------------------------------------------

    def _send_client_data(self, neutral: bool = False) -> None:
        intent = self.intent
        if neutral:
            flags = 0
            actions = 0
            aim = self._sent_orientation
        else:
            flags = wire.pack_flags(intent.flags())
            actions = wire.pack_flags((
                intent.primary, intent.secondary, intent.zoom, False,
                True, False, False, intent.hover,
            ))
            aim = wire.normalize(intent.aim)
        fields = {
            "loop": self.loop, "player_id": self.player_id or 0,
            "tool": self.tool, "orientation": aim, "flags": flags,
            "actions": actions,
        }
        if self.mutate_client_data is not None:
            fields = self.mutate_client_data(self, fields)
            if fields is None:
                return
        self.send(wire.client_data(**fields))
        self.stats.client_data_sent += 1

    def _action_frame(self):
        """(label, origin) an action of this frame is sent with."""

        if self.shot_phase:
            return max(0, self.loop - 1), self.position
        return self.loop, self.position

    def _emit(self, kind: str, build: Callable[[int, tuple], bytes]) -> None:
        """Send one action packet now (phase 0) or after the next frame."""

        if self.shot_phase:
            self._deferred.append((kind, build, self.loop))
            return
        label, origin = self.loop, self.position
        self._send_action(kind, build, label, origin)

    def _flush_deferred(self) -> None:
        if not self._deferred:
            return
        pending, self._deferred = self._deferred, []
        for kind, build, label in pending:
            self._send_action(kind, build, label, self.position)

    def _send_action(self, kind, build, label, origin) -> None:
        if self.mutate_action is not None:
            changed = self.mutate_action(self, kind, label, origin)
            if changed is None:
                return
            label, origin = changed
        packet = build(label, origin)
        if packet:
            self.send(packet)

    # -- weapons -----------------------------------------------------------

    def _on_tool_changed(self) -> None:
        # A tool switch abandons the running reload of every gun.
        for gun in self.guns.values():
            gun.reload_left = 0.0
            gun.reload_pending = False
            gun.burst_left = 0
            gun.minigun_interval = 0.3
        self.switch_delay = TOOL_SWITCH_DELAY

    def _weapons(self, aim) -> None:
        intent = self.intent
        self.block_cooldown = max(0.0, self.block_cooldown - FRAME)
        for tool in list(self.oriented_cooldown):
            self.oriented_cooldown[tool] = max(
                0.0, self.oriented_cooldown[tool] - FRAME
            )
        for entry in self.deployables.values():
            entry[1] = max(0.0, entry[1] - FRAME)
        for launcher in self.launchers.values():
            self._advance_launcher(launcher)
        tool = self.tool
        if self.switch_delay > 0.0:
            # The tool is still being pulled out.
            self.switch_delay = max(0.0, self.switch_delay - FRAME)
            return
        if tool in MELEE_TOOLS:
            self._melee(tool, aim, intent)
            return
        gun = self.guns.get(tool)
        profile = WEAPON_PROFILES.get(tool)
        if gun is None or profile is None:
            return
        scale = self.cooldown_scale
        gun.cooldown = max(0.0, gun.cooldown - FRAME)
        if gun.cooldown <= 1e-9:
            gun.cooldown = 0.0
        if gun.reload_pending and gun.cooldown == 0.0 and gun.burst_left == 0:
            gun.reload_pending = False
            self._start_reload(tool, gun, profile)
        elif gun.reload_left > 0.0:
            gun.reload_left = max(0.0, gun.reload_left - FRAME)
            if gun.reload_left <= 1e-9:
                gun.reload_left = 0.0
                self._finish_reload(tool, gun, profile, intent)
        if tool == int(C.MINIGUN_TOOL):
            held = (intent.primary or intent.secondary) and gun.reload_left == 0.0
            if held:
                gun.minigun_interval = max(0.1, gun.minigun_interval - 0.15 * FRAME)
            else:
                gun.minigun_interval = min(0.3, gun.minigun_interval + 0.075 * FRAME)
        if gun.burst_left > 0:
            gun.burst_cooldown -= FRAME
            if gun.burst_cooldown <= 1e-9 and gun.clip > 0:
                gun.burst_left -= 1
                gun.burst_cooldown = 0.1
                self._fire(tool, gun, profile, aim, intent)
            return
        if not intent.primary:
            return
        if gun.cooldown > 0.0 or gun.reload_pending:
            return
        if gun.reload_left > 0.0:
            clip_reload = bool(getattr(profile, "clip_reload", False))
            if not (clip_reload and gun.clip > 0):
                return
            gun.reload_left = 0.0   # firing interrupts a shell-by-shell reload
        if gun.clip <= 0 and not self.ignore_ammo:
            self._start_reload(tool, gun, profile)
            return
        if tool == int(C.MINIGUN_TOOL):
            if gun.minigun_interval >= 0.28:
                return
            interval = gun.minigun_interval
        else:
            interval = float(profile.fire_interval)
        self._fire(tool, gun, profile, aim, intent)
        gun.cooldown = interval * scale
        if tool == int(C.ASSAULT_RIFLE_TOOL):
            gun.burst_left = 2
            gun.burst_cooldown = 0.1

    def _fire(self, tool, gun, profile, aim, intent) -> None:
        if not self.ignore_ammo:
            gun.clip = max(0, gun.clip - 1)
        seed = self.rng.randint(1, 255)
        player_id = self.player_id or 0
        world_loop = self.world_loop
        block_damage = int(getattr(profile, "block_damage", 0) or 0)

        def build(label, origin):
            return wire.shoot(
                loop=label, player_id=player_id, world_loop=world_loop,
                origin=origin, direction=aim, damage=block_damage,
                penetration=2, seed=seed,
            )

        self._emit("shot", build)
        self.stats.shots_sent += 1
        self.stats.shots_by_tool[tool] = self.stats.shots_by_tool.get(tool, 0) + 1
        if gun.clip <= 0 and gun.reserve > 0 and not self.ignore_ammo:
            gun.reload_pending = True

    def _advance_launcher(self, launcher: _Launcher) -> None:
        launcher.cooldown = max(0.0, launcher.cooldown - FRAME)
        if launcher.cooldown <= 1e-9:
            launcher.cooldown = 0.0
        if launcher.reload_pending and launcher.cooldown == 0.0:
            launcher.reload_pending = False
            launcher.reload_left = launcher.reload_time
        elif launcher.reload_left > 0.0:
            launcher.reload_left = max(0.0, launcher.reload_left - FRAME)
            if launcher.reload_left <= 1e-9:
                launcher.reload_left = 0.0
                needed = launcher.clip_size - launcher.clip
                if launcher.clip_reload:
                    needed = min(1, needed)
                loaded = max(0, min(needed, launcher.reserve))
                launcher.clip += loaded
                launcher.reserve -= loaded
                if (launcher.clip_reload and launcher.clip < launcher.clip_size
                        and launcher.reserve > 0):
                    launcher.reload_left = launcher.reload_time

    def _start_reload(self, tool, gun, profile) -> None:
        if gun.reload_left > 0.0 or gun.reserve <= 0:
            return
        if gun.clip >= int(profile.clip_size):
            return
        gun.reload_left = float(profile.reload_time)
        self.send(wire.weapon_reload(player_id=self.player_id or 0, tool=tool))
        self.stats.reloads_sent += 1

    def _finish_reload(self, tool, gun, profile, intent) -> None:
        needed = max(0, int(profile.clip_size) - gun.clip)
        clip_reload = bool(getattr(profile, "clip_reload", False))
        if clip_reload:
            needed = min(1, needed)
        loaded = min(needed, gun.reserve)
        gun.clip += loaded
        gun.reserve -= loaded
        self.send(wire.weapon_reload(
            player_id=self.player_id or 0, tool=tool, done=True,
        ))
        if (clip_reload and gun.clip < int(profile.clip_size)
                and gun.reserve > 0 and not intent.primary):
            self._start_reload(tool, gun, profile)

    def request_reload(self) -> None:
        gun = self.guns.get(self.tool)
        profile = WEAPON_PROFILES.get(self.tool)
        if gun is None or profile is None:
            return
        if gun.reload_left > 0.0 or gun.reload_pending:
            return
        if gun.clip >= int(profile.clip_size) or gun.reserve <= 0:
            return
        if gun.cooldown > 0.0 or gun.burst_left:
            gun.reload_pending = True
        else:
            self._start_reload(self.tool, gun, profile)

    def _melee(self, tool, aim, intent) -> None:
        cooldown = max(0.0, self.melee_cooldown.get(tool, 0.0) - FRAME)
        self.melee_cooldown[tool] = cooldown
        if cooldown > 0.0 or not (intent.primary or intent.secondary):
            return
        profile = WEAPON_CATALOG.get(tool)
        interval = float(getattr(profile, "fire_interval", 0.5) or 0.5)
        if intent.secondary and not intent.primary:
            interval = float(
                getattr(profile, "secondary_fire_interval", 0.0) or interval
            )
        secondary = bool(intent.secondary and not intent.primary)
        player_id = self.player_id or 0
        world_loop = self.world_loop
        block_damage = int(getattr(profile, "block_damage", 0) or 0)

        def build(label, origin):
            return wire.shoot(
                loop=label, player_id=player_id, world_loop=world_loop,
                origin=origin, direction=aim, damage=block_damage,
                penetration=0, secondary=secondary, seed=0,
            )

        self._emit("melee", build)
        self.stats.melee_sent += 1
        self.stats.shots_by_tool[tool] = self.stats.shots_by_tool.get(tool, 0) + 1
        self.melee_cooldown[tool] = interval * self.cooldown_scale

    # -- one-shot actions ---------------------------------------------------

    def queue(self, action: str, **arguments) -> None:
        self._queued_actions.append((action, arguments))

    def _run_actions(self, aim) -> None:
        while self._queued_actions:
            action, arguments = self._queued_actions.popleft()
            handler = getattr(self, f"_do_{action}", None)
            if handler is not None:
                handler(aim, **arguments)

    def _do_reload(self, aim) -> None:
        self.request_reload()

    def can_throw(self, tool: int) -> bool:
        launcher = self.launchers.get(int(tool))
        if launcher is None or self.tool != int(tool) or self.switch_delay > 0.0:
            return False
        if self.ignore_ammo:
            return launcher.cooldown == 0.0
        if int(tool) == int(getattr(C, "SNOWBLOWER_TOOL", 29)) and self.blocks <= 0:
            return False
        return (
            launcher.cooldown == 0.0 and launcher.reload_left == 0.0
            and not launcher.reload_pending and launcher.clip > 0
        )

    def _do_throw(self, aim, tool, speed, fuse, interval=1.0, offset=0.0) -> None:
        tool = int(tool)
        if not self.can_throw(tool):
            return
        launcher = self.launchers[tool]
        launcher.cooldown = launcher.interval * self.cooldown_scale
        if not self.ignore_ammo:
            launcher.clip -= 1
            if tool == int(getattr(C, "SNOWBLOWER_TOOL", 29)):
                self.blocks = max(0, self.blocks - 1)
            if launcher.clip <= 0 and launcher.reserve > 0 and launcher.clip_size:
                launcher.reload_pending = True
        player_id = self.player_id or 0
        velocity_now = self.velocity

        def build(label, origin):
            position = tuple(origin[i] + aim[i] * offset for i in range(3))
            velocity = tuple(
                aim[i] * float(speed) + velocity_now[i] for i in range(3)
            )
            return wire.oriented_item(
                loop=label, player_id=player_id, tool=tool, fuse=fuse,
                position=position, velocity=velocity,
            )

        self._emit("throw", build)
        self.stats.throws_sent += 1

    def _do_block_line(self, aim, start, end, cost=None) -> None:
        if self.tool != int(C.BLOCK_TOOL) or self.block_cooldown > 0.0:
            return
        if self.switch_delay > 0.0:
            return
        cells = (
            sum(abs(int(a) - int(b)) for a, b in zip(start, end)) + 1
            if cost is None else int(cost)
        )
        if not self.ignore_ammo:
            if self.blocks < cells:
                return
            self.blocks -= cells
        self.block_cooldown = 0.5 * self.cooldown_scale
        player_id = self.player_id or 0

        def build(label, _origin):
            return wire.block_line(
                loop=label, player_id=player_id, start=start, end=end,
            )

        self._emit("block", build)
        self.stats.blocks_sent += 1

    def _do_prefab(self, aim, name, cell, yaw=0, color=0x707070) -> None:
        if self.tool != int(C.PREFAB_TOOL):
            return
        player_id = self.player_id or 0

        def build(label, _origin):
            return wire.build_prefab(
                loop=label, player_id=player_id, name=name, position=cell,
                yaw=yaw, color=color,
            )

        self._emit("prefab", build)
        self.stats.prefabs_sent += 1

    def _do_deploy(self, aim, packet_id, cell, face=None, yaw=None,
                   tool=None) -> None:
        if tool is not None:
            entry = self.deployables.get(int(tool))
            if entry is None or self.tool != int(tool) or self.switch_delay > 0.0:
                return
            if not self.ignore_ammo and (entry[0] <= 0 or entry[1] > 0.0):
                return
            entry[0] -= 1
            entry[1] = entry[2] * self.cooldown_scale
        player_id = self.player_id or 0

        def build(label, _origin):
            return wire.place_entity(
                int(packet_id), loop=label, cell=cell, player_id=player_id,
                face=face, yaw=yaw,
            )

        self._emit("deploy", build)
        self.stats.deployables_sent += 1

    def _do_flare(self, aim, cell) -> None:
        self._emit("deploy", lambda label, _o: wire.place_flare(loop=label, cell=cell))
        self.stats.deployables_sent += 1

    def _do_disguise(self, aim, active=True) -> None:
        import shared.packet as P

        self._emit("deploy", lambda label, _o: wire.simple(
            P.DisguisePacket, loop_count=label, active=1 if active else 0,
        ))
        self.stats.deployables_sent += 1

    def _do_detonate(self, aim) -> None:
        import shared.packet as P

        self._emit("deploy", lambda label, _o: wire.simple(
            P.DetonateC4, loop_count=label,
        ))

    def _do_use(self, aim) -> None:
        import shared.packet as P

        self.send(wire.simple(P.UseCommand))

    def _do_color(self, aim, value) -> None:
        self.send(wire.set_color(player_id=self.player_id or 0, value=value))

    def _do_class(self, aim, class_id, loadout=(), prefabs=()) -> None:
        self.send(wire.set_class_loadout(
            player_id=self.player_id or 0, class_id=class_id,
            loadout=loadout, prefabs=prefabs,
        ))
        self.send(wire.change_class(
            player_id=self.player_id or 0, class_id=class_id,
        ))

    def _do_team(self, aim, team) -> None:
        self.send(wire.change_team(player_id=self.player_id or 0, team=team))

    def _do_raw(self, aim, packet) -> None:
        self.send(packet)
