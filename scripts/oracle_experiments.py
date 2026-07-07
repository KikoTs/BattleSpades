"""oracle_experiments.py - extract ground-truth physics from the live game.

Creates a fresh aoslib.world.Player ("oracle") inside the running game via
world.create_object, configures known class multipliers, then runs scripted
deterministic scenarios by stepping p.update(dt, []) in-game and recording
every frame. Results are saved as JSON fixtures under logs/oracle/ which
scripts/replay_parity.py replays through OUR py3 physics engine to find
divergences.

The oracle is not auto-updated by the game loop (verified), so every step
is under our control and scenarios are exactly reproducible.

Prereqs: game running with physics_tracer v3 (console on 32896), player
spawned in-world (scripts/auto_join.py) so scene.world exists.

Usage:
    py scripts/auto_join.py             # once per game boot
    py scripts/oracle_experiments.py    # runs all scenarios, writes fixtures
    py scripts/oracle_experiments.py --only gravity_fall walk_accel_flat
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from game_console import GameConsole, ConsoleError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "logs" / "oracle"

DT = 1.0 / 60.0

# Soldier movement profile, set explicitly on the oracle so the replay knows
# exactly what was configured (values mirrored from shared.constants).
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

# In-game helper functions, exec'd once into the persistent console namespace.
BOOTSTRAP = r"""
from shared.glm import Vector3 as _V3

def orc_orient(p, x, y, z):
    p.set_orientation(_V3(x, y, z))

def orc_new():
    from aoslib.world import Player as _WP
    p = manager.scene.world.create_object(_WP)
    p.set_class_accel_multiplier(%(accel_multiplier)r)
    p.set_class_sprint_multiplier(%(sprint_multiplier)r)
    p.set_class_jump_multiplier(%(jump_multiplier)r)
    p.set_class_crouch_sneak_multiplier(%(crouch_sneak_multiplier)r)
    p.set_class_water_friction(%(water_friction)r)
    p.set_class_can_sprint_uphill(%(can_sprint_uphill)r)
    p.set_class_fall_on_water_damage_multiplier(%(fall_on_water_damage_multiplier)r)
    p.set_class_falling_damage_min_distance(%(falling_damage_min_distance)r)
    p.set_class_falling_damage_max_distance(%(falling_damage_max_distance)r)
    p.set_class_falling_damage_max_damage(%(falling_damage_max_damage)r)
    return p

def orc_state(p):
    return [p.position.x, p.position.y, p.position.z,
            p.velocity.x, p.velocity.y, p.velocity.z,
            1 if p.airborne else 0, 1 if p.wade else 0,
            1 if p.jump_this_frame else 0]

def orc_step(p, frames, dt):
    out = []
    for _i in range(int(frames)):
        r = p.update(dt, [])
        s = orc_state(p)
        s.append(r if r is not None else -999)
        out.append(s)
    return out

def orc_settle(p, max_frames=2400, dt=1.0/60.0):
    # Let the oracle fall until it lands and velocity calms down.
    for _i in range(int(max_frames)):
        p.update(dt, [])
        if not p.airborne and abs(p.velocity.z) < 1e-06:
            break
    for _i in range(120):
        p.update(dt, [])
    return orc_state(p)

def orc_surface_z(x, y):
    m = manager.client.map
    for z in range(0, 512):
        if m.get_solid(int(x), int(y), z):
            return z
    return None

def orc_find_dry_flat(cx, cy, radius=60, max_surface_z=230):
    # Scan around (cx, cy) for a dry column whose 3x3 neighborhood is flat.
    best = None
    for dx in range(-radius, radius + 1, 4):
        for dy in range(-radius, radius + 1, 4):
            x = int(cx) + dx
            y = int(cy) + dy
            z = orc_surface_z(x, y)
            if z is None or z > max_surface_z:
                continue
            flat = True
            for nx in (-2, 0, 2):
                for ny in (-2, 0, 2):
                    nz = orc_surface_z(x + nx, y + ny)
                    if nz != z:
                        flat = False
                        break
                if not flat:
                    break
            if flat:
                return (x + 0.5, y + 0.5, z)
    return best

state.orc_spot = orc_find_dry_flat(155, 346)
state.orc_ready = True
_ = repr(state.orc_spot)
"""

FRAME_FIELDS = ["x", "y", "z", "vx", "vy", "vz", "airborne", "wade",
                "jump_this_frame", "update_result"]


def run_eval(console: GameConsole, code: str):
    """Run code in-game, parse the repr'd result back into Python data."""
    result = console.run(code)
    try:
        return ast.literal_eval(result)
    except (ValueError, SyntaxError):
        return result


