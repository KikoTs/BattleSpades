"""Reproducible retail VXL arenas, foundations and opt-in launch presets."""
from __future__ import annotations

import json
import struct
from pathlib import Path

from .catalog import SPECS
from .arena_designs import BY_NAME, build_designed_arena

MAPS = {"FunYard": "yard", "FunCourse": "course", "FunTunnels": "tunnels",
        "FunBridge": "bridge", "FunTowers": "towers", "FunFloors": "floors", "FunFort": "fort"}
PACK_MAPS = (*(spec.map_name for spec in SPECS), *MAPS)


def atomic_write(path: Path, data: bytes) -> None:
    """Readers see the complete old or new asset while an operator playtests."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def grounded_foundation(bounds=(180, 332, 196, 316)):
    """Encode solid earth to z=239 using VXL's implicit final-span interior."""
    from aoslib.vxl import VXL
    land = bytes((0, 130, 130, 0)) + struct.pack("<I", 0x803D444B)
    sea = bytes((0, 239, 239, 0)) + struct.pack("<I", 0x80465E69)
    rows = []
    x0, x1, y0, y1 = bounds
    for y in range(512):
        rows.append(sea * x0 + land * (x1 - x0 + 1) + sea * (511 - x1) if y0 <= y <= y1 else sea * 512)
    raw = b"".join(rows)
    return VXL(-1, raw, len(raw), 2)


def build_arena(name: str):
    """Return a retail VXL and inert sidecar, without importing retail assets."""
    if name in BY_NAME:
        return build_designed_arena(name)
    layout = MAPS[name]
    world = grounded_foundation()

    def box(x0, x1, y0, y1, z0, z1, color=0x73777E, solid=True):
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                for z in range(z0, z1 + 1):
                    if solid:
                        world.set_point(x, y, z, 0x80000000 | color)
                    else:
                        world.remove_point(x, y, z)

    # Bedrock/failure floor and a separate protected waiting platform.
    box(180, 190, 198, 206, 88, 89, 0xA0A0A0)
    for x in (181, 188):
        for y in (199, 204):
            box(x, x + 1, y, y + 1, 90, 130, 0x73777E)
    anchors = {"blue": [204.5, 256.5, 93.75], "green": [308.5, 256.5, 93.75],
               "center": [256.5, 256.5, 93.75], "waiting": [185.5, 202.5, 85.75],
               "exit": [308.5, 280.5, 93.75], "fort": [256.5, 256.5, 93.75],
               "core_blue": [210, 256, 95], "core_green": [302, 256, 95]}
    data = {"layout": layout, "anchors": anchors, "fall_z": 115,
            "paint_bounds": [235, 277, 242, 270, 96]}
    if layout not in ("bridge", "course", "floors"):
        box(192, 320, 208, 304, 96, 130, 0x738078)
        for x in (192, 320):
            box(x, x, 208, 304, 82, 96)
        for y in (208, 304):
            box(192, 320, y, y, 82, 96)
    if layout == "yard":
        for x in (224, 248, 272, 296):
            for y in (225, 287):
                box(x, x + 5, y, y + 5, 90, 95, 0x8D7961)
        for x in (210, 302):
            box(x - 1, x + 1, 255, 257, 95, 95, 0xF0B840)
    if layout == "course":
        for i, x in enumerate(range(205, 316, 10)):
            y = 256 + (4 if i % 2 else -4)
            box(x - 4, x + 4, y - 4, y + 4, 82, 83, 0x70B0A0 if i % 3 else 0xE7B645)
            for dx in (-3, 2):
                for dy in (-3, 2):
                    box(x + dx, x + dx + 1, y + dy, y + dy + 1, 84, 130, 0x73777E)
        checkpoints = [[205.5, 252.5, 79.75], [235.5, 260.5, 79.75],
                       [265.5, 252.5, 79.75], [295.5, 260.5, 79.75], [315.5, 260.5, 79.75]]
        data["checkpoints"] = checkpoints
        data["bot_route"] = [[x + .5, 256.5 + (4 if i % 2 else -4), 79.75]
                             for i, x in enumerate(range(205, 316, 10))]
        anchors["blue"] = anchors["green"] = checkpoints[0]
        anchors["center"] = checkpoints[2]
        data["fall_z"] = 106
    if layout == "bridge":
        box(192, 216, 220, 292, 96, 130, 0x5982B2)
        box(296, 320, 220, 292, 96, 130, 0x62A66D)
        box(251, 261, 249, 263, 96, 130, 0xC09B59)
    if layout == "floors":
        for z in (82, 90, 98):
            box(208, 304, 224, 288, z, z, 0xA99772)
        for x in (208, 303):
            for y in range(224, 289, 16):
                box(x, x + 1, y, min(y + 1, 288), 83, 130, 0x73777E)
        anchors["blue"] = [220.5, 250.5, 79.75]
        anchors["green"] = [290.5, 262.5, 79.75]
        data["fall_z"] = 108
    if layout == "tunnels":
        box(202, 310, 220, 292, 78, 104, 0x74675E)
        for x in (212, 300):
            box(x - 6, x + 6, 249, 263, 96, 101, solid=False)
        anchors["blue"] = [212.5, 256.5, 99.75]
        anchors["green"] = [300.5, 256.5, 99.75]
        for i, (x, y) in enumerate(((240, 235), (256, 276), (272, 240)), 1):
            box(x - 3, x + 3, y - 3, y + 3, 96, 101, solid=False)
            anchors[f"cache{i}"] = [x + 0.5, y + 0.5, 100.5]
        anchors["center"] = anchors["cache2"]
    if layout == "towers":
        for x, key, color in ((216, "core_blue", 0x5982B2), (296, "core_green", 0x62A66D)):
            for dx in (-4, 4):
                for dy in (-4, 4):
                    box(x + dx, x + dx, 256 + dy, 256 + dy, 77, 95, color)
            box(x - 5, x + 5, 251, 261, 76, 76, color)
            box(x, x, 256, 256, 75, 75, 0xF0B840)
            anchors[key] = [x, 256, 75]
    if layout == "fort":
        for x in (235, 277):
            box(x, x + 1, 235, 277, 85, 95, 0x8D7961)
        for y in (235, 277):
            box(235, 278, y, y + 1, 85, 95, 0x8D7961)
        gates = []
        for x, y in ((235, 256), (277, 256), (256, 235), (256, 277)):
            box(x - 1, x + 2, y - 1, y + 2, 90, 95, solid=False)
            gates.append([[x, y + d, z] for d in (-1, 0, 1) for z in range(90, 96)])
        data["closing_gates"] = gates
    for i, (x, y) in enumerate(((236, 232), (276, 232), (236, 280), (276, 280), (256, 224), (256, 288)), 1):
        anchors[f"loot{i}"] = [x + 0.5, y + 0.5, 94.5]
    # Barricade job: grounded gap in a short, highly visible wall.
    if layout == "yard":
        box(247, 265, 240, 240, 93, 95, 0xC09B59)
        data["repair_cells"] = [[x, 240, 95] for x in range(254, 259)]
    payload = {"vxl_format": "retail", "gravity": 1.0, "fog_color": [145, 168, 183],
               "unofficial": data}
    return world, payload


