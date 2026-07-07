# Physics Calibration — Oracle-Measured Ground Truth

All values below were extracted from the **live compiled game engine**
(`aceofspades_nonsteam/aoslib/world.pyd`) by running controlled experiments
inside the running game. The `aoslib-reversed` folder is a hand-written
reimplementation by a previous AI, NOT a true decompile — several of its
formulas are wrong. **This document supersedes it.**

## How the extraction works (reusable workflow)

1. `physics_tracer.py` (game folder) loads at game boot, wraps
   `GameManager.update` for a per-frame hook, captures every frame to
   `logs/physics_capture_<session>.ndjson`, and serves a **TCP eval console
   on 127.0.0.1:32896** that executes code on the game thread.
2. `scripts/auto_join.py` — connects + spawns fully autonomously.
3. `scripts/oracle_experiments.py` — creates a fresh `aoslib.world.Player`
   via `scene.world.create_object(Player)` (the "oracle": real physics, not
   auto-updated by the game loop, fully scriptable), runs scenarios, saves
   fixtures to `logs/oracle/*.json`.
4. `scripts/replay_parity.py` — replays fixtures through OUR py3
   `aoslib/world.pyx` and diffs frame-by-frame. **ALL PASS** required.
5. `scripts/game_console.py` — REPL/one-shot client for the in-game console.

Note: `world.create_object(Player)` only works inside the live game —
standalone py2 segfaults. The in-game console is the only reliable oracle.

## The movement model (per frame, dt = real frame time)

```
# inputs: up/down/left/right/jump/crouch/sneak/sprint (via Character.set_*)

# 1. jump (impulse REPLACES the gravity step this frame)
if jump and grounded:  vz = -0.36 * jump_multiplier        # then damped below

# 2. acceleration — selected multiplier * dt; crouch/sneak REPLACE,
#    sprint REPLACES with the sprint multiplier (do not stack with accel)
if (crouch and not wade) or sneak: a = crouch_sneak_multiplier
elif sprint and not burdened:      a = sprint_multiplier
else:                              a = accel_multiplier
if airborne: a *= 0.5                                       # stale flag (prev frame)
a *= dt
if (up or down) and (left or right): a *= sqrt(0.5)         # diagonal
velocity.xy += direction * a

# 3. gravity + vertical damping (skipped on the jump frame)
vz += dt * 1.0            # hover *0.75, jetpack *0.05, wade+crouch buoyancy
vz /= (1 + dt)

# 4. horizontal friction (divisor form), selected by the PREVIOUS frame's state
wade:     v /= (1 + water_friction * dt)    # class water_friction, 8.0 soldier
grounded: v /= (1 + 4.0 * dt)               # NOT 2.0 (reversed repo is wrong)
airborne: v /= (1 + 2.0 * dt)               # NOT 4.0

# 5. move: SINGLE-PASS boxclipmove (NOT substepped) — see the IDA decompile of
#    aoslib.world.so @0x3e90, ported verbatim into aoslib/world.pyx _move_box and
#    pinned offline by scripts/replay_movebox.py (logs/oracle/movebox_probes.json,
#    every probe within 1e-4 of the live client). Per frame, in order:
#      X section -> X glide pass -> Y section (glided z) -> Y glide pass -> finalize
#    - horizontal probe = 4 corners (±0.45) at the lp feet heights ONLY (feet,
#      feet-1, feet-2 standing); the head block is never probed by a lateral move;
#      no epsilons; <int> truncation (== floor for the +x/+y coords).
#    - climb gate = not crouch AND not hover AND (not sprint OR can_sprint_uphill).
#      There is NO orientation.z gate and NO wade gate (the old `orientation.z<0.5`
#      was an aoslib-reversed inheritance, never measured — REMOVED).
#    - climb / penetration push-out = per blocked axis, glide = (4·v_axis² + 0.05)
#      ·dt·32 with vz=0; BOTH axes fire for a straight climb -> the measured +0.1.
#      A glide frame SKIPS the vertical move entirely (z frozen, airborne held).
#    - a blocked vertical move keeps the EXACT frame-start z (landing never
#      partially advances — the fix for the old 0.57-block hard-landing error).
#    - velocity is zeroed only on a one-block-up climb-probe hit or a glide
#      head-revert, never unconditionally on a blocked wall.

# 5b. idle ledge-lip nudge (check_for_ground_holes @0x2290): when grounded and
#    pressing no movement key but floor(feet+1.0) under own column is empty, the
#    engine OVERWRITES horizontal velocity toward the hole centre by offset·dt·5
#    (offset <= 0.2, straight or one-resolved-diagonal neighbour solid). Ported to
#    _check_for_ground_holes; without it the server stands still where the client
#    creeps -> reconciliation drags the player into the cliff ("stuck on edges").

# 6. flags (owned by boxclipmove, exactly as the compiled engine)
airborne: set True before the vertical probe; cleared on a downward landing or a
          climb; HELD through a glide frame.  (the old start-of-frame grounded
          probe with epsilon 0.00875 is retired; the move owns the flag now.)
wade: written ONLY on a landing frame = (frame-start z > 237.0). Equivalent to the
      live-measured feet>=239 bracket (238.99 dry / 239.99 wades) but exact.
```

