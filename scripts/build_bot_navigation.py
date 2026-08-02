"""Build or verify content-hashed semantic bot navigation caches.

Each ``.botnav`` file is a compressed fixed-width data cache derived solely
from the matching VXL. It contains no executable code. The worker validates
its source digest and safely rebuilds in memory when a custom map has no cache.

Examples::

    py -3 scripts/build_bot_navigation.py
    py -3 scripts/build_bot_navigation.py --map CastleWars --map TokyoNeon
    py -3 scripts/build_bot_navigation.py --check
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import heapq
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.bot_ai.compact_vxl import CompactVoxelMap
from server.bot_ai.navigation_atlas import (
    ColumnFlag,
    NavigationAtlas,
    cache_path,
    source_digest,
)


def _map_paths(maps_path: Path, names: list[str]) -> list[Path]:
    if not names:
        return sorted(path for path in maps_path.glob("*.vxl") if path.is_file())
    result: list[Path] = []
    for name in names:
        candidate = maps_path / Path(name).name
        if candidate.suffix.casefold() != ".vxl":
            candidate = candidate.with_suffix(".vxl")
        if not candidate.is_file():
            raise FileNotFoundError(f"map is missing: {candidate}")
        result.append(candidate)
    return result


def _simulate_longest_routes(
    atlas: NavigationAtlas,
    *,
    count: int = 32,
) -> int:
    """Follow the longest cached routes and reject cycles or wrong endings."""

    starts = heapq.nlargest(
        max(1, int(count)),
        (
            index
            for index, flags in enumerate(atlas.flags)
            if flags & int(ColumnFlag.WATER)
            and atlas.water_distance[index] > 0
        ),
        key=lambda index: int(atlas.water_distance[index]),
    )
    longest = 0
    for start in starts:
        start_position = start % atlas.width, start // atlas.width
        position = start_position
        expected = int(atlas.water_distance[start])
        seen: set[tuple[int, int]] = set()
        steps = 0
        while True:
            route = atlas.water_route(*position)
            if route is None:
                break
            if position in seen:
                raise ValueError(f"cyclic water route from {start_position}")
            seen.add(position)
            position = route.next_x, route.next_y
            steps += 1
            if steps > expected:
                raise ValueError(
                    f"water route exceeded distance from {start_position}"
                )
        if steps != expected:
            raise ValueError(
                f"water route ended after {steps}, expected {expected}, "
                f"from {start_position}"
            )
        context = atlas.context(position[0], position[1], atlas.water_support)
        if context is None or context.water:
            raise ValueError(
                f"water route did not end on dry terrain from "
                f"{start_position}"
            )
        longest = max(longest, steps)
    return longest


def _load_cache(path: Path, raw_vxl: bytes) -> NavigationAtlas:
    return NavigationAtlas.from_cache_bytes(
        path.read_bytes(),
        expected_digest=source_digest(raw_vxl),
    )


def process_map(
    map_path: Path,
    *,
    check_only: bool,
    write: bool,
) -> dict[str, object]:
    """Build/verify one atlas and return machine-readable diagnostics."""

    started = time.perf_counter()
    raw_vxl = map_path.read_bytes()
    destination = cache_path(map_path.parent, map_path.name)
    cache_hit = False
    if check_only:
        atlas = _load_cache(destination, raw_vxl)
        cache_hit = True
    else:
        atlas = NavigationAtlas.from_vxl(CompactVoxelMap(raw_vxl))
        atlas.validate()
        if write:
            atlas.write_cache(destination, source_digest(raw_vxl))
            # Verify bytes from disk, not merely the in-memory source object.
            atlas = _load_cache(destination, raw_vxl)
            cache_hit = True
    stats = atlas.stats()
    if stats.unreachable_water_columns:
        raise ValueError(
            f"{map_path.name} has {stats.unreachable_water_columns} "
            "water columns without a deterministic bank route"
        )
    longest = _simulate_longest_routes(atlas)
    result: dict[str, object] = {
        "map": map_path.name,
        "cache": str(destination),
        "cache_bytes": destination.stat().st_size if destination.is_file() else 0,
        "cache_verified": cache_hit,
        "longest_route_simulated": longest,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    result.update(asdict(stats))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--maps-path",
        type=Path,
        default=ROOT / "maps",
        help="directory containing VXL maps (default: repository maps)",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        help="map stem or VXL filename; repeat to select multiple maps",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify existing caches without rebuilding them",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="build and simulate entirely in memory",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one JSON record per map",
    )
    args = parser.parse_args()

    maps_path = args.maps_path.resolve()
    paths = _map_paths(maps_path, list(args.map))
    if not paths:
        raise FileNotFoundError(f"no VXL maps found in {maps_path}")
    if args.check and args.no_write:
        parser.error("--check and --no-write are mutually exclusive")

    for path in paths:
        result = process_map(
            path,
            check_only=bool(args.check),
            write=not bool(args.no_write),
        )
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(
                "{map}: dry={dry_columns} water={water_columns} "
                "assisted={assisted_water_columns} "
                "unreachable={unreachable_water_columns} regions={regions} "
                "layers={layered_columns} longest={longest_route_simulated} "
                "cache={cache_bytes}B time={elapsed_seconds}s".format(**result)
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
