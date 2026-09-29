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
from server.join_greeting import DEFAULT_JOIN_GREETING, DEFAULT_MOTD
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Tuple, Optional
from urllib.parse import urlsplit

# Retail server-only MAX_SERVER_NAME_SIZE (A2269).
MAX_SERVER_NAME_SIZE = int(getattr(C, "MAX_SERVER_NAME_SIZE", 31))


# The sample [admin] password shipped in config.toml. A server still using it
# (or an empty/short secret) has in-game /admin login disabled: anyone who has
# read the docs would otherwise own the server.
DEFAULT_ADMIN_PASSWORD = "changeme"
MIN_ADMIN_PASSWORD_LENGTH = 12

# Size classes accepted by [lobby] map_size_overrides; the areas they stand
# for live in server.voting.MAP_SIZE_CLASS_AREAS.
MAP_SIZE_CLASSES = ("small", "medium", "large")


def admin_password_problem(password: object) -> Optional[str]:
    """Why ``password`` cannot enable /admin login, or None when it can."""

    text = "" if password is None else str(password)
    if not text.strip():
        return "the [admin] password is empty"
    if text == DEFAULT_ADMIN_PASSWORD:
        return 'the [admin] password is still the shipped default "changeme"'
    if len(text) < MIN_ADMIN_PASSWORD_LENGTH:
        return (
            f"the [admin] password is shorter than {MIN_ADMIN_PASSWORD_LENGTH} "
            "characters"
        )
    return None


def _steam_token(name: str, value: object, limit: int) -> str:
    """Validate one legacy ASCII tag/version token from untrusted TOML."""

    token = str(value).strip()
    if len(token) > limit:
        raise ValueError(f"steam.{name} must be at most {limit} characters")
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
    )
    if any(character not in allowed for character in token):
        raise ValueError(
            f"steam.{name} may contain only ASCII letters, digits, _, ., and -"
        )
    return token


def _revival_token(name: str, value: object, limit: int) -> str:
    """Validate one short ASCII registry value from untrusted TOML."""

    token = str(value).strip()
    if len(token) > limit:
        raise ValueError(f"revival.{name} must be at most {limit} characters")
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
    )
    if any(character not in allowed for character in token):
        raise ValueError(
            f"revival.{name} may contain only ASCII letters, digits, _, ., and -"
        )
    return token


@dataclass
class BotConfig:
    """Bounded isolated bot-runtime configuration."""

    enabled: bool = False
    population_mode: str = "backfill"
    fill_target: int = 12
    max_bots: int = 12
    reserve_human_slots: int = 2
    difficulty: str = "mixed"
    worker: str = "thread"
    perception_hz: float = 10.0
    decision_hz: float = 8.0
    path_requests_per_second: int = 24
    main_thread_budget_ms: float = 0.75
    seed: int = 0
    debug_visualization: bool = False
    # Recycle the isolated planner after this many completed game boundaries.
    # Zero disables the periodic deep reset; per-round state still resets.
    clean_slate_games: int = 3
    configured: bool = False
    behavior_version: str = "cooperative"
    friendly_mischief: bool = True
    # Sparse, rate-limited bot chat reacting to kills and round boundaries.
    chatter: bool = True
    # Per-team bot skill balancing (server/bot_ai/skill_balance.py): when the
    # humans on one team clearly out-kill the other side, that team's bots are
    # eased and the other team's sharpened, by at most ``skill_balance_max_shift``
    # of each profile field, moving ``skill_balance_rate`` per second and only
    # outside a ``skill_balance_deadband`` edge once a team has
    # ``skill_balance_min_events`` kills/deaths on record.
    skill_balance: bool = True
    skill_balance_max_shift: float = 0.35
    skill_balance_rate: float = 0.03
    skill_balance_deadband: float = 0.15
    skill_balance_min_events: int = 6


@dataclass
class AntiCheatConfig:
    """Server-side validation switches.

    Checks that could reject legitimate retail play under packet loss start
    log-only (``enforce_* = False``): violations are counted and logged by
    ``server.anticheat`` but the action is still accepted. Flip a switch to
    true once a play session shows no false positives.
    """

    # Shot/throw/build origin vs the server eye at the shot's input frame.
    enforce_shot_origin: bool = False
    shot_origin_tolerance: float = 1.5
    # A shot direction vs that frame's aim (degrees).
    enforce_aim_direction: bool = False
    aim_direction_tolerance_deg: float = 10.0
    # Input starvation: airborne bodies fall after this many starved ticks,
    # and a fully silent in-game client times out.
    enforce_input_starvation: bool = False
    starvation_airborne_ticks: int = 24
    starvation_timeout_seconds: float = 8.0
    # Input backlog (fake lag): catch up once the queue stays above this.
    enforce_input_backlog: bool = False
    backlog_max_frames: int = 6
    # Stock server-only MIN_BLOCK_INTERVAL (0.1 s) between one player's
    # accepted block builds/lines; log-only until a play session is clean.
    enforce_block_interval: bool = False
    # Protocol violations the stock client never produces (NaN, forged tool,
    # impossible packets) kick immediately when true.
    kick_on_protocol_violation: bool = True
    # Admin login attempts before a kick.
    admin_login_attempts: int = 3
    # Rate-limited per-player summary log interval (seconds). Also the
    # suspicion report's evaluation interval (server/anticheat_report.py).
    summary_interval_seconds: float = 60.0

    # Statistical suspicion report (server/anticheat_report.py). Log/report
    # only: it never kicks. Keys and defaults mirror anticheat_report.DEFAULTS.
    report_enabled: bool = True
    report_path: str = "logs/anticheat.jsonl"
    report_max_bytes: int = 5_000_000
    report_backups: int = 3
    flag_min_score: float = 1.0
    # Headshot share of hitscan kills.
    headshot_kill_ratio: float = 0.60
    headshot_kill_min_kills: int = 30
    # Headshot share of single-ray hitscan hits.
    headshot_hit_ratio: float = 0.55
    headshot_hit_min_hits: int = 60
    # Per-weapon accuracy vs the human population.
    accuracy_min_shots: int = 100
    accuracy_min_population: int = 20
    accuracy_percentile: float = 99.0
    accuracy_min_margin: float = 0.15
    # Aim snaps.
    snap_min_deg: float = 20.0
    snap_frames: int = 2
    snap_head_radius: float = 0.35
    snap_margin_deg: float = 0.75
    snap_min_events: int = 4
    snap_min_engaged: int = 20
    snap_ratio: float = 0.10
    # Acquisition ("reaction") time.
    reaction_acquire_deg: float = 10.0
    reaction_history_frames: int = 60
    reaction_min_samples: int = 15
    reaction_median_ms: float = 90.0
    reaction_reengage_seconds: float = 3.0
    engage_cone_deg: float = 4.0
    # Pellet seed skew.
    pellet_seed_min_shots: int = 64
    pellet_seed_top_share: float = 0.2
    # Sustained log-only violations.
    sustained_min_minutes: float = 5.0
    sustained_min_count: int = 20
    sustained_per_minute: float = 3.0