def bootstrap(console: GameConsole) -> str:
    spot = console.run(BOOTSTRAP % CLASS_SETUP)
    if spot in ("None", ""):
        raise ConsoleError("no dry flat spot found near search center")
    print(f"dry flat test spot: {spot}")
    return spot


# ---------------------------------------------------------------------------
# Scenarios. Each returns a dict fixture:
#   {name, dt, class_setup, setup: {...}, frames: [[...], ...]}
# All in-game stepping happens inside single console requests => atomic and
# deterministic (no interleaving with the game's own frames).
# ---------------------------------------------------------------------------

def scenario_gravity_fall(console):
    frames = run_eval(console, (
        "p = orc_new()\n"
        "p.set_position(60.0, 60.0, 100.0)\n"
        "p.set_velocity(0.0, 0.0, 0.0)\n"
        "orc_orient(p, 1.0, 0.0, 0.0)\n"
        "_ = orc_step(p, 240, 1.0/60.0)"))
    return {"setup": {"start": [60.0, 60.0, 100.0], "note": "free fall from air"},
            "frames": frames}


def scenario_friction_air(console):
    frames = run_eval(console, (
        "p = orc_new()\n"
        "p.set_position(60.0, 60.0, 100.0)\n"
        "p.set_velocity(2.0, 0.0, 0.0)\n"
        "orc_orient(p, 1.0, 0.0, 0.0)\n"
        "_ = orc_step(p, 120, 1.0/60.0)"))
    return {"setup": {"start": [60.0, 60.0, 100.0], "v0": [2.0, 0.0, 0.0],
                      "note": "horizontal velocity decay while airborne"},
            "frames": frames}


def scenario_friction_ground(console):
    frames = run_eval(console, (
        "p = orc_new()\n"
        "sx, sy, sz = state.orc_spot\n"
        "p.set_position(sx, sy, sz - 8.0)\n"
        "p.set_velocity(0.0, 0.0, 0.0)\n"
        "orc_orient(p, 1.0, 0.0, 0.0)\n"
        "landed = orc_settle(p)\n"
        "p.set_velocity(2.0, 0.0, 0.0)\n"
        "frames = orc_step(p, 120, 1.0/60.0)\n"
        "_ = {'landed': landed, 'frames': frames}"))
    return {"setup": {"landed_state": frames["landed"],
                      "v0": [2.0, 0.0, 0.0],
                      "note": "horizontal velocity decay while grounded"},
            "frames": frames["frames"]}


def scenario_walk_accel_flat(console):
    frames = run_eval(console, (
        "p = orc_new()\n"
        "sx, sy, sz = state.orc_spot\n"
        "p.set_position(sx, sy, sz - 8.0)\n"
        "p.set_velocity(0.0, 0.0, 0.0)\n"
        "orc_orient(p, 1.0, 0.0, 0.0)\n"
        "landed = orc_settle(p)\n"
        "p.set_walk(True, False, False, False)\n"
        "frames = orc_step(p, 240, 1.0/60.0)\n"
        "p.set_walk(False, False, False, False)\n"
        "_ = {'landed': landed, 'frames': frames}"))
    return {"setup": {"landed_state": frames["landed"],
                      "input": {"up": True},
                      "note": "walk forward from rest on terrain"},
            "frames": frames["frames"]}


def scenario_walk_stop(console):
    frames = run_eval(console, (
        "p = orc_new()\n"
        "sx, sy, sz = state.orc_spot\n"
        "p.set_position(sx, sy, sz - 8.0)\n"
        "p.set_velocity(0.0, 0.0, 0.0)\n"
        "orc_orient(p, 1.0, 0.0, 0.0)\n"
        "landed = orc_settle(p)\n"
        "p.set_walk(True, False, False, False)\n"
        "warm = orc_step(p, 180, 1.0/60.0)\n"
        "p.set_walk(False, False, False, False)\n"
        "frames = orc_step(p, 120, 1.0/60.0)\n"
        "_ = {'landed': landed, 'warm': warm, 'frames': frames}"))
    return {"setup": {"landed_state": frames["landed"],
                      "warmup_frames": frames["warm"],
                      "note": "release input after 180 walk frames; decay"},
            "frames": frames["frames"]}


def scenario_walk_diagonal(console):
    frames = run_eval(console, (
        "p = orc_new()\n"
        "sx, sy, sz = state.orc_spot\n"
        "p.set_position(sx, sy, sz - 8.0)\n"
        "p.set_velocity(0.0, 0.0, 0.0)\n"
        "orc_orient(p, 1.0, 0.0, 0.0)\n"
        "landed = orc_settle(p)\n"
        "p.set_walk(True, False, True, False)\n"
        "frames = orc_step(p, 240, 1.0/60.0)\n"
        "p.set_walk(False, False, False, False)\n"
        "_ = {'landed': landed, 'frames': frames}"))
    return {"setup": {"landed_state": frames["landed"],
                      "input": {"up": True, "left": True},
                      "note": "diagonal walk: forward+left"},
            "frames": frames["frames"]}


