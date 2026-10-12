"""Authored arena themes and reusable voxel architecture, all bedrock connected."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArenaDesign:
    name: str
    base: str
    theme: str
    feature: str


DESIGNS = {
    "block_hunt": ArenaDesign("LanternQuarter", "FunYard", "night", "village"),
    "false_wall": ArenaDesign("FacadeDistrict", "FunYard", "town", "decoys"),
    "crumbling_run": ArenaDesign("FoundryRun", "FunCourse", "forge", "race"),
    "last_exit": ArenaDesign("DuskwaterDocks", "FunYard", "harbor", "docks"),
    "night_shift": ArenaDesign("BlackPineOutpost", "FunYard", "night", "outpost"),
    "tunnel_rats": ArenaDesign("Coppercatacombs", "FunTunnels", "forge", "bunker"),
    "bridge_chicken": ArenaDesign("RelayViaduct", "FunBridge", "alpine", "viaduct"),
    "towerfall": ArenaDesign("TwinBellFoundry", "FunTowers", "forge", "towers"),
    "floor_tax": ArenaDesign("Glassworks", "FunFloors", "alpine", "floors"),
    "hot_potato": ArenaDesign("FuseCourtyard", "FunYard", "town", "courtyard"),
    "lockdown": ArenaDesign("BreakwaterPrison", "FunYard", "harbor", "prison"),
    "paint_raid": ArenaDesign("ChromaticPlaza", "FunYard", "garden", "paint"),
    "smugglers_run": ArenaDesign("MoonshineWharf", "FunYard", "night", "wharf"),
    "one_in_the_spade": ArenaDesign("TheKiln", "FunYard", "forge", "duel"),
    "borrowed_arsenal": ArenaDesign("ArsenalArcade", "FunYard", "town", "arcade"),
    "reverse_siege": ArenaDesign("BreachCitadel", "FunFort", "alpine", "citadel"),
    "kingmaker": ArenaDesign("CrownGardens", "FunYard", "garden", "gardens"),
    "red_light_raid": ArenaDesign("SignalSprint", "FunCourse", "night", "signals"),
    "scrapyard_engineers": ArenaDesign("SalvageWorks", "FunYard", "harbor", "scrap"),
    "spadeball": ArenaDesign("SpadeStadium", "FunYard", "garden", "stadium"),
}
BY_NAME = {d.name: d for d in DESIGNS.values()}

# These sky meshes and audio names exist in shipped retail map metadata.
THEMES = {
    "town": dict(stone=0xC4B89C, roof=0x87594D, ground=0x807B69, wood=0x94714D,
                 foliage=0x587254, trim=0xE5D5B4, sky="WW1.txt", sound="amb_ww_lighter",
                 fog=[163, 156, 135], light=[238, 216, 183], ambient=[124, 133, 137]),
    "night": dict(stone=0x73858A, roof=0x425362, ground=0x5C7068, wood=0x8F7461,
                  foliage=0x35554D, trim=0xCFB480, sky="BranCastle.txt", sound="amb_zombieisland",
                  fog=[59, 76, 89], light=[158, 182, 205], ambient=[88, 109, 132]),
    "forge": dict(stone=0x8D766C, roof=0x464D55, ground=0x786B60, wood=0xAC7547,
                  foliage=0x5C6853, trim=0xE1AE5C, sky="SecretBase.txt", sound="amb_doomwind",
                  fog=[140, 115, 100], light=[225, 178, 137], ambient=[130, 118, 107]),
    "harbor": dict(stone=0x9FAFB0, roof=0x526F79, ground=0x858B84, wood=0x957657,
                   foliage=0x617E61, trim=0xDFCB91, sky="Chicago.txt", sound="amb_ww_lighter",
                   fog=[136, 165, 181], light=[208, 222, 225], ambient=[128, 149, 162]),
    "alpine": dict(stone=0xB1BEC1, roof=0x546B81, ground=0x92A09D, wood=0x958373,
                   foliage=0x526E69, trim=0xE3D7AE, sky="ArcticBase.txt", sound="amb_doomwind",
                   fog=[174, 199, 214], light=[226, 237, 241], ambient=[137, 160, 182]),
    "garden": dict(stone=0xD0C6AB, roof=0x7A8E79, ground=0x699071, wood=0x9D7A51,
                   foliage=0x467345, trim=0xE7C979, sky="MayanJungle.txt", sound="amb_jungle",
                   fog=[164, 193, 169], light=[240, 226, 187], ambient=[134, 158, 133]),
}


def shade(color: int, amount: int) -> int:
    return sum(max(0, min(255, ((color >> shift) & 255) + amount)) << shift for shift in (16, 8, 0))


class Architect:
    def __init__(self, world, theme: dict):
        self.world, self.theme = world, theme
        self.lights = []

    def box(self, x0, x1, y0, y1, z0, z1, color=None, *, air=False):
        color = self.theme["stone"] if color is None else color
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                for z in range(z0, z1 + 1):
                    if air:
                        self.world.remove_point(x, y, z)
                    else:
                        self.world.set_point(x, y, z, 0x80000000 | color)

    def slab(self, x0, x1, y0, y1, z, color):
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                self.world.set_point(x, y, z, 0x80000000 | shade(color, ((x * 17 + y * 31) % 7 - 3) * 2))

    def crate(self, x, y, *, size=4, floor=96):
        self.box(x, x + size - 1, y, y + size - 1, floor - size, floor - 1, self.theme["wood"])
        for k in (0, size - 1):
            self.box(x + k, x + k, y, y + size - 1, floor - size, floor - size, self.theme["trim"])
            self.box(x, x + size - 1, y + k, y + k, floor - size, floor - 1, shade(self.theme["wood"], -24))

    def lamp(self, x, y, floor=96):
        self.box(x, x, y, y, floor - 8, floor - 1, 0x454F56)
        self.box(x - 1, x + 1, y - 1, y + 1, floor - 9, floor - 8, 0xF3CD78)
        self.box(x - 1, x + 1, y - 1, y + 1, floor - 10, floor - 10, self.theme["roof"])
        if len(self.lights) < 12:
            self.lights.append(dict(item="flare_block", position=[x, y, floor - 8], color=[243, 205, 120]))

    def tree(self, x, y, floor=96):
        self.box(x, x + 1, y, y + 1, floor - 12, floor - 1, self.theme["wood"])
        for level, radius in ((12, 4), (15, 3), (17, 1)):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if abs(dx) + abs(dy) <= radius + 1:
                        self.box(x + dx, x + dx, y + dy, y + dy, floor - level, floor - level + 3,
                                 shade(self.theme["foliage"], dx * 2 - dy * 2))

    def building(self, x, y, width=14, depth=14, height=12, floor=96):
        wall, roof, trim = (self.theme[k] for k in ("stone", "roof", "trim"))
        top = floor - height
        self.box(x, x + width, y, y + depth, top, floor - 1, wall)
        self.box(x + 2, x + width - 2, y + 2, y + depth - 2, top + 2, floor - 1, air=True)
        # Two opposite doors, plus side windows: never a decorative sealed box.
        for edge in (y, y + depth - 1):
            self.box(x + width // 2 - 2, x + width // 2 + 2, edge, edge + 1, floor - 6, floor - 1, air=True)
        for edge in (x, x + width - 1):
            for offset in (4, depth - 4):
                self.box(edge, edge + 1, y + offset - 1, y + offset + 1, top + 4, top + 6, air=True)
        for z in range(top + 3, floor - 2, 4):
            for edge in (x, x + width):
                self.box(edge, edge, y, y + depth, z, z, shade(wall, -17))
        for edge in (y, y + depth):
            for xx in range(x, x + width + 1):
                for z in range(top + 2, floor):
                    if self.world.get_solid(xx, edge, z):
                        mortar = z % 4 == 0 or (xx + (z // 4 % 2) * 2) % 5 == 0
                        self.world.set_point(xx, edge, z, 0x80000000 | shade(wall, -20 if mortar else (xx * 3 + z) % 9))
        self.box(x - 1, x + width + 1, y - 1, y + depth + 1, top - 1, top, roof)
        for step in range(1, 4):
            self.box(x + step, x + width - step, y, y + depth, top - 1 - step, top - step, roof)
        for edge in (x, x + width):
            self.box(edge, edge, y, y, top, floor - 1, trim)
            self.box(edge, edge, y + depth, y + depth, top, floor - 1, trim)
        self.box(x + 2, x + 4, y + 2, y + 4, top - 7, top - 1, shade(wall, -30))
        # Low benches inside, leaving a straight five-cell doorway aisle.
        self.box(x + 2, x + 3, y + 3, y + depth - 3, floor - 2, floor - 1, self.theme["wood"])
        self.box(x + width - 3, x + width - 2, y + 3, y + depth - 3,
                 floor - 2, floor - 1, self.theme["wood"])

    def arch(self, x, y, width=12, floor=96, height=10):
        for edge in (x, x + width):
            self.box(edge, edge + 1, y, y + 1, floor - height, floor - 1)
        self.box(x, x + width + 1, y, y + 1, floor - height, floor - height + 1, self.theme["trim"])

    def container(self, x, y, color=0x537985):
        self.box(x, x + 12, y, y + 7, 89, 95, color)
        self.box(x, x + 12, y + 2, y + 5, 91, 95, air=True)
        for dx in range(1, 13, 3):
            for edge in (y, y + 7):
                self.box(x + dx, x + dx, edge, edge, 90, 95, shade(color, 22))


def build_designed_arena(name: str):
    from .maps import build_arena
    design = BY_NAME[name]
    if design.feature == "outpost":
        from .revised_maps import build_survival
        return build_survival(design)
    if design.feature == "courtyard":
        from .revised_maps import build_tag_arena
        return build_tag_arena(design)
    if design.feature in ("race", "signals", "floors"):
        from .revised_maps import build_course, build_spleef
        return build_spleef(design) if design.feature == "floors" else build_course(design)
    world, payload = build_arena(design.base)
    theme = THEMES[design.theme]
    a = Architect(world, theme)
    data = payload["unofficial"]
    anchors = data["anchors"]
    layout, feature = data["layout"], design.feature
    # A terraced island skirt replaces the floating-grey-box silhouette.
    for inset, top in ((0, 122), (3, 116), (6, 110)):
        for x in (180 + inset, 332 - inset):
            a.box(x, x + 2, 208, 304, top, 130, shade(theme["stone"], -20))
        for y in (196 + inset, 316 - inset):
            a.box(192, 320, y, y + 2, top, 130, shade(theme["stone"], -20))
    # Waiting terrace is a supported viewing deck, with railings and a lantern.
    for x in (180, 190):
        a.box(x, x, 198, 206, 86, 88, theme["trim"])
    a.lamp(181, 199, floor=88)
    if layout in ("yard", "towers", "fort"):
        a.slab(193, 319, 209, 303, 96, theme["ground"])
        for x in (192, 320):
            a.box(x, x, 208, 304, 82, 95, air=True)
            a.box(x, x, 208, 304, 92, 95, theme["stone"])
        for y in (208, 304):
            a.box(192, 320, y, y, 82, 95, air=True)
            a.box(192, 320, y, y, 92, 95, theme["stone"])
        for x in range(192, 321, 16):
            for y in (208, 304):
                a.box(x, x + 1, y, y + 1, 88, 96, theme["trim"])
        for y in (248, 264):
            a.slab(199, 313, y, y + 1, 96, theme["trim"])
        a.slab(252, 260, 212, 300, 96, shade(theme["stone"], -10))
        for x in (214, 298):
            for y in (246, 266):
                a.lamp(x, y)
        # Remove the old test boxes and unused repair wall before architecture.
        for x in (224, 248, 272, 296):
            for y in (225, 287):
                a.box(x, x + 5, y, y + 5, 90, 95, air=True)
        a.box(247, 265, 240, 240, 93, 95, air=True)
    if feature in ("village", "decoys", "arcade"):
        for x in (219, 275):
            for y in (214, 279):
                a.building(x, y, 17, 16, height=12 + (x % 3))
        if feature == "decoys":
            for x in (197, 300):
                for y in (235, 269):
                    a.building(x, y, 12, 10, height=9)
        else:
            for x in (200, 302):
                a.building(x, 235, 11, 11, height=10)
        for x in (241, 267):
            for y in (240, 269):
                a.crate(x, y, size=3)
        a.arch(249, 229)
        a.arch(249, 282)
        if feature == "village":
            data["hunt_points"] = [[x + .5, y + .5, 93.75] for x, y in
                                   ((223, 218), (232, 288), (280, 219), (287, 289),
                                    (205, 239), (307, 239), (244, 274), (264, 238))]
        if feature == "arcade":
            for x, y in ((239, 232), (264, 271)):
                a.building(x, y, 10, 10, height=8)
            for x in (244, 269):
                a.box(x, x + 1, 250, 253, 91, 95, theme["stone"])
        if feature == "village":
            for x, y in ((242, 216), (268, 296), (201, 282), (306, 218)):
                a.tree(x, y)
    elif feature in ("docks", "wharf", "scrap"):
        for x in (218, 273):
            a.building(x, 211, 21, 20, height=15)
            a.container(x + 2, 277, 0x9D6F4C if x == 218 else 0x527D8B)
        for x, y in ((242, 237), (263, 269), (201, 282), (301, 218)):
            a.crate(x, y)
        for x in (210, 300):
            # Crane has a broad mast and a connected cantilever, not a floating beam.
            a.box(x, x + 2, 294, 296, 69, 96, 0xB99A50)
            a.box(x - 12, x + 3, 294, 296, 69, 71, 0xCEAF61)
            a.box(x - 11, x - 11, 295, 295, 72, 83, 0x49525A)
        if feature == "wharf":
            for x in (235, 273):
                a.box(x, x + 3, 236, 243, 93, 95, theme["wood"])
        if feature == "scrap":
            for x in (210, 302):
                a.box(x - 3, x + 3, 252, 260, 96, 97, 0x3C535B)
                for y in (251, 261):
                    a.box(x - 3, x + 3, y, y, 91, 95, 0xAC8650)
    elif feature in ("outpost", "prison"):
        for x in (199, 291):
            for y in (218, 279):
                a.building(x, y, 21, 18, height=10)
        for x in (233, 273):
            for y in (233, 272):
                a.box(x, x + 8, y, y + 1, 93, 95, theme["wood"])
        if feature == "outpost":
            a.box(247, 265, 240, 241, 93, 95, theme["trim"])
            # Top row: exposed from either face, with solid footing beneath.
            # Removing a bottom row buried under a thick wall made the old job
            # impossible to understand or repair from the player's viewpoint.
            data["repair_cells"] = [[x, 240, 93] for x in range(254, 259)]
            a.slab(251, 262, 235, 238, 96, 0xE7B645)
            for x, y in ((229, 217), (282, 295), (240, 287), (274, 216)):
                a.tree(x, y)
        else:
            for x in (196, 315):
                for y in (239, 272):
                    a.arch(x - 3, y, width=7)
            for x in (204, 308):
                a.slab(x - 6, x + 6, 251, 261, 96, 0xD6B976)
    elif feature in ("courtyard", "duel", "paint", "gardens"):
        for x in (213, 291):
            for y in (223, 283):
                a.arch(x, y, width=10)
                if feature == "gardens":
                    a.tree(x + 4, y + (6 if y == 283 else -6))
                elif feature == "duel":
                    a.box(x, x + 10, y, y + 2, 90, 95, theme["stone"])
        if feature in ("courtyard", "duel"):
            for x, y in ((234, 243), (270, 265), (247, 275), (259, 233)):
                a.box(x, x + 7, y, y + 1, 92, 95, theme["stone"])
                a.crate(x + 2, y + 2, size=3)
        if feature == "paint":
            x0, x1, y0, y1, z = data["paint_bounds"]
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    world.set_point(x, y, z, 0x80000000 | (0xC6C0A7 if (x // 4 + y // 4) % 2 else 0x9EA99D))
            for y in (y0 - 1, y1 + 1):
                a.box(x0 - 1, x1 + 1, y, y, z, z, theme["trim"])
        if feature == "gardens":
            for r, z in ((9, 95), (7, 94)):
                a.box(256 - r, 256 + r, 256 - r, 256 + r, z, 96, theme["stone"])
            anchors["center"] = [256.5, 256.5, 91.75]
            for x in (231, 279):
                for y in (236, 274):
                    a.box(x, x + 2, y, y + 5, 93, 95, theme["foliage"])
    elif feature == "stadium":
        a.slab(197, 315, 230, 282, 96, 0x508265)
        for y in (231, 281):
            a.box(199, 313, y, y, 96, 96, 0xE1DBBC)
        a.box(256, 256, 231, 281, 96, 96, 0xE1DBBC)
        for x in (204, 308):
            for y in (250, 262):
                a.box(x, x, y, y, 88, 95, 0xECE1C1)
            a.box(x, x, 250, 262, 88, 88, 0xECE1C1)
        for row in range(5):
            for y in (214 + row * 3, 295 - row * 3):
                a.box(201, 311, y, y + 2, 86 + row * 2, 96, theme["stone"])
                a.slab(201, 311, y, y + 1, 86 + row * 2, 0x527A9F if y < 256 else 0x69905D)
    elif feature in ("race", "signals"):
        for x in (195, 323):
            a.box(x, x + 2, 222, 292, 91, 130, theme["stone"])
        for x in (205, 235, 265, 295, 315):
            a.arch(x - 5, 245 if x % 20 else 264, width=10, floor=83, height=11)
            # Every arch footing lands on its pad or a pier all the way down.
            for dx in (-5, -4, 5, 6):
                y = 245 if x % 20 else 264
                a.box(x + dx, x + dx, y, y + 1, 83, 130, theme["stone"])
        for x in range(207, 316, 18):
            a.box(x, x + 3, 225, 230, 86, 130, theme["stone"])
            a.box(x, x + 3, 282, 287, 86, 130, theme["stone"])
            a.lamp(x + 1, 226, floor=86)
        if feature == "signals":
            for x in (220, 250, 280, 310):
                a.box(x, x + 1, 270, 271, 75, 130, 0x48555D)
                for z, color in ((74, 0xDB6660), (76, 0xDACA62), (78, 0x76B485)):
                    a.box(x, x + 2, 268, 269, z, z + 1, color)
    elif feature == "bunker":
        # Subterranean branch halls stop short of caches: digging remains necessary.
        for x in (212, 300):
            a.box(x - 3, x + 3, 231, 280, 96, 101, air=True)
            for y in range(232, 281, 12):
                a.arch(x - 4, y, width=7, floor=102, height=7)
        for x0, x1 in ((215, 230), (280, 297)):
            a.box(x0, x1, 254, 260, 96, 101, air=True)
        for x in (234, 278):
            a.box(x, x + 2, 231, 285, 81, 82, theme["trim"])
        for x, y in ((238, 233), (254, 274), (270, 238)):
            a.lamp(x, y, floor=102)
    elif feature == "viaduct":
        for x in (195, 299):
            a.building(x, 224, 14, 15, height=12)
            a.building(x, 274, 14, 15, height=12)
        for x in (214, 297):
            for y in (225, 284):
                a.box(x, x + 1, y, y + 1, 70, 130, theme["stone"])
        a.arch(251, 263, width=9, floor=96, height=10)
    elif feature == "towers":
        for x in (216, 296):
            a.box(x - 6, x + 6, 250, 262, 76, 76, theme["roof"])
            # Restore the exposed target at its documented coordinate.
            a.box(x, x, 256, 256, 75, 75, 0xF0B840)
            for y in (232, 279):
                a.crate(x - 2, y, size=4)
        for y in (218, 289):
            a.building(245, y, 18, 10, height=13)
    elif feature == "floors":
        for z in (82, 90, 98):
            a.slab(209, 303, 225, 287, z, shade(theme["ground"], (z - 90) * 2))
            for x in range(212, 301, 8):
                a.box(x, x, 225, 287, z, z, theme["trim"])
        for x in (206, 305):
            for y in range(224, 290, 16):
                a.box(x, x + 1, y, y + 1, 74, 130, theme["stone"])
        data["play_bounds"] = [208, 305, 224, 289]
    elif feature == "citadel":
        for x in (230, 274):
            for y in (230, 274):
                a.building(x, y, 9, 9, height=17)
        for y in (218, 292):
            a.box(213, 227, y, y + 1, 92, 95, theme["stone"])
            a.box(285, 299, y, y + 1, 92, 95, theme["stone"])
        a.arch(248, 245, width=15, height=10)
    # Furnished secondary routes: small clusters create choices between
    # exposed central lanes and cover along the perimeter.
    if layout == "yard" and feature != "stadium":
        for x, y in ((210, 227), (299, 283), (238, 294), (268, 216)):
            if feature in ("paint", "gardens", "courtyard"):
                a.box(x - 1, x + 6, y - 1, y + 4, 95, 95, theme["stone"])
                a.box(x, x + 5, y, y + 3, 93, 94, theme["foliage"])
                a.tree(x + 2, y + 1)
            else:
                a.crate(x, y, size=3)
                a.crate(x + 4, y + 2, size=2)
    # Keep authored objective and spawn access clear, with ordinary walking lanes.
    if layout in ("yard", "towers", "fort"):
        for key in ("blue", "green", "exit", "fort", *[f"loot{i}" for i in range(1, 7)]):
            if feature == "gardens" and key == "fort":
                continue
            x, y, _ = anchors[key]
            a.box(int(x) - 3, int(x) + 3, int(y) - 3, int(y) + 3, 91, 95, air=True)
        if feature != "gardens":
            x, y, _ = anchors["center"]
            a.box(int(x) - 3, int(x) + 3, int(y) - 3, int(y) + 3, 91, 95, air=True)
    # All play surfaces now have independent foundations; these probes are used
    # by the generation audit for real native collapse and reachability checks.
    data.update(design_version=3, title=name, mode=next(k for k, d in DESIGNS.items() if d.name == name),
                bounds=[180, 332, 196, 316], theme=design.theme)
    payload.update(name=name, author="BattleSpades community arenas", vxl_format="retail", ugc_entities=a.lights,
                   skybox_texture=theme["sky"], fog_color=theme["fog"],
                   light_color=theme["light"], light_direction=[.3, .4, .85],
                   back_light_color=theme["ambient"], back_light_direction=[-.5, -.3, .7],
                   ambient_light_color=theme["ambient"], ambient_light_intensity=.65,
                   ambient_sounds=[[theme["sound"], [], .38, 0.0]],
                   ground_colors=[[78, 64, 51, 32], [58, 49, 40, 238], [70, 94, 105, 239]])
    return world, payload
