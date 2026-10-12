"""Purpose-built race and Spleef arenas with independently supported scenery."""
from __future__ import annotations

from .arena_designs import Architect, THEMES


def payload(design, data, lights):
    theme = THEMES[design.theme]
    return dict(name=design.name, author="BattleSpades", vxl_format="retail", gravity=1.0,
                skybox_texture=theme["sky"], fog_color=theme["fog"], light_color=theme["light"],
                light_direction=[.3, .4, .85], ambient_light_color=theme["ambient"],
                ambient_light_intensity=.72, ambient_sounds=[[theme["sound"], [], .38, 0.0]],
                ugc_entities=lights,
                unofficial=dict(data, design_version=3, title=design.name, theme=design.theme))


def build_course(design):
    """800+ blocks of broad switchbacks, safe checkpoints and short lane gaps."""
    from .maps import grounded_foundation
    bounds = (140, 372, 140, 370)
    world = grounded_foundation(bounds)
    a = Architect(world, THEMES[design.theme])
    route, checkpoints = [], []
    for row, y in enumerate((160, 224, 288, 352)):
        xs = list(range(160, 353, 8))
        if row % 2:
            xs.reverse()
        # Single-voxel running surface. Structural beams run beside the lanes,
        # so a 2x2 cut opens air below the runner instead of exposing a second slab.
        a.slab(153, 359, y - 6, y + 6, 82, a.theme["ground"])
        for edge in (y - 6, y + 6):
            a.box(153, 359, edge, edge, 83, 85, a.theme["roof"])
            for x in range(154, 360, 24):
                a.box(x, x + 1, edge, edge, 86, 130)
        for x in (160, 256, 352):
            checkpoints.append([x + .5, y + .5, 79.75]) if not row % 2 else None
            a.slab(x - 6, x + 6, y - 6, y + 6, 82, a.theme["trim"])
            for dx in (-6, 6):
                for dy in (-6, 6):
                    a.box(x + dx, x + dx, y + dy, y + dy, 83, 130)
            # A high gate with feet on the side rails; the runner's aisle is clear.
            if x == 256:
                for yy in (y - 6, y + 6):
                    a.box(x, x, yy, yy, 70, 81)
                a.box(x, x, y - 6, y + 6, 70, 71, a.theme["trim"])
        if row % 2:
            checkpoints.extend([[x + .5, y + .5, 79.75] for x in (352, 256, 160)])
        # Never put a navigation arrival point inside a jump gap: the bot
        # must aim for the far landing, not turn back toward empty air.
        route.extend([[x + .5, y + .5, 79.75] for x in xs if x not in (224, 288)])
        # Alternating side gaps offer a jump or a clear lane around them.
        for index, x in enumerate((194, 224, 288, 318)):
            lo, hi = (y - 4, y) if index % 2 else (y, y + 4)
            a.box(x, x + 1, lo, hi, 82, 82, air=True)
        if row < 3:
            end = xs[-1]
            a.slab(end - 6, end + 6, y, y + 64, 82, a.theme["ground"])
            for edge in (end - 6, end + 6):
                a.box(edge, edge, y, y + 64, 83, 85, a.theme["roof"])
                for yy in range(y, y + 65, 16):
                    a.box(edge, edge, yy, yy + 1, 86, 130)
            route.extend([[end + .5, yy + .5, 79.75] for yy in range(y + 8, y + 64, 8)])
    # Low industrial scenery never connects underneath a running lane.
    for y in (188, 252, 316):
        for x in (188, 236, 284, 332):
            a.box(x, x + 7, y, y + 8, 105, 130)
            a.lamp(x + 3, y + 4, floor=105)
        a.box(150, 363, y + 13, y + 16, 128, 130, 0xC97737)
    a.box(142, 150, 142, 152, 82, 130)
    anchors = dict(blue=checkpoints[0], green=checkpoints[0], center=checkpoints[5],
                   waiting=[146.5, 147.5, 79.75], exit=checkpoints[-1])
    data = dict(layout="course", bounds=list(bounds), anchors=anchors, fall_z=103,
                checkpoints=checkpoints, bot_route=route, crumble_floor=82,
                mode="crumbling_run" if design.feature == "race" else "red_light_raid")
    return world, payload(design, data, a.lights)


def build_spleef(design):
    from .maps import grounded_foundation
    bounds = (204, 308, 204, 308)
    world = grounded_foundation(bounds)
    a = Architect(world, THEMES[design.theme])
    for z, color in ((82, 0xD0B169), (92, 0x73A4B8), (102, 0x8B7AAA)):
        a.slab(232, 280, 232, 280, z, color)
        for x in range(232, 281, 6):
            a.slab(x, x, 232, 280, z, a.theme["trim"])
        # Frames live outside the elimination boundary. Multiple piers support
        # every floor independently, including after most of the floor is cut.
        for edge in (231, 281):
            a.box(edge, edge, 231, 281, z, z + 1)
            a.box(231, 281, edge, edge, z, z + 1)
    for x in (230, 282):
        for y in range(230, 283, 13):
            a.box(x, x + 1, y, y + 1, 75, 130)
            a.lamp(x, y, floor=75)
    for y in (230, 282):
        for x in range(230, 283, 13):
            a.box(x, x + 1, y, y + 1, 75, 130)
    # Spectator terrace and grandstands are separate from the playable floors.
    a.box(209, 223, 244, 268, 81, 130)
    for i in range(4):
        a.box(237, 275, 211 + i * 3, 213 + i * 3, 80 + i * 2, 130)
        a.box(237, 275, 299 - i * 3, 301 - i * 3, 80 + i * 2, 130)
    anchors = dict(blue=[242.5, 250.5, 79.75], green=[270.5, 262.5, 79.75],
                   center=[256.5, 256.5, 79.75], waiting=[216.5, 256.5, 78.75])
    data = dict(layout="floors", bounds=list(bounds), anchors=anchors, fall_z=113,
                play_bounds=[232, 281, 232, 281], floor_levels=[82, 92, 102], mode="floor_tax")
    return world, payload(design, data, a.lights)


