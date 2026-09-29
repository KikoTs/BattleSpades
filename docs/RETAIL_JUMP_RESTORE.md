# Retail jump rollback: the launch-frame position restore

Measured and fixed 2026-09-24 against the stock `aoslib/character.pyd`
(SHA-256 `52ec520d83fe9e0e…`, byte-identical in the Revival release client,
the tracer-instrumented dev client and the Steam build).

## Symptom

Every jump on the retail client produced one soft correction (native
`position_lerp_timer` re-arm) even on a loss-free local link, and a held jump
that re-fired after mantling a ledge yanked the player back down by more than
a block. Kiril reported this as "jumping rolls back a bit" and "slowdown while
falling after a jump". The BattleSpades native client, which owns its own
prediction, did not show it.

Gate: `scripts/scenarios/movement_stress.py` with the Python jump-smoothing
wrapper disabled (`AOS_DISABLE_CHARACTER_JUMP_SMOOTHING=1`), segments
`settle,sprint,jump_in_place,jump_run,turn_left`, Soldier, local server.

| Client binary | ADJUST | SNAP | max matched error |
| --- | ---: | ---: | ---: |
| stock character.pyd | 8 (one per jump) | 0 | 0.187 blocks |
| patched character.pyd | 0 | 0 | 0.0014 blocks |

## Mechanism (IDA, `Character.update_alive`, character.pyx line 1169-1195)

```
if self.world_object.jump_this_frame:
    if self.jump_sound_repeat_timer <= 0.0:
        ...play jump sound...
    self.world_object.set_position(self.network_position.x,
                                   self.network_position.y,
                                   self.network_position.z)      # line 1184
else:
    if self.main:
        self.apply_player_network_correction(dt, players)
    else:
        self.apply_interpolations(dt)
self.update_jetpack(dt)
```

`network_position` is the last WorldUpdate self row the client received. On
the exact frame the native mover launches a jump, the client therefore keeps
the launch velocity but resets its position to a row that is several frames
old (more with ping), discarding that frame's own displacement. The
authoritative server simulates the same launch and keeps the displacement, so
it is one frame ahead in the arc; the next self row is then 0.15-0.19 blocks
away from the client's history entry (above the 0.1-block ADJUST threshold)
and the client rewinds and replays. Per-frame captures show it exactly: on the
launch frame the velocity becomes -0.4085 while the position does not move.

The old client-side wrappers (`aoslib/jump_smoothing_patch.py`,
`aoslib/character_jump_smoothing.py`) only cancelled restores larger than
0.25 blocks, and did so by restoring the pre-frame position, which still
loses the launch displacement. They could never remove the correction.

## Fix

