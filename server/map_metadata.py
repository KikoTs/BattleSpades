"""Authored map environment, zones, and entities stored beside a VXL map.

The VXL stream contains voxel columns only.  Battle Builders UGC maps store
spawn/base zones and drop points in a JSON sidecar (usually ``.txt`` or
``.ugc``) with an ``ugc_entities`` array. Original stock map metadata used
Python-style assignments for environment fields such as ``skybox_texture``;
those scalar fields are parsed as syntax and never executed. Keeping this
parser separate from the voxel loader prevents coloured terrain from being
mistaken for metadata.
"""

from __future__ import annotations

import ast
import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import shared.constants as C

from server.game_constants import TEAM1, TEAM2


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MapZone:
    kind: str
    team: int
    x: float
    y: float
    z: float
    extents: tuple[float, float, float, float, float, float]
    item: str

    def xy_bounds(self) -> tuple[int, int, int, int]:
        x0, x1, y0, y1, _z0, _z1 = self.extents
        return (
            int(self.x + x0),
            int(self.x + x1),
            int(self.y + y0),
            int(self.y + y1),
        )

    def contains_surface_z(self, surface_z: int) -> bool:
        _x0, _x1, _y0, _y1, z0, z1 = self.extents
        return self.z + z0 <= surface_z <= self.z + z1


@dataclass(frozen=True)
class MapEntitySpec:
    entity_type: int
    kind: str
    x: float
    y: float
    z: float
    item: str
    color: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class MapAmbientSound:
    """One stock ``ambient_sounds`` row from map metadata.

    ``points`` is empty for a global bed and contains authored voxel-space
    emitters for local effects such as a river.  The retail registration packet
    carries at most 255 signed-short points; volume and attenuation are passed
    by the paired PlayAmbientSound(24) packet which starts the stream.
    """

    name: str
    points: tuple[tuple[int, int, int], ...] = ()
    volume: float = 1.0
    attenuation: float = 0.0


@dataclass(frozen=True)
class AchievementRegion:
    """One stock ``ac_*`` row: the volume of a map-specific Steam achievement.

    ``kind`` is ``ACH_KILL_REGION`` (kills made from inside the box) or
    ``ACH_BLOCK_DESTROY_REGION`` (the structure inside it is razed); one
    achievement may own several rows. ``bounds`` are absolute inclusive
    ``(x0, x1, y0, y1, z0, z1)``. ``team`` is the team that can earn it,
    ``None`` for either. server/achievements.py evaluates them.
    """

    api_name: str
    kind: int
    bounds: tuple[float, float, float, float, float, float]
    team: int | None = None
    kills: int = 0
    weapon: int = 0


@dataclass
class MapMetadata:
    source: Path | None = None
    # Stock maps still need a full VXL stream.  This flag only selects bundled
    # client presentation assets (sky mesh and ambience); it is never a map-
    # synchronization shortcut.
    official_map: bool = False
    # StateData carries gravity as signed 1.6 fixed point.  Store the
    # wire-canonical value here and apply this same scalar to the server's
    # native World; otherwise an authored value such as Lunar's 0.4 becomes
    # 0.40625 on the retail client while authority continues integrating 0.4.
    gravity: float = 1.0
    # Stock VXL files do not embed their original spawn rectangles.  Most use
    # Blue/TEAM1 on the west side and Green/TEAM2 on the east side, but Tokyo
    # Neon is authored the other way around.  These regions are used only when
    # explicit sidecar/UGC spawn zones are absent.
    fallback_spawn_regions: dict[int, tuple[int, int, int, int]] = field(
        default_factory=lambda: {
            TEAM1: (64, 128, 192, 384),
            TEAM2: (320, 128, 448, 384),
        }
    )
    # Packet 51 is a client mesh-environment filename, not a map basename.
    # The original feature server called this ``skybox_texture`` while UGC
    # JSON exports call it ``skybox_name``.
    skybox_name: str | None = None
    # VXL stores voxel spans only. Environment and static-light colours are
    # authored in the companion metadata file used by the stock server.
    fog_color: tuple[int, int, int] | None = None
    light_color: tuple[int, int, int] | None = None
    light_direction: tuple[float, float, float] | None = None
    back_light_color: tuple[int, int, int] | None = None
    back_light_direction: tuple[float, float, float] | None = None
    ambient_light_color: tuple[int, int, int] | None = None
    ambient_light_intensity: float | None = None
    ambient_sounds: list[MapAmbientSound] = field(default_factory=list)
    # Retail UGC exports keep the terrain/water material palette in `.ugc`.
    # InitialInfo consumes these RGBA rows verbatim (at most 32 entries).
    ground_colors: list[tuple[int, int, int, int]] = field(default_factory=list)
    # Index 0 is the green chroma family; index 1 is the blue family.
    static_light_colors: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    spawn_zones: dict[int, list[MapZone]] = field(
        default_factory=lambda: {TEAM1: [], TEAM2: []}
    )
    base_zones: dict[int, list[MapZone]] = field(
        default_factory=lambda: {TEAM1: [], TEAM2: []}
    )
    # Neutral objective volumes are authored by the Map Creator for modes
    # such as Multi-Hill, Territory Control, and Diamond Mine.  They are not
    # team bases: the retail UGC schema deliberately assigns them
    # TEAM_NEUTRAL and lets the active ruleset decide ownership at runtime.
    neutral_base_zones: list[MapZone] = field(default_factory=list)
    # Occupation is deliberately asymmetric in the retail rules: Green owns
    # one defended base while bombs spawn at authored points for Blue to
    # retrieve.  Keep those placements distinct from ordinary team bases so
    # CTF/Demolition cannot accidentally consume Occupation geometry.
    occupation_base_zone: MapZone | None = None
    occupation_bomb_points: list[tuple[float, float, float]] = field(
        default_factory=list
    )
    # Diamond Mine drop-offs have a per-zone team restriction and capacity.
    # The parallel capacity array mirrors the original sidecar format while
    # MapZone retains the canonical volume/team representation used on wire.
    diamond_base_zones: list[MapZone] = field(default_factory=list)
    diamond_base_capacities: list[int] = field(default_factory=list)
    # Demolition's legacy map description can require a minimum number of
    # objective voxels before a team base is accepted.  Preserve the value so
    # the mode can validate/fallback without reparsing an inert sidecar.
    base_min_destruction: dict[int, int] = field(
        default_factory=lambda: {TEAM1: 0, TEAM2: 0}
    )
    entities: list[MapEntitySpec] = field(default_factory=list)
    # Retail ``team_one_spawn``/``team_two_spawn`` point lists (Trenches).  The
    # authored spawn *areas* already enclose them and remain what spawning
    # uses; the points are kept so tools and tests can check containment.
    spawn_points: dict[int, list[tuple[float, float, float]]] = field(
        default_factory=lambda: {TEAM1: [], TEAM2: []}
    )
    display_name: str | None = None
    cap_limit: int | None = None
    time_limit: float | None = None
    # Retail ``ac_*`` rows (DragonIsland, MayanJungle, SpookyMansion).
    achievement_regions: list[AchievementRegion] = field(default_factory=list)
    # Which metadata key supplied each gameplay layout family for the active
    # mode ("spawn", "base", "neutral", "occupation", "diamond"); absent
    # families fall back to the modes' terrain inference.
    layout_sources: dict[str, str] = field(default_factory=dict)
    # Retail map catalogue (``aos.pkg`` playlists.mapinfo) for stock maps:
    # modes the retail client allowed on this map, the modes its official
    # playlists actually served, and the retail player cap.
    retail_modes: tuple[str, ...] = ()
    retail_playlist_modes: tuple[str, ...] = ()
    retail_max_players: int | None = None


