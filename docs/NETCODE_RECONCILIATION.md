# Client Reconciliation Contract — extracted from aoslib.character.so (IDA)

Ground truth for how the 1.x client applies a WorldUpdate self-row, recovered
by reverse-engineering the compiled `aoslib.character.so` (macOS build, same
Cython source as the Windows `character.pyd`) on 2026-06-12 with IDA Pro
(raw-byte + opcode verified, not inferred). **This supersedes every earlier
guess about `worldupdate_loop_offset`.**

## Functions (IDA addresses, character.so)

| Function | Addr | Role |
|---|---|---|
| `set_network_position_and_velocity` | 0xcf2c0 | WorldUpdate self-row handler: stores server pos/vel + loop_count |
| `apply_player_network_correction` | 0x7be90 | the reconciliation core (snap / adjust / no-op) |
| `get_position_diff_squared` | 0x7ffe0 | the distance metric |
| `get_old_movement_data` | 0x7f870 | history lookup by EXACT loop_count |
| `update_alive` | 0x9482e | per-frame: appends a PlayerMovementHistory record |
| `apply_network_position_smoothing` | 0x798b0 | post-correction visual lerp |

## The metric (verified: 3 Subtract, 3 Multiply, 2 Add, zero SSE/sqrt)

```python
get_position_diff_squared(self, movement_data, server_position):
    dx = server_position.x - movement_data.position.x
    dy = server_position.y - movement_data.position.y
    dz = server_position.z - movement_data.position.z
    return dx*dx + dy*dy + dz*dz     # SQUARED, all axes equal, NO sqrt
```

## The pairing (verified: zero arithmetic on loop_count; RichCompare EQ)

`set_network_position_and_velocity(pos, vel, interpolate, last_loop_count, force_update)`:
- dedupe: `if not force_update and network_position_loop_count == last_loop_count: return`
- on accept: `network_position_loop_count = last_loop_count`,
  `network_position_updated = True`, store `network_position`, `network_velocity`.

`get_old_movement_data(loop_count)` walks `self.movement_history` and returns
`(entry, index)` for the entry whose `.loop_count == loop_count` **exactly**
(`RichCompare op=2/EQ`) — or `None`. **The loop_count is passed verbatim; NO
+/- offset anywhere.**

## The reconciliation (verified branch + raw-byte thresholds)

```python
apply_player_network_correction(self, dt, players):          # called each tick
    if not self.network_position_updated:
        self.apply_network_position_smoothing(dt); return
    self.network_position_updated = False
    net_pos, net_vel = self.network_position, self.network_velocity
    md = self.get_old_movement_data(self.network_position_loop_count)  # offset 0
    if md is None:                                            # tick already pruned
        self.world_object.set_position(*net_pos); self.movement_history = []; return
    entry, index = md
    d2 = self.get_position_diff_squared(entry, net_pos)       # SQUARED distance
    if d2 > 16.0:                       # POSITION_RESET_TOLERANCE  (linear 4.0)
        self.world_object.set_position(*net_pos)              # SNAP
        self.movement_history = []
    elif d2 > 0.010000000000000002:     # POSITION_TOLERANCE = 0.1*0.1 (linear 0.1)
        self.position_lerp_timer = 0.1                        # ADJUST (smooth replay)
        self.world_object.set_position(*net_pos)
        self.world_object.set_velocity(*net_vel)
        moves = self.movement_history[:index + 1]
        self.movement_history = []
        for move in reversed(moves):                          # replay matched..now
            move.get_client_data(self.world_object)
            self.world_object.update(dt, players)
            self.movement_history.insert(0,
                PlayerMovementHistory(self.world_object, move.loop_count))
        self.apply_network_position_smoothing(dt)
    else:                               # d2 <= 0.1 blocks: NO correction (prediction stands)
        pass
```

### Exact constants (IEEE754 raw bytes, verified)

| Constant | Value | dist meaning | role |
|---|---|---|---|
| `POSITION_RESET_TOLERANCE` | `16.0` (0x4030000000000000) | 4.0 blocks | snap above this |
| `POSITION_TOLERANCE` | `0.010000000000000002` (=`0.1*0.1`) | 0.1 blocks | adjust above this, no-op below |
| `position_lerp_timer` arm | `0.1` | — | set only in ADJUST |

## Terrain/action ordering invariant (2026-07-12)

Client-origin topology packets must not mutate VXL during packet drain. The
client records movement for action loop `L` before the echoed terrain packet
commits its local ghost; authoritative loop `L` must therefore also run against
the pre-edit map. `WorldMutationService` validates/reserves during drain and
commits after player physics reaches `L`. This currently covers BlockLine (40),
BlockBuild (32), and block-tool BlockLiberate (35).