# ``[weapons]`` keys still parsed for old configs but ignored (see
# ServerConfig.rifle_damage); setting any of them logs a deprecation warning.
_DEPRECATED_WEAPON_KEYS = frozenset({
    "rifle_damage",
    "smg_damage",
    "shotgun_damage",
    "spade_damage",
    "grenade_damage",
})
# ``[world] map_size_*`` are informational; the retail VXL is always this size.
_INFORMATIONAL_MAP_SIZE = {"map_size_x": 512, "map_size_y": 512, "map_size_z": 240}


# ``[anticheat]`` values that are shares (0..1) or a percentile (0..100);
# every other numeric key is only clamped to be non-negative.
_ANTICHEAT_UNIT_KEYS = frozenset({
    "headshot_kill_ratio",
    "headshot_hit_ratio",
    "accuracy_min_margin",
    "snap_ratio",
    "pellet_seed_top_share",
})
_ANTICHEAT_BOUNDS = {
    "accuracy_percentile": (0.0, 100.0),
    "snap_frames": (1, 30),
    "reaction_history_frames": (1, 600),
    "report_backups": (0, 20),
    "report_max_bytes": (4096, 1024 * 1024 * 1024),
}


@dataclass
class ConductConfig:
    """Team-grief and AFK kicks (``server.conduct``).

    Grief points: ``grief_team_damage_points`` per 100 teammate HP removed
    plus ``grief_team_kill_points`` per team kill, including a teammate's
    death from a deployable you set off (shooting their landmine). One point
    decays every ``grief_decay_seconds``; one burst is capped at
    ``grief_incident_max_points`` so an accident alone never kicks.
    """

    grief_kick_enabled: bool = True
    grief_kick_points: float = 10.0
    grief_warn_points: float = 5.0
    grief_decay_seconds: float = 60.0
    grief_team_kill_points: float = 3.0
    grief_team_damage_points: float = 1.0
    grief_incident_seconds: float = 3.0
    grief_incident_max_points: float = 6.0
    grief_exempt_admins: bool = True
    # Seconds of no real input (keys/aim) before an AFK kick; 0 disables.
    afk_kick_seconds: float = 600.0
    afk_warn_seconds: float = 540.0
    # Spectators idle longer; 0 exempts them.
    afk_spectator_kick_seconds: float = 1800.0
    afk_exempt_admins: bool = True
    # System-chat line to everyone when someone is kicked for grief/AFK.
    announce_kicks: bool = True
    # Extra names (beyond the built-in Server/Admin/Console/...) no player
    # may use; compared after homoglyph folding.
    reserved_names: List[str] = field(default_factory=list)


@dataclass
class SteamMasterConfig:
    """Optional legacy Steam master-server registration.

    The retail AoS Steamworks runtime is 32-bit.  BattleSpades therefore
    launches it through the isolated ``battlespades-steam-bridge`` helper;
    these settings never load Valve DLLs into the authoritative server.
    """

    enabled: bool = False
    app_id: int = 224540
    runtime_dir: str = ""
    # Directory containing a compatible x86 steamclient.dll plus tier0_s.dll
    # and vstdlib_s.dll. Blank auto-discovers the installed Steam directory.
    steamclient_dir: str = ""
    helper_path: str = ""
    # Copy a supplied steamclient.dll into the isolated runtime. The inspected
    # legacy file can hang inside SteamGameServer_Init, so safe default is to
    # let steam_api locate the installed Steam client instead.
    use_supplied_steamclient: bool = False
    steam_port: int = 8766
    # Zero means game port + 1, matching the original separate query socket.
    query_port: int = 0
    public: bool = True
    secure: bool = False
    region: str = ""
    game_version: str = "1.0.0.0"
    protocol_version: int = 168
    playlist_id: int = 8
    texture_skin: str = ""
    require_registration: bool = False
    startup_timeout_seconds: float = 8.0
    publish_interval_seconds: float = 1.0

    def effective_query_port(self, game_port: int) -> int:
        """Return the dedicated Steam query port for one game port."""

        return int(self.query_port or (int(game_port) + 1))


@dataclass
class RevivalMasterConfig:
    """Public Revival registry and account/statistics bridge.

    The shared write credential is deliberately read from
    ``AOS_MASTER_WRITE_TOKEN`` at runtime and never accepted from TOML.
    """

    enabled: bool = True
    base_url: str = "https://www.aosplay.net"
    public_host: str = "127.0.0.1"
    server_id: str = ""
    region: str = "europe"
    official: bool = False
    require_identity: bool = False
    heartbeat_interval_seconds: float = 30.0
    request_timeout_seconds: float = 5.0
    results_path: str = "state/round-results.sqlite3"


