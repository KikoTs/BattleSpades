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

Note: thresholds are **constant-folded inline** — editing the module globals
at runtime would NOT change behavior. Server must mirror the SQUARED values.

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

## DECISION (2026-06-13): self-rows OFF by default for smooth feel

`worldupdate_include_self = false`. Rationale, all measured/RE-proven:

- With self-rows off, `network_position_updated` is never set, so
  `apply_player_network_correction` does nothing but smoothing — the local
  player runs **pure prediction**: measured **0 direction reversals over 128
  blocks** of walking (butter).
- Jumps **cannot** snap (the no-`network_position_updated` path changes no
  position) — the old "jump → spawn" snap was the empty-world MAP bug, since
  fixed, NOT missing self-rows.
- Other players still stream at 60 Hz (only the recipient's own row is
  omitted), and the server sim stays authoritative internally for
  combat/anti-cheat. Multiplayer is unaffected.

Self-rows ON (the original-server behavior) is fully implemented and correct
(per-recipient stamp + rate-matched input drain + `worldupdate_loop_offset=2`),
but a client whose game-loop rate isn't bit-identical to the server's 60 Hz
drifts a **fractional** loop_count phase that no integer offset cancels,
leaving a residual ~6–16% ADJUST rate (~0.13-block nudges). Turn it on only
for a fixed-60 Hz client, or to add strict server position authority.

### Known measurement caveat

The autonomous test client runs in a **background, vsync-unlocked window at
~58.5 fps** (dt ≈ 0.0171) against the 60 Hz server — an ~18-tick drift over a
12s walk. A fixed offset is exact at the window start but drifts by the end, so
the felt-reversal count varies run-to-run (one run measured 0 reversals, butter;
another 137 at the same config). A **vsync-locked 60 fps real client has no such
drift** and should hold the +2 offset stably. To certify on a real client, run
the calibration workflow above against it.