The player collision list is another input to the same native replay. Its team
filter must match `InitialInfo.same_team_collision`; otherwise client and
server apply different velocity impulses even with identical terrain and
button history.

Note: thresholds are **constant-folded inline** — editing the module globals
at runtime would NOT change behavior. Server must mirror the SQUARED values.

### Terrain-step landing parity (2026-07-13)

Landing is not equivalent to the box helper's downward-collision flag. Retail
enters the shared post-box landing/glide path whenever velocity Z is zero
(`world.pyd:0x100130F7`). A terrain step can be classified as a climb while the
box move still zeros Z; excluding that frame kept server X/Y at twice the
retail speed. The severe path (`~0x100131DF`) is
`saved_landing_vz > 0.8 / gravity` and halves X/Y exactly.

All collision intermediates in the box mover remain float32 to match the
32-bit client at voxel contact planes. Fall distance is accumulated from the
actual post-box Z displacement, reset by a meaningful upward displacement or
an active parachute, and consumed by the same post-box Z-zero branch.

The exact failing ArcticBase frame and the end-to-end gate now pass: the clean
artifact contains a real block mutation, 1,200 samples, zero ADJUST/SNAP/
rollback, and 0.000031-block maximum matched error:
`logs/movement/solo-block-after-landing-fix/run-1/movement-stress-20260713T030204.777214Z.json`.

### Smoothing (`apply_network_position_smoothing`, only while `position_lerp_timer > 0`)

```python
ip = self.interpolated_position
ip.{x,y,z} = ip.{x,y,z}*0.1 + world_object.position.{x,y,z}*0.9   # convex blend
ip.{x,y,z} += world_object.velocity.{x,y,z} * (dt * 16.0)         # dead-reckon
self.position_lerp_timer -= dt
self.world_object.set_position(ip.x, ip.y, ip.z)
```

## Implication for the server (THE design)

The self-row the server sends player P must carry, as its loop_count, the
**exact** client loop_count `L` whose `movement_history[L].position` equals the
position being reported. The server reports the position it computed by
consuming P's input stamped `L`, so the self-row loop_count must be the
loop_count of the input the sim actually consumed (`player.last_applied_input_loop`),
with **offset 0**.

If the server's reported position for `L` lands within **0.1 blocks** of the
client's `history[L]`, the client **no-ops** → perfect prediction, zero
correction. The ~0.131-block walk yanks measured earlier = the server landing
just over that 0.1 threshold (one walk-tick of phase error), firing an ADJUST
replay every packet. The fix is purely to put the right loop_count on the row
so `d2 < 0.01` — calibrated deterministically by `tmp/reconcile_sim.py` against
recorded logs, not by feel.

Workflow that produced this: wvr2j7sjl (5 agents, raw-byte verified).

## Server implementation (2026-06-12, applied)

- **Per-recipient self-row stamp** = `player.last_applied_input_loop` (the
  loop_count of the input the sim consumed) `+ worldupdate_loop_offset`.
- **Strict in-order input consumption** (`server/player.py apply_buffered_input`):
  one buffered input per tick in loop_count order, never skip/dup. The old
  "newest ≤ sim_tick" logic dropped a frame of motion whenever the client
  clock ran ahead → ~0.13-block divergence → ADJUST every packet.
- **`worldupdate_loop_offset = 2`** — deterministically calibrated via
  `tmp/reconcile_sim.py` (replays a recorded walk through THIS exact contract,
  joined on the common loop_count clock). +2 is the stable structural phase
  between the server stamp and the client's history-index labeling. It peaked
  at +2 on every clean run.

### Calibration workflow (reusable, deterministic — no guessing by feel)

1. Set `[debug] debug_selfrow = true`, restart server.
2. Join, walk straight ~12s (`auto_join` + a tagged W press).
3. `py tmp/reconcile_sim.py` → prints the snap/adjust/no-op distribution per
   candidate offset and the BEST offset (max no-op fraction). With the server
   already sending +2, the sim peaks at 0 (total +2 confirmed).
4. Set `debug_selfrow = false` for normal play.

## DECISION (2026-07-11): self-rows ON by default

`worldupdate_include_self = true`. The stock client needs a fresh local
`network_position` anchor. A no-self-row A/B looked clean when only
SNAP/ADJUST counters were inspected, but the strengthened retail gate caught
the real failure: repeated jump runs visibly rolled the player back toward the
CreatePlayer spawn anchor by up to 25.15 blocks while the counters stayed at
zero.

Self rows are therefore required and are sent at the 30 Hz WorldUpdate cadence.
The stamp remains per recipient: `player.last_applied_input_loop +
worldupdate_loop_offset`, with `worldupdate_loop_offset = 0` and
`clock_sync_loop_bias = 0` for the current retail client.

### Current foreground evidence