def scenario_walk_pitch_down(console):
    frames = run_eval(console, (
        "p = orc_new()\n"
        "sx, sy, sz = state.orc_spot\n"
        "p.set_position(sx, sy, sz - 8.0)\n"
        "p.set_velocity(0.0, 0.0, 0.0)\n"
        "orc_orient(p, 0.70710678, 0.0, 0.70710678)\n"
        "landed = orc_settle(p)\n"
        "p.set_walk(True, False, False, False)\n"
        "frames = orc_step(p, 240, 1.0/60.0)\n"
        "p.set_walk(False, False, False, False)\n"
        "_ = {'landed': landed, 'frames': frames}"))
    return {"setup": {"landed_state": frames["landed"],
                      "orientation": [0.70710678, 0.0, 0.70710678],
                      "input": {"up": True},
                      "note": "walk while looking 45deg down (pitch handling)"},
            "frames": frames["frames"]}


def scenario_crouch_probe(console):
    """Discover set_crouch signature, then capture crouch-walk."""
    sig = run_eval(console, (
        "import traceback\n"
        "p = orc_new()\n"
        "res = {}\n"
        "for args in [(True,), (True, False), (True, False, False)]:\n"
        "    try:\n"
        "        p.set_crouch(*args)\n"
        "        res[len(args)] = 'OK'\n"
        "        break\n"
        "    except TypeError as e:\n"
        "        res[len(args)] = str(e)\n"
        "state.orc_crouch_probe = p\n"
        "_ = res"))
    frames = run_eval(console, (
        "p = orc_new()\n"
        "sx, sy, sz = state.orc_spot\n"
        "p.set_position(sx, sy, sz - 8.0)\n"
        "p.set_velocity(0.0, 0.0, 0.0)\n"
        "orc_orient(p, 1.0, 0.0, 0.0)\n"
        "landed = orc_settle(p)\n"
        "try:\n"
        "    p.set_crouch(True, False)\n"
        "except TypeError:\n"
        "    try:\n"
        "        p.set_crouch(True)\n"
        "    except TypeError:\n"
        "        p.set_crouch(True, False, False)\n"
        "crouched = orc_state(p)\n"
        "p.set_walk(True, False, False, False)\n"
        "frames = orc_step(p, 240, 1.0/60.0)\n"
        "p.set_walk(False, False, False, False)\n"
        "_ = {'landed': landed, 'crouched': crouched, 'frames': frames}"))
    return {"setup": {"signature_probe": sig,
                      "landed_state": frames["landed"],
                      "state_after_crouch": frames["crouched"],
                      "input": {"up": True, "crouch": True},
                      "note": "crouch walk on terrain"},
            "frames": frames["frames"]}


SCENARIOS = {
    "gravity_fall": scenario_gravity_fall,
    "friction_air": scenario_friction_air,
    "friction_ground": scenario_friction_ground,
    "walk_accel_flat": scenario_walk_accel_flat,
    "walk_stop": scenario_walk_stop,
    "walk_diagonal": scenario_walk_diagonal,
    "walk_pitch_down": scenario_walk_pitch_down,
    "crouch_probe": scenario_crouch_probe,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None,
                    help="subset of scenario names")
    ap.add_argument("--wait", type=float, default=10.0)
    args = ap.parse_args()

    console = GameConsole()
    console.connect(wait_seconds=args.wait)
    ready = console.run("getattr(manager, 'scene', None) is not None and "
                        "getattr(manager.scene, 'world', None) is not None")
    if ready != "True":
        print("error: game has no scene.world yet â€” run scripts/auto_join.py first",
              file=sys.stderr)
        return 1

    spot = bootstrap(console)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    names = args.only or list(SCENARIOS)
    for name in names:
        fn = SCENARIOS[name]
        print(f"running scenario: {name} ...", end=" ", flush=True)
        t0 = time.monotonic()
        try:
            fixture = fn(console)
        except ConsoleError as exc:
            print(f"FAILED: {exc}")
            continue
        fixture.update({
            "name": name,
            "dt": DT,
            "class_setup": CLASS_SETUP,
            "frame_fields": FRAME_FIELDS,
            "dry_spot": spot,
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        out = OUT_DIR / f"{name}.json"
        out.write_text(json.dumps(fixture, indent=1), encoding="utf-8")
        n = len(fixture.get("frames") or [])
        print(f"ok ({n} frames, {time.monotonic()-t0:.1f}s) -> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

