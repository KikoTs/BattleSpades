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


@dataclass
class MapMetadata:
    source: Path | None = None
    # Stock maps still need a full VXL stream.  This flag only selects bundled
    # client presentation assets (sky mesh and ambience); it is never a map-
    # synchronization shortcut.
    official_map: bool = False
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
    # Index 0 is the green chroma family; index 1 is the blue family.
    static_light_colors: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    spawn_zones: dict[int, list[MapZone]] = field(
        default_factory=lambda: {TEAM1: [], TEAM2: []}
    )
    base_zones: dict[int, list[MapZone]] = field(
        default_factory=lambda: {TEAM1: [], TEAM2: []}
    )
    entities: list[MapEntitySpec] = field(default_factory=list)


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
STOCK_MAP_SKYBOXES = {
    "20thcenturytown": "WW1.txt",
    "alcatraz": "Alcatraz.txt",
    "ancientegypt": "Egypt.txt",
    "arcticbase": "ArcticBase.txt",
    "atlantis": "Atlantis.txt",
    "blockness": "User_Grassland.txt",
    "brancastle": "BranCastle.txt",
    "castlewars": "Classic.txt",
    "cityofchicago": "Chicago.txt",
    "classic": "Classic.txt",
    "crossroads": "User_Grassland.txt",
    "doubledragon": "GreatWall.txt",
    "dragonisland": "SecretBase.txt",
    "frontier": "Frontier.txt",
    "greatwall": "GreatWall.txt",
    "hiesville": "User_Grassland.txt",
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
    "skybox_texture", "skybox_name", "skybox", "fog_color",
    "light_color", "light_direction", "back_light_color",
    "back_light_direction", "ambient_light_color",
    "ambient_light_intensity", "ambient_sounds",
    "static_light_color0", "static_light_color1",
    "ammo_crate_drop_points", "health_crate_drop_points",
    "block_crate_drop_points",
    "team_one_spawn_area", "team_two_spawn_area",
    "team_one_base_point", "team_one_base_w_h_d",
    "team_two_base_point", "team_two_base_w_h_d",
))


def _candidate_sidecars(map_path: Path) -> Iterable[Path]:
    # UGC downloads use both .txt and .ugc.  Accept .json for hand-authored
    # server maps and the map.vxl.json convention as well.
    yield map_path.with_suffix(".json")
    yield map_path.with_suffix(".txt")
    yield map_path.with_suffix(".ugc")
    yield Path(str(map_path) + ".json")


def _mode_applies(entity_mode: object, active_mode: str) -> bool:
    value = str(entity_mode or "nor").lower()
    # The editor writes "nor" for map-global drop points.  Explicit mode
    # zones remain restricted to that mode.
    return value in ("", "nor", "all", "any") or value == active_mode.lower()


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


def _append_legacy_team_zones(result: MapMetadata, payload: dict[str, object]) -> None:
    """Translate stock server spawn/base volumes into canonical map zones."""
    teams = (("team_one", TEAM1), ("team_two", TEAM2))
    for prefix, team in teams:
        areas = payload.get(f"{prefix}_spawn_area", ())
        if isinstance(areas, (list, tuple)):
            for area in areas:
                if not isinstance(area, (list, tuple)) or len(area) < 2:
                    continue
                center = _point3(area[0])
                extents = _centered_extents(area[1])
                if center is None or extents is None:
                    continue
                result.spawn_zones[team].append(MapZone(
                    "spawn", team, *center, extents, f"{prefix}_spawn_area",
                ))

        center = _point3(payload.get(f"{prefix}_base_point"))
        extents = _centered_extents(payload.get(f"{prefix}_base_w_h_d"))
        if center is not None and extents is not None:
            result.base_zones[team].append(MapZone(
                "base", team, *center, extents, f"{prefix}_base_point",
            ))


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
    map_key = map_path.stem.casefold()
    official_map = map_key in STOCK_MAP_SKYBOXES
    sidecar = next((p for p in _candidate_sidecars(map_path) if p.is_file()), None)
    payload = _read_sidecar(sidecar) if sidecar is not None else {}
    if payload is None:
        # An unreadable/malformed file did not contribute any metadata. Keep
        # ``source`` truthful so diagnostics do not claim it was accepted.
        sidecar = None
        payload = {}

    raw_skybox = next(
        (
            payload[key]
            for key in ("skybox_texture", "skybox_name", "skybox")
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
        skybox_name=skybox_name,
        fog_color=fog_color,
        light_color=normalize_rgb(payload.get("light_color")),
        light_direction=normalize_vector3(payload.get("light_direction")),
        back_light_color=normalize_rgb(payload.get("back_light_color")),
        back_light_direction=normalize_vector3(payload.get("back_light_direction")),
        ambient_light_color=normalize_rgb(payload.get("ambient_light_color")),
        ambient_light_intensity=ambient_intensity,
        ambient_sounds=ambient_sounds,
        static_light_colors=static_light_colors,
    )
    _append_legacy_drop_points(result, payload)
    _append_legacy_team_zones(result, payload)
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
        if team not in (TEAM1, TEAM2):
            continue
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
        target = result.spawn_zones if kind == "spawn" else result.base_zones
        target[team].append(zone)

    logger.info(
        "Loaded map metadata %s (official %s, skybox %s, fog %s, ambience %s, static lights %d, "
        "spawn zones %d/%d, bases %d/%d, entities %d)",
        sidecar or "<stock inference>",
        result.official_map,
        result.skybox_name or "default",
        result.fog_color or "default",
        ",".join(sound.name for sound in result.ambient_sounds) or "none",
        len(result.static_light_colors),
        len(result.spawn_zones[TEAM1]),
        len(result.spawn_zones[TEAM2]),
        len(result.base_zones[TEAM1]),
        len(result.base_zones[TEAM2]),
        len(result.entities),
    )
    return result
