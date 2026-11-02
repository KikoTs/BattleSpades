"""Recover reviewed light-top props, without treating bright terrain as lights."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server.config import ServerConfig
from server.runtime_vxl import _iter_explicit_voxels
from server.world_manager import WorldManager

MAP = "SpookyMansion"
TERRAIN_SHA256 = "0b6d2c1a6de97d9586f79847c27cab1bb2a66ba1055d217e6d7c6671ff7d0e2e"
LIGHT_RGB = (250, 250, 200)
# Reviewed VXL props: a single brown post, and the indoor candelabra arms.
SUPPORT_RGB = ((66, 41, 6), (143, 108, 3))


def reconstruct(name: str = MAP) -> dict:
    if name == MAP:
        digest, light_rgb, support_rgb = TERRAIN_SHA256, LIGHT_RGB, SUPPORT_RGB
        expected, max_side, air_above = [36, 41], 0, 2
    elif name == "MayanJungle":
        digest = "06f3877ef8f87a16cd5a9765b9a995ac77923a5ba2ae7edf988933082a6db7b8"
        light_rgb, support_rgb = (224, 172, 29), ((101, 62, 7),)
        expected, max_side, air_above = [20], 1, 1
    else:
        raise ValueError(name)
    raw = (ROOT / "maps" / f"{name}.vxl").read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"{name} terrain differs from the reviewed VXL")
    logging.disable(logging.WARNING)
    world = WorldManager(ServerConfig())
    if not world.load_map(name):
        raise ValueError("Native map load failed")
    wanted = (light_rgb[0] << 16) | (light_rgb[1] << 8) | light_rgb[2]
    supports = {(r << 16) | (g << 8) | b for r, g, b in support_rgb}
    points = []
    counts = Counter()
    for x, y, z, color in _iter_explicit_voxels(raw):
        if color & 0xFFFFFF != wanted:
            continue
        runtime_z = z + world.map.source_z_shift
        if not world.get_solid(x, y, runtime_z + 1):
            continue
        base = world.get_color(x, y, runtime_z + 1) & 0xFFFFFF
        if base not in supports:
            continue
        if sum(world.get_solid(x + dx, y + dy, runtime_z)
               for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))) > max_side:
            continue
        if any(world.get_solid(x, y, runtime_z - offset) for offset in range(1, air_above + 1)):
            continue
        points.append([x, y, z])
        counts[base] += 1
    if sorted(counts.values()) != expected:
        raise ValueError(f"Reviewed fixture inventory changed: {counts}")
    return {
        "vxl_sha256": digest,
        "coordinate_space": "source VXL",
        "light_color": list(light_rgb),
        "light_gain": 0.5,
        "support_colors": [list(rgb) for rgb in support_rgb],
        "provenance": "Existing light-top voxels on reviewed brown posts/candelabras or attached torch props; source map palette. Original server entity list unavailable.",
        "generator": "tools/map_metadata/recover_fixture_lights.py",
        "points": sorted(points),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name in (MAP, "MayanJungle"):
        path = ROOT / "maps" / f"{name}.lights.json"
        payload = reconstruct(name)
        if args.check:
            if not path.is_file() or json.loads(path.read_text()) != payload:
                parser.exit(1, f"Stale {name} reviewed fixture lights\n")
        else:
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"{name}: {len(payload['points'])} reviewed light-top voxels")


if __name__ == "__main__":
    main()