`aceofspades_revival/tools/patch_character_jump_restore.py` replaces the first
instruction of the restore statement's basic block (`mov eax,
ds:__pyx_n_s_world_object` at 0x10081582, file offset 0x80982) with `jmp
0x1008197A`, the `update_jetpack` statement that follows. Every entry into
that block has released its temporaries (Cython invariant), and the block only
allocates and frees its own objects, so the jump lands in the state the block
normally exits with. The jump sound is untouched; the same-frame correction
skip is unchanged from stock.

The instruction's 4-byte absolute operand carries a HIGHLOW base relocation.
The patcher also rewrites that `.reloc` entry to type ABSOLUTE (ignored by the
loader); without it Windows rebases the DLL and adds the load delta to the
jump's rel32, which crashed the client on the first jump.

Patched SHA-256: `2db2a0dbad619fa0…`. `--check` reports original / patched /
partial; `--revert` restores the stock bytes. Both Python wrappers now detect
the patched binary (`native_restore_removed()`) and stand down, because their
"large restore" rule would otherwise discard a legitimate sprint-jump step.

## What the server still does

Nothing changed server-side for this: authority never rewound to the owner
row (see `Player.update`), which is exactly why the patched client and the
server now agree to 0.0014 blocks. The stock Steam client keeps its retail
behaviour (one ADJUST per jump).

## Internet-ping gate (patched client + lost-frame refill)

`scripts/udp_lag_proxy.py --delay-ms 40 --jitter-ms 8 --loss 0.005` (~80 ms
RTT), Soldier, segments settle, jump_in_place, sprint, jump_run, turn_left,
slope_diagonal, crouch_walk, 32 s, 619 samples:

| Server | ADJUST | SNAP | max matched error | note |
| --- | ---: | ---: | ---: | --- |
| one step per received packet | 145 | 0 | 0.618 blocks | chain starts at the first lost ClientData |
| with `input_gap_fill_limit = 8` | 0 | 0 | 0.0003 blocks | 15 frames refilled; flat route |
| same, rough route with lost button edges | 27 | 0 | 0.439 blocks | a lost crouch release's refilled row forced a 0.9-block correction |
| + no owner row on a refilled label, aim midpoint | 1 | 0 | 0.098 blocks | final; 9 frames refilled, one 0.098 correction while turning |

Lossy runs vary with the route: rough terrain plus a lost packet that carried
a button change still produces a few 0.1-0.4 block corrections per minute
(see docs/RETAIL_INPUT_LOSS.md, "Residual"). The other remaining gate flag is
self-row age: ENet head-of-line blocking behind a lost reliable packet can
hold the unreliable WorldUpdate stream for up to a second on a lossy link
(observed once, 69 loops, while the server kept consuming input at 1/tick).
Prediction is exact so the local player does not move wrongly; remote players
freeze for that second.

## Jump pack boundaries (Rocketeer 66, 2026-09-24)

The flight gate (`--class-id 2 --segments settle,rocketeer_jump_pack_hold`)
still produced two ADJUST corrections per burn: one right after ignition and
one right after fuel exhaustion (0.5 blocks). Both boundaries are
unacknowledged: the retail owner starts thrust when the WorldUpdate row with
action bit 0x04 arrives and stops it when the inactive row arrives, and
ClientData never echoes that state. The server therefore starts its own
thrust `jetpack_activation_defer_frames` accepted-input frames after
announcing activation and keeps it `jetpack_exhaustion_tail_frames` frames
after announcing exhaustion (both in `[debug]`, config.toml). The old
`jetpack_owner_*handoff_input_frames` knobs are not wired to anything.

Method: run the gate with `--client-frame-capture` against a validation
server started with `--debug-selfrow --debug-parity`, then align the client's
60 Hz capture (`physics_capture_*.ndjson`, `loop_count`) with the server's
per-input-frame samples (`logs/input_samples.ndjson`, `loop`). The server
sample for loop L is that frame's pre-move state, i.e. the client's post-move
state of loop L-1; compare `client(L)` with `server(L+1)`
(scratchpad `jet_align.py`).

Findings (loopback, patched client):

- The client applies each transition row 2 or 3 frames after the server
  announces it. Which of the two depends on the phase between the server tick
  and the client frame and is stable within a session but varies between
  runs. A one-frame residual grows into a ~0.2-block divergence by the next
  self row (thrust adds ~0.014 blocks/frame per frame), above the 0.1 ADJUST
  threshold.
- Divergence sign decides how a correction feels: client behind the server
  (server started earlier / stopped later) corrects as a forward nudge;
  client ahead corrects as a rollback.

| defer | tail | ignition (client - server, aligned) | exhaustion | corrections |
| ---: | ---: | ---: | ---: | ---: |
| 2 | 1 (old default) | +0.178 (client behind) | -0.199 (client ahead, rollback) | 2 |
| 3 | 2 | -0.239 (client ahead, rollback) | 0.000 | 1 |
| 3 | 3 | 0.000 | 0.000 | 0 (gate passed) |
| 4 | 3 | -0.239 (client ahead) | +0.157 (client behind) | 2 |

| 2 | 3 | +0.259 (client behind) | 0.000 | 1 |
| 2 | 3 | +0.259 (client behind) | 0.000 | 1 |
| 3 | 3 | 0.000 | +0.157 (client behind) | 1 |
| 3 | 3 | -0.178 (client ahead, rollback) | +0.157 (client behind) | 2 |

The client's delay was 2 at some boundaries and 3 at others, even within one
burn, so no fixed pair is exact. Defaults: `jetpack_activation_defer_frames = 2`,
`jetpack_exhaustion_tail_frames = 3`, the one pair whose residual is a forward
nudge at either boundary and never a rollback (a delay of 2 makes ignition
exact and exhaustion a nudge; a delay of 3 the reverse). Real ping only
lengthens the client's delay, which keeps the same sign. Removing the nudge
needs the server to learn the client's actual thrust frame from the velocity
kick in its ClientData and re-simulate the one to three frames in between; that
server-side rewind is noted in the backlog.
