"""replay_movebox.py - replay the in-game move_box probes through OUR engine.

scripts/probe_movebox_ingame.py built a clean arena in the live game and ran
single-variable _move_box probes against the REAL compiled physics, saving the
results to logs/oracle/movebox_probes.json. replay_parity.py SKIPS that file
(it has no top-level "frames" key), so the cliff / edge / 1-block-step / hard-
landing / penetration behaviours it captures were never gated.

This harness rebuilds the identical arena on a synthetic VXL, replays each probe
program exactly as probe_movebox_ingame.py did (same fresh()/settle/velocity/
walk), and diffs every frame against the oracle. It is the offline ground truth
for the boxclipmove rewrite (cliffs + 1-block climb) — the live game is not
needed.

Usage:
    py scripts/replay_movebox.py            # all probes
    py scripts/replay_movebox.py --show     # print per-frame divergence
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aoslib.vxl import VXL  # noqa: E402
from aoslib.world import World, Player as NativePlayer  # noqa: E402

FIXTURE = ROOT / "logs" / "oracle" / "movebox_probes.json"
DT = 1.0 / 60.0
RGBA = (120, 120, 120, 255)

# Soldier class setup (identical to the walk_* oracle fixtures; orc_new() in the
# game uses the soldier class, and these probes were captured with it).
CLASS_SETUP = {
    "accel_multiplier": 0.7,
    "sprint_multiplier": 1.4,
    "jump_multiplier": 1.2,
    "crouch_sneak_multiplier": 0.5,
    "water_friction": 8.0,
    "can_sprint_uphill": True,
    "fall_on_water_damage_multiplier": 1.0,
    "falling_damage_min_distance": 3,
    "falling_damage_max_distance": 10,
    "falling_damage_max_damage": 100,
}


def build_arena() -> World:
    """platform x40..59 y40..59 z150; wall x52 y40..59 z146..149;
    step x48 y40..59 z149 — exactly probe_movebox_ingame.py's arena."""
    m = VXL(-1, b"", 0, 2)
    for x in range(40, 60):
        for y in range(40, 60):
            m.set_point(x, y, 150, RGBA)
    for y in range(40, 60):
        for z in range(146, 150):
            m.set_point(52, y, z, RGBA)
        m.set_point(48, y, 149, RGBA)
    assert m.get_solid(45, 45, 150) and m.get_solid(52, 45, 147)
    assert m.get_solid(48, 45, 149) and not m.get_solid(45, 45, 149)
    return World(m)


def make_player(world: World) -> NativePlayer:
    p = NativePlayer(world)
    p.set_class_accel_multiplier(CLASS_SETUP["accel_multiplier"])
    p.set_class_sprint_multiplier(CLASS_SETUP["sprint_multiplier"])
    p.set_class_jump_multiplier(CLASS_SETUP["jump_multiplier"])
    p.set_class_crouch_sneak_multiplier(CLASS_SETUP["crouch_sneak_multiplier"])
    p.set_class_water_friction(CLASS_SETUP["water_friction"])
    p.set_class_can_sprint_uphill(CLASS_SETUP["can_sprint_uphill"])
    p.set_class_fall_on_water_damage_multiplier(
        CLASS_SETUP["fall_on_water_damage_multiplier"])
    p.set_class_falling_damage_min_distance(
        CLASS_SETUP["falling_damage_min_distance"])
    p.set_class_falling_damage_max_distance(
        CLASS_SETUP["falling_damage_max_distance"])
    p.set_class_falling_damage_max_damage(
        CLASS_SETUP["falling_damage_max_damage"])
    return p


def st(p) -> list:
    return [p.position.x, p.position.y, p.position.z,
            p.velocity.x, p.velocity.y, p.velocity.z,
            1 if p.airborne else 0, 1 if p.wade else 0]


def fresh(world, x, y, z, vx=0.0, vy=0.0, vz=0.0, ox=1.0, oy=0.0, oz=0.0,
          walk=None, settle=0):
    p = make_player(world)
    p.set_orientation((ox, oy, oz))
    p.set_position(x, y, z)
    p.set_velocity(0.0, 0.0, 0.0)
    for _i in range(int(settle)):
        p.update(DT, [])
    p.set_velocity(vx, vy, vz)
    if walk is not None:
        p.set_walk(*walk)
    return p