The current gate restores the retail window to the foreground and drives
inputs on the client's own clock. The old +2/background conclusion below is
superseded and is not a release invariant. Testing `-1` changed the observed
client/server loop lag by exactly one but did not reduce corrections, so offset
0 remains the production setting.

The original 88 -> 41 orientation claim is withdrawn: that controller wrote
the world object's orientation directly and the native Character overwrote it.
The corrected controller changes `Character.yaw`, records the resulting yaw,
and proves that real turning occurred. One-step replay of the corrected capture
matches grounded movement within roughly 0.0006 blocks at p95. Buttons remain
latched for the next observed frame while orientation is taken from the current
packet. This was the 2026-07-13 state; the state-bounded Engineer owner handoff
below supersedes the then-open airborne/jetpack correction statement.

The jump-launch mismatch is now separated from sustained airborne flight. A
pre-correction capture proved that post-ADJUST `matched_history_position` is
not evidence of the original comparison: retail clears and replays its history
during correction. `Character.update_alive` then exposed the missing invariant:
whenever `jump_this_frame` is true it restores all three coordinates from its
cached `network_position` after native physics, while retaining launch velocity
and airborne state. Holding only Z left the server one sprint step ahead.

The maintained validation/decompiled clients now wrap the public
`Character.update` seam with the same narrow guard as the server: on the exact
grounded-to-`jump_this_frame` transition, a cached-position restore larger than
0.25 blocks is discarded while native velocity and airborne state are kept.
The wrapper installs only after model/GameManager initialization; importing
Character earlier pulls model-backed tools before `BLOCK_MODEL` exists and
aborts client startup. The client PYD and ordinary sub-voxel restore path are
otherwise unchanged.

Engineer thrust uses a separate state-bounded delivery rule. The reliable
active transition is sent immediately, then only that owner's correction row
is withheld for the finite active fuel burn; observers still receive the
authoritative player row at 30 Hz. A fixed 30-input resume produced a
0.230469-block mid-flight pre-correction error in
`logs/movement/engineer_clean_20260714/`. The replacement full-exhaustion run
in `logs/movement/engineer_exhaust_release_20260714/` passed 291 samples with
zero ADJUST/SNAP/visible rollback/stall, then resumed owner rows after release,
settle, and ground contact.

The server records every exact owner row only after it is queued. IDA proves
the local-player path uses `force_update=True`, so duplicate pong stamps are
accepted and must remain as separate ordered events; the former "keep first
duplicate" mapping was incorrect. Because movement buttons are one observed
frame latched, a jump sourced by ClientData J may only use a row whose pong is
strictly less than J and whose send occurred before the server received J. A
row failing either condition was definitely too late. This is a necessary
causal filter, not proof that GameScene consumed the selected row before J;
outbound delivery has no application acknowledgement. The ordered history is
bounded to 128 rows and spawn remains the fallback.

Do not replace this filter with an aggregate ENet transition coordinator. A
validation-only `OwnerTransitionCoordinator` observed
`reliableDataInTransit`, but that counter can coalesce unrelated reliable
commands and ends at transport acknowledgement, before GameScene application.
It regressed Engineer jetpack behavior and was removed.

A foreground retail run on 2026-07-13 validates the behavior without turning
the causal filter into a stronger protocol claim. `jump_run` recorded zero
ADJUST, zero SNAP, zero visible rollback, and zero matched-loop error. The
combined block/Engineer run placed a real block (inventory 2000 -> 1999) and
reduced maximum backward movement from the earlier 0.42-1.25 block failures to
0.00069 blocks. Its remaining 13 adjustments began only after Engineer
jetpack activation; ordinary grounded launch and block placement were clean.
Artifact:
`logs/causal-owner-live/movement/movement-stress-20260712T212311.730994Z.json`.

The WorldUpdate header and row pong are also separate clocks. A deliberate
retail split-clock probe made `pong = header - 7`; all 62 observed cache-stamp
transitions followed row pong and none followed the header. Production now
uses the current global server loop in the header while each player row keeps
that player's applied ClientData loop in pong.

Crouch is intentionally mixed-phase. Retail applies `set_crouch` before storing
history row L, so current packet L supplies crouch geometry while locomotion,
jump, sneak, and sprint remain latched from the prior observed packet. Delaying
crouch by one row creates an immediate 0.9-block anchor mismatch.

Engineer flight has a different native lifetime for the same input bit. Retail
clears jump at `world.pyd:0x10012D3F-0x10012D48` only when airborne without a
jetpack or parachute, and `send_client_data` reads the retained state afterward
at `gameScene.pyd:0x1016B037`. Engineer consequently keeps the ordinary
one-observed-frame latch and native sustained SPACE thrust. Applying the
grounded launch anchor restore to those airborne frames would create hovering or
stutter even while velocity and fuel continued to change.

