"""Authored map zones and entities stored beside a VXL map.

The VXL stream contains voxel columns only.  Battle Builders UGC maps store
spawn/base zones and drop points in a JSON sidecar (usually ``.txt`` or
``.ugc``) with an ``ugc_entities`` array.  Keeping this parser separate from
the voxel loader prevents coloured terrain from being mistaken for metadata.
"""

from __future__ import annotations

import json
import logging
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
    extents: tuple[int, int, int, int, int, int]
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


@dataclass
class MapMetadata:
    source: Path | None = None
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


def load_map_metadata(map_path: str | Path, active_mode: str) -> MapMetadata:
    """Load the first valid UGC JSON sidecar next to ``map_path``."""
    map_path = Path(map_path)
    sidecar = next((p for p in _candidate_sidecars(map_path) if p.is_file()), None)
    if sidecar is None:
        return MapMetadata()

    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring invalid map metadata %s: %s", sidecar, exc)
        return MapMetadata()

    result = MapMetadata(source=sidecar)
    rows = payload.get("ugc_entities", []) if isinstance(payload, dict) else []
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
        "Loaded map metadata %s (spawn zones %d/%d, bases %d/%d, entities %d)",
        sidecar,
        len(result.spawn_zones[TEAM1]),
        len(result.spawn_zones[TEAM2]),
        len(result.base_zones[TEAM1]),
        len(result.base_zones[TEAM2]),
        len(result.entities),
    )
    return result
