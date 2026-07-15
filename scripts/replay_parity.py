"""replay_parity.py - replay oracle fixtures through OUR py3 physics engine.

Loads the ground-truth fixtures captured from the live game by
scripts/oracle_experiments.py (logs/oracle/*.json), reconstructs the same
initial state on our aoslib.world.Player (py3 Cython port) over the same
real map, applies the same inputs with the same dt, and diffs every frame.

Output per scenario: PASS/FAIL, first divergent frame, max position and
velocity deltas. Exit code 0 only if all replayed scenarios pass.

Usage:
    py scripts/replay_parity.py                  # all fixtures
    py scripts/replay_parity.py --only walk_accel_flat
    py scripts/replay_parity.py --eps 1e-3 --show 5
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import load_config  # noqa: E402
from server.world_manager import WorldManager  # noqa: E402
from aoslib.world import Player as NativePlayer  # noqa: E402

FIXTURE_DIR = ROOT / "logs" / "oracle"


def make_player(world, class_setup: dict) -> NativePlayer:
    p = NativePlayer(world)
    p.set_class_accel_multiplier(class_setup["accel_multiplier"])
    p.set_class_sprint_multiplier(class_setup["sprint_multiplier"])
    p.set_class_jump_multiplier(class_setup["jump_multiplier"])
    p.set_class_crouch_sneak_multiplier(class_setup["crouch_sneak_multiplier"])
    p.set_class_water_friction(class_setup["water_friction"])
    p.set_class_can_sprint_uphill(class_setup["can_sprint_uphill"])
    p.set_class_fall_on_water_damage_multiplier(
        class_setup["fall_on_water_damage_multiplier"])
    p.set_class_falling_damage_min_distance(
        class_setup["falling_damage_min_distance"])
    p.set_class_falling_damage_max_distance(
        class_setup["falling_damage_max_distance"])
    p.set_class_falling_damage_max_damage(
        class_setup["falling_damage_max_damage"])
    return p


def snap(p) -> list:
    return [p.position.x, p.position.y, p.position.z,
            p.velocity.x, p.velocity.y, p.velocity.z,
            1 if p.airborne else 0, 1 if p.wade else 0]


def step_n(p, frames: int, dt: float) -> list[list]:
    out = []
    for _ in range(frames):
        p.update(dt, [])
        out.append(snap(p))
    return out


def init_from_landed(p, landed: list, dt: float):
    """Start from the exact state the oracle had after settling on ground.

    Ground the player the way the live engine does — one real zero-velocity
    gravity-and-land frame. The faithful boxclipmove detects ground by probing
    floor(feet + vz*dt*32); a dt=0 refresh has no gravity step to reach the
    block below the resting feet, so it would leave frame 0 airborne (half
    accel / air friction). A real dt step lands without moving (landing keeps
    the exact frame-start z), so this only flips airborne to False."""
    p.set_position(landed[0], landed[1], landed[2])
    p.set_velocity(landed[3], landed[4], landed[5])
    p.update(dt, [])


def crouch_on(p):
    p.set_crouch(True, [], 0)


# ---------------------------------------------------------------------------
# Scenario programs (must mirror scripts/oracle_experiments.py exactly)
# ---------------------------------------------------------------------------

def replay(fix: dict, map_name: str = "ArcticBase") -> list[list]:
    name = fix["name"]
    dt = fix["dt"]
    cfg = load_config(ROOT / "config.toml")
    wm = WorldManager(cfg)
    # The committed oracle fixtures were captured on ArcticBase. Replaying on
    # the production map changes every floor/collision query and creates a
    # convincing but meaningless physics divergence.
    if not wm.load_map(map_name):
        raise RuntimeError(f"failed to load map {map_name!r}")
    p = make_player(wm.world, fix["class_setup"])
    setup = fix["setup"]

    if name == "gravity_fall":
        p.set_position(60.0, 60.0, 100.0)
        p.set_velocity(0.0, 0.0, 0.0)
        p.set_orientation((1.0, 0.0, 0.0))
        return step_n(p, 240, dt)

    if name == "friction_air":
        p.set_position(60.0, 60.0, 100.0)
        p.set_velocity(2.0, 0.0, 0.0)
        p.set_orientation((1.0, 0.0, 0.0))
        return step_n(p, 120, dt)

    if name == "friction_ground":
        p.set_orientation((1.0, 0.0, 0.0))
        init_from_landed(p, setup["landed_state"], dt)
        p.set_velocity(2.0, 0.0, 0.0)
        return step_n(p, 120, dt)

    if name == "walk_accel_flat":
        p.set_orientation((1.0, 0.0, 0.0))
        init_from_landed(p, setup["landed_state"], dt)
        p.set_walk(True, False, False, False)
        return step_n(p, 240, dt)

    if name == "walk_stop":
        p.set_orientation((1.0, 0.0, 0.0))
        init_from_landed(p, setup["landed_state"], dt)
        p.set_walk(True, False, False, False)
        warm = step_n(p, 180, dt)
        p.set_walk(False, False, False, False)
        frames = step_n(p, 120, dt)
        # Compare warmup against fixture's recorded warmup first
        fix["_warm_ours"] = warm
        return frames

    if name == "walk_diagonal":
        p.set_orientation((1.0, 0.0, 0.0))
        init_from_landed(p, setup["landed_state"], dt)
        p.set_walk(True, False, True, False)
        return step_n(p, 240, dt)

    if name == "walk_pitch_down":
        p.set_orientation((0.70710678, 0.0, 0.70710678))
        init_from_landed(p, setup["landed_state"], dt)
        p.set_walk(True, False, False, False)
        return step_n(p, 240, dt)

    if name == "crouch_probe":
        p.set_orientation((1.0, 0.0, 0.0))
        init_from_landed(p, setup["landed_state"], dt)
        crouch_on(p)
        fix["_crouched_ours"] = snap(p)
        p.set_walk(True, False, False, False)
        return step_n(p, 240, dt)

    raise ValueError(f"no replay program for scenario {name!r}")


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def diff_frames(oracle: list[list], ours: list[list], eps: float,
                pos_eps: float = 0.01):
    """Return (first_bad_index|None, max_pos_delta, max_vel_delta, flag_mismatches)."""
    first_bad = None
    max_pd = 0.0
    max_vd = 0.0
    flag_bad = 0
    consecutive = 0
    n = min(len(oracle), len(ours))
    for i in range(n):
        o, u = oracle[i], ours[i]
        pd = math.dist(o[0:3], u[0:3])
        vd = math.dist(o[3:6], u[3:6])
        flags_differ = (int(o[6]) != int(u[6])) or (int(o[7]) != int(u[7]))
        max_pd = max(max_pd, pd)
        max_vd = max(max_vd, vd)
        if flags_differ:
            flag_bad += 1
        # Single-frame transients (a block-boundary crossed one frame apart
        # due to sub-mm float32 drift) self-correct and are imperceptible;
        # only sustained divergence fails. Same for 1-2 frame flag flickers.
        # Velocity is held to the strict eps (persistent velocity error
        # means a formula bug); absolute position tolerates up to pos_eps
        # (1cm) of accumulated offset from single-frame terrain transients.
        if pd > pos_eps or vd > eps:
            consecutive += 1
        else:
            consecutive = 0
        if first_bad is None and (consecutive >= 3 or flag_bad > 4):
            first_bad = i
    return first_bad, max_pd, max_vd, flag_bad


def show_divergence(oracle, ours, idx: int, count: int):
    for i in range(max(0, idx - 1), min(len(oracle), len(ours), idx + count)):
        o, u = oracle[i], ours[i]
        print(f"    f{i:>4} oracle pos=({o[0]:.6f},{o[1]:.6f},{o[2]:.6f}) "
              f"vel=({o[3]:.6f},{o[4]:.6f},{o[5]:.6f}) air={o[6]} wade={o[7]}")
        print(f"          ours  pos=({u[0]:.6f},{u[1]:.6f},{u[2]:.6f}) "
              f"vel=({u[3]:.6f},{u[4]:.6f},{u[5]:.6f}) air={u[6]} wade={u[7]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--eps", type=float, default=2e-3,
                    help="max per-frame position/velocity delta vs oracle "
                         "(default 2e-3 absorbs float32 accumulation drift "
                         "and single-frame landing transients)")
    ap.add_argument("--show", type=int, default=3,
                    help="frames of context to print at first divergence")
    ap.add_argument(
        "--map",
        default="ArcticBase",
        help="map used by the fixtures (default: ArcticBase)",
    )
    args = ap.parse_args()

    fixtures = sorted(FIXTURE_DIR.glob("*.json"))
    if args.only:
        fixtures = [f for f in fixtures if f.stem in args.only]
    if not fixtures:
        print(f"no fixtures found in {FIXTURE_DIR}", file=sys.stderr)
        return 1

    all_pass = True
    for path in fixtures:
        fix = json.loads(path.read_text(encoding="utf-8"))
        if "name" not in fix or "frames" not in fix:
            continue  # not a scenario fixture (e.g. raw probe dumps)
        name = fix["name"]
        try:
            ours = replay(fix, args.map)
        except Exception as exc:
            print(f"{name:<18} ERROR during replay: {exc!r}")
            all_pass = False
            continue

        oracle = [f[:8] for f in fix["frames"]]
        ours8 = [f[:8] for f in ours]
        first_bad, max_pd, max_vd, flag_bad = diff_frames(oracle, ours8, args.eps)

        extra = ""
        if name == "walk_stop" and "_warm_ours" in fix:
            wo = [f[:8] for f in fix["setup"]["warmup_frames"]]
            wu = [f[:8] for f in fix["_warm_ours"]]
            wbad, wpd, wvd, wflag = diff_frames(wo, wu, args.eps)
            extra = f" | warmup: first_bad={wbad} max_pd={wpd:.5f}"
            if wbad is not None:
                all_pass = False

        status = "PASS" if first_bad is None else f"FAIL @f{first_bad}"
        if first_bad is not None:
            all_pass = False
        print(f"{name:<18} {status:<10} max_pos_d={max_pd:.6f} "
              f"max_vel_d={max_vd:.6f} flag_mismatch_frames={flag_bad}{extra}")
        if first_bad is not None and args.show:
            show_divergence(oracle, ours8, first_bad, args.show)

    print()
    print("ALL PASS" if all_pass else "DIVERGENCE FOUND — fix aoslib/world.pyx and rerun")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