_ITEM_IDS = {name: int(item_id) for item_id, name in C.UGC_TOOL_IMAGES.items()}
_DROP_TYPES = {
    "ugc_ammo_drop": (int(C.AMMO_CRATE), "ammo"),
    "ugc_health_drop": (int(C.HEALTH_CRATE), "health"),
    "ugc_block_drop": (int(C.BLOCK_CRATE), "block"),
}
_LEGACY_DROP_FIELDS = {
    "ammo_crate_drop_points": (int(C.AMMO_CRATE), "ammo", "legacy_ammo_drop"),
    "health_crate_drop_points": (int(C.HEALTH_CRATE), "health", "legacy_health_drop"),
    "block_crate_drop_points": (int(C.BLOCK_CRATE), "block", "legacy_block_drop"),
}
_STATIC_FLARE_ITEMS = frozenset(("flare_block", "flareblock", "glowblock", "static_flare"))

DEFAULT_SKYBOX_NAME = "User_Grassland.txt"
_SKYBOX_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,62}\.txt$")
_AMBIENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,62}$")

# The retail installation exposes only these streaming assets beneath
# ``ambients/``.  Keeping the allow-list server-side prevents a downloaded UGC
# sidecar from asking the native client to resolve arbitrary resource names.
AMBIENT_SOUND_ASSETS = frozenset((
    "amb_alcatraz", "amb_arctic", "amb_area51", "amb_castlewars",
    "amb_castula", "amb_city", "amb_desert", "amb_doomwind",
    "amb_harbour", "amb_high", "amb_invasion", "amb_jungle",
    "amb_moon", "amb_oldchicago", "amb_poolhall", "amb_rural",
    "amb_western", "amb_ww_coastalcold", "amb_ww_lighter",
    "amb_zombieisland", "em_river",
))

# Official map identity is a presentation catalog, not a protocol mode.  The
# aliases are the shipped VXL basenames whose matching mesh manifest uses a
# different resource name.  Unknown/community maps fall back to their sidecar
# or the safe UGC grassland environment.
#
# Evidence (docs/MAP_METADATA.md has the full table): DragonIsland,
# MayanJungle, SpookyMansion and Trenches are read from the retail ``.txtc``
# sidecars.  CastleWars and DoubleDragon are identified from the retail
# loading art (``png/ui/game_loading/map_images``): CastleWars shows the red
# volcanic sky, mountain ranges and falling fireballs that only the Invasion
# dome carries, DoubleDragon the stars, moon and meteor streak that only
# SecretBase_Night carries.  Crossroads' art is the sepia smoke haze of the
# WW dome family (not the cyan UGC grassland void) and Hiesville, the retail
# Normandy map, is the only stock candidate for the otherwise unused WW2 dome.
STOCK_MAP_SKYBOXES = {
    "20thcenturytown": "WW1.txt",
    "alcatraz": "Alcatraz.txt",
    "ancientegypt": "Egypt.txt",
    "arcticbase": "ArcticBase.txt",
    "atlantis": "Atlantis.txt",
    "blockness": "User_Grassland.txt",
    "brancastle": "BranCastle.txt",
    "castlewars": "Invasion.txt",
    "cityofchicago": "Chicago.txt",
    "classic": "Classic.txt",
    "crossroads": "WW1.txt",
    "doubledragon": "SecretBase_Night.txt",
    "dragonisland": "SecretBase.txt",
    "frontier": "Frontier.txt",
    "greatwall": "GreatWall.txt",
    "hiesville": "WW2.txt",
    "invasion": "Invasion.txt",
    "london": "London.txt",
    "lunarbase": "LunarBase.txt",
    "mayanjungle": "MayanJungle.txt",
    "spookymansion": "BranCastle.txt",
    "thecolosseum": "Colosseum.txt",
    "tokyoneon": "Tokyo.txt",
    "tothebridge": "WW2_DockLands.txt",
    "training": "Classic_B.txt",
    "trenches": "WW1.txt",
    "wintervalley": "ArcticBase.txt",
    "ww1": "WW1.txt",
}

# Recovered stock rules which are not carried by the VXL voxel stream.  Lunar
# uses the same 0.4 authored value as the shipped LunarBaseplate metadata.
# Tokyo's blue chroma base is at x ~= 393 and its green base at x ~= 147, so
# its fallback team regions are deliberately reversed from the common layout.
_STOCK_MAP_GRAVITY = {
    "lunarbase": 0.4,
}
#
# The other entries below are likewise read off team-coloured geometry that
# the retail VXLs carry (``tools/map_metadata/survey_team_sides.py`` prints the
# measurements; the retail menu previews in ``maps/*.png`` show the same art
# rotated 90 degrees).  Blue is TEAM1 and Green TEAM2 on the wire.  Regions
# are ``(x0, y0, x1, y1)``; the world manager clusters spawns around the dry
# column nearest the region centre, so each box is centred on that team's
# own base structures:
#
# * CastleWars: the blue-roofed castle fills the south-east corner and the
#   green castle the north-west one (the old west/east default put Blue in
#   the Green castle's half).
# * Atlantis: the paired #0028BE/#00BE2A team markers sit at the north-west
#   (Blue) and south-east (Green) jetty huts of the diagonal layout.
# * DoubleDragon: all twelve marker blobs split along the island chain,
#   Blue towards the south-west tower, Green the north-east tower.
# * WW1: blue markers lie in the southern trench system (y 354-467) and green
#   markers in the northern one (y 49-153): the map is split north/south.
# * ToTheBridge: two islands joined by the one bridge; blue buildings stand
#   on the southern island, green ones on the northern island.
# * Crossroads: the blue-roofed town block and bunker are north of the
#   crossroads, the green-roofed block and bunker south of it.
_STOCK_FALLBACK_SPAWN_REGIONS = {
    "tokyoneon": {
        TEAM1: (320, 128, 448, 384),
        TEAM2: (64, 128, 192, 384),
    },
    "castlewars": {
        TEAM1: (336, 336, 480, 480),
        TEAM2: (32, 32, 176, 176),
    },
    "atlantis": {
        TEAM1: (48, 48, 176, 176),
        TEAM2: (336, 336, 464, 464),
    },
    "doubledragon": {
        TEAM1: (160, 352, 288, 496),
        TEAM2: (224, 16, 352, 160),
    },
    "ww1": {
        TEAM1: (64, 352, 448, 464),
        TEAM2: (64, 48, 448, 160),
    },
    "tothebridge": {
        TEAM1: (64, 336, 448, 480),
        TEAM2: (64, 32, 448, 176),
    },
    "crossroads": {
        TEAM1: (160, 80, 352, 208),
        TEAM2: (160, 304, 352, 432),
    },
}