def write_pack(directory: Path, config_directory: Path | None = None, *, overwrite: bool = False) -> list[Path]:
    """Write reproducible maps/configs, refusing to replace operator edits."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / f"{name}{ext}" for name in PACK_MAPS for ext in (".vxl", ".txt")]
    if config_directory is not None:
        config_directory.mkdir(parents=True, exist_ok=True)
        paths += [config_directory / f"{spec.code}.toml" for spec in SPECS]
    if not overwrite and any(path.exists() for path in paths):
        raise FileExistsError("Pack files already exist; use --overwrite only to regenerate them")
    for name in PACK_MAPS:
        world, payload = build_arena(name)
        atomic_write(directory / f"{name}.vxl", world.generate_vxl(False))
        atomic_write(directory / f"{name}.txt", (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
    if config_directory is not None:
        for spec in SPECS:
            night = spec.code == "night_shift"
            text = (f'[server]\nname = "Unofficial: {spec.title}"\n\n'
                    f'[game]\ndefault_mode = "{spec.code}"\ndefault_map = "{spec.map_name}"\n'
                    f'\n[lobby]\nmap_rotation = ["{spec.map_name}"]\n'
                    '\n[world]\n' + f'maps_path = {json.dumps(directory.as_posix())}\n'
                    'entities_wire_ready = true\n\n[bots]\n'
                    'enabled = true\n'
                    + ('population_mode = "admin"\nmax_bots = 12\n' if night else
                       'population_mode = "backfill"\nfill_target = 6\nmax_bots = 8\n')
                    + 'reserve_human_slots = 2\n'
                    '\n[achievements]\nenabled = false\n'
                    f'\n[modes.{spec.code}]\nrounds = 4\nround_time = {spec.duration}\nprepare_time = {spec.preparation}\n')
            atomic_write(config_directory / f"{spec.code}.toml", text.encode("utf-8"))
    return paths
