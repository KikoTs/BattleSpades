"""
Configuration loader for BattleSpades server.
"""

import toml
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Tuple, Optional


@dataclass
class ServerConfig:
    """Server configuration container."""

    # Server settings
    name: str = "BattleSpades Server"
    port: int = 32887
    max_players: int = 32
    tick_rate: int = 60

    # Network settings
    timeout_ms: int = 10000
    max_connections: int = 64
    bandwidth_limit: int = 0

    # Game settings
    default_mode: str = "ctf"
    default_map: str = "classicgen"
    respawn_time: float = 5.0
    friendly_fire: bool = False
    fall_damage: bool = True
    build_damage: bool = True
    score_limit: int = 10
    # "server": positions come from the server's own physics simulation.
    # "client": echo the client-reported position back (interim mode while
    # the physics engine is being brought to parity with the original game).
    movement_authority: str = "server"
    # "full": always stream the complete world state (REQUIRED: measured
    # 2026-06-12 — the client uses its local map file only to answer the
    # CRC validation; world content comes exclusively from the MapSync
    # stream, so an empty delta leaves the client world hollow).
    # "auto": changed-columns delta for matching CRCs — experimental.
    map_sync_mode: str = "full"

    # Team settings
    team1_name: str = "TEAM1_COLOR"
    team1_color: Tuple[int, int, int] = (44, 117, 179)
    team2_name: str = "TEAM2_COLOR"
    team2_color: Tuple[int, int, int] = (137, 179, 44)
    auto_balance: bool = True
    balance_threshold: int = 2

    # Dev bots: server-side AI players spawned at startup (0 = none).
    bot_count: int = 0

    # Map-entity (crate/intel) wire emission. The Entity byte layout was
    # RE-verified against the compiled client (shared/packet.pyx Entity.read/
    # write rewritten to match: id, type/state/player_id bytes, then pos/vel/
    # yaw/color-rgb/radius fixed-shorts, face byte, fuse short, int/float/ugc
    # count bytes, then int+float property arrays).
    entities_wire_ready: bool = True

    # Weapon damage
    rifle_damage: int = 49
    smg_damage: int = 29
    shotgun_damage: int = 27
    spade_damage: int = 50
    grenade_damage: int = 100

    # World settings
    map_size_x: int = 512
    map_size_y: int = 512
    map_size_z: int = 255
    water_level: int = 62
    water_damage: bool = True
    fog_color_rgb: Tuple[int, int, int] = (12, 13, 11)
    maps_path: str = "maps"

    # Admin settings
    admin_password: str = "changeme"
    log_commands: bool = True

    # Logging
    log_level: str = "INFO"
    log_file: str = "server.log"
    log_console: bool = True
    log_suppress_packets: List[int] = field(default_factory=lambda: [2, 4, 11])

    # Debug
    debug_parity: bool = True
    debug_parity_host: str = "127.0.0.1"
    debug_parity_port: int = 32895
    # A/B isolation switch: with WorldUpdate broadcasting off, the local
    # player runs on pure client prediction (no server corrections at all).
    # If walking is smooth with this off and chunky with it on, the jank
    # lives in the WorldUpdate/correction loop, not the movement engine.
    broadcast_world_updates: bool = True
    # Include each client's own row in the WorldUpdates it receives (the
    # original server did; the client reconciles self-rows against its
    # movement history at the packet's loop_count). Without self-rows the
    # client's network anchor never moves past CreatePlayer, and its engine
    # snaps the player back to spawn on jump (measured 6/6 jumps, two
    # sessions). Also lets one serialized packet serve every connection.
    # False = legacy per-connection exclusion.
    worldupdate_include_self: bool = True
    # Constant added to the per-recipient self-row stamp (the input tick
    # the server actually consumed for that player) — the client's history
    # indexing convention, latency-invariant. Measured best: -1.
    worldupdate_loop_offset: int = -1
    # Send the recipient's own row every Nth tick (others stream at 60Hz).
    # The original client runs ~58.5fps against our 60Hz ticks and skips a
    # loop ~1.5x/sec to stay clock-aligned; self-rows landing on skipped
    # loops mispair by one frame regardless of stamp, so fewer self-rows =
    # fewer residual micro-corrections. 2 == the original NETWORK_FPS=30.
    worldupdate_self_row_interval: int = 2
    # When true, append every self-row's (stamp, position) to
    # logs/selfrow_samples.ndjson for offline reconciliation calibration
    # (join with the client capture via tmp/reconcile_sim.py). Debug only.
    debug_selfrow: bool = False
    # Added to the loop_count we report in ClockSync replies. The client
    # paces its clock from this, so +1 makes it run one tick AHEAD of us:
    # ClientData stamped N then arrives while we are still at N-1 and is
    # guaranteed to be buffered before tick N simulates — without margin,
    # input application is a per-packet race (applied at N or N+1), which
    # no fixed WorldUpdate stamp offset can compensate.
    clock_sync_loop_bias: int = 1

    @property
    def map_name(self) -> str:
        return self.default_map

    @property
    def game_mode(self) -> str:
        return self.default_mode

    @property
    def server_name(self) -> str:
        return self.name

    @property
    def fog_color(self) -> Tuple[int, int, int]:
        return self.fog_color_rgb


