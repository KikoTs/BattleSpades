"""Print the stock map x retail mode layout-coverage table.

For every stock map and every mode the retail catalogue allows on it, report
where the server's spawn and objective layout comes from:

* ``retail``   -- recovered retail metadata key (``maps/<Map>.json``);
* ``evidence`` -- fallback team region oriented by team-coloured geometry in
  the retail VXL (``_STOCK_FALLBACK_SPAWN_REGIONS``);
* ``fallback`` -- generic west/east terrain inference in the world manager or
  the mode.

The output is the table embedded in ``docs/MAP_METADATA.md``.

Usage::

    py -3.12 tools/map_metadata/coverage.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.game_constants import TEAM1, TEAM2  # noqa: E402
from server.map_metadata import (  # noqa: E402
    STOCK_MAP_SKYBOXES,
    _STOCK_FALLBACK_SPAWN_REGIONS,
    load_map_metadata,
    retail_map_catalogue,
)

_OBJECTIVE_FAMILY = {
    "ctf": "base",
    "dem": "base",
    "dia": "diamond",
    "mh": "neutral",
    "tc": "neutral",
    "oc": "occupation",
}


def _spawn_source(metadata, map_key: str) -> str:
    keys = [metadata.layout_sources.get(f"spawn{team}") for team in (TEAM1, TEAM2)]
    if all(keys):
        unique = sorted(set(keys))
        return "retail " + "/".join(unique)
    if map_key in _STOCK_FALLBACK_SPAWN_REGIONS:
        return "evidence (team colours)"
    return "fallback (west/east)"


def _objective_source(metadata, mode: str) -> str:
    family = _OBJECTIVE_FAMILY.get(mode)
    if family is None:
        return "-"
    if family == "base":
        keys = {metadata.layout_sources.get(f"base{team}") for team in (TEAM1, TEAM2)}
        keys.discard(None)
        if keys and all(metadata.base_zones[t] for t in (TEAM1, TEAM2)):
            return "retail " + "/".join(sorted(keys))
        return "fallback (team anchor)"
    key = metadata.layout_sources.get(family)
    if key is None:
        return "fallback (mode inference)"
    if mode == "oc":
        bombs = len(metadata.occupation_bomb_points)
        return f"retail {key} + {bombs} bomb points"
    return f"retail {key}"


def rows(maps_dir: Path = ROOT / "maps") -> list[tuple[str, str, str, str]]:
    logging.disable(logging.WARNING)
    catalogue = retail_map_catalogue(maps_dir)
    result = []
    for path in sorted(maps_dir.glob("*.vxl"), key=lambda p: p.stem.casefold()):
        key = path.stem.casefold()
        if key not in STOCK_MAP_SKYBOXES:
            continue
        entry = catalogue.get(key)
        modes = entry["valid_modes"] if entry else ["tdm"]
        for mode in modes:
            metadata = load_map_metadata(path, mode)
            result.append((
                path.stem,
                mode if entry else f"{mode} (not a retail map)",
                _spawn_source(metadata, key),
                _objective_source(metadata, mode),
            ))
    return result


def main() -> int:
    print("| Map | Mode | Spawns | Objectives |")
    print("|---|---|---|---|")
    for name, mode, spawns, objectives in rows():
        print(f"| {name} | {mode} | {spawns} | {objectives} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