@dataclass
class ServerConfig:
    """Server configuration container."""

    # Server settings. Every default here matches the shipped config.toml so
    # an omitted key behaves like the documented sample (tests/
    # test_config_defaults.py). The retail Steam-browser port 32887 is an
    # explicit opt-in (docs/ADMIN_GUIDE.md), not the fallback.
    name: str = "BattleSpades Server"
    port: int = 27015
    max_players: int = 24
    tick_rate: int = 60
    # Stable uint64 identity used by InitialInfo for non-Steam dedicated hosts.
    steam_id: int = 90087911866072064
    # Private, once-per-connection chat after the GameScene is ready.
    join_greeting: str = DEFAULT_JOIN_GREETING
    motd: list[str] = field(default_factory=lambda: list(DEFAULT_MOTD))

    # Network settings
    # ``timeout_ms`` and ``bandwidth_limit`` are accepted for old configs but
    # IGNORED: retail creates its ENet host with 0/0 bandwidth and never
    # overrides peer.timeout (library defaults: limit 32, 5-30 s), and so do
    # the native client and this server.  A non-default value logs a warning.
    timeout_ms: int = 10000
    max_connections: int = 64
    bandwidth_limit: int = 0
    # Refuse an ENet connect whose data is not PROTOCOL_VERSION (168): retail
    # connects with shared.steam.game_version() (168) and the native client
    # with 168.  Older -> ERROR_CLIENT_OUT_OF_DATE (10), newer ->
    # ERROR_SERVER_OUT_OF_DATE (3).
    require_protocol_version: bool = True
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
    # Exact pre-snapshot destroyed cells sent per ClientData while a reconnect
    # remains gameplay-gated. At 256, a typical Drill tunnel converges in a
    # handful of 60 Hz input frames without one join monopolizing the tick.
    map_air_catchup_batch_limit: int = 256
    # Re-send every destroyed cell since map load as a per-cell Damage packet
    # after the joiner reaches GameScene. The MapSync stream already carries
    # the current spans of every edited column, so this is a belt-and-braces
    # repair that costs one reliable packet per cell and is visible to the
    # player as terrain changing after spawn. Off by default; see
    # docs/MAP_SYNC_JOIN.md for the live verification.
    map_air_catchup_enabled: bool = False
    # ENet lowers a peer's unreliable-send throttle whenever a round trip
    # measures worse than the recent mean plus twice its variance and then
    # drops that share of unreliable packets for up to packetThrottleInterval
    # (5 s). Our per-peer unreliable traffic (30 Hz WorldUpdate) is a few KB/s,
    # so that throttle can only hurt: 0 keeps every unreliable packet flowing
    # (ENet default deceleration is 2). Guard only: in the measured lossy-link
    # runs the throttle stayed at 32/32 (`tick stats: thr=`), and the observed
    # one-second WorldUpdate gap was ENet head-of-line blocking behind a lost
    # reliable packet instead (docs/RETAIL_INPUT_LOSS.md).
    unreliable_throttle_deceleration: int = 0
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
    # Prefab expansion is an authoritative world mutation. Competitive
    # prefabs commit whole in one tick (so the client renders them at once),
    # capped across all players by ``prefab_competitive_cell_budget``; large
    # UGC editor models drain over multiple frames in
    # ``prefab_cell_batch_limit`` cell batches.
    prefab_queue_limit: int = 32
    prefab_cell_batch_limit: int = 16
    prefab_competitive_cell_budget: int = 2048
    prefab_validation_batch_limit: int = 1024
    # BlockManager health-state rows per reliable packet when a joiner is sent
    # the damaged-block/prefab health snapshot (server/prefab_actions.py).
    prefab_health_state_batch: int = 128
    # Server-side hitbox rewind for hitscan/melee hits
    # (server/lag_compensation.py): targets are checked where the shooter saw
    # them, one round trip (+ ``lag_compensation_view_delay_ms``) ago, never
    # more than RTT + ``lag_compensation_extra_ms`` nor
    # ``lag_compensation_max_ms``. The retail client extrapolates remotes, so
    # view delay 0 is the measured contract.
    lag_compensation_enabled: bool = True
    lag_compensation_max_ms: float = 250.0
    lag_compensation_extra_ms: float = 50.0
    lag_compensation_view_delay_ms: float = 0.0
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
    # Native collapse is derived independently by each retail BlockManager.
    # Confirm its exact air cells sooner, still in a bounded per-tick lane.
    terrain_collapse_repair_batch_limit: int = 8
    terrain_collapse_repair_delay_ticks: int = 18
    # Packet 52 gives the retail GameScene time to enter its terminal map
    # state before ENet reason 18 closes the old session. Zero is useful only
    # for deterministic tests; production should retain a visible grace.
    transition_grace_seconds: float = 1.25

    # Game settings
    default_mode: str = "tdm"
    default_map: str = "MayanJungle"
    respawn_time: float = 5.0
    friendly_fire: bool = False
    fall_damage: bool = True
    build_damage: bool = True
    score_limit: int = 10
    # RadarStationEntity only starts its native countdown when CreateEntity
    # carries a non-zero fuse (the client has no lifetime of its own).  The
    # retail value is 45 s (``C.RADAR_STATION_LIFETIME``, retail A1901; see
    # docs/RETAIL_VALUES.md); the server expiry and wire countdown share it.
    radar_station_lifetime_seconds: float = 45.0
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
    # Retail baseSquadLobbyMenu shuffles the mode_map list once when the
    # lobby is created (random.shuffle(map_list)); the rotation/vote order
    # below is shuffled once at startup the same way.
    map_rotation_shuffle: bool = True
    # Retail playlists.mapinfo max_players (DragonIsland 16, London and
    # LunarBase 20, BlockNess and SpookyMansion 24): a map whose cap is
    # below the human player count is offered only after every map that
    # fits (inferred use; the retail server-side consumer is lost).
    map_vote_retail_max_players: bool = True
    # Seconds the retail statistics overlay remains visible after a map vote
    # resolves and before MapEnded starts the next validated loader handshake.
    end_screen_seconds: float = 12.0
    # Retail end-of-round presentation: ShowTextMessage(73) headline plus
    # ForceShowScores(72) holding the scoreboard open for end_screen_seconds,
    # released again on the in-place restart.
    end_round_scoreboard: bool = True
    # ShowTextMessage(73) scoreboard headline at the win (separate knob so
    # the two retail packets can be bisected live).
    end_round_headline: bool = True
    # Map-vote candidate ranking (server/voting.py). With size fit on, maps
    # whose playable area suits the lobby (players x area_per_player, never
    # below min_area) are offered first; bots count as ``map_vote_bot_weight``
    # players. The last ``map_vote_recent_exclude`` maps go to the back.
    # ``map_size_overrides`` maps a map name to an area (int) or to one of
    # "small"/"medium"/"large" when its .botnav cache is missing or misleading.
    map_vote_size_fit: bool = True
    map_vote_area_per_player: float = 8000.0
    map_vote_min_area: float = 24000.0
    map_vote_recent_exclude: int = 2
    map_vote_bot_weight: float = 1.0
    map_size_overrides: dict = field(default_factory=dict)
    # Kick-vote cooldowns per starter (retail MIN_TIME_BETWEEN_KICK_VOTES =
    # 300 s and MIN_TIME_BETWEEN_CANCELLED_KICK_VOTES = 45 s, shared
    # constants C:5269-5273). A denial sends KICK_DENIED_REASON_VOTE_TOO_SOON.
    votekick_cooldown_seconds: float = 300.0
    votekick_cancelled_cooldown_seconds: float = 45.0
    # Retail KICK_NOT_ENOUGH_PLAYERS: "There must be at least 3 players on a
    # team to initiate a kick" (counted on the starter's team, bots included).
    votekick_min_team_players: int = 3
    # [audio] mode_start_music: start a looping in-round music bed (a
    # last_man_standing_00N track) at every round start and for joiners.
    # A deliberate deviation kept on the owner's request: retail rounds were
    # silent plus map ambience until the final 61 s (game_ending_00N), and
    # last_man_standing played only for the Zombie last survivor. false =
    # retail silence.
    mode_start_music: bool = True
    game_rules: GameRules = field(default_factory=GameRules.server_defaults)

    # Team settings
    team1_name: str = "TEAM1_COLOR"
    team1_color: Tuple[int, int, int] = (44, 117, 179)
    team2_name: str = "TEAM2_COLOR"
    team2_color: Tuple[int, int, int] = (137, 179, 44)
    auto_balance: bool = True
    balance_threshold: int = 2
    # Mid-match auto-balance (server/team_balance.py), checked every
    # ``balance_check_interval`` seconds while auto_balance is on. A lead of
    # balance_threshold must last ``balance_grace_seconds`` (a reconnect or bot
    # backfill may fix it); then a dead bot switches sides, after
    # ``balance_bot_wait_seconds`` a bot is retired/replaced, and finally a
    # dead human is moved. Nobody is moved twice within
    # ``balance_player_cooldown`` seconds.
    balance_mid_match: bool = True
    balance_grace_seconds: float = 5.0
    balance_player_cooldown: float = 600.0
    balance_bot_wait_seconds: float = 10.0
    balance_check_interval: float = 1.0

    # [objectives]: escape watch (server/escape_watch.py) and objective
    # guards (modes/objective_guard.py). The watch flags players standing
    # above the map ceiling (sky), inside solid terrain (embedded), or an
    # objective holder sealed in a tiny pocket (entomb) for the given seconds.
    escape_watch_enabled: bool = True
    escape_watch_interval: float = 1.0
    escape_watch_sky_seconds: float = 5.0
    escape_watch_embedded_seconds: float = 3.0
    escape_watch_entomb_seconds: float = 5.0
    # An objective (intel/diamond/bomb) buried in an unreachable pocket for
    # this long is returned/respawned so it can never be made unobtainable.
    objective_entomb_seconds: float = 5.0
    objective_pickup_requires_los: bool = True
    objective_pickup_ends_spawn_protection: bool = True
    # Players idle (no real input) this long do not count toward holding a
    # TC/MH zone; 0 disables.
    objective_afk_seconds: float = 60.0
    # CTF: a carrier in an open-sky pit dug up to this many blocks below the
    # base still scores, so defenders cannot excavate captures away.
    ctf_base_pit_depth: float = 24.0

    # Dev bots: server-side AI players spawned at startup (0 = none).
    bot_count: int = 0
    # New isolated runtime. ``configured`` distinguishes an explicit [bots]
    # table from legacy game.bot_count fixed-population behavior.
    bots: BotConfig = field(default_factory=BotConfig)
    anticheat: AntiCheatConfig = field(default_factory=AntiCheatConfig)
    conduct: ConductConfig = field(default_factory=ConductConfig)
    steam: SteamMasterConfig = field(default_factory=SteamMasterConfig)
    revival: RevivalMasterConfig = field(default_factory=RevivalMasterConfig)

    # Map-entity (crate/intel) wire emission. The Entity byte layout was
    # RE-verified against the compiled client (shared/packet.pyx Entity.read/
    # write rewritten to match: id, type/state/player_id bytes, then pos/vel/
    # yaw/color-rgb/radius fixed-shorts, face byte, fuse short, int/float/ugc
    # count bytes, then int+float property arrays).
    entities_wire_ready: bool = True

    # DEPRECATED ``[weapons]`` keys: accepted for old configs but IGNORED.
    # Damage comes from the retail per-weapon/per-body-part tables
    # (server/weapons_retail.py, shared/constants.py); a flat per-weapon
    # number cannot express them and wiring one would break retail parity.
    # Setting any of these keys logs a warning.
    rifle_damage: int = 49
    smg_damage: int = 29
    shotgun_damage: int = 27
    spade_damage: int = 50
    grenade_damage: int = 100

    # World settings. ``map_size_*`` are INFORMATIONAL: every retail VXL is
    # 512x512x240 and the loader never reads these; a non-stock value logs a
    # warning.
    map_size_x: int = 512
    map_size_y: int = 512
    map_size_z: int = int(C.MAP_Z) if hasattr(C, "MAP_Z") else 240
    # Informational only: the water plane is fixed by the retail VXL format
    # (z 238/239) and retail has no water damage besides the class
    # fall-on-water multiplier.  Neither key changes the simulation.
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
    # "kill_points": 1, "headshot_bonus": 0}. Empty = use mode_data defaults.
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
    # Logging runs behind a bounded queue, but the sink must be bounded too.
    # Old packet tracing produced multi-hundred-megabyte server.log files on
    # long-lived hosts; rotating in the listener thread keeps both disk usage
    # and gameplay-thread latency independent of server uptime.
    log_max_bytes: int = 16 * 1024 * 1024
    log_backup_count: int = 3

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
    # that asynchronous boundary. During sustained flight this accepted-input
    # interval also bounds periodic causal checkpoints so a long Glide Jetpack
    # burn cannot accumulate into one release-time hard snap. Observer rows
    # remain unaffected.
    jetpack_owner_handoff_input_frames: int = 30
    # Fuel exhaustion while SPACE remains held changes the native client back
    # to ballistic movement asynchronously. Keep the owner row quiet through
    # release, settle, and landing, with this accepted-input safety bound.
    jetpack_owner_release_handoff_input_frames: int = 600
    # The retail owner applies pack thrust when its WorldUpdate row with the
    # active bit arrives, and stops it when the inactive row arrives; neither
    # moment is acknowledged. The authoritative simulation therefore starts
    # thrust this many accepted-input frames after announcing activation and
    # keeps it this many frames after announcing exhaustion. Calibrated with
    # scripts/scenarios/movement_stress.py (rocketeer_jump_pack_hold), see
    # docs/RETAIL_JUMP_RESTORE.md.
    jetpack_activation_defer_frames: int = 2
    jetpack_exhaustion_tail_frames: int = 3
    # When true, append every self-row's (stamp, position) to
    # logs/selfrow_samples.ndjson for offline reconciliation calibration
    # (join with the client capture via tmp/reconcile_sim.py). Debug only.
    debug_selfrow: bool = False
    movement_debug_capture: bool = False
    # Retail ClientData is emitted after the local frame but its movement
    # buttons may describe the preceding native step. Kept as an explicit A/B
    # switch until the transition chronology is fully certified (0 or 1).
    movement_input_latch_frames: int = 1
    # Retail ClientData is ENet SEND_UNSEQUENCED (measured with
    # scripts/enet_sniff.py), so a lost packet is never retransmitted, and the
    # client labels every update with a contiguous loop_count that only jumps
    # when it is more than MAX_CLOCK_SYNC_DIFFERENCE (10) loops off. A gap of
    # up to this many missing labels is therefore lost input, not a clock
    # jump: the server synthesizes one held-input frame per tick for each so
    # its step count keeps matching the client's frame count. Without this a
    # single lost packet leaves authority one frame behind for good and the
    # client corrects on every self row (docs/RETAIL_INPUT_LOSS.md). 0 disables.
    input_gap_fill_limit: int = 8
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
        """The advertised name, capped at retail MAX_SERVER_NAME_SIZE (31).

        The constant is server-only (no stock client binary reads it), so
        the retail server enforced it; InitialInfo, A2S and the master
        adverts all use this capped value.
        """
        return str(self.name)[:MAX_SERVER_NAME_SIZE]

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


