"""Rebuild reviewed landmark masks, preserving every unrelated sidecar field.

These are reconstructed targets from screenshots and the bundled terrain,
not recovered retail achievement volumes. See docs/MAP_ACHIEVEMENTS.md.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import logging
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.config import ServerConfig
from server.world_manager import WorldManager

TERRAIN_HASHES = {
    "AncientEgypt": "9c9e3d6d5c67ce4108a465873e7a709ab34fe3d465b0db24c66231cdf8a54536",
    "GreatWall": "d40df566bb52c72f10eb918258e49892dc05fb6975af72e316e72ec5dc3cc1ea",
    "London": "cb691562c79198becde0b38f8b1d6117ee41368db77dc877ba39324a16c7d84c",
    "LunarBase": "07bc5cc2878879a59d2b82fe0dacb70e64748c686bf38bc97b6e44b304266631",
    "CastleWars": "a47f01f360a2f093112c2c98b8238e47268582d4e1be5cc6f5094948f0b8d530",
    "TheColosseum": "22bc6749adf46cfe752cb2980138c309e29f1af72e70e014a2b0dade25d8d98d",
}
Bounds = tuple[int, int, int, int, int, int]


def box_cells(bounds: Bounds):
    x0, x1, y0, y1, z0, z1 = bounds
    return itertools.product(range(x0, x1 + 1), range(y0, y1 + 1), range(z0, z1 + 1))


def targets(name: str) -> list[tuple[str, Bounds, int, int]]:
    """API, runtime bounds, eligible wire team, kill threshold."""
    if name == "AncientEgypt":
        return [("map_egypt_destroy", (260, 260, 250, 250, 195, 196), 0, 0)]
    if name == "LunarBase":
        return [("map_moon_destroy", (304, 307, 458, 458, 221, 229), 0, 0)]
    if name == "London":
        return [("map_london_destroy", (348, 354, 235, 241, 115, 120), 0, 0)]
    if name == "GreatWall":
        return [("map_greatwall_destroy", (x, x, y, y + 3, 178, 186), 0, 0)
                for x in (309, 312, 315) for y in (191, 197, 203)]
    if name == "CastleWars":
        return [("map_concrete_destroy", (x, x + 1, y, y + 1, 220, 225), team, 0)
                for team, xs, ys in ((1, (56, 67, 78, 89), (42, 67, 92)),
                                     (2, (419, 430, 441, 452), (415, 440, 465)))
                for x in xs for y in ys]
    if name == "TheColosseum":
        return [("map_colosseum_kill", (214, 279, 216, 291, 216, 228), 0, 10)]
    raise ValueError(name)


def build_payload(world: WorldManager, name: str) -> dict:
    path = ROOT / "maps" / f"{name}.json"
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if "_retail_source" in payload:
        raise ValueError(f"{name}: refusing to replace recovered metadata")
    digest = hashlib.sha256((ROOT / "maps" / f"{name}.vxl").read_bytes()).hexdigest()
    if digest != TERRAIN_HASHES[name]:
        raise ValueError(f"{name}: terrain differs from the reviewed VXL")
    if not world.load_map(name):
        raise ValueError(f"{name}: native terrain failed to load")
    shift = world.map.source_z_shift
    rows = targets(name)
    for key in ("ac_ids", "ac_types", "ac_centres", "ac_w_h_d", "ac_teams", "ac_kills", "ac_cells"):
        payload[key] = []
    for api, bounds, team, kills in rows:
        x0, x1, y0, y1, z0, z1 = bounds
        if kills:
            # Original air below the arena's covered floors. The open central
            # pit and outdoor entrances do not become achievement tunnels.
            covered = {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)
                       if any(world.get_solid(x, y, z) for z in range(198, 224))}
            cells = [cell for cell in box_cells(bounds)
                     if cell[:2] in covered and not world.get_solid(*cell)]
        else:
            cells = [cell for cell in box_cells(bounds) if world.get_solid(*cell)]
        if not cells:
            raise ValueError(f"{name}: empty target {bounds}")
        payload["ac_ids"].append(api)
        payload["ac_types"].append(1 if kills else 2)
        edge = 1 if kills else 0
        payload["ac_centres"].append([(x0 + x1 + edge) / 2, (y0 + y1 + edge) / 2, (z0 + z1 + edge) / 2 - shift])
        # Positive box dimensions enclose singleton voxel axes; ac_cells
        # still supplies the exact discrete target.
        payload["ac_w_h_d"].append([max(.5, x1 - x0 + edge), max(.5, y1 - y0 + edge), max(.5, z1 - z0 + edge)])
        payload["ac_teams"].append(team)
        payload["ac_kills"].append(kills)
        payload["ac_cells"].append([[x, y, z - shift] for x, y, z in sorted(cells)])
    payload["_achievement_source"] = {
        "kind": "Screenshot-guided reconstruction against native VXL geometry; not recovered retail volumes",
        "guide": "https://steamcommunity.com/sharedfiles/filedetails/?id=135523055",
        "vxl_sha256": digest,
        "coordinate_space": "source VXL; achievement engine applies source_z_shift once",
        "generator": "tools/map_metadata/author_achievement_regions.py",
    }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.WARNING)
    world = WorldManager(ServerConfig())
    stale = []
    for name in TERRAIN_HASHES:
        path = ROOT / "maps" / f"{name}.json"
        payload = build_payload(world, name)
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if current != payload:
            stale.append(name)
            if not args.check:
                # Compact cell triples keep this reviewed data readable without
                # thousands of one-coordinate lines from generic indentation.
                text = json.dumps(payload, indent=2)
                text = re.sub(r'\[\n\s+(-?\d+),\n\s+(-?\d+),\n\s+(-?\d+)\n\s+\]', r'[\1, \2, \3]', text)
                path.write_text(text + "\n", encoding="utf-8")
        print(f"{name}: {len(payload['ac_ids'])} regions, {sum(map(len, payload['ac_cells']))} cells")
    if args.check and stale:
        parser.exit(1, "Stale achievement targets: " + ", ".join(stale) + "\n")


if __name__ == "__main__":
    main()