_MAP_AMBIENT_OVERRIDES = {
    "20thcenturytown": "amb_city",
    "alcatraz": "amb_alcatraz",
    "arcticbase": "amb_arctic",
    "castlewars": "amb_castlewars",
    "cityofchicago": "amb_oldchicago",
    "dragonisland": "amb_doomwind",
    "mayanjungle": "amb_jungle",
    "spookymansion": "amb_zombieisland",
    "trenches": "amb_ww_lighter",
}

_SKYBOX_AMBIENTS = {
    "Alcatraz.txt": "amb_alcatraz",
    "ArcticBase.txt": "amb_arctic",
    "Atlantis.txt": "amb_harbour",
    "BranCastle.txt": "amb_castula",
    "Chicago.txt": "amb_oldchicago",
    "Classic.txt": "amb_rural",
    "Classic_B.txt": "amb_rural",
    "Colosseum.txt": "amb_desert",
    "Egypt.txt": "amb_desert",
    "Frontier.txt": "amb_western",
    "GreatWall.txt": "amb_high",
    "Invasion.txt": "amb_invasion",
    "London.txt": "amb_city",
    "LunarBase.txt": "amb_moon",
    "MayanJungle.txt": "amb_jungle",
    "SecretBase.txt": "amb_area51",
    "SecretBase_Night.txt": "amb_area51",
    "Tokyo.txt": "amb_city",
    "User_Desert.txt": "amb_desert",
    "User_Grassland.txt": "amb_ww_lighter",
    "User_Lunar.txt": "amb_moon",
    "User_Mountain.txt": "amb_castula",
    "User_Temple.txt": "amb_invasion",
    "User_Urban.txt": "amb_ww_lighter",
    "WW1.txt": "amb_ww_lighter",
    "WW2.txt": "amb_ww_coastalcold",
    "WW2_DockLands.txt": "amb_harbour",
}

_LEGACY_ENVIRONMENT_KEYS = frozenset((
    "skybox_texture", "skybox_name", "skybox", "fog_color", "gravity",
    "light_color", "light_direction", "back_light_color",
    "back_light_direction", "ambient_light_color",
    "ambient_light_intensity", "ambient_sounds",
    "static_light_color0", "static_light_color1",
    "ammo_crate_drop_points", "health_crate_drop_points",
    "block_crate_drop_points",
    "team_one_spawn_area", "team_two_spawn_area",
    "team_one_base_point", "team_one_base_w_h_d",
    "team_two_base_point", "team_two_base_w_h_d",
    "team_one_min_destruction", "team_two_min_destruction",
    "mh_base_points", "mh_base_w_h_d",
    "occupation_base_point", "occupation_base_w_h_d",
    "occupation_bomb_points",
    "diamond_base_points", "diamond_base_w_h_d",
    "diamond_base_teams", "diamond_base_capacity",
    # Per-mode layout overrides found in the retail ``.txtc`` modules.
    "ctf_base_points", "ctf_base_w_h_d",
    "tc_base_points", "tc_base_w_h_d",
    "oc_team_one_spawn_area", "oc_team_two_spawn_area",
    "zombie_spawn_area", "survivor_spawn_area",
    "team_one_spawn", "team_two_spawn",
    "ground_colors", "name", "cap_limit", "time_limit",
))


def canonical_gravity(value: object, fallback: float = 1.0) -> float:
    """Return a safe StateData 1.6-fixed gravity scalar.

    Map metadata is trusted only as inert data.  Reject non-finite or extreme
    values, then quantize exactly as ``shared.packet.StateData`` does for the
    positive gameplay range.  The authoritative native world consumes this
    returned value too, keeping prediction and authority bit-identical.
    """

    try:
        gravity = float(value)
    except (TypeError, ValueError):
        gravity = float(fallback)
    if not math.isfinite(gravity) or not 0.0 < gravity <= 8.0:
        gravity = float(fallback)
    return math.floor(gravity * 64.0 + 0.5) / 64.0


def _is_ugc_override(skybox_source: Path | None, fog_source: Path | None) -> bool:
    """Whether a ``.ugc`` skydome choice sits above a ``.txt`` fog pin."""

    return (
        skybox_source is not None
        and skybox_source.suffix.casefold() == ".ugc"
        and fog_source is not None
        and fog_source.suffix.casefold() == ".txt"
    )


def _candidate_sidecars(map_path: Path) -> Iterable[Path]:
    # UGC projects use both .txt and .ugc.  The project sidecar is later than
    # its immutable baseplate .txt and therefore owns settings changed in the
    # editor (skybox, water palette, objects).  Explicit server JSON remains
    # the highest-priority operator override.
    yield map_path.with_suffix(".json")
    yield map_path.with_suffix(".ugc")
    yield map_path.with_suffix(".txt")
    yield Path(str(map_path) + ".json")


RETAIL_MAP_INFO_NAME = "retail_map_info.json"
_RETAIL_MODE_CODES = frozenset((
    "ctf", "tdm", "dem", "dia", "mh", "oc", "tc", "vip", "zom", "tut", "ugc",
))
_CATALOGUE_CACHE: dict[Path, tuple[float, dict[str, dict[str, object]]]] = {}


