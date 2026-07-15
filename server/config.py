"""
Configuration loader for BattleSpades server.
"""

try:
    import tomllib
except ImportError:  # Python 3.10 release fallback
    tomllib = None
import toml
import shared.constants as C
from server.game_rules import GameRules
from server.lobby import LOBBY_MATCH_LENGTH_OPTIONS
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Tuple, Optional


@dataclass
class BotConfig:
    """Bounded isolated bot-runtime configuration."""

    enabled: bool = False
    population_mode: str = "backfill"
    fill_target: int = 12
    max_bots: int = 12
    reserve_human_slots: int = 2
    difficulty: str = "mixed"
    worker: str = "process"
    perception_hz: float = 10.0
    decision_hz: float = 8.0
    path_requests_per_second: int = 24
    main_thread_budget_ms: float = 0.75
    seed: int = 0
    debug_visualization: bool = False
    configured: bool = False


@dataclass
class ServerConfig:
    """Server configuration container."""

    # Server settings
    name: str = "BattleSpades Server"
    port: int = 32887
    max_players: int = 50
    tick_rate: int = 60
    # Stable uint64 identity used by InitialInfo for non-Steam dedicated hosts.
    steam_id: int = 90087911866072064

    # Network settings
    timeout_ms: int = 10000
    max_connections: int = 64
    bandwidth_limit: int = 0
    network_event_budget: int = 512
    max_pending_packets: int = 4096
    packet_drain_budget: int = 4096
    # Time budget for plugin event callbacks executed from the gameplay tick.
    # Exceeding the budget skips remaining callbacks for that event; plugin
    # work is optional and must not steal a 60 Hz frame from authoritative sim.
    plugin_event_budget_ms: float = 2.0
    # Terrain packets retained for a joining client between its MapSync
    # snapshot and first ClientData. Overflow marks the join as unsafe instead
    # of admitting a desynced player.
    max_map_mutation_journal: int = 8192
    # Upper bound for per-frame behavior on_tick calls. Touch/proximity uses a
    # spatial index and still checks all relevant entities; this prevents a
    # pathological pile of ticking effects from monopolizing one frame.
    entity_tick_batch_limit: int = 8192
    # Synchronous gameplay events are authoritative but still bounded. A large
    # burst is deferred across ticks; only a completely saturated queue drops
    # new mode callbacks and increments an operational counter.
    mode_event_queue_limit: int = 8192
    mode_event_drain_budget: int = 512
    # Client-origin terrain edits wait until the movement frame that emitted
    # them has been simulated.  Bound both retained requests and commit work.
    world_mutation_queue_limit: int = 2048
    world_mutation_batch_limit: int = 256
    world_mutation_cell_budget: int = 4096
    world_mutation_timeout_ticks: int = 180
    # Prefab expansion is an authoritative world mutation, but a large KV6
    # model is deliberately committed over multiple simulation frames. These
    # limits bound both retained placements and per-tick cell/packet work.
    prefab_queue_limit: int = 32
    prefab_cell_batch_limit: int = 16
    # Reliable mutation packets are primary. This delayed, bounded canonical
    # replay repairs rare native BlockManager rejection/prediction divergence.
    terrain_repair_enabled: bool = True
    terrain_repair_queue_limit: int = 8192
    # Eight cells every three 60 Hz ticks drains 160 cells/second: enough for
    # several sustained 27-cell Super Spade footprints while retaining a hard
    # per-tick/per-recipient send bound.
    terrain_repair_batch_limit: int = 8
    terrain_repair_interval_ticks: int = 3
    terrain_repair_delay_ticks: int = 120
    # Packet 52 gives the retail GameScene time to enter its terminal map
    # state before ENet reason 18 closes the old session. Zero is useful only
    # for deterministic tests; production should retain a visible grace.
    transition_grace_seconds: float = 1.25

    # Game settings
    default_mode: str = "ctf"
    default_map: str = "classicgen"
    respawn_time: float = 5.0
    friendly_fire: bool = False
    fall_damage: bool = True
    build_damage: bool = True
    score_limit: int = 10
    # This value is sent in InitialInfo and must also govern the authoritative
    # collision list.  A mismatch makes the native client predict through an
    # ally while the server injects a collision impulse, producing rollback.
    same_team_collision: bool = False
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

    # Match Lobby settings recovered from matchSettingsPanel.pyc. ``None``
    # keeps each playlist's retail default duration; a value applies globally
    # unless [modes.<code>].time_limit provides a mode-specific override.
    match_length_minutes: Optional[int] = None
    # Empty means discover every .vxl in maps_path. A non-empty list is the
    # ordered catalog used by map voting and future lobby hosting.
    map_rotation: List[str] = field(default_factory=list)
    game_rules: GameRules = field(default_factory=GameRules.server_defaults)

    # Team settings
    team1_name: str = "TEAM1_COLOR"
    team1_color: Tuple[int, int, int] = (44, 117, 179)
    team2_name: str = "TEAM2_COLOR"
    team2_color: Tuple[int, int, int] = (137, 179, 44)
    auto_balance: bool = True
    balance_threshold: int = 2

    # Dev bots: server-side AI players spawned at startup (0 = none).
    bot_count: int = 0
    # New isolated runtime. ``configured`` distinguishes an explicit [bots]
    # table from legacy game.bot_count fixed-population behavior.
    bots: BotConfig = field(default_factory=BotConfig)

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
    map_size_z: int = int(C.MAP_Z) if hasattr(C, "MAP_Z") else 240
    water_level: int = int(C.Z_ABOVE_WATERPLANE)
    water_damage: bool = True
    fog_color_rgb: Tuple[int, int, int] = (12, 13, 11)
    # Used only when a VXL has no map-owned skybox sidecar. Packet 51 must
    # name a stock client mesh environment; map sidecars override this.
    default_skybox: str = "User_Grassland.txt"
    maps_path: str = "maps"
    prefabs_path: str = "prefabs"
    plugins_path: str = "plugins"
    bans_path: str = "bans.json"

    # Trusted local plugin discovery. Names are filename stems; an allowlist
    # limits loading when non-empty and the denylist always wins.
    plugins_enabled: bool = True
    plugin_allowlist: List[str] = field(default_factory=list)
    plugin_denylist: List[str] = field(default_factory=list)

    # Admin settings
    admin_password: str = "changeme"
    log_commands: bool = True

    # Per-mode setting overlays from config.toml [modes.<code>] tables.
    # e.g. mode_settings["tdm"] = {"score_limit": 200, "time_limit": 900,
    # "kill_points": 1, "headshot_bonus": 1}. Empty = use mode_data defaults.
    mode_settings: dict = field(default_factory=dict)

    # Logging
    log_level: str = "INFO"
    log_file: str = "server.log"
    log_console: bool = True
    log_suppress_packets: List[int] = field(default_factory=lambda: [2, 4, 11])
    # Full packet parsing + hex dumps are reverse-engineering diagnostics, not
    # ordinary DEBUG logging. Keep them explicitly opt-in so debug messages do
    # not add serialization work to the gameplay thread.
    packet_trace: bool = False
    log_queue_capacity: int = 8192

    # Debug
    # Physics parity capture is an invasive reverse-engineering tool.  It owns
    # a UDP socket and can produce large captures, so production must opt in.
    debug_parity: bool = False
    debug_parity_host: str = "127.0.0.1"
    debug_parity_port: int = 32895
    # Records contain full movement snapshots; 256 bounds worst-case memory
    # while leaving ample headroom for short disk stalls.
    debug_parity_queue_capacity: int = 256
    debug_parity_sample_hz: float = 10.0
    debug_parity_flush_interval: float = 1.0
    debug_parity_flush_batch: int = 128
    # A/B isolation switch: with WorldUpdate broadcasting off, the local
    # player runs on pure client prediction (no server corrections at all).
    # If walking is smooth with this off and chunky with it on, the jank
    # lives in the WorldUpdate/correction loop, not the movement engine.
    broadcast_world_updates: bool = True
    # Retail network cadence: send WorldUpdate every 2 simulation ticks (30 Hz).
    worldupdate_broadcast_interval: int = 2
    # The stock client needs a fresh self row. If the recipient's own row is
    # omitted, its CreatePlayer network_position can stay at spawn and the next
    # jump may visibly correct back to that stale anchor.
    worldupdate_include_self: bool = True
    # Constant added to the per-recipient self-row stamp (the input tick
    # the server actually consumed for that player) — the client's history
    # indexing convention, latency-invariant. Exact after the one-loop
    # ClientData flag latch: 0 (consumed loop L == movement_history[L]).
    worldupdate_loop_offset: int = 0
    # Refresh grounded owner anchors at ordinary observer cadence.
    worldupdate_self_row_interval: int = 2
    # Airborne vertical phase differs slightly across independent client/server
    # frame clocks. Six ticks was the highest measured cadence that reduced
    # correction chatter without approaching the 60-entry retail history cap.
    worldupdate_airborne_self_row_interval: int = 6
    # WorldUpdate is the retail owner's only jetpack-active signal.  Because
    # ClientData has no application acknowledgement, ordinary position rows
    # are withheld after the reliable transition row while GameScene crosses
    # that asynchronous boundary. Active thrust remains owner-row-free until
    # release; this frame count is the inactive-transition safety fallback.
    # Observer rows remain unaffected.
    jetpack_owner_handoff_input_frames: int = 30
    # Fuel exhaustion while SPACE remains held changes the native client back
    # to ballistic movement asynchronously. Keep the owner row quiet through
    # release, settle, and landing, with this accepted-input safety bound.
    jetpack_owner_release_handoff_input_frames: int = 600
    # When true, append every self-row's (stamp, position) to
    # logs/selfrow_samples.ndjson for offline reconciliation calibration
    # (join with the client capture via tmp/reconcile_sim.py). Debug only.
    debug_selfrow: bool = False
    movement_debug_capture: bool = False
    # Retail ClientData is emitted after the local frame but its movement
    # buttons may describe the preceding native step. Kept as an explicit A/B
    # switch until the transition chronology is fully certified (0 or 1).
    movement_input_latch_frames: int = 1
    # Added to the loop_count we report in ClockSync replies. The client
    # paces its clock from this, so +1 makes it run one tick AHEAD of us:
    # ClientData stamped N then arrives while we are still at N-1 and is
    # guaranteed to be buffered before tick N simulates — without margin,
    # input application is a per-packet race (applied at N or N+1), which
    # no fixed WorldUpdate stamp offset can compensate.
    clock_sync_loop_bias: int = 0

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

    def configured_time_limit(self, mode_code: str, default: float) -> float:
        """Resolve one mode clock with narrow-to-broad precedence."""

        overlay = self.mode_settings.get(str(mode_code), {})
        if "time_limit" in overlay:
            return max(0.0, float(overlay["time_limit"]))
        if self.match_length_minutes is not None:
            return float(self.match_length_minutes * 60)
        return max(0.0, float(default))

    def mode_rule(
        self,
        mode_code: str,
        overlay_key: str,
        rule_key: str,
    ):
        """Resolve a mode rule while preserving legacy [modes.*] overlays."""

        overlay = self.mode_settings.get(str(mode_code), {})
        if overlay_key in overlay:
            return overlay[overlay_key]
        return self.game_rules.get(rule_key)


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
        # Use the installed ``toml`` package in production and honor focused
        # tests/plugins that instrument its loader. Some legacy movement tests
        # install a file-less SimpleNamespace stub before importing config; in
        # that one case the standard-library parser prevents collection order
        # from silently turning every config into an empty mapping.
        if tomllib is not None and getattr(toml, "__file__", None) is None:
            with path.open("rb") as stream:
                data = tomllib.load(stream)
        else:
            data = toml.load(path)
    except Exception as e:
        print(f"Warning: Failed to load config from {path}: {e}")
        return config

    if "server" in data:
        s = data["server"]
        config.name = s.get("name", config.name)
        config.port = s.get("port", config.port)
        config.max_players = min(255, max(1, int(
            s.get("max_players", config.max_players)
        )))
        config.tick_rate = min(240, max(10, int(
            s.get("tick_rate", config.tick_rate)
        )))
        config.steam_id = max(0, int(s.get("steam_id", config.steam_id)))

    if "lobby" in data:
        lobby = data["lobby"]
        if "match_length_minutes" in lobby:
            minutes = int(lobby["match_length_minutes"])
            if minutes not in LOBBY_MATCH_LENGTH_OPTIONS:
                raise ValueError(
                    "lobby.match_length_minutes must be one of "
                    "5,10,15,20,25,30,35,40,45,50,55,60,90"
                )
            config.match_length_minutes = minutes
        rotation = lobby.get("map_rotation", config.map_rotation)
        if not isinstance(rotation, list):
            raise ValueError("lobby.map_rotation must be a TOML array")
        normalized_rotation: list[str] = []
        seen_maps: set[str] = set()
        for value in rotation:
            name = Path(str(value).strip()).stem
            if not name or Path(name).name != name:
                raise ValueError(f"Unsafe map name in lobby.map_rotation: {value!r}")
            folded = name.casefold()
            if folded not in seen_maps:
                seen_maps.add(folded)
                normalized_rotation.append(name)
        config.map_rotation = normalized_rotation

    game_rule_data = data.get("game_rules", {})
    if game_rule_data:
        if not isinstance(game_rule_data, dict):
            raise ValueError("game_rules must be a TOML table")
        config.game_rules.apply(game_rule_data)

    if "network" in data:
        n = data["network"]
        config.timeout_ms = n.get("timeout_ms", config.timeout_ms)
        config.max_connections = n.get("max_connections", config.max_connections)
        config.bandwidth_limit = n.get("bandwidth_limit", config.bandwidth_limit)
        config.network_event_budget = max(32, int(n.get(
            "event_budget", config.network_event_budget)))
        config.max_pending_packets = max(256, int(n.get(
            "max_pending_packets", config.max_pending_packets)))
        config.packet_drain_budget = max(64, int(n.get(
            "packet_drain_budget", config.packet_drain_budget)))
        config.plugin_event_budget_ms = max(0.1, float(n.get(
            "plugin_event_budget_ms", config.plugin_event_budget_ms)))
        config.max_map_mutation_journal = max(64, int(n.get(
            "max_map_mutation_journal", config.max_map_mutation_journal)))
        config.entity_tick_batch_limit = max(64, int(n.get(
            "entity_tick_batch_limit", config.entity_tick_batch_limit)))
        config.mode_event_queue_limit = max(64, int(n.get(
            "mode_event_queue_limit", config.mode_event_queue_limit)))
        config.mode_event_drain_budget = max(1, int(n.get(
            "mode_event_drain_budget", config.mode_event_drain_budget)))
        config.world_mutation_queue_limit = max(64, int(n.get(
            "world_mutation_queue_limit", config.world_mutation_queue_limit)))
        config.world_mutation_batch_limit = max(1, int(n.get(
            "world_mutation_batch_limit", config.world_mutation_batch_limit)))
        config.world_mutation_cell_budget = max(64, int(n.get(
            "world_mutation_cell_budget", config.world_mutation_cell_budget)))
        config.world_mutation_timeout_ticks = max(30, int(n.get(
            "world_mutation_timeout_ticks", config.world_mutation_timeout_ticks)))
        config.prefab_queue_limit = min(128, max(1, int(n.get(
            "prefab_queue_limit", config.prefab_queue_limit))))
        config.prefab_cell_batch_limit = min(128, max(1, int(n.get(
            "prefab_cell_batch_limit", config.prefab_cell_batch_limit))))
        config.terrain_repair_enabled = bool(n.get(
            "terrain_repair_enabled", config.terrain_repair_enabled))
        config.terrain_repair_queue_limit = max(64, int(n.get(
            "terrain_repair_queue_limit", config.terrain_repair_queue_limit)))
        config.terrain_repair_batch_limit = max(1, int(n.get(
            "terrain_repair_batch_limit", config.terrain_repair_batch_limit)))
        config.terrain_repair_interval_ticks = max(1, int(n.get(
            "terrain_repair_interval_ticks", config.terrain_repair_interval_ticks)))
        config.terrain_repair_delay_ticks = max(1, int(n.get(
            "terrain_repair_delay_ticks", config.terrain_repair_delay_ticks)))
        config.transition_grace_seconds = min(5.0, max(0.0, float(n.get(
            "transition_grace_seconds", config.transition_grace_seconds))))

    if "game" in data:
        g = data["game"]
        config.default_mode = g.get("default_mode", config.default_mode)
        config.default_map = g.get("default_map", config.default_map)
        config.respawn_time = g.get("respawn_time", config.respawn_time)
        config.friendly_fire = g.get("friendly_fire", config.friendly_fire)
        config.fall_damage = g.get("fall_damage", config.fall_damage)
        config.build_damage = g.get("build_damage", config.build_damage)
        config.score_limit = max(0, int(g.get("score_limit", config.score_limit)))
        config.same_team_collision = bool(g.get(
            "same_team_collision", config.same_team_collision
        ))
        config.bot_count = int(g.get("bot_count", config.bot_count))
        authority = str(g.get("movement_authority", config.movement_authority)).lower()
        if authority in ("server", "client"):
            config.movement_authority = authority
        sync_mode = str(g.get("map_sync_mode", config.map_sync_mode)).lower()
        if sync_mode in ("auto", "full"):
            config.map_sync_mode = sync_mode

    # New retail-named rules take precedence over compatibility fields. When
    # omitted, keep old configs authoritative and mirror their value into the
    # rule service so InitialInfo and runtime logic still agree.
    if "RULE_RESPAWN_TIMES" in config.game_rules.explicit:
        config.respawn_time = float(config.game_rules.get("RULE_RESPAWN_TIMES"))
    else:
        config.game_rules.values["RULE_RESPAWN_TIMES"] = config.respawn_time
    if "RULE_ENABLE_FALL_ON_WATER_DAMAGE" in config.game_rules.explicit:
        config.fall_damage = config.game_rules.enabled(
            "RULE_ENABLE_FALL_ON_WATER_DAMAGE"
        )
    else:
        config.game_rules.values[
            "RULE_ENABLE_FALL_ON_WATER_DAMAGE"
        ] = bool(config.fall_damage)

    if "bots" in data and isinstance(data["bots"], dict):
        b = data["bots"]
        config.bots.configured = True
        config.bots.enabled = bool(b.get("enabled", config.bots.enabled))
        population_mode = str(
            b.get("population_mode", config.bots.population_mode)
        ).lower()
        if population_mode in ("backfill", "fixed", "admin"):
            config.bots.population_mode = population_mode
        config.bots.fill_target = max(
            0, int(b.get("fill_target", config.bots.fill_target))
        )
        config.bots.max_bots = max(
            0, int(b.get("max_bots", config.bots.max_bots))
        )
        config.bots.reserve_human_slots = max(
            0,
            int(b.get("reserve_human_slots", config.bots.reserve_human_slots)),
        )
        difficulty = str(b.get("difficulty", config.bots.difficulty)).lower()
        if difficulty in ("casual", "normal", "hard", "mixed"):
            config.bots.difficulty = difficulty
        # The architecture intentionally supports only an isolated process.
        config.bots.worker = "process"
        config.bots.perception_hz = min(
            30.0,
            max(1.0, float(b.get("perception_hz", config.bots.perception_hz))),
        )
        config.bots.decision_hz = min(
            config.bots.perception_hz,
            max(1.0, float(b.get("decision_hz", config.bots.decision_hz))),
        )
        config.bots.path_requests_per_second = max(
            1,
            int(
                b.get(
                    "path_requests_per_second",
                    config.bots.path_requests_per_second,
                )
            ),
        )
        config.bots.main_thread_budget_ms = max(
            0.1,
            float(
                b.get(
                    "main_thread_budget_ms",
                    config.bots.main_thread_budget_ms,
                )
            ),
        )
        config.bots.seed = int(b.get("seed", config.bots.seed))
        config.bots.debug_visualization = bool(
            b.get("debug_visualization", config.bots.debug_visualization)
        )

    if "teams" in data:
        t = data["teams"]
        config.team1_name = t.get("team1_name", config.team1_name)
        config.team2_name = t.get("team2_name", config.team2_name)
        if "team1_color" in t:
            color = tuple(int(value) for value in t["team1_color"])
            if len(color) != 3 or any(not 0 <= value <= 255 for value in color):
                raise ValueError("teams.team1_color must be three bytes")
            config.team1_color = color
        if "team2_color" in t:
            color = tuple(int(value) for value in t["team2_color"])
            if len(color) != 3 or any(not 0 <= value <= 255 for value in color):
                raise ValueError("teams.team2_color must be three bytes")
            config.team2_color = color
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
        config.default_skybox = w.get("default_skybox", config.default_skybox)
        config.maps_path = w.get("maps_path", config.maps_path)
        config.prefabs_path = w.get("prefabs_path", config.prefabs_path)
        config.entities_wire_ready = bool(w.get(
            "entities_wire_ready", config.entities_wire_ready
        ))
        if "fog_color_rgb" in w:
            color = tuple(int(value) for value in w["fog_color_rgb"])
            if len(color) != 3 or any(not 0 <= value <= 255 for value in color):
                raise ValueError("world.fog_color_rgb must be three bytes")
            config.fog_color_rgb = color

    if "plugins" in data:
        p = data["plugins"]
        config.plugins_enabled = bool(p.get("enabled", config.plugins_enabled))
        config.plugins_path = str(p.get("path", config.plugins_path))
        allowlist = p.get("allowlist", config.plugin_allowlist)
        denylist = p.get("denylist", config.plugin_denylist)
        if not isinstance(allowlist, list) or not isinstance(denylist, list):
            raise ValueError("plugins.allowlist and plugins.denylist must be arrays")
        config.plugin_allowlist = [str(value).strip() for value in allowlist if str(value).strip()]
        config.plugin_denylist = [str(value).strip() for value in denylist if str(value).strip()]

    # Per-mode overlays: [modes.tdm], [modes.ctf], ... Each table's keys
    # override that mode's defaults (score_limit, time_limit, kill_points...).
    if "modes" in data and isinstance(data["modes"], dict):
        for code, settings in data["modes"].items():
            if isinstance(settings, dict):
                config.mode_settings[str(code)] = dict(settings)

    if "admin" in data:
        a = data["admin"]
        config.admin_password = a.get("password", config.admin_password)
        config.log_commands = a.get("log_commands", config.log_commands)
        config.bans_path = str(a.get("bans_path", config.bans_path))

    if "logging" in data:
        lg = data["logging"]
        config.log_level = lg.get("level", config.log_level)
        config.log_file = lg.get("file", config.log_file)
        config.log_console = lg.get("console", config.log_console)
        config.packet_trace = bool(lg.get("packet_trace", config.packet_trace))
        config.log_queue_capacity = max(256, int(lg.get(
            "queue_capacity", config.log_queue_capacity)))
        if "suppress_packets" in lg:
            config.log_suppress_packets = lg["suppress_packets"]

    if "debug" in data:
        dbg = data["debug"]
        config.debug_parity = dbg.get("debug_parity", config.debug_parity)
        config.debug_parity_host = dbg.get("debug_parity_host", config.debug_parity_host)
        config.debug_parity_port = dbg.get("debug_parity_port", config.debug_parity_port)
        config.debug_parity_queue_capacity = max(64, int(dbg.get(
            "debug_parity_queue_capacity", config.debug_parity_queue_capacity)))
        config.debug_parity_sample_hz = max(0.1, min(10.0, float(dbg.get(
            "debug_parity_sample_hz", config.debug_parity_sample_hz))))
        config.debug_parity_flush_interval = max(0.1, float(dbg.get(
            "debug_parity_flush_interval", config.debug_parity_flush_interval)))
        config.debug_parity_flush_batch = max(1, int(dbg.get(
            "debug_parity_flush_batch", config.debug_parity_flush_batch)))
        config.broadcast_world_updates = dbg.get(
            "broadcast_world_updates", config.broadcast_world_updates)
        config.worldupdate_broadcast_interval = max(1, int(dbg.get(
            "worldupdate_broadcast_interval", config.worldupdate_broadcast_interval)))
        config.worldupdate_include_self = dbg.get(
            "worldupdate_include_self", config.worldupdate_include_self)
        config.worldupdate_loop_offset = int(dbg.get(
            "worldupdate_loop_offset", config.worldupdate_loop_offset))
        config.worldupdate_self_row_interval = max(1, int(dbg.get(
            "worldupdate_self_row_interval", config.worldupdate_self_row_interval)))
        config.worldupdate_airborne_self_row_interval = max(1, int(dbg.get(
            "worldupdate_airborne_self_row_interval",
            config.worldupdate_airborne_self_row_interval,
        )))
        config.jetpack_owner_handoff_input_frames = max(0, min(120, int(
            dbg.get(
                "jetpack_owner_handoff_input_frames",
                config.jetpack_owner_handoff_input_frames,
            )
        )))
        config.jetpack_owner_release_handoff_input_frames = max(0, min(1200, int(
            dbg.get(
                "jetpack_owner_release_handoff_input_frames",
                config.jetpack_owner_release_handoff_input_frames,
            )
        )))
        config.debug_selfrow = bool(dbg.get("debug_selfrow", config.debug_selfrow))
        config.movement_debug_capture = bool(dbg.get(
            "movement_debug_capture", config.movement_debug_capture))
        config.movement_input_latch_frames = max(0, min(1, int(dbg.get(
            "movement_input_latch_frames", config.movement_input_latch_frames
        ))))
        config.clock_sync_loop_bias = int(dbg.get(
            "clock_sync_loop_bias", config.clock_sync_loop_bias))

    return config
