"""Builder for InitialInfo(114) — the first big packet sent to a client.

Drives every field from real server/config/mode/map state, replacing the
hand-rolled hardcoded version that lived in connection.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shared.packet import InitialInfo

from server import class_data
from server import mode_data
from server.game_rules import get_rules

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
    # Once anonymous GameServer logon completes, advertise Valve's assigned
    # server SteamID. Local/test servers retain the configured stable value.
    steam_master = getattr(server, 'steam_master', None)
    registered_id = int(getattr(steam_master, 'steam_id', 0) or 0)
    if registered_id:
        return registered_id
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
    "20thCenturyTown": "WW1",         # stock ships WW10-13.png for this theme
    "London": "London",
    "Alcatraz": "Alcatraz",
    "Invasion": "Invasion",
    "Frontier": "Frontier",
    "Trenches": "Trenches",
    "Crossroads": "Crossroads",
}

# Packet 53 indexes ``png/ui/level_screenshots/<map_name><camera>.png``
# directly. A missing file raises ResourceNotFoundException in the retail
# client, so custom/utility maps must use the safe in-scene score hold instead.
_STOCK_LEVEL_SCREENSHOT_NAMES = frozenset({
    "Alcatraz",
    "Ancient Egypt",
    "Arctic Base",
    "Atlantis",
    "Block Ness",
    "Bran Castle",
    "Castle Wars",
    "City of Chicago",
    "Crossroads",
    "Double Dragon",
    "Dragon Island",
    "Frontier",
    "Great Wall",
    "Hiesville",
    "Invasion",
    "London",
    "Lunar Base",
    "Mayan Jungle",
    "Spooky Mansion",
    "The Colosseum",
    "To The Bridge",
    "Tokyo Neon",
    "Trenches",
    "Winter Valley",
    "WW1",
})


def _map_display_name(server: 'BattleSpadesServer') -> str:
    """Spaced display name matching the stock level-screenshot assets."""
    base = _map_filename(server)
    if base in _MAP_DISPLAY_NAMES:
        return _MAP_DISPLAY_NAMES[base]
    # Fallback: split CamelCase into words ("DragonIsland" -> "Dragon Island").
    import re
    spaced = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', base)
    return spaced


def supports_game_stats_screen(server: 'BattleSpadesServer') -> bool:
    """Return whether packet 53 can resolve a bundled retail screenshot."""

    return _map_display_name(server) in _STOCK_LEVEL_SCREENSHOT_NAMES


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
    multiplier = float(get_rules(server.config).get("RULE_CHARACTER_SPEED"))
    return [
        float(value) * multiplier
        for value in class_data.initial_info_movement_multipliers()
    ]


def _classes_disabled(server: 'BattleSpadesServer') -> list[int]:
    """Compute disabled_classes as the inverse of mode.allowed_classes.
    Returns the integer class IDs the client must hide from the picker."""
    mode = mode_data.get(server.config.game_mode)
    allowed = set(mode.allowed_classes)
    if not allowed:
        return []
    all_class_ids = set(class_data.CLASS_IDS)
    disabled = all_class_ids - allowed
    disabled.update(get_rules(server.config).disabled_classes())
    return sorted(disabled)


def build_initial_info(server: 'BattleSpadesServer') -> InitialInfo:
    """Construct an InitialInfo packet matching the active server state."""
    cfg = server.config
    mode = mode_data.get(cfg.game_mode)
    rules = get_rules(cfg)

    pkt = InitialInfo()

    # ---- Server identity ------------------------------------------------
    pkt.server_steam_id = _server_steam_id(server)
    steam_master = getattr(server, 'steam_master', None)
    pkt.server_ip = int(getattr(steam_master, 'public_ip', 0) or 0)
    pkt.server_port = int(cfg.port)
    if bool(getattr(steam_master, 'query_active', False)):
        pkt.query_port = int(cfg.steam.effective_query_port(cfg.port))
    else:
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
    pkt.enable_minimap = int(rules.enabled("RULE_ENABLE_MINI_MAP") and not mode.classic)
    # The mover receives nearby player positions separately from terrain.
    # Keep this flag and Player._build_player_collision_positions identical or
    # the two simulations apply different contact impulses.
    pkt.same_team_collision = 1 if cfg.same_team_collision else 0
    pkt.max_draw_distance = 192
    pkt.enable_colour_picker = int(rules.enabled("RULE_ENABLE_COLOUR_PICKER"))
    pkt.enable_colour_palette = pkt.enable_colour_picker
    pkt.enable_deathcam = int(rules.enabled("RULE_ENABLE_DEATH_CAM"))
    pkt.enable_sniper_beam = int(rules.enabled("RULE_ENABLE_SNIPER_BEAM"))
    pkt.enable_spectator = int(rules.enabled("RULE_ENABLE_SPECTATORS"))
    pkt.exposed_teams_always_on_minimap = 0
    pkt.enable_numeric_hp = 1
    # The native InitialInfo field is a null-terminated string. Mafia modes
    # select the shipped gangster UI skin; an empty string selects default.
    pkt.texture_skin = 'mafia' if mode.mafia else None
    pkt.beach_z_modifiable = 1
    pkt.enable_minimap_height_icons = 0
    pkt.enable_fall_on_water_damage = int(
        rules.enabled("RULE_ENABLE_FALL_ON_WATER_DAMAGE")
    )
    pkt.block_wallet_multiplier = float(
        rules.get("RULE_CHARACTER_BLOCK_WALLETS")
    )
    pkt.block_health_multiplier = float(rules.get("RULE_BLOCK_HEALTH"))
    pkt.enable_player_score = 1
    pkt.allow_shooting_holding_intel = int(
        rules.enabled("RULE_CTF_ENABLE_SHOOT_WITH_INTEL")
    )
    pkt.friendly_fire = 1 if cfg.friendly_fire else 0
    pkt.enable_corpse_explosion = int(
        rules.enabled("RULE_ENABLE_CORPSE_EXPLOSION")
    )

    # ---- Class / movement / loadouts ------------------------------------
    # Retail otherwise inserts FLAREBLOCK_TOOL as a fake first prefab tile.
    # The same tuple is the default for class normalization, keeping the menu
    # declaration and every CreatePlayer loadout in lockstep.
    # Disabled tool IDs are both a menu declaration and an authorization
    # invariant. Config's server defaults retain the flare-tool compatibility
    # suppression that DEFAULT_DISABLED_TOOLS historically provided.
    pkt.disabled_tools = list(rules.selection_disabled_tools())
    pkt.disabled_classes = _classes_disabled(server)
    pkt.movement_speed_multipliers = _movement_speed_multipliers(server)
    pkt.ugc_prefab_sets = list(_DEFAULT_UGC_PREFAB_SETS)
    metadata = getattr(getattr(server, "world_manager", None), "map_metadata", None)
    authored_ground_colors = getattr(metadata, "ground_colors", None) or ()
    pkt.ground_colors = list(authored_ground_colors or _DEFAULT_GROUND_COLORS)
    pkt.custom_game_rules = []
    pkt.loadout_overrides = {}

    configure = getattr(
        getattr(server, 'mode', None), 'configure_initial_info', None
    )
    if callable(configure):
        configure(pkt)
    return pkt