def retail_map_catalogue(maps_dir: str | Path) -> dict[str, dict[str, object]]:
    """Return the recovered retail map catalogue keyed by casefolded map name.

    ``maps/retail_map_info.json`` is generated from the client's
    ``playlists.mapinfo`` by ``tools/map_metadata/extract_retail.py``. It is
    optional: an operator map directory without it simply has no catalogue.
    """

    path = Path(maps_dir) / RETAIL_MAP_INFO_NAME
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _CATALOGUE_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        rows = document.get("maps", {})
    except (OSError, UnicodeError, ValueError, AttributeError):
        logger.warning("Ignoring unreadable retail map catalogue %s", path)
        rows = {}
    catalogue = {
        str(name).casefold(): entry
        for name, entry in (rows.items() if isinstance(rows, dict) else ())
        if isinstance(entry, dict)
    }
    _CATALOGUE_CACHE[path] = (mtime, catalogue)
    return catalogue


# Single-mode retail playlist per server mode code ("cctf" = the classic
# playlist, whose own mode code is "ctf").
_RETAIL_MODE_PLAYLISTS = {
    "ctf": "ctf", "cctf": "classic", "dem": "demolition", "dia": "diamond",
    "mh": "multihill", "oc": "occupation", "tc": "tc", "tdm": "tdm",
    "vip": "vip", "zom": "zombie", "tut": "tutorial", "ugc": "ugc",
}


def retail_mode_pool(maps_dir: str | Path, mode: object) -> tuple[str, ...]:
    """Return the maps retail's single-mode playlist served for ``mode``.

    Mirrors ``playlists.PlayList.__init__``: every map named by the mode's
    playlist ``.txt``, skipping maps whose ``invalid_modes`` contain the
    mode, whose classic/mafia flag differs from the playlist's, or which are
    not ``release``.  The playlist's own map order is kept.  Empty when the
    catalogue is absent or the mode has no retail playlist.
    """

    code = canonical_mode_code(mode)
    playlist_name = _RETAIL_MODE_PLAYLISTS.get(code)
    if playlist_name is None:
        return ()
    path = Path(maps_dir) / RETAIL_MAP_INFO_NAME
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        playlist = document["playlists"][playlist_name]
        rows = document["maps"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return ()
    if not isinstance(playlist, dict) or not isinstance(rows, dict):
        return ()
    modes = [str(value) for value in playlist.get("modes", ())]
    classic = bool(playlist.get("classic", False))
    mafia = bool(playlist.get("mafia", code in ("tc", "vip")))
    by_name = {str(name).casefold(): (str(name), entry)
               for name, entry in rows.items() if isinstance(entry, dict)}
    pool: list[str] = []
    for name in playlist.get("maps", ()):
        found = by_name.get(str(name).casefold())
        if found is None:
            continue
        real_name, entry = found
        invalid = {str(value) for value in entry.get("invalid_modes", ())}
        if any(value in invalid for value in modes):
            continue
        if bool(entry.get("classic", False)) != classic:
            continue
        if bool(entry.get("mafia", False)) != mafia:
            continue
        if not bool(entry.get("release", False)):
            continue
        if real_name not in pool:
            pool.append(real_name)
    return tuple(pool)


def canonical_mode_code(mode: object) -> str:
    """Return the retail short code for ``mode`` ("occupation" -> "oc").

    Admin commands and config accept human aliases; map sidecars and the
    objective filters below compare against the protocol short codes. An
    unknown name is kept verbatim (lowercase) instead of collapsing into
    "nor", so a custom registered mode keeps its own tag.
    """
    from server import mode_data

    value = str(mode or "").strip().lower()
    if not value:
        return value
    code = mode_data.get(value).code
    return value if code == "nor" and value != "nor" else code


def _mode_applies(entity_mode: object, active_mode: str) -> bool:
    value = str(entity_mode or "nor").strip().lower()
    # The editor writes "nor" for map-global drop points.  Explicit mode
    # zones remain restricted to that mode (aliases compare by short code).
    return (
        value in ("", "nor", "all", "any")
        or canonical_mode_code(value) == canonical_mode_code(active_mode)
    )


def normalize_skybox_name(value: object) -> str | None:
    """Return a safe retail skybox filename or ``None``.

    The client joins this value underneath its ``mesh`` resource tree.  Packet
    51 must therefore carry a plain asset filename: paths, drive names, NULs,
    and arbitrary extensions are rejected before reaching a retail client.
    """
    if not isinstance(value, str):
        return None
    name = value.strip()
    return name if _SKYBOX_NAME_PATTERN.fullmatch(name) else None


def normalize_rgb(value: object) -> tuple[int, int, int] | None:
    """Return a validated 8-bit RGB triplet from map-owned metadata."""
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value <= 0xFFFFFF:
            return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
        return None
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        rgb = tuple(int(component) for component in value[:3])
    except (TypeError, ValueError):
        return None
    return rgb if all(0 <= component <= 255 for component in rgb) else None


def normalize_ground_colors(
    value: object,
) -> list[tuple[int, int, int, int]]:
    """Return the bounded native RGBA terrain palette from untrusted data."""

    if not isinstance(value, (list, tuple)):
        return []
    colors: list[tuple[int, int, int, int]] = []
    for row in value[:32]:
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            continue
        try:
            color = tuple(int(component) for component in row)
        except (TypeError, ValueError):
            continue
        if all(0 <= component <= 255 for component in color):
            colors.append(color)  # type: ignore[arg-type]
    return colors


def normalize_vector3(value: object) -> tuple[float, float, float] | None:
    """Return a finite three-component vector from authored metadata."""

    point = _point3(value)
    if point is None:
        return None
    return point if all(abs(component) <= 32767.0 for component in point) else None


def normalize_ambient_name(value: object) -> str | None:
    """Return a client-bundled ambient resource basename or ``None``."""

    if not isinstance(value, str):
        return None
    name = value.strip()
    if not _AMBIENT_NAME_PATTERN.fullmatch(name):
        return None
    return name if name in AMBIENT_SOUND_ASSETS else None


def default_ambient_sound(map_name: object, skybox_name: object = None) -> str:
    """Choose the safe stock bed for metadata that omits ``ambient_sounds``."""

    map_key = str(map_name or "").casefold()
    skybox = normalize_skybox_name(skybox_name)
    return (
        _MAP_AMBIENT_OVERRIDES.get(map_key)
        or _SKYBOX_AMBIENTS.get(skybox or "")
        or "amb_rural"
    )


def _parse_ambient_sounds(payload: dict[str, object]) -> list[MapAmbientSound]:
    """Validate original ``[name, points, volume, attenuation]`` rows."""

    rows = payload.get("ambient_sounds")
    if not isinstance(rows, (list, tuple)):
        return []

    result: list[MapAmbientSound] = []
    for row in rows[:32]:
        if not isinstance(row, (list, tuple)) or not row:
            continue
        name = normalize_ambient_name(row[0])
        if name is None:
            logger.warning("Ignoring unknown/unsafe ambient resource %r", row[0])
            continue
        raw_points = row[1] if len(row) > 1 else ()
        points: list[tuple[int, int, int]] = []
        if isinstance(raw_points, (list, tuple)):
            for raw_point in raw_points[:255]:
                point = normalize_vector3(raw_point)
                if point is None:
                    continue
                points.append(tuple(int(round(component)) for component in point))
        try:
            volume = float(row[2]) if len(row) > 2 else 1.0
            attenuation = float(row[3]) if len(row) > 3 else (1.0 if points else 0.0)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= volume <= 4.0 and 0.0 <= attenuation <= 16.0):
            continue
        result.append(MapAmbientSound(name, tuple(points), volume, attenuation))
    return result