def resolve_mode_code(value) -> str:
    """Validate ``game.default_mode`` and return its canonical registry code.

    An unknown mode used to leave the server running with ``mode = None``
    (no rules, no scoring, no round end). Fail fast with the accepted names.
    Aliases collapse to the retail short code ("zombie" -> "zom") so map
    metadata and ``[modes.*]`` overlays see one spelling.
    """
    from modes import canonical_mode_code, registered_mode_codes

    code = canonical_mode_code(str(value))
    if code is None:
        raise ValueError(
            f"game.default_mode {value!r} is not a registered game mode; "
            f"expected one of: {', '.join(registered_mode_codes())}"
        )
    return code


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
        if len(str(config.name)) > MAX_SERVER_NAME_SIZE:
            import logging

            logging.getLogger(__name__).warning(
                "[server] name %r is longer than the retail limit of %d "
                "characters; clients will see %r",
                config.name, MAX_SERVER_NAME_SIZE,
                str(config.name)[:MAX_SERVER_NAME_SIZE],
            )
        config.port = s.get("port", config.port)
        config.max_players = min(255, max(1, int(
            s.get("max_players", config.max_players)
        )))
        config.tick_rate = min(240, max(10, int(
            s.get("tick_rate", config.tick_rate)
        )))
        config.steam_id = max(0, int(s.get("steam_id", config.steam_id)))
        greeting = s.get("join_greeting", config.join_greeting)
        if not isinstance(greeting, str):
            raise ValueError("server.join_greeting must be a string")
        config.join_greeting = greeting
        motd = s.get("motd", config.motd)
        if isinstance(motd, str):
            motd = motd.splitlines()
        if not isinstance(motd, list) or not all(isinstance(line, str) for line in motd):
            raise ValueError("server.motd must be a string or an array of strings")
        config.motd = list(motd)

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
        config.map_rotation_shuffle = bool(
            lobby.get("map_rotation_shuffle", config.map_rotation_shuffle)
        )
        config.map_vote_retail_max_players = bool(lobby.get(
            "map_vote_retail_max_players", config.map_vote_retail_max_players
        ))
        config.end_screen_seconds = min(
            120.0,
            max(
                0.0,
                float(
                    lobby.get(
                        "end_screen_seconds",
                        config.end_screen_seconds,
                    )
                ),
            ),
        )
        config.end_round_scoreboard = bool(
            lobby.get("end_round_scoreboard", config.end_round_scoreboard)
        )
        config.end_round_headline = bool(
            lobby.get("end_round_headline", config.end_round_headline)
        )
        config.map_vote_size_fit = bool(
            lobby.get("map_vote_size_fit", config.map_vote_size_fit)
        )
        config.map_vote_area_per_player = min(262144.0, max(1.0, float(
            lobby.get("map_vote_area_per_player", config.map_vote_area_per_player)
        )))
        config.map_vote_min_area = min(262144.0, max(0.0, float(
            lobby.get("map_vote_min_area", config.map_vote_min_area)
        )))
        config.map_vote_recent_exclude = min(32, max(0, int(
            lobby.get("map_vote_recent_exclude", config.map_vote_recent_exclude)
        )))
        config.map_vote_bot_weight = min(1.0, max(0.0, float(
            lobby.get("map_vote_bot_weight", config.map_vote_bot_weight)
        )))
        config.votekick_cooldown_seconds = min(3600.0, max(0.0, float(
            lobby.get("votekick_cooldown_seconds", config.votekick_cooldown_seconds)
        )))
        config.votekick_cancelled_cooldown_seconds = min(3600.0, max(0.0, float(
            lobby.get(
                "votekick_cancelled_cooldown_seconds",
                config.votekick_cancelled_cooldown_seconds,
            )
        )))
        config.votekick_min_team_players = min(32, max(0, int(
            lobby.get("votekick_min_team_players", config.votekick_min_team_players)
        )))
        overrides = lobby.get("map_size_overrides", config.map_size_overrides)
        if not isinstance(overrides, dict):
            raise ValueError("lobby.map_size_overrides must be a TOML table")
        normalized_overrides: dict = {}
        for map_name, size in overrides.items():
            if isinstance(size, str):
                size_class = size.strip().lower()
                if size_class not in MAP_SIZE_CLASSES:
                    raise ValueError(
                        f"lobby.map_size_overrides.{map_name} must be an area "
                        "or one of small, medium, large"
                    )
                normalized_overrides[str(map_name)] = size_class
            elif isinstance(size, bool) or not isinstance(size, (int, float)):
                raise ValueError(
                    f"lobby.map_size_overrides.{map_name} must be an area "
                    "or one of small, medium, large"
                )
            else:
                normalized_overrides[str(map_name)] = min(262144, max(1, int(size)))
        config.map_size_overrides = normalized_overrides

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
        config.require_protocol_version = bool(n.get(
            "require_protocol_version", config.require_protocol_version))
        for dead_key, retail_value in (("timeout_ms", 10000), ("bandwidth_limit", 0)):
            if dead_key in n and n.get(dead_key) != retail_value:
                import logging

                logging.getLogger(__name__).warning(
                    "[network] %s is ignored: the ENet host keeps the retail "
                    "library defaults", dead_key,
                )
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
        config.map_air_catchup_batch_limit = max(1, int(n.get(
            "map_air_catchup_batch_limit",
            config.map_air_catchup_batch_limit)))
        config.map_air_catchup_enabled = bool(n.get(
            "map_air_catchup_enabled", config.map_air_catchup_enabled))
        config.unreliable_throttle_deceleration = max(0, min(32, int(n.get(
            "unreliable_throttle_deceleration",
            config.unreliable_throttle_deceleration))))
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
        config.prefab_competitive_cell_budget = min(8192, max(64, int(n.get(
            "prefab_competitive_cell_budget",
            config.prefab_competitive_cell_budget))))
        config.prefab_validation_batch_limit = min(4096, max(64, int(n.get(
            "prefab_validation_batch_limit",
            config.prefab_validation_batch_limit))))
        config.prefab_health_state_batch = min(4096, max(1, int(n.get(
            "prefab_health_state_batch",
            config.prefab_health_state_batch))))
        config.lag_compensation_enabled = bool(n.get(
            "lag_compensation_enabled", config.lag_compensation_enabled))
        config.lag_compensation_max_ms = min(1000.0, max(0.0, float(n.get(
            "lag_compensation_max_ms", config.lag_compensation_max_ms))))
        config.lag_compensation_extra_ms = min(250.0, max(0.0, float(n.get(
            "lag_compensation_extra_ms", config.lag_compensation_extra_ms))))
        config.lag_compensation_view_delay_ms = min(250.0, max(0.0, float(n.get(
            "lag_compensation_view_delay_ms",
            config.lag_compensation_view_delay_ms))))
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
        config.terrain_collapse_repair_batch_limit = max(1, int(n.get(
            "terrain_collapse_repair_batch_limit",
            config.terrain_collapse_repair_batch_limit)))
        config.terrain_collapse_repair_delay_ticks = max(1, int(n.get(
            "terrain_collapse_repair_delay_ticks",
            config.terrain_collapse_repair_delay_ticks)))
        config.transition_grace_seconds = min(5.0, max(0.0, float(n.get(
            "transition_grace_seconds", config.transition_grace_seconds))))

    if "game" in data:
        g = data["game"]
        config.default_mode = resolve_mode_code(
            g.get("default_mode", config.default_mode)
        )
        config.default_map = g.get("default_map", config.default_map)
        config.respawn_time = g.get("respawn_time", config.respawn_time)
        config.friendly_fire = g.get("friendly_fire", config.friendly_fire)
        config.fall_damage = g.get("fall_damage", config.fall_damage)
        config.build_damage = g.get("build_damage", config.build_damage)
        config.score_limit = max(0, int(g.get("score_limit", config.score_limit)))
        # 250 is an operator ceiling only (it is the retail radar RANGE, not a
        # lifetime); the retail lifetime is the 45 s default above.
        config.radar_station_lifetime_seconds = min(
            250.0,
            max(
                1.0,
                float(
                    g.get(
                        "radar_station_lifetime_seconds",
                        config.radar_station_lifetime_seconds,
                    )
                ),
            ),
        )
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
    # RULE_ENABLE_FALL_ON_WATER_DAMAGE ("Enable Damage for Falling in Water")
    # only zeroes the WATER landing multiplier; it never switches off land
    # fall damage, so an explicit rule must not rewrite ``fall_damage``
    # (rules audit 2026-09-27 #5). ``fall_damage`` stays the operator switch.
    if "RULE_ENABLE_FALL_ON_WATER_DAMAGE" not in config.game_rules.explicit:
        config.game_rules.values[
            "RULE_ENABLE_FALL_ON_WATER_DAMAGE"
        ] = bool(config.fall_damage)

    if "anticheat" in data and isinstance(data["anticheat"], dict):
        ac = data["anticheat"]
        for name, default in vars(AntiCheatConfig()).items():
            if name not in ac:
                continue
            value = ac[name]
            if isinstance(default, bool):
                setattr(config.anticheat, name, bool(value))
                continue
            if isinstance(default, str):
                text = str(value).strip()
                if not text:
                    raise ValueError(f"anticheat.{name} cannot be empty")
                setattr(config.anticheat, name, text)
                continue
            if isinstance(default, int):
                number = max(0, int(value))
            else:
                number = max(0.0, float(value))
            if name in _ANTICHEAT_UNIT_KEYS:
                number = min(1.0, number)
            bounds = _ANTICHEAT_BOUNDS.get(name)
            if bounds is not None:
                number = min(bounds[1], max(bounds[0], number))
            setattr(config.anticheat, name, number)

    if "audio" in data:
        au = data["audio"]
        if not isinstance(au, dict):
            raise ValueError("audio must be a TOML table")
        config.mode_start_music = bool(
            au.get("mode_start_music", config.mode_start_music)
        )

    if "objectives" in data:
        ob = data["objectives"]
        if not isinstance(ob, dict):
            raise ValueError("objectives must be a TOML table")
        for name in (
            "escape_watch_enabled",
            "objective_pickup_requires_los",
            "objective_pickup_ends_spawn_protection",
        ):
            setattr(config, name, bool(ob.get(name, getattr(config, name))))
        config.escape_watch_interval = min(60.0, max(0.1, float(ob.get(
            "escape_watch_interval", config.escape_watch_interval))))
        for name in (
            "escape_watch_sky_seconds",
            "escape_watch_embedded_seconds",
            "escape_watch_entomb_seconds",
            "objective_entomb_seconds",
        ):
            setattr(config, name, min(600.0, max(0.0, float(
                ob.get(name, getattr(config, name))))))
        config.objective_afk_seconds = min(3600.0, max(0.0, float(ob.get(
            "objective_afk_seconds", config.objective_afk_seconds))))
        config.ctf_base_pit_depth = min(240.0, max(0.0, float(ob.get(
            "ctf_base_pit_depth", config.ctf_base_pit_depth))))

    if "conduct" in data and isinstance(data["conduct"], dict):
        cd = data["conduct"]
        for name, default in vars(ConductConfig()).items():
            if name not in cd:
                continue
            value = cd[name]
            if isinstance(default, bool):
                setattr(config.conduct, name, bool(value))
            elif isinstance(default, list):
                if isinstance(value, (list, tuple)):
                    setattr(
                        config.conduct,
                        name,
                        [str(item) for item in value if str(item).strip()],
                    )
            else:
                setattr(config.conduct, name, max(0.0, float(value)))

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
        worker = str(b.get("worker", config.bots.worker)).lower()
        config.bots.worker = (
            worker if worker in ("thread", "process") else "thread"
        )
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
        behavior = str(b.get("behavior_version", config.bots.behavior_version)).lower()
        if behavior not in {"classic", "cooperative"}:
            raise ValueError("bots.behavior_version must be classic or cooperative")
        config.bots.behavior_version = behavior
        config.bots.friendly_mischief = bool(
            b.get("friendly_mischief", config.bots.friendly_mischief)
        )
        config.bots.chatter = bool(b.get("chatter", config.bots.chatter))
        config.bots.debug_visualization = bool(
            b.get("debug_visualization", config.bots.debug_visualization)
        )
        config.bots.clean_slate_games = max(
            0,
            int(b.get("clean_slate_games", config.bots.clean_slate_games)),
        )
        config.bots.skill_balance = bool(
            b.get("skill_balance", config.bots.skill_balance)
        )
        # Bounds match the clamps skill_balance.py applies at use.
        config.bots.skill_balance_max_shift = min(0.9, max(0.0, float(
            b.get("skill_balance_max_shift", config.bots.skill_balance_max_shift)
        )))
        config.bots.skill_balance_rate = min(1.0, max(0.0, float(
            b.get("skill_balance_rate", config.bots.skill_balance_rate)
        )))
        config.bots.skill_balance_deadband = min(0.95, max(0.0, float(
            b.get("skill_balance_deadband", config.bots.skill_balance_deadband)
        )))
        config.bots.skill_balance_min_events = min(1000, max(1, int(
            b.get("skill_balance_min_events", config.bots.skill_balance_min_events)
        )))

    if "steam" in data:
        steam = data["steam"]
        if not isinstance(steam, dict):
            raise ValueError("steam must be a TOML table")
        config.steam.enabled = bool(steam.get("enabled", config.steam.enabled))
        config.steam.app_id = int(steam.get("app_id", config.steam.app_id))
        if config.steam.app_id != 224540:
            raise ValueError(
                "steam.app_id must be 224540; the retail browser filters the "
                "Ace of Spades application, and app 480 is only Spacewar"
            )
        config.steam.runtime_dir = str(
            steam.get("runtime_dir", config.steam.runtime_dir)
        ).strip()
        config.steam.steamclient_dir = str(
            steam.get("steamclient_dir", config.steam.steamclient_dir)
        ).strip()
        config.steam.helper_path = str(
            steam.get("helper_path", config.steam.helper_path)
        ).strip()
        config.steam.use_supplied_steamclient = bool(
            steam.get(
                "use_supplied_steamclient",
                config.steam.use_supplied_steamclient,
            )
        )
        config.steam.steam_port = int(
            steam.get("steam_port", config.steam.steam_port)
        )
        config.steam.query_port = int(
            steam.get("query_port", config.steam.query_port)
        )
        config.steam.public = bool(steam.get("public", config.steam.public))
        config.steam.secure = bool(steam.get("secure", config.steam.secure))
        config.steam.region = _steam_token(
            "region",
            steam.get("region", config.steam.region),
            31,
        )
        config.steam.game_version = _steam_token(
            "game_version",
            steam.get("game_version", config.steam.game_version),
            31,
        )
        if not config.steam.game_version:
            raise ValueError("steam.game_version cannot be empty")
        config.steam.protocol_version = max(
            0,
            int(steam.get("protocol_version", config.steam.protocol_version)),
        )
        config.steam.playlist_id = max(
            0,
            int(steam.get("playlist_id", config.steam.playlist_id)),
        )
        config.steam.texture_skin = _steam_token(
            "texture_skin",
            steam.get("texture_skin", config.steam.texture_skin),
            31,
        )
        config.steam.require_registration = bool(
            steam.get(
                "require_registration",
                config.steam.require_registration,
            )
        )
        config.steam.startup_timeout_seconds = min(
            60.0,
            max(
                1.0,
                float(
                    steam.get(
                        "startup_timeout_seconds",
                        config.steam.startup_timeout_seconds,
                    )
                ),
            ),
        )
        config.steam.publish_interval_seconds = min(
            10.0,
            max(
                0.25,
                float(
                    steam.get(
                        "publish_interval_seconds",
                        config.steam.publish_interval_seconds,
                    )
                ),
            ),
        )
        if not 1 <= int(config.steam.steam_port) <= 65535:
            raise ValueError("steam.steam_port must be a valid UDP port")
        if config.steam.query_port and not 1 <= int(config.steam.query_port) <= 65535:
            raise ValueError("steam.query_port must be a valid UDP port")
        if config.steam.enabled:
            effective_query = config.steam.effective_query_port(config.port)
            if not 1 <= int(effective_query) <= 65535:
                raise ValueError("steam.query_port must be a valid UDP port")
            ports = {
                int(config.port),
                int(config.steam.steam_port),
                int(effective_query),
            }
            if len(ports) != 3:
                raise ValueError(
                    "server.port, steam.steam_port, and the Steam query port "
                    "must be distinct"
                )
        if config.steam.secure and not config.steam.public:
            raise ValueError("steam.secure requires steam.public")

    if "revival" in data:
        revival = data["revival"]
        if not isinstance(revival, dict):
            raise ValueError("revival must be a TOML table")
        config.revival.enabled = bool(
            revival.get("enabled", config.revival.enabled)
        )
        config.revival.base_url = str(
            revival.get("base_url", config.revival.base_url)
        ).strip().rstrip("/")
        parsed_base_url = urlsplit(config.revival.base_url)
        local_http = (
            parsed_base_url.scheme == "http"
            and parsed_base_url.hostname in {"127.0.0.1", "localhost", "::1"}
        )
        if (
            not parsed_base_url.netloc
            or parsed_base_url.username is not None
            or parsed_base_url.password is not None
            or parsed_base_url.query
            or parsed_base_url.fragment
            or not (parsed_base_url.scheme == "https" or local_http)
        ):
            raise ValueError(
                "revival.base_url must be an HTTPS origin "
                "(localhost HTTP is allowed for development)"
            )
        config.revival.public_host = str(
            revival.get("public_host", config.revival.public_host)
        ).strip()
        if not config.revival.public_host:
            raise ValueError("revival.public_host cannot be empty")
        config.revival.server_id = str(
            revival.get("server_id", config.revival.server_id)
        ).strip()
        config.revival.region = _revival_token(
            "region",
            revival.get("region", config.revival.region),
            31,
        ) or "europe"
        config.revival.results_path = str(
            revival.get("results_path", config.revival.results_path)
        ).strip()
        if not config.revival.results_path:
            raise ValueError("revival.results_path cannot be empty")
        config.revival.official = bool(
            revival.get("official", config.revival.official)
        )
        config.revival.require_identity = bool(
            revival.get("require_identity", config.revival.require_identity)
        )
        config.revival.heartbeat_interval_seconds = min(
            60.0,
            max(
                15.0,
                float(
                    revival.get(
                        "heartbeat_interval_seconds",
                        config.revival.heartbeat_interval_seconds,
                    )
                ),
            ),
        )
        config.revival.request_timeout_seconds = min(
            15.0,
            max(
                1.0,
                float(
                    revival.get(
                        "request_timeout_seconds",
                        config.revival.request_timeout_seconds,
                    )
                ),
            ),
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
        config.balance_mid_match = bool(
            t.get("balance_mid_match", config.balance_mid_match)
        )
        config.balance_grace_seconds = min(600.0, max(0.0, float(
            t.get("balance_grace_seconds", config.balance_grace_seconds)
        )))
        config.balance_player_cooldown = min(86400.0, max(0.0, float(
            t.get("balance_player_cooldown", config.balance_player_cooldown)
        )))
        config.balance_bot_wait_seconds = min(600.0, max(0.0, float(
            t.get("balance_bot_wait_seconds", config.balance_bot_wait_seconds)
        )))
        config.balance_check_interval = min(60.0, max(0.1, float(
            t.get("balance_check_interval", config.balance_check_interval)
        )))

    if "weapons" in data:
        w = data["weapons"]
        config.rifle_damage = w.get("rifle_damage", config.rifle_damage)
        config.smg_damage = w.get("smg_damage", config.smg_damage)
        config.shotgun_damage = w.get("shotgun_damage", config.shotgun_damage)
        config.spade_damage = w.get("spade_damage", config.spade_damage)
        config.grenade_damage = w.get("grenade_damage", config.grenade_damage)
        present = sorted(key for key in _DEPRECATED_WEAPON_KEYS if key in w)
        if present:
            import logging

            logging.getLogger(__name__).warning(
                "[weapons] %s ignored: damage comes from the retail per-weapon "
                "tables (server/weapons_retail.py, shared/constants.py); "
                "remove the [weapons] table",
                "/".join(present),
            )

    if "world" in data:
        w = data["world"]
        config.map_size_x = w.get("map_size_x", config.map_size_x)
        config.map_size_y = w.get("map_size_y", config.map_size_y)
        config.map_size_z = w.get("map_size_z", config.map_size_z)
        changed = [
            f"{key}={w[key]!r}"
            for key, stock in _INFORMATIONAL_MAP_SIZE.items()
            if key in w and w[key] != stock
        ]
        if changed:
            import logging

            logging.getLogger(__name__).warning(
                "[world] %s ignored: map dimensions are fixed by the retail "
                "VXL format (512x512x240); map_size_* is informational",
                ", ".join(changed),
            )
        config.water_level = w.get("water_level", config.water_level)
        config.water_damage = w.get("water_damage", config.water_damage)
        if "water_level" in w or "water_damage" in w:
            import logging

            logging.getLogger(__name__).warning(
                "[world] water_level/water_damage are ignored (the retail "
                "water plane is fixed at z 238/239 and has no water damage)"
            )
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
        from modes import canonical_mode_code

        # Modes read their overlay by retail short code ("zom", "oc"), so an
        # alias table such as [modes.zombie] used to be silently ignored.
        # Canonical-key tables win over alias tables for the same mode.
        items = sorted(
            data["modes"].items(),
            key=lambda item: canonical_mode_code(str(item[0])) == str(item[0]).lower(),
        )
        for code, settings in items:
            if isinstance(settings, dict):
                key = canonical_mode_code(str(code)) or str(code)
                merged = dict(config.mode_settings.get(key, {}))
                merged.update(settings)
                config.mode_settings[key] = merged

    if "admin" in data:
        a = data["admin"]
        if not isinstance(a, dict):
            raise ValueError("admin must be a TOML table")
        password = a.get("password", config.admin_password)
        if isinstance(password, (dict, list)):
            raise ValueError("admin.password must be a string")
        config.admin_password = "" if password is None else str(password)
        config.log_commands = a.get("log_commands", config.log_commands)
        config.bans_path = str(a.get("bans_path", config.bans_path))
    problem = admin_password_problem(config.admin_password)
    if problem is not None:
        import logging

        logging.getLogger("BattleSpades.config").warning(
            "In-game /admin login is DISABLED: %s. Set [admin] password in "
            "%s to a unique secret of at least %d characters to enable it.",
            problem,
            path if path is not None else "config.toml",
            MIN_ADMIN_PASSWORD_LENGTH,
        )

    if "logging" in data:
        lg = data["logging"]
        config.log_level = lg.get("level", config.log_level)
        config.log_file = lg.get("file", config.log_file)
        config.log_console = lg.get("console", config.log_console)
        config.packet_trace = bool(lg.get("packet_trace", config.packet_trace))
        config.log_queue_capacity = max(256, int(lg.get(
            "queue_capacity", config.log_queue_capacity)))
        config.log_max_bytes = max(
            1024 * 1024,
            min(
                1024 * 1024 * 1024,
                int(lg.get("max_bytes", config.log_max_bytes)),
            ),
        )
        config.log_backup_count = max(
            1,
            min(10, int(lg.get("backup_count", config.log_backup_count))),
        )
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
        config.jetpack_activation_defer_frames = max(0, min(30, int(
            dbg.get("jetpack_activation_defer_frames",
                    config.jetpack_activation_defer_frames)
        )))
        config.jetpack_exhaustion_tail_frames = max(0, min(30, int(
            dbg.get("jetpack_exhaustion_tail_frames",
                    config.jetpack_exhaustion_tail_frames)
        )))
        config.debug_selfrow = bool(dbg.get("debug_selfrow", config.debug_selfrow))
        config.movement_debug_capture = bool(dbg.get(
            "movement_debug_capture", config.movement_debug_capture))
        config.movement_input_latch_frames = max(0, min(1, int(dbg.get(
            "movement_input_latch_frames", config.movement_input_latch_frames
        ))))
        config.input_gap_fill_limit = max(0, min(9, int(dbg.get(
            "input_gap_fill_limit", config.input_gap_fill_limit
        ))))
        config.clock_sync_loop_bias = int(dbg.get(
            "clock_sync_loop_bias", config.clock_sync_loop_bias))

    return config