def load_config(path: Optional[Path] = None) -> ServerConfig:
    """
    Load configuration from a TOML file.
    Falls back to defaults if file doesn't exist.
    """
    config = ServerConfig()

    if path is None:
        path = Path("config.toml")

    if not path.exists():
        return config

    try:
        data = toml.load(path)
    except Exception as e:
        print(f"Warning: Failed to load config from {path}: {e}")
        return config

    if "server" in data:
        s = data["server"]
        config.name = s.get("name", config.name)
        config.port = s.get("port", config.port)
        config.max_players = s.get("max_players", config.max_players)
        config.tick_rate = s.get("tick_rate", config.tick_rate)

    if "network" in data:
        n = data["network"]
        config.timeout_ms = n.get("timeout_ms", config.timeout_ms)
        config.max_connections = n.get("max_connections", config.max_connections)
        config.bandwidth_limit = n.get("bandwidth_limit", config.bandwidth_limit)

    if "game" in data:
        g = data["game"]
        config.default_mode = g.get("default_mode", config.default_mode)
        config.default_map = g.get("default_map", config.default_map)
        config.respawn_time = g.get("respawn_time", config.respawn_time)
        config.friendly_fire = g.get("friendly_fire", config.friendly_fire)
        config.fall_damage = g.get("fall_damage", config.fall_damage)
        config.build_damage = g.get("build_damage", config.build_damage)
        config.bot_count = int(g.get("bot_count", config.bot_count))
        authority = str(g.get("movement_authority", config.movement_authority)).lower()
        if authority in ("server", "client"):
            config.movement_authority = authority
        sync_mode = str(g.get("map_sync_mode", config.map_sync_mode)).lower()
        if sync_mode in ("auto", "full"):
            config.map_sync_mode = sync_mode

    if "teams" in data:
        t = data["teams"]
        config.team1_name = t.get("team1_name", config.team1_name)
        config.team2_name = t.get("team2_name", config.team2_name)
        if "team1_color" in t:
            config.team1_color = tuple(t["team1_color"])
        if "team2_color" in t:
            config.team2_color = tuple(t["team2_color"])
        config.auto_balance = t.get("auto_balance", config.auto_balance)
        config.balance_threshold = t.get("balance_threshold", config.balance_threshold)

    if "weapons" in data:
        w = data["weapons"]
        config.rifle_damage = w.get("rifle_damage", config.rifle_damage)
        config.smg_damage = w.get("smg_damage", config.smg_damage)
        config.shotgun_damage = w.get("shotgun_damage", config.shotgun_damage)
        config.spade_damage = w.get("spade_damage", config.spade_damage)
        config.grenade_damage = w.get("grenade_damage", config.grenade_damage)

    if "world" in data:
        w = data["world"]
        config.map_size_x = w.get("map_size_x", config.map_size_x)
        config.map_size_y = w.get("map_size_y", config.map_size_y)
        config.map_size_z = w.get("map_size_z", config.map_size_z)
        config.water_level = w.get("water_level", config.water_level)
        config.water_damage = w.get("water_damage", config.water_damage)

    if "admin" in data:
        a = data["admin"]
        config.admin_password = a.get("password", config.admin_password)
        config.log_commands = a.get("log_commands", config.log_commands)

    if "logging" in data:
        lg = data["logging"]
        config.log_level = lg.get("level", config.log_level)
        config.log_file = lg.get("file", config.log_file)
        config.log_console = lg.get("console", config.log_console)
        if "suppress_packets" in lg:
            config.log_suppress_packets = lg["suppress_packets"]

    if "debug" in data:
        dbg = data["debug"]
        config.debug_parity = dbg.get("debug_parity", config.debug_parity)
        config.debug_parity_host = dbg.get("debug_parity_host", config.debug_parity_host)
        config.debug_parity_port = dbg.get("debug_parity_port", config.debug_parity_port)
        config.broadcast_world_updates = dbg.get(
            "broadcast_world_updates", config.broadcast_world_updates)
        config.worldupdate_include_self = dbg.get(
            "worldupdate_include_self", config.worldupdate_include_self)
        config.worldupdate_loop_offset = int(dbg.get(
            "worldupdate_loop_offset", config.worldupdate_loop_offset))
        config.worldupdate_self_row_interval = max(1, int(dbg.get(
            "worldupdate_self_row_interval", config.worldupdate_self_row_interval)))
        config.debug_selfrow = bool(dbg.get("debug_selfrow", config.debug_selfrow))
        config.clock_sync_loop_bias = int(dbg.get(
            "clock_sync_loop_bias", config.clock_sync_loop_bias))

    return config
