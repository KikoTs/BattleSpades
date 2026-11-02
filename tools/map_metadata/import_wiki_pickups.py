"""Write reviewed wiki-derived crate layouts without changing other map data.

The manifest records rounded source markers and terrain-reviewed floor choices,
not recovered retail-server coordinates. The four recovered retail tables and
the two existing UGC crate layouts are deliberately outside this importer.

Usage: ``py -3.12 tools/map_metadata/import_wiki_pickups.py [--check]``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("wiki_pickups.json")
KINDS = ("health", "ammo", "block")


def sidecar_payload(maps_dir: Path, entry: dict) -> dict:
    """Merge only crate fields, preserving atmosphere, spawns and objectives."""
    name = entry["name"]
    vxl = maps_dir / f"{name}.vxl"
    if hashlib.sha256(vxl.read_bytes()).hexdigest() != entry["vxl_sha256"]:
        raise ValueError(f"{name}: terrain differs from the reviewed VXL")
    path = maps_dir / f"{name}.json"
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if "_retail_source" in payload:
        raise ValueError(f"{name}: refusing to replace recovered retail metadata")
    for suffix in (".ugc", ".txt"):
        sibling = maps_dir / f"{name}{suffix}"
        if sibling.exists():
            # .ugc spawn/base rows are fine. Existing crate rows are not: those
            # must be reviewed separately rather than doubled by legacy lists.
            text = sibling.read_text(encoding="utf-8")
            if any(f'"ugc_{kind}_drop"' in text for kind in KINDS):
                raise ValueError(f"{name}: refusing to duplicate authored UGC crates")
    shift = entry["source_z_shift"]
    for kind in KINDS:
        payload[f"{kind}_crate_drop_points"] = [
            [point["position"][0], point["position"][1], point["position"][2] - shift]
            for point in entry["points"] if point["type"] == kind
        ]
    payload["_wiki_pickup_source"] = {
        "page": f"https://aceofspadeswiki.info/wiki/{entry['page'].removesuffix('.html')}",
        "page_sha256": entry["page_sha256"],
        "vxl_sha256": entry["vxl_sha256"],
        "coordinate_space": "source VXL; loader applies source_z_shift once",
        "accuracy": "Rounded wiki icon footprints; terrain-reviewed floor heights. Not exact retail coordinates.",
        "manifest": "tools/map_metadata/wiki_pickups.json",
    }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify without writing")
    parser.add_argument("--maps-dir", type=Path, default=ROOT / "maps")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    stale = []
    for entry in manifest["maps"]:
        path = args.maps_dir / f"{entry['name']}.json"
        payload = sidecar_payload(args.maps_dir, entry)
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if current != payload:
            stale.append(entry["name"])
            if not args.check:
                path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.check and stale:
        parser.exit(1, "Stale crate layouts: " + ", ".join(stale) + "\n")
    print(f"{'Verified' if args.check else 'Wrote'} {len(manifest['maps'])} map crate layouts ({len(stale)} changed)")


if __name__ == "__main__":
    main()
