"""Persistent project and retail-asset model for the isolated map creator.

The stock editor stores a map as three siblings: ``<name>.vxl`` contains the
voxel world, ``<name>.txt`` contains the normal map atmosphere metadata, and
``<name>.ugc`` is plain JSON describing editor objects and publishing data.
This module deliberately has no dependency on a running server, which keeps
project validation and launcher health checks deterministic and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ast
import json
import os
from pathlib import Path
import re
import shutil
from typing import Iterable, Mapping, Sequence

import shared.constants as C
import shared.constants_gamemode as MG
import shared.constants_prefabs as CP
from shared.constants_ugc_objectives import (
    UGC_OBJECTIVES,
    UGC_OBJECTIVES_TYPES,
)


WORLD_XY = 512
WORLD_Z = 256
MAX_UGC_ENTITIES = 8192
COMMON_MODE = "nor"
TARGET_MODES = ("tdm", "ctf", "dem", "mh", "oc", "tc", "vip", "zom", "dia")


@dataclass(frozen=True, slots=True)
class TerrainSpec:
    """One terrain button exposed by the recovered Map Creator playlist."""

    key: str
    stem: str
    prefab_tag: int


# The shipped playlist has nine choices.  Only the original six have explicit
# prefab tags in constants_prefabs; the three later terrains inherit the
# closest authored palette used by the retail assets.
TERRAINS: tuple[TerrainSpec, ...] = (
    TerrainSpec("desert", "DesertBaseplate", int(CP.DESERT_BASE_PLATE)),
    TerrainSpec("lunar", "LunarBaseplate", int(CP.LUNAR_BASE_PLATE)),
    TerrainSpec("mountain", "MountainBaseplate", int(CP.MOUNTAIN_BASE_PLATE)),
    TerrainSpec("grassland", "GrasslandBaseplate", int(CP.GRASSLAND_BASE_PLATE)),
    TerrainSpec("temple", "TempleBaseplate", int(CP.TEMPLE_BASE_PLATE)),
    TerrainSpec("urban", "UrbanBaseplate", int(CP.URBAN_BASE_PLATE)),
    TerrainSpec("marsh", "MarshBaseplate", int(CP.GRASSLAND_BASE_PLATE)),
    TerrainSpec("snowy", "SnowyBaseplate", int(CP.MOUNTAIN_BASE_PLATE)),
    TerrainSpec("water", "WaterBaseplate", int(CP.GRASSLAND_BASE_PLATE)),
)

_TERRAIN_BY_KEY = {terrain.key: terrain for terrain in TERRAINS}
_TERRAIN_BY_STEM = {terrain.stem.casefold(): terrain for terrain in TERRAINS}
_ITEM_TO_NAME = {int(item): str(name) for item, name in C.UGC_TOOL_IMAGES.items()}
_NAME_TO_ITEM = {name.casefold(): item for item, name in _ITEM_TO_NAME.items()}
_COMMON_ITEMS = {
    int(C.UGC_ITEM_HEALTH_DROP_POINT),
    int(C.UGC_ITEM_AMMO_DROP_POINT),
    int(C.UGC_ITEM_BLOCK_DROP_POINT),
}


def terrain_spec(value: str) -> TerrainSpec:
    """Resolve a terrain key or retail baseplate filename stem."""

    normalized = Path(str(value).strip()).stem.casefold()
    terrain = _TERRAIN_BY_KEY.get(normalized) or _TERRAIN_BY_STEM.get(normalized)
    if terrain is None:
        choices = ", ".join(item.key for item in TERRAINS)
        raise ValueError(f"unknown UGC terrain {value!r}; choose one of: {choices}")
    return terrain


def normalize_target_mode(value: str) -> str:
    """Return one publishable retail UGC target-mode code."""

    normalized = str(value).strip().lower()
    normalized = {"zombie": "zom", "territory": "tc"}.get(normalized, normalized)
    if normalized not in TARGET_MODES:
        raise ValueError(
            f"unsupported UGC target mode {value!r}; choose one of: "
            + ", ".join(TARGET_MODES)
        )
    return normalized


def mode_id(code: str) -> int:
    """Translate a sidecar mode code into the native one-byte mode id."""

    normalized = COMMON_MODE if str(code).lower() == COMMON_MODE else normalize_target_mode(code)
    return int(MG.MODE_MODE_IDS[normalized])


def mode_code(value: int) -> str:
    """Translate a native mode id, rejecting editor-incompatible values."""

    code = str(MG.MODE_IDS_MODE.get(int(value), ""))
    if code == COMMON_MODE:
        return code
    return normalize_target_mode(code)


def item_name(item_id: int) -> str:
    """Return the exact string stored in retail ``ugc_entities`` records."""

    try:
        return _ITEM_TO_NAME[int(item_id)]
    except KeyError as exc:
        raise ValueError(f"unknown UGC item id {item_id!r}") from exc


def item_id(value: str | int) -> int:
    """Resolve an item id or its retail sidecar string."""

    if isinstance(value, int):
        candidate = int(value)
    else:
        token = str(value).strip()
        try:
            candidate = int(token)
        except ValueError:
            candidate = _NAME_TO_ITEM.get(token.casefold(), -1)
    if candidate not in _ITEM_TO_NAME:
        raise ValueError(f"unknown UGC item {value!r}")
    return candidate


def authored_mode_for_item(item: int, target_mode: str) -> str:
    """Apply the retail rule that shared crate points belong to ``nor``."""

    return COMMON_MODE if int(item) in _COMMON_ITEMS else normalize_target_mode(target_mode)


@dataclass(frozen=True, slots=True)
class UGCPlacement:
    """One immutable editor object represented by packet 97/98 and `.ugc`."""

    x: int
    y: int
    z: int
    item_id: int
    mode: str = COMMON_MODE

    def __post_init__(self) -> None:
        if not 0 <= int(self.x) < WORLD_XY or not 0 <= int(self.y) < WORLD_XY:
            raise ValueError(f"UGC entity is outside the 512x512 world: {self.position}")
        if not 0 <= int(self.z) < WORLD_Z:
            raise ValueError(f"UGC entity z is outside 0..255: {self.z}")
        item_name(self.item_id)
        normalized_mode = (
            COMMON_MODE
            if str(self.mode).strip().lower() == COMMON_MODE
            else normalize_target_mode(self.mode)
        )
        object.__setattr__(self, "x", int(self.x))
        object.__setattr__(self, "y", int(self.y))
        object.__setattr__(self, "z", int(self.z))
        object.__setattr__(self, "item_id", int(self.item_id))
        object.__setattr__(self, "mode", normalized_mode)

    @property
    def position(self) -> tuple[int, int, int]:
        """Return the raw voxel coordinate used on the wire."""

        return self.x, self.y, self.z

    def to_sidecar(self) -> dict[str, object]:
        """Serialize one record using the recovered retail JSON field names."""

        return {
            "position": [self.x, self.y, self.z],
            "mode": self.mode,
            "item": item_name(self.item_id),
        }

    @classmethod
    def from_sidecar(cls, value: Mapping[str, object]) -> "UGCPlacement":
        """Parse and validate one untrusted sidecar record."""

        position = value.get("position")
        if not isinstance(position, Sequence) or isinstance(position, (str, bytes)):
            raise ValueError("UGC entity position must be a three-number array")
        if len(position) != 3:
            raise ValueError("UGC entity position must contain exactly three values")
        return cls(
            int(position[0]),
            int(position[1]),
            int(position[2]),
            item_id(value.get("item", -1)),
            str(value.get("mode", COMMON_MODE)),
        )


@dataclass(frozen=True, slots=True)
class UGCObjectiveStatus:
    """One native validation row and whether its recovered limits are met."""

    objective_id: str
    value: int
    minimum: int
    maximum: int
    priority: int

    @property
    def complete(self) -> bool:
        return self.minimum <= self.value <= self.maximum


@dataclass(frozen=True, slots=True)
class UGCValidation:
    """Complete validation result in the same priority order as the client."""

    mode: str
    objectives: tuple[UGCObjectiveStatus, ...]

    @property
    def complete(self) -> bool:
        return all(objective.complete for objective in self.objectives)


@dataclass(slots=True)
class UGCProject:
    """Mutable authoritative state for one hosted Map Creator session."""

    title: str
    description: str
    author: str
    baseplate: str
    target_mode: str = "tdm"
    placements: list[UGCPlacement] = field(default_factory=list)
    ground_colors: list[tuple[int, int, int, int]] = field(default_factory=list)
    skybox_name: str = ""
    use_overhead_image: bool = False
    aos_ugc_handle: int = int(C.UGC_INVALID_STEAM_PUBLISHED_FILE_HANDLE)
    modified_since_publish: bool = True
    tags: list[str] = field(default_factory=lambda: ["map", "tdm"])

    def __post_init__(self) -> None:
        self.target_mode = normalize_target_mode(self.target_mode)
        self.baseplate = terrain_spec(self.baseplate).stem
        self.title = _clean_text(self.title, "Untitled Map", 80)
        self.description = _clean_text(self.description, self.title, 512)
        self.author = _clean_text(self.author, "Unknown", 80)
        if len(self.placements) > MAX_UGC_ENTITIES:
            raise ValueError(f"UGC project exceeds {MAX_UGC_ENTITIES} entities")
        self.placements = list(dict.fromkeys(self.placements))
        self.ground_colors = [_rgba(value) for value in self.ground_colors[:32]]
        self.tags = _normalized_tags(self.tags, self.target_mode)

    @property
    def terrain(self) -> TerrainSpec:
        return terrain_spec(self.baseplate)

    def place(self, x: int, y: int, z: int, item: int, *, mode: str | None = None) -> bool:
        """Insert one object, returning false for an exact duplicate or full project."""

        if len(self.placements) >= MAX_UGC_ENTITIES:
            return False
        placement = UGCPlacement(
            x,
            y,
            z,
            item_id(item),
            mode or authored_mode_for_item(item, self.target_mode),
        )
        if placement in self.placements:
            return False
        self.placements.append(placement)
        self.modified_since_publish = True
        return True

    def remove(self, x: int, y: int, z: int, item: int | None = None) -> UGCPlacement | None:
        """Remove the newest matching object at a coordinate.

        Retail secondary-click replaces an object by sending remove then add.
        Prefer the supplied item id but fall back to the coordinate so a host
        whose palette changed between the two clicks can still erase safely.
        """

        position = (int(x), int(y), int(z))
        candidate_item = None if item is None else item_id(item)
        for index in range(len(self.placements) - 1, -1, -1):
            placement = self.placements[index]
            if placement.position == position and (
                candidate_item is None or placement.item_id == candidate_item
            ):
                self.modified_since_publish = True
                return self.placements.pop(index)
        if candidate_item is not None:
            return self.remove(*position, item=None)
        return None

    def set_target_mode(self, value: str) -> None:
        """Change validation context while preserving multi-mode authored objects."""

        self.target_mode = normalize_target_mode(value)
        self.tags = _normalized_tags(self.tags, self.target_mode)
        self.modified_since_publish = True

    def validation(self, target_mode: str | None = None) -> UGCValidation:
        """Count editor objects against the exact recovered menu requirements."""

        mode = normalize_target_mode(target_mode or self.target_mode)
        objective_names = list(UGC_OBJECTIVES.get("COMMON", ()))
        objective_names.extend(UGC_OBJECTIVES.get(mode, ()))
        rows: list[UGCObjectiveStatus] = []
        for objective_name in objective_names:
            definition = UGC_OBJECTIVES_TYPES[objective_name]
            entity_ids = {int(value) for value in definition["entity_ids"]}
            value = sum(
                1
                for placement in self.placements
                if placement.item_id in entity_ids
                and (placement.mode in (COMMON_MODE, mode))
            )
            rows.append(
                UGCObjectiveStatus(
                    objective_id=str(objective_name),
                    value=value,
                    minimum=int(definition["min"]),
                    maximum=int(definition["max"]),
                    priority=int(definition["priority"]),
                )
            )
        rows.sort(key=lambda row: row.priority)
        return UGCValidation(mode, tuple(rows))

    def to_sidecar(self) -> dict[str, object]:
        """Produce the field-compatible JSON object read by the retail menus."""

        return {
            "use_overhead_image": bool(self.use_overhead_image),
            "description": self.description,
            "title": self.title,
            "ugc_entities": [placement.to_sidecar() for placement in self.placements],
            "ground_colors": [list(color) for color in self.ground_colors],
            "skybox_name": self.skybox_name,
            "aos_ugc_handle": int(self.aos_ugc_handle),
            "author": self.author,
            "baseplate": self.baseplate,
            "modified_since_publish": bool(self.modified_since_publish),
            "tags": list(self.tags),
        }

    def save(self, path: str | Path) -> Path:
        """Atomically checkpoint the small sidecar; never expose partial JSON."""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        payload = json.dumps(self.to_sidecar(), indent=4, ensure_ascii=False) + "\n"
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, target)
        return target

    @classmethod
    def load(cls, path: str | Path, *, target_mode: str | None = None) -> "UGCProject":
        """Load an existing retail sidecar with strict entity validation."""

        source = Path(path)
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(data, Mapping):
            raise ValueError(f"UGC sidecar root must be an object: {source}")
        raw_entities = data.get("ugc_entities", ())
        if not isinstance(raw_entities, Sequence) or isinstance(raw_entities, (str, bytes)):
            raise ValueError("ugc_entities must be an array")
        tags = [str(tag) for tag in data.get("tags", ())]
        inferred_mode = target_mode or next(
            (tag.lower() for tag in tags if tag.lower() in TARGET_MODES),
            "tdm",
        )
        return cls(
            title=str(data.get("title", source.stem)),
            description=str(data.get("description", data.get("title", source.stem))),
            author=str(data.get("author", "Unknown")),
            baseplate=str(data.get("baseplate", "GrasslandBaseplate")),
            target_mode=inferred_mode,
            placements=[UGCPlacement.from_sidecar(value) for value in raw_entities],
            ground_colors=list(data.get("ground_colors", ())),
            skybox_name=str(data.get("skybox_name", "")),
            use_overhead_image=bool(data.get("use_overhead_image", False)),
            aos_ugc_handle=int(
                data.get("aos_ugc_handle", C.UGC_INVALID_STEAM_PUBLISHED_FILE_HANDLE)
            ),
            modified_since_publish=bool(data.get("modified_since_publish", True)),
            tags=tags,
        )


@dataclass(frozen=True, slots=True)
class UGCAssetLayout:
    """Validated roots for genuine baseplates and the 455 retail KV6 assets."""

    root: Path
    maps: Path
    prefabs: Path

    def terrain_files(self, terrain: TerrainSpec) -> tuple[Path, Path]:
        """Resolve the terrain VXL and metadata case-insensitively."""

        vxl = _casefold_child(self.maps, terrain.stem + ".vxl")
        metadata = _casefold_child(self.maps, terrain.stem + ".txt")
        if vxl is None or metadata is None:
            raise FileNotFoundError(f"retail terrain {terrain.stem} is incomplete in {self.maps}")
        return vxl, metadata

    def compatible_prefabs(self, terrain: TerrainSpec) -> tuple[str, ...]:
        """Return only palette entries whose KV6 model is actually installed."""

        available = {
            path.stem.casefold(): path.stem
            for path in self.prefabs.glob("*.kv6")
            if path.is_file()
        }
        names = []
        for name, tags in CP.PREFABS_NAMES_WITH_TAGS.items():
            if int(terrain.prefab_tag) not in {int(tag) for tag in tags}:
                continue
            installed = available.get(str(name).casefold())
            if installed is not None:
                names.append(installed)
        return tuple(sorted(dict.fromkeys(names), key=str.casefold))


def discover_ugc_assets(retail_root: str | Path | None = None) -> UGCAssetLayout:
    """Find genuine editor assets without copying or modifying the client."""

    candidates: list[Path] = []
    if retail_root:
        candidates.append(Path(retail_root).expanduser())
    environment_root = os.environ.get("AOS_RETAIL_ROOT")
    if environment_root:
        candidates.append(Path(environment_root).expanduser())
    repository = Path(__file__).resolve().parents[1]
    candidates.extend(
        (
            repository / "retail",
            repository.parent / "AceOfSpades_no_steam_new",
            repository.parent / "aceofspades_decompiled",
        )
    )
    checked: list[str] = []
    for candidate in dict.fromkeys(path.resolve() for path in candidates):
        maps = candidate / "ugc" / "maps"
        prefabs = candidate / "ugc" / "kv6"
        checked.append(str(candidate))
        if maps.is_dir() and prefabs.is_dir():
            layout = UGCAssetLayout(candidate, maps, prefabs)
            # Refuse a lookalike directory missing any menu terrain.
            for terrain in TERRAINS:
                layout.terrain_files(terrain)
            return layout
    raise FileNotFoundError(
        "could not locate retail UGC assets; checked: " + ", ".join(checked)
    )


def create_project_files(
    project: UGCProject,
    directory: str | Path,
    slug: str,
    assets: UGCAssetLayout,
    *,
    overwrite: bool = False,
) -> tuple[Path, Path, Path]:
    """Materialize a new VXL/TXT/UGC triplet from one retail baseplate."""

    safe_slug = project_slug(slug)
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    vxl_path = destination / f"{safe_slug}.vxl"
    metadata_path = destination / f"{safe_slug}.txt"
    sidecar_path = destination / f"{safe_slug}.ugc"
    # Retail ``delete_ugc_file`` removes the authored VXL/UGC/PNG files but
    # leaves our optional atmosphere TXT behind. A lone TXT is not a live
    # project, so allow same-name recreation and replace it from the selected
    # baseplate. VXL or UGC still fail closed to protect authored work.
    if not overwrite and any(path.exists() for path in (vxl_path, sidecar_path)):
        raise FileExistsError(f"UGC project already exists: {destination / safe_slug}")
    source_vxl, source_metadata = assets.terrain_files(project.terrain)
    shutil.copyfile(source_vxl, vxl_path)
    shutil.copyfile(source_metadata, metadata_path)
    project.save(sidecar_path)
    return vxl_path, metadata_path, sidecar_path


def read_baseplate_presentation(
    metadata_path: str | Path,
) -> tuple[str, list[tuple[int, int, int, int]]]:
    """Recover the editor skybox and palette from inert assignment metadata."""

    source = Path(metadata_path)
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), mode="exec")
    values: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in {
            "skybox_texture",
            "skybox_name",
            "ground_colors",
        }:
            continue
        try:
            values[target.id] = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            continue
    skybox = str(
        values.get("skybox_name", values.get("skybox_texture", "")) or ""
    )
    raw_colors = values.get("ground_colors", ())
    colors: list[tuple[int, int, int, int]] = []
    if isinstance(raw_colors, Sequence) and not isinstance(raw_colors, (str, bytes)):
        for raw_color in raw_colors[:32]:
            try:
                colors.append(_rgba(raw_color))
            except (TypeError, ValueError):
                continue
    return skybox, colors


def project_slug(value: str) -> str:
    """Create the portable basename shared by all project files."""

    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value).strip()).strip("_-")
    if not slug:
        raise ValueError("UGC project name must contain a letter or digit")
    return slug[:64]


def _clean_text(value: str, fallback: str, limit: int) -> str:
    cleaned = str(value).replace("\x00", "").strip() or fallback
    return cleaned[:limit]


def _rgba(value: Sequence[int]) -> tuple[int, int, int, int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        raise ValueError("ground colors must be four-byte RGBA values")
    return tuple(int(component) & 0xFF for component in value)  # type: ignore[return-value]


def _normalized_tags(values: Iterable[str], target_mode: str) -> list[str]:
    tags = [str(value).strip().lower() for value in values if str(value).strip()]
    tags = list(dict.fromkeys(tags))
    if "map" not in tags:
        tags.insert(0, "map")
    if target_mode not in tags:
        tags.append(target_mode)
    return tags


def _casefold_child(directory: Path, filename: str) -> Path | None:
    expected = filename.casefold()
    return next(
        (child for child in directory.iterdir() if child.is_file() and child.name.casefold() == expected),
        None,
    )


__all__ = [
    "COMMON_MODE",
    "MAX_UGC_ENTITIES",
    "TARGET_MODES",
    "TERRAINS",
    "TerrainSpec",
    "UGCAssetLayout",
    "UGCObjectiveStatus",
    "UGCPlacement",
    "UGCProject",
    "UGCValidation",
    "authored_mode_for_item",
    "create_project_files",
    "discover_ugc_assets",
    "item_id",
    "item_name",
    "mode_code",
    "mode_id",
    "normalize_target_mode",
    "project_slug",
    "read_baseplate_presentation",
    "terrain_spec",
]
