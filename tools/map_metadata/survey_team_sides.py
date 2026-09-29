"""Measure which side of each stock map its Blue and Green teams own.

Retail shipped no spawn/base metadata for most stock maps (see
``docs/MAP_METADATA.md``), but many of those VXLs still carry team-coloured
geometry: the paired team markers ``#0028BE`` (Blue) / ``#00BE2A`` (Green)
and blue/green painted structures such as CastleWars' castles.  This survey
prints, for every map:

* ``markers`` -- exact marker voxels (each channel within 4), count and
  bounding box per team;
* ``hue`` -- top-surface voxels with saturated blue (hue 200-250) or green
  (hue 95-150) paint, count and centroid.  Green foliage pollutes the green
  centroid on vegetated maps; the blue centroid and the markers are the
  reliable signals.

The fallback team regions in ``server/map_metadata.py``
(``_STOCK_FALLBACK_SPAWN_REGIONS``) cite this output.

Usage::

    py -3.12 tools/map_metadata/survey_team_sides.py [MapName ...]
"""

from __future__ import annotations

import colorsys
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.runtime_vxl import _iter_explicit_voxels  # noqa: E402

_MARKERS = {"blue": (0x00, 0x28, 0xBE), "green": (0x00, 0xBE, 0x2A)}


def survey(path: Path) -> dict[str, dict[str, object]]:
    """Return marker and saturated-paint statistics for one VXL."""

    data = path.read_bytes()
    top: dict[tuple[int, int], tuple[int, int]] = {}
    markers: dict[str, list[tuple[int, int, int]]] = {"blue": [], "green": []}
    for x, y, z, color in _iter_explicit_voxels(data):
        rgb = ((color >> 16) & 255, (color >> 8) & 255, color & 255)
        for team, marker in _MARKERS.items():
            if all(abs(a - b) <= 4 for a, b in zip(rgb, marker)):
                markers[team].append((x, y, z))
        current = top.get((x, y))
        if current is None or z < current[0]:
            top[(x, y)] = (z, color)

    paint: dict[str, list[tuple[int, int]]] = {"blue": [], "green": []}
    for (x, y), (_z, color) in top.items():
        red, green, blue = ((color >> 16) & 255, (color >> 8) & 255, color & 255)
        hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
        if saturation < 0.45 or value < 0.35:
            continue
        degrees = hue * 360.0
        if 200.0 <= degrees <= 250.0:
            paint["blue"].append((x, y))
        elif 95.0 <= degrees <= 150.0:
            paint["green"].append((x, y))

    result: dict[str, dict[str, object]] = {}
    for team in ("blue", "green"):
        points = markers[team]
        row: dict[str, object] = {"markers": len(points)}
        if points:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            row["marker_box"] = (min(xs), min(ys), max(xs), max(ys))
        cells = paint[team]
        row["paint"] = len(cells)
        if cells:
            row["paint_centroid"] = (
                round(sum(c[0] for c in cells) / len(cells)),
                round(sum(c[1] for c in cells) / len(cells)),
            )
        result[team] = row
    return result


def main(argv: list[str] | None = None) -> int:
    names = list(argv if argv is not None else sys.argv[1:])
    maps_dir = ROOT / "maps"
    paths = (
        [maps_dir / f"{name}.vxl" for name in names]
        if names else sorted(maps_dir.glob("*.vxl"))
    )
    for path in paths:
        stats = survey(path)
        parts = []
        for team in ("blue", "green"):
            row = stats[team]
            text = f"{team}: markers {row['markers']}"
            if "marker_box" in row:
                text += f" box {row['marker_box']}"
            text += f", paint {row['paint']}"
            if "paint_centroid" in row:
                text += f" @ {row['paint_centroid']}"
            parts.append(text)
        print(f"{path.stem:16} " + " | ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