def build_survival(design):
    """Three approach lanes, an open repair yard and a rear evacuation pad."""
    from .maps import grounded_foundation
    bounds = (148, 364, 180, 332)
    world = grounded_foundation(bounds)
    a = Architect(world, THEMES[design.theme])
    a.box(152, 360, 184, 328, 96, 130, a.theme["ground"])
    for y in (216, 256, 296):
        a.slab(161, 351, y - 4, y + 4, 96, 0x85908B)
    for x in (152, 359):
        a.box(x, x + 1, 184, 328, 92, 96)
    for y in (184, 327):
        a.box(152, 360, y, y + 1, 92, 96)
    for y in (190, 302):
        a.building(180, y, 28, 20, height=13)
        a.building(310, y, 25, 20, height=16)
    for x in (235, 270, 303):
        for y in (231, 272):
            a.box(x, x + 9, y, y + 1, 93, 95, a.theme["wood"])
            a.crate(x + 12, y + 3, size=3)
    for x, y in ((223, 196), (256, 318), (283, 192), (344, 310), (165, 305)):
        a.tree(x, y)
    for x in (190, 220, 254, 288, 336):
        for y in (247, 266):
            a.lamp(x, y)
    # Short, grounded wall: every missing target cell is exposed above its
    # supporting row and a yellow apron directs players to the correct face.
    a.box(218, 230, 240, 240, 93, 95, 0xE7B645)
    a.slab(216, 232, 235, 238, 96, 0xE7B645)
    repair = [[x, 240, 93] for x in range(222, 227)]
    a.slab(159, 175, 248, 264, 96, 0xD0C79C)
    a.slab(164, 170, 248, 264, 96, 0x719DA9)
    a.slab(159, 175, 253, 259, 96, 0x719DA9)
    # Side observation towers use four broad piers, independent of the wall.
    for x, y in ((225, 310), (280, 198)):
        for dx in (0, 8):
            for dy in (0, 8):
                a.box(x + dx, x + dx + 1, y + dy, y + dy + 1, 78, 95)
        a.box(x, x + 9, y, y + 9, 77, 78, a.theme["roof"])
    anchors = dict(blue=[199.5, 256.5, 93.75], green=[346.5, 256.5, 93.75],
                   center=[225.5, 256.5, 93.75], exit=[167.5, 256.5, 93.75],
                   waiting=[163.5, 194.5, 93.75],
                   enemy_north=[344.5, 216.5, 93.75], enemy_south=[344.5, 296.5, 93.75])
    data = dict(layout="yard", bounds=list(bounds), anchors=anchors, fall_z=120,
                repair_cells=repair, mode="night_shift")
    return world, payload(design, data, a.lights)


def build_tag_arena(design):
    """Compact figure-eight chase routes with low vaults and open sightlines."""
    from .maps import grounded_foundation
    world = grounded_foundation((210, 302, 210, 302))
    a = Architect(world, THEMES[design.theme])
    a.box(218, 294, 218, 294, 96, 130, a.theme["ground"])
    for edge in (218, 294):
        a.box(edge, edge, 218, 294, 88, 96)
        a.box(218, 294, edge, edge, 88, 96)
    for x in (238, 270):
        a.box(x - 4, x + 4, 249, 263, 94, 95, a.theme["wood"])
        a.box(x - 3, x + 3, 252, 260, 91, 93)
    for y in (237, 275):
        a.box(249, 261, y, y + 1, 94, 95, a.theme["trim"])
    a.slab(252, 258, 220, 292, 96, a.theme["trim"])
    for x, y in ((226, 226), (286, 226), (226, 286), (286, 286)):
        a.tree(x, y)
        a.lamp(x + 3, y + 3)
    a.box(211, 216, 250, 262, 88, 130)
    spawns = [[x + .5, y + .5, 93.75] for x, y in
              ((248, 248), (264, 248), (264, 264), (248, 264),
               (256, 244), (256, 268), (246, 256), (264, 256))]
    for y in range(226, 288, 6):
        for x in range(226, 288, 6):
            point = [x + .5, y + .5, 93.75]
            if (point not in spawns and len(spawns) < 64
                    and all(not world.get_solid(x, y, z) for z in (93, 94, 95))):
                spawns.append(point)
    anchors = dict(blue=spawns[0], green=spawns[2], center=[256.5, 256.5, 93.75],
                   waiting=[213.5, 256.5, 85.75])
    data = dict(layout="yard", bounds=[210, 302, 210, 302], anchors=anchors,
                spawn_points=spawns, fall_z=110, mode="hot_potato")
    return world, payload(design, data, a.lights)
