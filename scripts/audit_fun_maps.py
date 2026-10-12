"""Audit actual VXL solids and write a visual atlas of the community arenas."""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from scipy import ndimage
from PIL import Image, ImageDraw, ImageFont
from server.config import ServerConfig
from server.world_manager import WorldManager
from modes.unofficial.arena_designs import DESIGNS
from modes.unofficial.catalog import SPECS, CATALOG


def audit(output: Path) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    cards, report = [], []
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    font = ImageFont.truetype(str(font_path), 18) if font_path.exists() else ImageFont.load_default(size=18)
    small = ImageFont.truetype(str(font_path), 13) if font_path.exists() else ImageFont.load_default(size=13)
    for spec in SPECS:
        code, design = spec.code, DESIGNS[spec.code]
        config = ServerConfig()
        config.maps_path = "maps/unofficial"
        wm = WorldManager(config)
        assert wm.load_map(design.name)
        data = wm.map_metadata.unofficial
        x0, x1, y0, y1 = data["bounds"]
        # Full connected-component proof, not a flood with a work-budget escape.
        solid = np.fromiter((wm.map.get_solid(x, y, z)
                             for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)
                             for z in range(50, 240)), dtype=bool).reshape(x1 - x0 + 1, y1 - y0 + 1, 190)
        labels, count = ndimage.label(solid, structure=ndimage.generate_binary_structure(3, 2))
        grounded = np.unique(labels[:, :, -1])
        floating = int(np.count_nonzero(solid & ~np.isin(labels, grounded)))
        spawn_keys = ("blue", "green", "waiting")
        invalid = [key for key in spawn_keys if not wm.spawn_position_is_safe(data["anchors"][key])]
        if code == "block_hunt":
            invalid += [f"hide{i}" for i, p in enumerate(data["hunt_points"]) if not wm.spawn_position_is_safe(p)]
        if "checkpoints" in data:
            invalid += [f"checkpoint{i}" for i, p in enumerate(data["checkpoints"]) if not wm.spawn_position_is_safe(p)]
        unreachable = []
        if data["layout"] in ("yard", "towers", "fort"):
            # Indoor walkability: use body clearance below roofs, not just a
            # height map, and allow the authored one-voxel garden steps.
            levels = {}
            for x in range(x0 + 1, x1):
                for y in range(y0 + 1, y1):
                    for floor in (94, 95, 96):
                        if wm.spawn_position_is_safe((x + .5, y + .5, floor - 2.25)):
                            levels[x, y] = floor
                            break
            start = tuple(int(v) for v in data["anchors"]["blue"][:2])
            reached, pending = {start}, deque([start])
            while pending:
                point = pending.popleft()
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    candidate = point[0] + dx, point[1] + dy
                    if candidate not in reached and candidate in levels and abs(levels[candidate] - levels.get(point, 96)) <= 1:
                        reached.add(candidate)
                        pending.append(candidate)
            keys = ["green", "center"]
            if code in ("last_exit", "night_shift", "reverse_siege"):
                keys.append("exit")
            if code == "last_exit":
                keys.extend(f"loot{i}" for i in range(1, 7))

            unreachable = [key for key in keys if tuple(int(v) for v in data["anchors"][key][:2]) not in reached]
            if code == "block_hunt":
                unreachable += [f"hide{i}" for i, point in enumerate(data["hunt_points"])
                                if tuple(int(v) for v in point[:2]) not in reached]
        # Probe ordinary single-cell digging through the production collapse API.
        probes = []
        for team in ("blue", "green"):
            x, y, z = data["anchors"][team]
            cell = int(x) + 2, int(y), int(z + 2.25)
            if wm.map.get_solid(*cell):
                color = wm.map.get_color(*cell)
                wm.map.remove_point(*cell)
                probes.append(sum(len(chunk) for chunk in wm.find_unsupported_chunks([cell])))
                wm.map.set_point(*cell, color)
        row = dict(mode=code, map=design.name, solid_voxels=int(solid.sum()), components=count,
                   floating_voxels=floating, invalid_positions=invalid, dig_collapse_sizes=probes,
                   unreachable_objectives=unreachable,
                   sky=wm.map_metadata.skybox_name, ambience=[s.name for s in wm.map_metadata.ambient_sounds])
        report.append(row)
        print(json.dumps(row), flush=True)
        heights = solid.argmax(axis=2) + 50
        picture = np.zeros((y1 - y0 + 1, x1 - x0 + 1, 3), dtype=np.uint8)
        for ix in range(picture.shape[1]):
            for iy in range(picture.shape[0]):
                color = wm.map.get_color(ix + x0, iy + y0, int(heights[ix, iy]))
                picture[iy, ix] = ((color >> 16) & 255, (color >> 8) & 255, color & 255)
        dx, dy = np.gradient(heights.astype(float))
        light = np.clip(1 + dx * .06 + dy * .035, .60, 1.16).T[..., None]
        view = Image.fromarray(np.clip(picture * light, 0, 255).astype(np.uint8))
        view = view.resize((612, 484), Image.Resampling.NEAREST)
        draw = ImageDraw.Draw(view)
        for key, color in (("blue", "#66a9ef"), ("green", "#8fcd88"), ("center", "#ffd77a")):
            x, y, _ = data["anchors"][key]
            px, py = (x - x0) * 612 / (x1 - x0 + 1), (y - y0) * 484 / (y1 - y0 + 1)
            draw.ellipse((px - 6, py - 6, px + 6, py + 6), fill=color, outline="#101820", width=2)
        view.save(output / f"{design.name}.png")
        # Actual top-surface isometric projection, useful for checking roofs,
        # foundations and silhouettes that an overhead alone can hide.
        iso = Image.new("RGB", (840, 670), "#18242e")
        d = ImageDraw.Draw(iso)
        scale = min(3.0, 780 / (picture.shape[0] + picture.shape[1]))
        origin_x = 420 - (picture.shape[1] - picture.shape[0]) * scale / 2
        for total in range(picture.shape[0] + picture.shape[1]):
            for ix in range(picture.shape[1]):
                iy = total - ix
                if not 0 <= iy < picture.shape[0]:
                    continue
                h = 130 - int(heights[ix, iy])
                px, py = origin_x + (ix - iy) * scale, 220 + (ix + iy) * scale / 2 - h * scale
                rgb = tuple(int(v) for v in picture[iy, ix])
                for nx, ny, edge, light in ((ix + 1, iy, scale, .70), (ix, iy + 1, -scale, .53)):
                    next_z = int(heights[nx, ny]) if nx < picture.shape[1] and ny < picture.shape[0] else 130
                    drop = max(0, next_z - int(heights[ix, iy])) * scale
                    if drop:
                        d.polygon([(px + edge, py), (px, py + scale / 2),
                                   (px, py + scale / 2 + drop), (px + edge, py + drop)],
                                  fill=tuple(int(v * light) for v in rgb))
                d.polygon([(px, py - scale / 2), (px + scale, py), (px, py + scale / 2), (px - scale, py)], fill=rgb)
        iso.save(output / f"{design.name}-iso.png")
        card = Image.new("RGB", (420, 340), "#18242e")
        card.paste(view.resize((400, 260), Image.Resampling.NEAREST), (10, 60))
        d = ImageDraw.Draw(card)
        d.text((12, 9), design.name, font=font, fill="#f2dfb3")
        d.text((12, 35), CATALOG[code].title, font=small, fill="#b4cbd4")
        cards.append(card)
        del labels, solid
    atlas = Image.new("RGB", (420 * 4, 340 * ((len(cards) + 3) // 4)), "#101820")
    for i, card in enumerate(cards):
        atlas.paste(card, ((i % 4) * 420, (i // 4) * 340))
    atlas.save(output / "arena-atlas.png")
    (output / "audit.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    rows = audit(Path("docs/arena-previews"))
    raise SystemExit(int(any(row["floating_voxels"] or row["invalid_positions"] or row["unreachable_objectives"]
                            or any(n > 32 for n in row["dig_collapse_sizes"]) for row in rows)))
