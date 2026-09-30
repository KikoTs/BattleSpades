# Retail jump rollback: the launch-frame position restore

## Current package compatibility (September 30, 2026)

The stationary launch compensation described in the historical section below
has been removed. It discarded the first native movement step and therefore
conflicted with `AoS-Retail-Fixes.zip`, whose jump patch preserves that step.
Server authority now keeps the original mover's result for every client.
The absence of BSCF flight capability does not distinguish patched retail.
Stock clients still perform their local stale-anchor reset; supporting their
reset by changing authoritative launch physics breaks the fixed client.

The original-native 45-frame fixture in `retail_native_jump_arc.json` covers
ascent, apex, descent and landing. Three focused cases failed with the server
compensation and pass after its removal. Windows and Linux each pass 1,953
movement/network/flight checks. Native BattleSpades remains unchanged. The
retail-only nominal 30 Hz delivery and guarded idle backlog catch-up remain.
Pack-equipped ordinary jumps use replay spacing only outside active flight
and pending handoff, and flight retires an earlier ordinary-jump marker.
These checks do not establish zero corrections on every live route.

The local Steam installation was restored byte-for-byte from the six runtime
files in the user's ZIP after removing the temporary diagnostic loader.
Earlier full-suite and memory-soak reports predate these later changes.


## Server compatibility fix with stock corrections enabled (2026-09-30)

The restored stock retail client now has actual standing and moving jump
captures, replacing the earlier idle-only evidence. Its Character binary is
unchanged (`52ec520d83fe9e0ed8338a1038b176c272752e81d65e9f923fe90036aaa107f7`);
neither its launch restore nor its correction routine is suppressed.

Three server changes address distinct errors:

- `Player._stationary_retail_jump_origin` matches the stock launch's discarded
  first displacement only for settled, stationary retail infantry. It requires
  recent, equal, zero-velocity owner anchors spanning the estimated round trip.
  It retains the native launch velocity and collision result. Moving, coasting,
  crouching, newly landed, unlabelled and native/BSCF actors keep ordinary native
  movement. No guessed old snapshot becomes an authoritative position.
- Stock Character records normal movement history before movement, but rebuilds
  correction history after movement under the old labels. Another nearby owner
  row can therefore trigger the same correction again. After sending the first
  post-jump anchor, `ReplicationService` briefly waits for the consumed input
  label to pass the estimated replay window (newest received input + RTT frames
  + three service phases), then resumes retail's two-tick/30 Hz cadence. The wait
  has a server-tick deadline; all sent rows still describe real consumed input.
  Observer rows and urgent flight transitions are not delayed. Native BSCF
  players retain their six-tick airborne cadence and native physics.
- A bounded idle-only catch-up consumes a second already-received input frame
  while a retail player is settled and fully idle. Otherwise a startup/render
  stall can leave a permanent FIFO delay even after the network recovers.
  Contiguous idle labels, matching inactive actions, unchanged terrain, no
  pending mutation/impulse/flight, and no player within four blocks are required.
  Nothing is dropped or acknowledged without simulation. Active movement and
  native clients keep their existing backlog policy.

On the same flat ArcticBase route, the old server produced 642 above-threshold
history comparisons and 48 actual correction displacements across 18 requested
taps. The changed server produced zero standing corrections and one correction
for each of 12 moving taps (12 comparisons/displacements total), both locally
and with 40 ms per-direction latency plus 8 ms jitter. The stock launch still
pulls a moving player toward its cached owner position before the server hears
about the jump: maximum matched error was 0.692 blocks locally and 1.720 blocks
with latency. These captures show the repeated correction loop is resolved on
this route, **not zero corrections or complete physics parity**. The stock
ClientData packet has no received-owner ACK, position or velocity from which
the server could recover the exact cached position at arbitrary latency.

Evidence: `_wave8/codex-recovery/jump-parity-live-12` (baseline), `-17` (final
server, local) and `-18` (final server, impaired link), plus the 45-frame stock
jump fixture in `tests/fixtures/retail_stock_jump_arc.json`. Captures 17/18 predate
the later idle-only catch-up; final validation identifies its separate captures.
Original functions
ran under passive observation; input was driven through the game's ordinary
scene keyboard state. No client suppression patch was installed. A general
launch-position rewind was investigated and rejected.

## Earlier stock Steam playtest recovery (2026-09-30)

The earlier zero-ADJUST result below belongs to the patched client. The installed
Steam `aoslib.character.pyd` is still the stock SHA-256
`52ec520d83fe9e0ed8338a1038b176c272752e81d65e9f923fe90036aaa107f7`.
It must not be represented as having the client-side launch fix.

Repeated jump taps on a local server exposed disruptive corrections with the
shipped airborne self-row interval of six ticks (10 Hz). Changing only that
server setting to two ticks (30 Hz) produced a user-confirmed reduction to a
small nudge. This workaround is now **retail-only**:
`worldupdate_retail_airborne_self_row_interval` defaults to two ticks, while
`worldupdate_airborne_self_row_interval` retains six for native BattleSpades
peers advertising their existing BSCF capability. Grounded and observer
delivery, native prediction, physics and urgent flight transitions are unchanged.
Regression coverage decodes mixed-client owner/observer packets through
flight and landing, for split/sequenced transport and both configuration
sources. The server still stamps only actual consumed input; authoritative
physics never rewinds to an estimated received row.

This is a server-side freshness improvement, not removal of the stock client's
launch-frame restore. The residual local reset happens after client physics,
before the server can receive that jump input. Neither a guessed owner anchor
nor a false future pong establishes what the client has received. No client
binary or runtime hook was changed in this recovery.

The user-supplied `continuous_jump.py`, if imported into retail, intercepts the
launch-frame `set_position(network_position)` call while preserving other
position updates. Its whitelist contains only `207.148.19.167:32887`, so it
does not apply to the local `127.0.0.1:28630` playtest. Reading that file does not
establish that it is installed; the inspected Steam loader was mouse-only.

## Earlier client-patch investigation

Measured and fixed 2026-09-24 against the stock `aoslib/character.pyd`
(SHA-256 `52ec520d83fe9e0eâ€¦`, byte-identical in the Revival release client,
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

Patched SHA-256: `2db2a0dbad619fa0â€¦`. `--check` reports original / patched /
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

### Re-read of the same captures (2026-09-29)

The paragraph above cannot be done: ClientData carries buttons and aim only
(its unnamed byte is the aim anti-cheat countdown), and neither client sends
a position, so nothing reveals the owner's thrust frame afterwards.

What the eight captures do show, with S the label being simulated when the
row is queued and N the newest label already received: the owner's thrust
changed on `N + 2` (10 boundaries) or `N + 3` (6), never earlier. It does not
follow S. With two labels buffered the owner was on `S + 4` all three times
while the fixed constant said `S + 3`, and the constants ignore the round
trip entirely, so at 100 ms the server ignited about five frames early.

`[debug] jetpack_handoff_latency_aware = true` (default) applies the bound:
ignition on `max(S + 3, N + 2)`, exhaustion on `max(S + 4, N + 3)`, both
moved by the round trip in frames (ENet's estimate minus 8 ms, rounded down
for ignition and up for exhaustion). Either way a mismatch is a forward
nudge. Against the recorded ignitions that is 4 of 8 exact instead of 2,
still no rollback. One frame of uncertainty remains and is not removable
with the stock protocol. The round-trip term has not been run against the
stock client at real ping yet.