def _append_legacy_drop_points(result: MapMetadata, payload: dict[str, object]) -> None:
    """Translate stock ``*_crate_drop_points`` arrays into entity specs."""
    for field_name, (entity_type, kind, item) in _LEGACY_DROP_FIELDS.items():
        points = payload.get(field_name, [])
        if not isinstance(points, (list, tuple)):
            continue
        for position in points:
            if not isinstance(position, (list, tuple)) or len(position) < 3:
                continue
            try:
                x, y, z = (float(position[0]), float(position[1]), float(position[2]))
            except (TypeError, ValueError):
                continue
            result.entities.append(MapEntitySpec(entity_type, kind, x, y, z, item))


def _point3(value: object) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        return float(value[0]), float(value[1]), float(value[2])
    except (TypeError, ValueError):
        return None


def _centered_extents(
    dimensions: object,
) -> tuple[float, float, float, float, float, float] | None:
    size = _point3(dimensions)
    if size is None or any(component <= 0.0 for component in size):
        return None
    half_x, half_y, half_z = (component / 2.0 for component in size)
    return (-half_x, half_x, -half_y, half_y, -half_z, half_z)


def _spawn_area_zones(areas: object, team: int, item: str) -> list[MapZone]:
    """Translate retail ``[(centre, (w, h, d)), ...]`` spawn rows.

    A retail spawn box is a volume a player is dropped into, not a surface
    filter: SpookyMansion's Blue box is centred at z=222 (212..232) over a
    shore whose ground is z=233, and MayanJungle's first Blue box overhangs
    terrain up to 12 voxels lower.  A player placed in the box falls onto
    that ground, so the zone's floor extends down to the bottom of the map
    (VXL z grows downward).  The top stays authored, which keeps roofs and
    raised structures above the box out of the zone.
    """

    zones: list[MapZone] = []
    if not isinstance(areas, (list, tuple)):
        return zones
    floor = float(int(C.MAP_Z) - 1)
    for area in areas:
        if not isinstance(area, (list, tuple)) or len(area) < 2:
            continue
        center = _point3(area[0])
        extents = _centered_extents(area[1])
        if center is None or extents is None:
            continue
        x0, x1, y0, y1, z0, z1 = extents
        extents = (x0, x1, y0, y1, z0, max(z1, floor - center[2]))
        zones.append(MapZone("spawn", team, *center, extents, item))
    return zones


def _parallel_zones(
    payload: dict[str, object],
    points_key: str,
    sizes_key: str,
    team_for_index,
) -> list[MapZone]:
    """Translate parallel retail ``*_points``/``*_w_h_d`` arrays."""

    points = payload.get(points_key, ())
    sizes = payload.get(sizes_key, ())
    if not isinstance(points, (list, tuple)) or not isinstance(sizes, (list, tuple)):
        return []
    zones: list[MapZone] = []
    for index, (point, size) in enumerate(zip(points, sizes)):
        center = _point3(point)
        extents = _centered_extents(size)
        if center is None or extents is None:
            continue
        zones.append(MapZone(
            "base", int(team_for_index(index)), *center, extents,
            f"{points_key}[{index}]",
        ))
    return zones


def _append_legacy_team_zones(result: MapMetadata, payload: dict[str, object]) -> None:
    """Translate stock server spawn/base volumes into canonical map zones."""
    teams = (("team_one", TEAM1), ("team_two", TEAM2))
    for prefix, team in teams:
        result.spawn_zones[team].extend(_spawn_area_zones(
            payload.get(f"{prefix}_spawn_area", ()), team, f"{prefix}_spawn_area",
        ))
        points = payload.get(f"{prefix}_spawn", ())
        if isinstance(points, (list, tuple)):
            result.spawn_points[team].extend(
                point for point in (_point3(row) for row in points) if point is not None
            )

        center = _point3(payload.get(f"{prefix}_base_point"))
        extents = _centered_extents(payload.get(f"{prefix}_base_w_h_d"))
        if center is not None and extents is not None:
            result.base_zones[team].append(MapZone(
                "base", team, *center, extents, f"{prefix}_base_point",
            ))

        try:
            minimum = int(payload.get(f"{prefix}_min_destruction", 0))
        except (TypeError, ValueError):
            minimum = 0
        result.base_min_destruction[team] = max(0, minimum)


def _append_achievement_regions(
    result: MapMetadata,
    payload: dict[str, object],
) -> None:
    """Translate the stock parallel ``ac_*`` arrays.

    ``ac_ids``/``ac_types``/``ac_centres``/``ac_w_h_d`` describe each volume;
    ``ac_teams`` (0 = either, 1 = Blue, 2 = Green), ``ac_kills`` and
    ``ac_weapons`` are optional and may be shorter. Malformed rows are
    ignored independently.
    """

    ids = payload.get("ac_ids", ())
    kinds = payload.get("ac_types", ())
    centres = payload.get("ac_centres", ())
    sizes = payload.get("ac_w_h_d", ())
    if not all(isinstance(rows, (list, tuple)) for rows in (ids, kinds, centres, sizes)):
        return

    def optional(key: str, index: int) -> int:
        rows = payload.get(key, ())
        try:
            return int(rows[index])
        except (IndexError, KeyError, TypeError, ValueError):
            return 0

    for index, (api_name, kind, centre, size) in enumerate(
        zip(ids, kinds, centres, sizes)
    ):
        center = _point3(centre)
        extents = _centered_extents(size)
        if not isinstance(api_name, str) or center is None or extents is None:
            continue
        try:
            kind = int(kind)
        except (TypeError, ValueError):
            continue
        x0, x1, y0, y1, z0, z1 = extents
        result.achievement_regions.append(AchievementRegion(
            api_name=api_name,
            kind=kind,
            bounds=(
                center[0] + x0, center[0] + x1,
                center[1] + y0, center[1] + y1,
                center[2] + z0, center[2] + z1,
            ),
            team={1: TEAM1, 2: TEAM2}.get(optional("ac_teams", index)),
            kills=max(0, optional("ac_kills", index)),
            weapon=optional("ac_weapons", index),
        ))


