"""Builder for InitialInfo(114) — the first big packet sent to a client.

Drives every field from real server/config/mode/map state, replacing the
hand-rolled hardcoded version that lived in connection.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shared.packet import InitialInfo

from server import class_data
from server import mode_data
from server.class_selection import DEFAULT_DISABLED_TOOLS

if TYPE_CHECKING:
    from server.main import BattleSpadesServer


# Server-wide defaults that apply to every InitialInfo. Keep these here
# (not deeper in connection.py) so the build is one trip through one file.
_DEFAULT_GROUND_COLORS: list[tuple[int, int, int, int]] = [
    (59, 58, 55, 238),  # default dirt-edge
    (40, 54, 64, 239),
]
_DEFAULT_UGC_PREFAB_SETS: list[int] = [0, 1]


def _server_steam_id(server: 'BattleSpadesServer') -> int:
    # The original game's `server_steam_id` is a uint64 the master server
    # gives out. For local/test servers we use a stable placeholder.
    return int(getattr(server.config, 'steam_id', 90087911866072064))


def _map_filename(server: 'BattleSpadesServer') -> str:
    """The original protocol carries both `map_name` (DISPLAY) and
    `filename` (the .vxl basename the client may have cached). `filename`
    is the raw basename used for the .vxl / CRC; `map_name` is the display
    name (see _map_display_name)."""
    name = server.world_manager.map_name if server.world_manager else server.config.map_name
    return name or 'classicgen'


# The compiled client builds the end-of-round stats screenshot path as
# `level_screenshots/<map_name><0..N>.png`. The stock assets are named with
# SPACES and lowercase connectors ("City of Chicago0.png", "Arctic Base0.png"),
# so sending the bare .vxl basename ("CityOfChicago") makes ShowGameStats(53)
# raise ResourceNotFoundException and CRASH the client at the end of the round.
# map_name must therefore be the spaced DISPLAY name; filename stays the
# basename. Keys are our .vxl basenames; values match the stock screenshot set.
_MAP_DISPLAY_NAMES = {
    "ArcticBase": "Arctic Base",
    "CastleWars": "Castle Wars",
    "CityOfChicago": "City of Chicago",
    "20thCenturyTown": "WW",          # stock ships WW0-3.png for this theme
    "London": "London",
    "Alcatraz": "Alcatraz",
    "Invasion": "Invasion",
    "Frontier": "Frontier",
    "Trenches": "Trenches",
    "Crossroads": "Crossroads",
}


def _map_display_name(server: 'BattleSpadesServer') -> str:
    """Spaced display name matching the stock level-screenshot assets."""
    base = _map_filename(server)
    if base in _MAP_DISPLAY_NAMES:
        return _MAP_DISPLAY_NAMES[base]
    # Fallback: split CamelCase into words ("DragonIsland" -> "Dragon Island").
    import re
    spaced = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', base)
    return spaced


def _map_checksum(server: 'BattleSpadesServer') -> int:
    """CRC32 of the RAW .vxl file bytes, as a signed wire int.

    The client compares this against the crc32 of its local copy of
    `filename`; on a match it loads that local file as its world base.
    (Measured: the original London declaration 592649088 is exactly
    zlib.crc32(London.vxl).) A chunker/stream CRC here makes the client
    reject its pristine local map and play in an empty world."""
    wm = getattr(server, 'world_manager', None)
    crc = int(getattr(wm, 'map_file_crc', 0)) if wm is not None else 0
    crc &= 0xFFFFFFFF
    return crc - (1 << 32) if crc >= (1 << 31) else crc


def _movement_speed_multipliers(server: 'BattleSpadesServer') -> list[float]:
    return class_data.initial_info_movement_multipliers()


def _classes_disabled(server: 'BattleSpadesServer') -> list[int]:
    """Compute disabled_classes as the inverse of mode.allowed_classes.
    Returns the integer class IDs the client must hide from the picker."""
    mode = mode_data.get(server.config.game_mode)
    allowed = set(mode.allowed_classes)
    if not allowed:
        return []
    all_class_ids = set(class_data.CLASS_IDS)
    return sorted(all_class_ids - allowed)


def build_initial_info(server: 'BattleSpadesServer') -> InitialInfo:
    """Construct an InitialInfo packet matching the active server state."""
    cfg = server.config
    mode = mode_data.get(cfg.game_mode)

    pkt = InitialInfo()

    # ---- Server identity ------------------------------------------------
    pkt.server_steam_id = _server_steam_id(server)
    pkt.server_ip = 0
    pkt.server_port = int(cfg.port)
    pkt.query_port = int(cfg.port)
    pkt.server_name = cfg.server_name

    # ---- Mode metadata --------------------------------------------------
    pkt.mode_name = mode.title_string
    pkt.mode_description = mode.description_string
    pkt.mode_infographic_text1 = mode.infographic1
    pkt.mode_infographic_text2 = mode.infographic2
    pkt.mode_infographic_text3 = mode.infographic3
    pkt.mode_key = mode.mode_id

    # ---- Map metadata ---------------------------------------------------
    # map_name = spaced DISPLAY name (drives the end-game screenshot lookup);
    # filename = raw .vxl basename (drives the map transfer + CRC validation).
    pkt.map_name = _map_display_name(server)
    pkt.filename = _map_filename(server)
    pkt.checksum = _map_checksum(server)
    pkt.map_is_ugc = 0
    pkt.ugc_mode = mode.mode_id

    # ---- Game rules / display -------------------------------------------
    pkt.classic = 1 if mode.classic else 0
    pkt.enable_minimap = 0 if mode.classic else 1
    # The mover receives nearby player positions separately from terrain.
    # Keep this flag and Player._build_player_collision_positions identical or
    # the two simulations apply different contact impulses.
    pkt.same_team_collision = 1 if cfg.same_team_collision else 0
    pkt.max_draw_distance = 192
    pkt.enable_colour_picker = 1
    pkt.enable_colour_palette = 1
    pkt.enable_deathcam = 1
    pkt.enable_sniper_beam = 1
    pkt.enable_spectator = 1
    pkt.exposed_teams_always_on_minimap = 0
    pkt.enable_numeric_hp = 1
    # The native InitialInfo field is a null-terminated string. Mafia modes
    # select the shipped gangster UI skin; an empty string selects default.
    pkt.texture_skin = 'mafia' if mode.mafia else None
    pkt.beach_z_modifiable = 1
    pkt.enable_minimap_height_icons = 0
    pkt.enable_fall_on_water_damage = 1
    pkt.block_wallet_multiplier = 1.0
    pkt.block_health_multiplier = 1.0
    pkt.enable_player_score = 1
    pkt.allow_shooting_holding_intel = 1
    pkt.friendly_fire = 1 if cfg.friendly_fire else 0
    pkt.enable_corpse_explosion = 1

    # ---- Class / movement / loadouts ------------------------------------
    # Retail otherwise inserts FLAREBLOCK_TOOL as a fake first prefab tile.
    # The same tuple is the default for class normalization, keeping the menu
    # declaration and every CreatePlayer loadout in lockstep.
    pkt.disabled_tools = list(DEFAULT_DISABLED_TOOLS)
    pkt.disabled_classes = _classes_disabled(server)
    pkt.movement_speed_multipliers = _movement_speed_multipliers(server)
    pkt.ugc_prefab_sets = list(_DEFAULT_UGC_PREFAB_SETS)
    pkt.ground_colors = list(_DEFAULT_GROUND_COLORS)
    pkt.custom_game_rules = []
    pkt.loadout_overrides = {}

    configure = getattr(
        getattr(server, 'mode', None), 'configure_initial_info', None
    )
    if callable(configure):
        configure(pkt)
    return pkt