## Key constants (all measured)

| Quantity | Value |
|---|---|
| gravity | 1.0, damped `vz=(vz+dt)/(1+dt)` |
| physics scale | 32.0 (displacement = v·dt·32) |
| ground friction divisor | `1 + 4·dt` |
| air friction divisor | `1 + 2·dt` |
| wade friction divisor | `1 + water_friction·dt` (class, 8.0) |
| jump impulse | `-0.36 × jump_multiplier`, replaces gravity that frame |
| climb/glide velocity | per blocked axis `4·v² + 0.05`, ·dt·32, zeroes vz (both axes -> measured `4·v²+0.1` straight-on) |
| grounded probe epsilon | 0.00875 (≈ one first-frame gravity step) |
| crouch z shift | +0.9 (anchor down) |
| contact offsets | standing 2.25, crouching 1.35 |
| diagonal accel factor | √½ per axis |
| airborne accel factor | 0.5 |
| terminal speeds (soldier, scale 1.40625) | walk 7.875 b/s, derived `a/(F·dt)·32` |

## The InitialInfo speed-scale discovery (critical!)

`InitialInfo.movement_speed_multipliers` is indexed **by class id** on the
client (selectTeam.py etc.) and is a **scale applied to the client's local
class constants** (gameClass.py):

```
accel_eff  = CLASS_ACCEL_MULTIPLIER[id]  * scale     # 0.7  * 1.40625
sprint_eff = CLASS_SPRINT_MULTIPLIER[id] * scale     # 1.4  * 1.40625
crouch_eff = CLASS_CROUCH_SNEAK[id]      * scale
jump: NOT scaled (measured -0.36*1.2 exactly)
```

The wire encodes in 1/64 steps (1.4 → 1.40625). **The server simulation
must use the wire-rounded scaled values** (server/player.py
`_apply_class_profile_to_world` + server/class_data.py `speed_scale`) or
client prediction drifts and rubber-bands.

## Verification status (2026-06-12)

- `py scripts/replay_parity.py` — ALL PASS (8 scenarios, sub-mm except one
  8mm terrain-edge transient in walk_diagonal).
- Live (server-authoritative): mean client/server position delta **4.4 mm**
  over 13k frames; 7 outliers >0.5 (manual teleports).
- `movement_authority = "server"` re-enabled in config.toml.

## Re-running after physics changes

```
# 1. stop the server (it locks the .pyds), rebuild
py setup.py build_ext --inplace
# 2. offline check (no game needed)
py scripts/replay_parity.py
# 3. live re-capture if world.pyd behavior questions arise:
py run_server.py                  # terminal 1
.\python\python.exe run.py +debug +connect 127.0.0.1:27015   # game folder
py scripts/auto_join.py
py scripts/oracle_experiments.py  # refresh fixtures
```
