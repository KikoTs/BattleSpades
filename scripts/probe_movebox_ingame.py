# -*- coding: utf-8 -*-
# In-game probe (py2, exec'd via game_console --file): builds a clean arena
# in empty sky and runs single-variable _move_box probes against the real
# compiled physics. Returns a dict of results.
#
# Arena layout (far from terrain, ocean air at z~150):
#   platform: x 40..59, y 40..59 solid at z=150 (floor surface = 150)
#   wall:     x=52, y 40..59, z=146..149 (4 high, blocks +x movement)
#   step1:    x=48, y 40..59, z=149 (1-block step up)
m = manager.client.map
RGBA = (120, 120, 120, 255)

def put(x, y, z):
    if not m.get_solid(x, y, z):
        m.add_point(x, y, z, RGBA)

def clr(x, y, z):
    if m.get_solid(x, y, z):
        m.remove_point(x, y, z)

# --- build arena ---
for x in range(40, 60):
    for y in range(40, 60):
        put(x, y, 150)
for y in range(40, 60):
    for z in range(146, 150):
        put(52, y, z)        # wall (top at 146, floor at 150)
    put(48, y, 149)          # 1-block step

# sanity
arena_ok = (m.get_solid(45, 45, 150) and m.get_solid(52, 45, 147)
            and m.get_solid(48, 45, 149) and not m.get_solid(45, 45, 149))

DT = 1.0 / 60.0
res = {'arena_ok': bool(arena_ok)}

def st(p):
    return [p.position.x, p.position.y, p.position.z,
            p.velocity.x, p.velocity.y, p.velocity.z,
            1 if p.airborne else 0, 1 if p.wade else 0]

def fresh(x, y, z, vx=0.0, vy=0.0, vz=0.0, ox=1.0, oy=0.0, oz=0.0,
          walk=None, settle=0):
    p = orc_new()
    orc_orient(p, ox, oy, oz)
    p.set_position(x, y, z)
    p.set_velocity(0.0, 0.0, 0.0)
    for _i in range(int(settle)):
        p.update(DT, [])
    p.set_velocity(vx, vy, vz)
    if walk is not None:
        p.set_walk(*walk)
    return p

GROUND_Z = 150.0 - 2.25 - 0.0004  # approx grounded anchor on platform

# A: mid-air wall hit, no floor nearby (feet at z 147.3, wall at x=52)
p = fresh(51.0, 45.5, 145.0, vx=0.5)
p.update(DT, [])
res['A_wall_midair_1f'] = st(p)

# B: grounded slide toward wall on platform floor (settle first)
p = fresh(50.5, 45.5, 146.0, settle=240)
res['B_settled'] = st(p)
p.set_velocity(0.5, 0.0, 0.0)
out = []
for _i in range(8):
    p.update(DT, [])
    out.append(st(p))
res['B_wall_grounded'] = out

# C: walk into the 1-block step at x=48 (from x=46, on platform)
p = fresh(46.5, 45.5, 146.0, settle=240, walk=(True, False, False, False))
out = []
for _i in range(150):
    p.update(DT, [])
    out.append(st(p))
res['C_step_up_walk'] = [out[0], out[1]] + out[40:90:5] + [out[-1]]
res['C_step_full'] = out

# D: walk off the platform edge at x=59 -> open air (step-down/ledge case)
p = fresh(57.5, 45.5, 146.0, settle=240, walk=(True, False, False, False))
out = []
for _i in range(180):
    p.update(DT, [])
    out.append(st(p))
res['D_walk_off_edge_full'] = out

# E: lateral move with feet deliberately inside the floor (penetration)
p = fresh(45.5, 45.5, 150.0 - 2.25 + 0.03, vy=-0.2)   # feet 0.03 into floor
p.update(DT, [])
res['E_penetration_lateral_1f'] = st(p)

# F: penetration push-out with zero velocity
p = fresh(45.5, 45.5, 150.0 - 2.25 + 0.03)
p.update(DT, [])
res['F_penetration_still_1f'] = st(p)

# G: hard landing (fast fall onto platform) - landing z resolution
p = fresh(45.5, 45.5, 140.0, vz=1.5)
out = []
for _i in range(12):
    p.update(DT, [])
    out.append(st(p))
res['G_hard_landing'] = out

_ = res