def _append_legacy_neutral_zones(
    result: MapMetadata,
    payload: dict[str, object],
) -> None:
    """Translate the stock ``mh_base_points``/``mh_base_w_h_d`` arrays.

    The original assignment-format sidecars keep the two arrays parallel.
    Malformed rows are ignored independently and the protocol-facing mode
    applies the retail 2..10 count bound after fallbacks are considered.
    """

    points = payload.get("mh_base_points", ())
    dimensions = payload.get("mh_base_w_h_d", ())
    if not isinstance(points, (list, tuple)) or not isinstance(
        dimensions, (list, tuple)
    ):
        return
    for index, (point, size) in enumerate(zip(points, dimensions)):
        center = _point3(point)
        extents = _centered_extents(size)
        if center is None or extents is None:
            continue
        result.neutral_base_zones.append(MapZone(
            "base",
            int(C.TEAM_NEUTRAL),
            *center,
            extents,
            f"mh_base_points[{index}]",
        ))


def _apply_mode_layout(
    result: MapMetadata,
    payload: dict[str, object],
    active_mode: str,
) -> None:
    """Apply the retail per-mode layout overrides for ``active_mode``.

    Retail map descriptions carry a default layout (``team_*_spawn_area``,
    ``team_*_base_point``, ``mh_base_points``) plus optional mode-specific
    replacements. Recovered from the shipped ``.txtc`` modules:

    * ``ctf_base_points``/``ctf_base_w_h_d`` -- CTF capture bases, index 0 is
      Blue/TEAM1 and index 1 Green/TEAM2 (Trenches: west/east, matching its
      team spawn areas).
    * ``tc_base_points``/``tc_base_w_h_d`` -- Territory Control territories,
      which differ from the Multi-Hill hills on the same map.
    * ``oc_team_one_spawn_area``/``oc_team_two_spawn_area`` -- Occupation
      attacker/defender spawns (SpookyMansion's defenders spawn inside the
      occupation base, attackers on the four shores).
    * ``zombie_spawn_area``/``survivor_spawn_area`` -- Zombie mode spawns for
      the zombie (TEAM1) and survivor (TEAM2) roles.
    """

    # Classic CTF ("cctf") is retail CTF on the classic playlist (Trenches,
    # WW1, ...), so it reads the same capture-base keys.
    if active_mode in ("ctf", "cctf"):
        bases = _parallel_zones(
            payload, "ctf_base_points", "ctf_base_w_h_d",
            lambda index: TEAM1 if index == 0 else TEAM2,
        )
        if len(bases) >= 2:
            result.base_zones = {TEAM1: [bases[0]], TEAM2: [bases[1]]}
            result.layout_sources["base"] = "ctf_base_points"
    elif active_mode == "tc":
        territories = _parallel_zones(
            payload, "tc_base_points", "tc_base_w_h_d",
            lambda _index: int(C.TEAM_NEUTRAL),
        )
        if territories:
            result.neutral_base_zones = territories
            result.layout_sources["neutral"] = "tc_base_points"
    elif active_mode == "oc":
        for key, team in (
            ("oc_team_one_spawn_area", TEAM1),
            ("oc_team_two_spawn_area", TEAM2),
        ):
            zones = _spawn_area_zones(payload.get(key, ()), team, key)
            if zones:
                result.spawn_zones[team] = zones
                result.layout_sources[f"spawn{team}"] = key
    elif active_mode == "zom":
        survivors = _spawn_area_zones(
            payload.get("survivor_spawn_area", ()), TEAM2, "survivor_spawn_area"
        )
        if survivors:
            result.spawn_zones[TEAM2] = survivors
            result.layout_sources[f"spawn{TEAM2}"] = "survivor_spawn_area"
        zombies = _spawn_area_zones(
            payload.get("zombie_spawn_area", ()), TEAM1, "zombie_spawn_area"
        )
        if zombies:
            # Retail zombies rise out of the sea: SpookyMansion's eight
            # zombie areas cover only the z=239 water ring around the island,
            # which the world manager deliberately never spawns on.  Keep the
            # retail areas first (any dry column in them is used) and the
            # map's default Blue spawn after them so zombies still start on
            # the retail team-one shore instead of a generic terrain guess.
            result.spawn_zones[TEAM1] = zombies + result.spawn_zones[TEAM1]
            result.layout_sources[f"spawn{TEAM1}"] = (
                "zombie_spawn_area+team_one_spawn_area"
                if len(result.spawn_zones[TEAM1]) > len(zombies)
                else "zombie_spawn_area"
            )


def _append_legacy_occupation(result: MapMetadata, payload: dict[str, object]) -> None:
    """Translate the retail Occupation base and bomb spawn placements."""

    center = _point3(payload.get("occupation_base_point"))
    extents = _centered_extents(payload.get("occupation_base_w_h_d"))
    if center is not None and extents is not None:
        result.occupation_base_zone = MapZone(
            "base",
            TEAM2,
            *center,
            extents,
            "occupation_base_point",
        )

    rows = payload.get("occupation_bomb_points", ())
    if isinstance(rows, (list, tuple)):
        result.occupation_bomb_points.extend(
            point for point in (_point3(row) for row in rows) if point is not None
        )


def _append_legacy_diamond_zones(
    result: MapMetadata,
    payload: dict[str, object],
) -> None:
    """Translate Diamond Mine's parallel point/size/team/capacity arrays."""

    points = payload.get("diamond_base_points", ())
    dimensions = payload.get("diamond_base_w_h_d", ())
    teams = payload.get("diamond_base_teams", ())
    capacities = payload.get("diamond_base_capacity", ())
    if not isinstance(points, (list, tuple)) or not isinstance(
        dimensions, (list, tuple)
    ):
        return

    for index, (point, size) in enumerate(zip(points, dimensions)):
        center = _point3(point)
        extents = _centered_extents(size)
        if center is None or extents is None:
            continue
        try:
            team = int(teams[index])
        except (IndexError, TypeError, ValueError):
            team = int(C.TEAM_NEUTRAL)
        if team not in (TEAM1, TEAM2, int(C.TEAM_NEUTRAL)):
            team = int(C.TEAM_NEUTRAL)
        try:
            capacity = max(1, int(capacities[index]))
        except (IndexError, TypeError, ValueError):
            capacity = 1
        result.diamond_base_zones.append(MapZone(
            "base",
            team,
            *center,
            extents,
            f"diamond_base_points[{index}]",
        ))
        result.diamond_base_capacities.append(capacity)