Airborne rows now use a bounded six-tick interval while grounded rows retain
the normal two-tick/30 Hz cadence. This does not change observer replication:
remote players and entities still receive every WorldUpdate. A fixed-spawn
ArcticBase Snowblower A/B measured 263 corrections at two ticks versus 39 at
six ticks, with no SNAP or visible rollback at six. Ten ticks caused a visible
rollback, and fully withholding airborne rows caused two, so neither wider nor
unbounded suppression is safe.

## Explosion prediction ordering (Snowball)

`DestroyEntity(19)` does not apply Snowball impulse. Native
`GameScene.process_packet_damage` (`gameScene.pyd:0x1018C270`) feeds
`explosion_damage_manager` before the frame's GameScene/Character update core
at `0x10149CF0`. For Snowball only, the server sends a reliable, zero-damage
`Damage(37)` with type 20 and the exact impact position before destroying the
projectile entity. `causer_id` must still resolve at that instant; entity id 0
is valid.

The server mirrors the native frame order by advancing projectile collisions
before player physics. The event is transient and must not enter the late-join
map journal.

Impact detection is not the state at which retail applies the predicted
impulse. Six clean retail contacts placed the matching authoritative state at
the third ClientData accepted after impact. The accepted implementation records
the player's current dense accepted-input sequence, queues the explosion origin,
radius, and min/max knockback for sequence `current + 3`, and applies it just
before that frame's player physics. At application time it recomputes the
impulse from the then-current authoritative position and crouch state. A frozen
impact-time vector is wrong when the player moves during packet delivery.

This dense sequence is an application witness, not a transport ACK. Client loop
labels may skip, `server.loop_count` describes a different clock, and the shared
owner send/receive sequence includes WorldUpdate sends. The following live
variants were rejected:

- server-loop targeting: one 1,077-sample run had 2 ADJUST and 0.373261 maximum
  error; a shorter repetition happened to pass, proving the result was not
  stable. A fresh current-code repetition had 2 ADJUST and 0.400019 maximum
  error;
- fixed `server loop + 2`: 3 ADJUST, 0.301891 maximum error, and 0.0515445
  maximum backward step;
- the dense accepted-input sequence with a two-frame delay and application-time
  recomputation: 2 ADJUST and 0.384826 maximum error.

The three-frame design passed the final two-client retail gate in
`logs/combined-replication/snowball-sequence3-final-live/20260714T014849/scenario-run-1/movement-stress-20260713T224938.344225Z.json`:
719 samples over 11.985602 seconds; zero ADJUST, SNAP, visible rollback, stall,
or unmatched sample; maximum/p95 matched error 0.000076; maximum backward step
0.008209. Pinned SHA-256 values were unchanged before and after the run:

- `server/main.py`: `5FCE093AB5F18E45119B4D6C5F9E379A158AACD8ACB48B70DFA4F568774AC998`
- `server/player.py`: `A255EBE576236CE2FCA656A65A51B625DAFB37CA06F3BF8A0D8B950818416469`
- `server/simulation_runtime.py`: `7AA38B827B60B7BACAB228692BD110CF8598DEC0AE113EA21873F95FCC1EA217`

### Disconnect is an owner-generation boundary

Player ids are small and immediately reusable, so a disconnect must invalidate
more than the `Player` dictionary row. RECEIVE and DISCONNECT can occur in the
same ENet pump, and a local drain batch can cross an `await`. The disconnect
path therefore removes packets queued for that exact Connection, while the
drain path independently checks that `connections[peer]` is still the same
Connection and `players[id]` is still the same Player before delivering each
packet. Those identity checks are the generation guard; numeric equality alone
would admit a stale deployable request for the replacement player.

Before releasing the id, `RoundLifecycle.forget_player` cancels reserved world
mutations, projectile records and their visible entities, rocket turrets, fire
sources/credit, per-weapon combat cadence, votes, and replication cadence. It
destroys owner-bound `deployable`, `medpack`, and `grave` entities but leaves
ordinary construction and objective state in the world. Machine guns use their
behavior owner rather than wire `player_id=0xFF`: an owned gun is unmounted and
destroyed, while a foreign gun merely carried by the departing player is only
unmounted. Radar stations pass through `_radar_station_removed`, so the team
visibility count is decremented exactly once and visibility is disabled only
when the last station disappears.

### Superseded background-window caveat

The autonomous test client runs in a **background, vsync-unlocked window at
~58.5 fps** (dt ≈ 0.0171) against the 60 Hz server — an ~18-tick drift over a
12s walk. A fixed offset is exact at the window start but drifts by the end, so
the felt-reversal count varies run-to-run (one run measured 0 reversals, butter;
another 137 at the same config). A **vsync-locked 60 fps real client has no such
drift** and should hold the +2 offset stably. To certify on a real client, run
the calibration workflow above against it.