def run_probes() -> dict:
    world = build_arena()
    res = {"arena_ok": True}

    # A: mid-air wall hit, no floor nearby
    p = fresh(world, 51.0, 45.5, 145.0, vx=0.5)
    p.update(DT, [])
    res["A_wall_midair_1f"] = st(p)

    # B: grounded slide toward wall on platform floor (settle first)
    p = fresh(world, 50.5, 45.5, 146.0, settle=240)
    res["B_settled"] = st(p)
    p.set_velocity(0.5, 0.0, 0.0)
    out = []
    for _i in range(8):
        p.update(DT, [])
        out.append(st(p))
    res["B_wall_grounded"] = out

    # C: walk into the 1-block step at x=48 (from x=46, on platform)
    p = fresh(world, 46.5, 45.5, 146.0, settle=240, walk=(True, False, False, False))
    out = []
    for _i in range(150):
        p.update(DT, [])
        out.append(st(p))
    res["C_step_up_walk"] = [out[0], out[1]] + out[40:90:5] + [out[-1]]
    res["C_step_full"] = out

    # D: walk off the platform edge at x=59 -> open air
    p = fresh(world, 57.5, 45.5, 146.0, settle=240, walk=(True, False, False, False))
    out = []
    for _i in range(180):
        p.update(DT, [])
        out.append(st(p))
    res["D_walk_off_edge_full"] = out

    # E: lateral move with feet deliberately inside the floor (penetration)
    p = fresh(world, 45.5, 45.5, 150.0 - 2.25 + 0.03, vy=-0.2)
    p.update(DT, [])
    res["E_penetration_lateral_1f"] = st(p)

    # F: penetration push-out with zero velocity
    p = fresh(world, 45.5, 45.5, 150.0 - 2.25 + 0.03)
    p.update(DT, [])
    res["F_penetration_still_1f"] = st(p)

    # G: hard landing (fast fall onto platform)
    p = fresh(world, 45.5, 45.5, 140.0, vz=1.5)
    out = []
    for _i in range(12):
        p.update(DT, [])
        out.append(st(p))
    res["G_hard_landing"] = out
    return res


def is_seq(v) -> bool:
    return isinstance(v, list) and v and isinstance(v[0], list)


def diff_one(name, oracle, ours, pos_eps, vel_eps, show):
    """Return (passed, worst_pos, worst_vel, flag_frames)."""
    if not is_seq(oracle):
        oracle, ours = [oracle], [ours]
    n = min(len(oracle), len(ours))
    worst_pd = worst_vd = 0.0
    flag_bad = 0
    first_bad = None
    for i in range(n):
        o, u = oracle[i], ours[i]
        pd = math.dist(o[0:3], u[0:3])
        vd = math.dist(o[3:6], u[3:6])
        worst_pd = max(worst_pd, pd)
        worst_vd = max(worst_vd, vd)
        if int(o[6]) != int(u[6]) or int(o[7]) != int(u[7]):
            flag_bad += 1
        if first_bad is None and (pd > pos_eps or vd > vel_eps):
            first_bad = i
    passed = first_bad is None and flag_bad == 0
    if show and not passed:
        lo = 0 if first_bad is None else max(0, first_bad - 1)
        for i in range(lo, min(n, lo + show + 1)):
            o, u = oracle[i], ours[i]
            print(f"    f{i:>3} oracle ({o[0]:.4f},{o[1]:.4f},{o[2]:.4f}) "
                  f"v=({o[3]:.4f},{o[4]:.4f},{o[5]:.4f}) air={o[6]} wade={o[7]}")
            print(f"         ours   ({u[0]:.4f},{u[1]:.4f},{u[2]:.4f}) "
                  f"v=({u[3]:.4f},{u[4]:.4f},{u[5]:.4f}) air={u[6]} wade={u[7]}")
    return passed, worst_pd, worst_vd, flag_bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos-eps", type=float, default=0.01)
    ap.add_argument("--vel-eps", type=float, default=2e-3)
    ap.add_argument("--show", type=int, default=0)
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args()

    oracle = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ours = run_probes()

    keys = [k for k in oracle if k != "arena_ok"]
    if args.only:
        keys = [k for k in keys if k in args.only]

    all_pass = True
    for k in keys:
        passed, pd, vd, fb = diff_one(
            k, oracle[k], ours.get(k), args.pos_eps, args.vel_eps, args.show)
        if not passed:
            all_pass = False
        status = "PASS" if passed else "FAIL"
        print(f"{k:<26} {status:<5} max_pos_d={pd:.5f} max_vel_d={vd:.5f} "
              f"flag_mismatch={fb}")

    print()
    print("ALL PASS" if all_pass else "DIVERGENCE — boxclipmove port not faithful")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