def _safe_metadata_literal(node: ast.AST, depth: int = 0) -> object:
    """Evaluate inert metadata literals plus bounded numeric arithmetic.

    Stock UGC exports sometimes write coordinates as ``236-3``. Python's
    ``ast.literal_eval`` correctly rejects that BinOp, but rejecting the whole
    ambience row loses an otherwise valid river emitter. This evaluator
    accepts only container literals and arithmetic on finite numbers; names,
    calls, attributes, comprehensions, and operators with side effects remain
    impossible.
    """

    if depth > 16:
        raise ValueError("metadata literal is too deeply nested")
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (str, int, float, bool, type(None))):
            return node.value
        raise ValueError("unsupported metadata constant")
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        if len(node.elts) > 4096:
            raise ValueError("metadata container is too large")
        values = [_safe_metadata_literal(value, depth + 1) for value in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(values)
        if isinstance(node, ast.Set):
            return set(values)
        return values
    if isinstance(node, ast.Dict):
        if len(node.keys) > 4096:
            raise ValueError("metadata mapping is too large")
        return {
            _safe_metadata_literal(key, depth + 1): _safe_metadata_literal(value, depth + 1)
            for key, value in zip(node.keys, node.values)
            if key is not None
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _safe_metadata_literal(node.operand, depth + 1)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("unary operator requires a number")
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
        left = _safe_metadata_literal(node.left, depth + 1)
        right = _safe_metadata_literal(node.right, depth + 1)
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               for value in (left, right)):
            raise ValueError("binary operator requires numbers")
        return left + right if isinstance(node.op, ast.Add) else left - right
    raise ValueError("metadata expression is not inert")


def _parse_legacy_environment(text: str) -> dict[str, object] | None:
    """Read original ``name = value`` map metadata without executing code."""
    try:
        tree = ast.parse(text, mode="exec")
    except SyntaxError:
        return None

    payload: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in _LEGACY_ENVIRONMENT_KEYS:
            continue
        try:
            payload[target.id] = _safe_metadata_literal(node.value)
        except (ValueError, TypeError, SyntaxError):
            continue
    return payload or None


def _read_sidecar(sidecar: Path) -> dict[str, object] | None:
    """Decode JSON/UGC metadata or the original server's assignment format."""
    try:
        text = sidecar.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        logger.warning("Ignoring unreadable map metadata %s: %s", sidecar, exc)
        return None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = _parse_legacy_environment(text)
    if not isinstance(payload, dict):
        logger.warning("Ignoring invalid map metadata %s", sidecar)
        return None
    return payload


def load_map_metadata(map_path: str | Path, active_mode: str) -> MapMetadata:
    """Load map-owned environment and gameplay metadata beside a VXL."""
    map_path = Path(map_path)
    active_mode = canonical_mode_code(active_mode)
    map_key = map_path.stem.casefold()
    official_map = map_key in STOCK_MAP_SKYBOXES
    # A retail Map Creator project is intentionally split across siblings:
    # ``.txt`` owns atmosphere/lighting while ``.ugc`` owns placements and
    # publishing metadata.  Stopping at the first file makes every authored
    # spawn/base/crate disappear when an editor project is later hosted as a
    # normal game.  Layer accepted sidecars in the historical priority order;
    # the first file containing a key wins, while a later sibling fills fields
    # that are absent.  This also preserves the old single-sidecar behavior.
    sidecar = None
    payload: dict[str, object] = {}
    contributing_sidecars: list[Path] = []
    key_sources: dict[str, Path] = {}
    for candidate in _candidate_sidecars(map_path):
        if not candidate.is_file():
            continue
        candidate_payload = _read_sidecar(candidate)
        if candidate_payload is None:
            continue
        if sidecar is None:
            sidecar = candidate
        contributing_sidecars.append(candidate)
        for key, value in candidate_payload.items():
            if key not in payload:
                payload[key] = value
                key_sources[key] = candidate

    raw_skybox = next(
        (
            payload[key]
            # UGC's explicit editor value supersedes its baseplate's immutable
            # ``skybox_texture`` assignment when both siblings are present.
            for key in ("skybox_name", "skybox_texture", "skybox")
            if key in payload
        ),
        None,
    )
    skybox_name = normalize_skybox_name(raw_skybox)
    if raw_skybox is not None and skybox_name is None:
        logger.warning("Ignoring unsafe skybox name %r in %s", raw_skybox, sidecar)

    inferred_skybox = f"{map_path.stem}.txt"
    if skybox_name is None:
        skybox_name = STOCK_MAP_SKYBOXES.get(map_key)
    if skybox_name is None and inferred_skybox in C.FOG_COLORS:
        skybox_name = inferred_skybox

    fog_color = normalize_rgb(payload.get("fog_color"))
    if fog_color is None and skybox_name is not None:
        fog_color = normalize_rgb(C.FOG_COLORS.get(skybox_name))
    elif skybox_name is not None and _is_ugc_override(
        key_sources.get("skybox_name"), key_sources.get("fog_color")
    ):
        # Retail GameScene.set_skybox_name (0x1012d4c0) sends FogColor(74)
        # with FOG_COLORS[name] whenever the editor picks a skydome, so an
        # authored project's skydome choice owns the fog, not the immutable
        # baseplate .txt ``fog_color`` pin beneath it.
        fog_color = normalize_rgb(C.FOG_COLORS.get(skybox_name)) or fog_color

    # A VXL chroma marker identifies a palette slot, not the light's RGB.
    # GrasslandBaseplate's editor palette is not evidence for other stock
    # maps: guessing it creates pale glowing blocks and extra point lights.
    # Keep missing families absent until that map supplies their colors.
    static_light_colors: dict[int, tuple[int, int, int]] = {}
    for index in (0, 1):
        color = normalize_rgb(payload.get(f"static_light_color{index}"))
        if color is not None:
            static_light_colors[index] = color

    ambient_sounds = _parse_ambient_sounds(payload)
    if not ambient_sounds:
        ambient_name = default_ambient_sound(map_path.stem, skybox_name)
        ambient_sounds = [MapAmbientSound(ambient_name)]

    try:
        ambient_intensity = float(payload["ambient_light_intensity"])
    except (KeyError, TypeError, ValueError):
        ambient_intensity = None
    if ambient_intensity is not None and not 0.0 <= ambient_intensity <= 4.0:
        ambient_intensity = None

    result = MapMetadata(
        source=sidecar,
        official_map=official_map,
        gravity=canonical_gravity(
            payload.get("gravity", _STOCK_MAP_GRAVITY.get(map_key, 1.0))
        ),
        fallback_spawn_regions=dict(
            _STOCK_FALLBACK_SPAWN_REGIONS.get(
                map_key,
                {
                    TEAM1: (64, 128, 192, 384),
                    TEAM2: (320, 128, 448, 384),
                },
            )
        ),
        skybox_name=skybox_name,
        fog_color=fog_color,
        light_color=normalize_rgb(payload.get("light_color")),
        light_direction=normalize_vector3(payload.get("light_direction")),
        back_light_color=normalize_rgb(payload.get("back_light_color")),
        back_light_direction=normalize_vector3(payload.get("back_light_direction")),
        ambient_light_color=normalize_rgb(payload.get("ambient_light_color")),
        ambient_light_intensity=ambient_intensity,
        ambient_sounds=ambient_sounds,
        ground_colors=normalize_ground_colors(payload.get("ground_colors")),
        static_light_colors=static_light_colors,
    )
    _append_legacy_drop_points(result, payload)
    _append_legacy_team_zones(result, payload)
    _append_legacy_neutral_zones(result, payload)
    _append_legacy_occupation(result, payload)
    _append_legacy_diamond_zones(result, payload)
    _append_achievement_regions(result, payload)
    for team, prefix in ((TEAM1, "team_one"), (TEAM2, "team_two")):
        if result.spawn_zones[team]:
            result.layout_sources[f"spawn{team}"] = f"{prefix}_spawn_area"
        if result.base_zones[team]:
            result.layout_sources[f"base{team}"] = f"{prefix}_base_point"
    if result.neutral_base_zones:
        result.layout_sources["neutral"] = "mh_base_points"
    if result.occupation_base_zone is not None:
        result.layout_sources["occupation"] = "occupation_base_point"
    if result.diamond_base_zones:
        result.layout_sources["diamond"] = "diamond_base_points"
    _apply_mode_layout(result, payload, active_mode)
    if result.layout_sources.get("base") == "ctf_base_points":
        result.layout_sources.pop("base")
        for team in (TEAM1, TEAM2):
            result.layout_sources[f"base{team}"] = "ctf_base_points"
    raw_name = payload.get("name")
    result.display_name = raw_name.strip()[:64] if isinstance(raw_name, str) else None
    try:
        result.cap_limit = max(1, int(payload["cap_limit"]))
    except (KeyError, TypeError, ValueError):
        result.cap_limit = None
    try:
        time_limit = float(payload["time_limit"])
        result.time_limit = time_limit if math.isfinite(time_limit) and time_limit > 0 else None
    except (KeyError, TypeError, ValueError):
        result.time_limit = None
    catalogue_entry = retail_map_catalogue(map_path.parent).get(map_key)
    if catalogue_entry is not None:
        result.retail_modes = tuple(catalogue_entry.get("valid_modes", ()))
        result.retail_playlist_modes = tuple(catalogue_entry.get("playlist_modes", ()))
        try:
            result.retail_max_players = int(catalogue_entry["max_players"])
        except (KeyError, TypeError, ValueError):
            result.retail_max_players = None
        # Retail PlayList skips a pair whose mode is in the map's
        # invalid_modes even when the raw playlist .txt names the map
        # (GreatWall in demolition.txt, BranCastle in occupation.txt).
        retail_code = "ctf" if active_mode == "cctf" else active_mode
        if (
            retail_code in _RETAIL_MODE_CODES
            and retail_code in tuple(catalogue_entry.get("invalid_modes", ()))
        ):
            logger.warning(
                "Retail catalogue lists %s as invalid for mode %s (valid: %s)",
                map_path.stem, active_mode, ",".join(result.retail_modes) or "none",
            )
    rows = payload.get("ugc_entities", [])
    if not isinstance(rows, list):
        logger.warning("Ignoring malformed ugc_entities in %s", sidecar)
        return result

    for row in rows:
        if not isinstance(row, dict) or not _mode_applies(row.get("mode"), active_mode):
            continue
        item = str(row.get("item", "")).lower()
        position = row.get("position")
        if not isinstance(position, (list, tuple)) or len(position) < 3:
            continue
        try:
            x, y, z = (float(position[0]), float(position[1]), float(position[2]))
        except (TypeError, ValueError):
            continue

        drop = _DROP_TYPES.get(item)
        if drop is not None:
            result.entities.append(MapEntitySpec(drop[0], drop[1], x, y, z, item))
            continue

        if item == "ugc_bomb_drop":
            result.occupation_bomb_points.append((x, y, z))
            continue

        if item in _STATIC_FLARE_ITEMS:
            color = normalize_rgb(row.get("color"))
            if color is not None:
                result.entities.append(MapEntitySpec(
                    int(C.FLARE_BLOCK), "static_flare", x, y, z, item, color
                ))
            continue

        item_id = _ITEM_IDS.get(item)
        if item_id is None or item_id not in C.UGC_ZONE_SIZES:
            continue
        team = int(C.UGC_ENTITY_TEAMS.get(item_id, C.TEAM_NEUTRAL))
        kind = "spawn" if "_spawn" in item else "base" if "_base" in item else ""
        if not kind:
            continue
        zone = MapZone(
            kind=kind,
            team=team,
            x=x,
            y=y,
            z=z,
            extents=tuple(int(v) for v in C.UGC_ZONE_SIZES[item_id]),
            item=item,
        )
        if team in (TEAM1, TEAM2):
            target = result.spawn_zones if kind == "spawn" else result.base_zones
            target[team].append(zone)
        elif team == int(C.TEAM_NEUTRAL) and kind == "base":
            result.neutral_base_zones.append(zone)
            if active_mode.lower() == "dia":
                result.diamond_base_zones.append(zone)
                result.diamond_base_capacities.append(1)

        if (
            active_mode.lower() == "oc"
            and kind == "base"
            and team == TEAM2
            and result.occupation_base_zone is None
        ):
            result.occupation_base_zone = zone

    logger.info(
        "Loaded map metadata %s (sources %d, official %s, skybox %s, fog %s, ambience %s, static lights %d, "
        "spawn zones %d/%d, bases %d/%d, neutral bases %d, occupation %s/%d, "
        "diamond bases %d, entities %d, layout %s)",
        sidecar or "<stock inference>",
        len(contributing_sidecars),
        result.official_map,
        result.skybox_name or "default",
        result.fog_color or "default",
        ",".join(sound.name for sound in result.ambient_sounds) or "none",
        len(result.static_light_colors),
        len(result.spawn_zones[TEAM1]),
        len(result.spawn_zones[TEAM2]),
        len(result.base_zones[TEAM1]),
        len(result.base_zones[TEAM2]),
        len(result.neutral_base_zones),
        "base" if result.occupation_base_zone is not None else "fallback",
        len(result.occupation_bomb_points),
        len(result.diamond_base_zones),
        len(result.entities),
        ",".join(
            f"{family}={key}" for family, key in sorted(result.layout_sources.items())
        ) or "terrain fallback",
    )
    return result
