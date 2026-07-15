# Engineering notes and investigation record

This file records the expensive lessons behind non-obvious server invariants.
Keep normal code comments focused on what must remain true; add investigation
history here so a later engineer can understand why.

## Build-run-jump rollback was a chronology and collision-rule mismatch

The old packet drain applied a player's terrain edit before replaying the
ClientData frame that emitted it. The retail movement history had simulated
that frame against the pre-edit map, while the server simulated the same loop
against the post-edit map. The exact failure was reproduced by scheduling a
real BlockLine commit, sprint on the next rendered frame, and jump one frame
later. The fix is a bounded post-physics mutation queue keyed by the packet's
retail loop label; never move this work back into packet draining.

A second two-client reproduction initially looked like flying-entity load: 73
adjustments and 11 visible rollbacks while Snowblower projectiles were active.
The mover was actually running into the emitter. `InitialInfo` advertised
`same_team_collision = 0`, but the server fed every alive player into native
collision. The authoritative list now excludes allies when the advertised
rule is disabled. A deterministic same-team contact run crossed the emitter
with 33 real projectiles, zero SNAPs, and zero visible rollback.

Two tempting cadence/input changes were rejected by live A/B evidence:

- applying ClientData buttons on the same observed label (latch 0) increased
  soft adjustments from 12 to 17 and maximum error from 0.39 to 0.55 blocks;
- sending airborne owner rows every 30 ticks retained 10 adjustments and
  increased loop lag to 32, too close to the client's 60-entry history.

The current release settings therefore remain input latch 1, grounded self-row
interval 2, and airborne interval 6. The final 12-second normal-cadence stress
run still recorded 14 soft ADJUSTs with 0.414-block maximum matched error, so
terrain-contact parity remains open even though severe rollback is gone. The
artifact is `logs/combined-replication/final-normal/`. This conclusion is
superseded by the stamp-aware launch-anchor result below.

### Stamp-aware owner anchors closed the repeated-jump rollback

IDA and a pre-correction trace proved that retail restores complete XYZ from
its cached `network_position` on `jump_this_frame`. The first server fix held
only pre-physics Z, leaving one grounded sprint step in X/Y. A later fix used
the newest queued owner row, but a one-frame input latch meant that row could
carry the same stamp as the jump source even though retail received it after
simulating that frame.

The final invariant is temporal: record an owner anchor only after enqueue, and
for jump source J use the newest row with stamp `< J`. Never overwrite the first
position recorded for a duplicate stamp. The bounded 128-row history plus spawn
fallback produced two fresh retail passes, including a 2,159-sample three-cycle
block -> sprint -> jump run, with zero correction/rollback and 0.000015-block
maximum error.

Crouch cannot share the same phase rule as the other buttons. Retail calls
`Character.set_crouch` before storing history L, so packet L's crouch bit is
current while locomotion is latched. Treating crouch as latched created a full
0.9-block mismatch; the current-crouch/latched-locomotion composition passed a
592-sample retail run with zero corrections.

## Terrain-step landing required the post-box Z gate

The last reproducible block -> sprint -> jump correction occurred on an
ArcticBase terrain step. Client and server reached the same position on the
impact frame, but the client halved horizontal velocity while the server kept
it. Six frames later the authoritative body was approximately 0.68 blocks
ahead.

IDA resolved the branch exactly. `world.pyd` saves landing velocity near
`0x10012FB4`, calls the box clip path near `0x1001308F`, and enters the shared
landing/glide branch when the **post-box** velocity Z equals zero at
`0x100130F7`. A step glide can satisfy that gate even when the helper reports a
climb, so a dedicated `collided_down` boolean is not equivalent. The severe
branch at approximately `0x100131DF` tests `landing_speed > 0.8 / gravity` and
multiplies X/Y by exactly 0.5.

The collision mover also needs float32 intermediates. At contact planes,
double precision can keep a sum just below an integer voxel boundary where the
32-bit retail build rounds to the boundary and chooses another collision
branch. The fall accumulator now uses actual post-box downward displacement;
an upward delta below -0.1 clears it, parachute activity clears it every update,
and passive jetpack/water modifiers are applied in the native landing branch.

The exact stock oracle for the failing frame returns position
`(318.781494140625, 223.5, 228.41697692871094)`, velocity
`(0.24531811475753784, 0, 0)`, and result `-1`. The post-fix retail gate then
recorded zero ADJUST, zero SNAP, zero visible rollback, and 0.000031-block
maximum error across 1,200 samples with a real block mutation. Evidence:

- before: `logs/movement/solo-block-current/run-1/movement-stress-20260713T023430.198147Z.json`;
- after: `logs/movement/solo-block-after-landing-fix/run-1/movement-stress-20260713T030204.777214Z.json`.

## Production tracing invalidated the early performance conclusion

An earlier live report called the server healthy after observing approximately
`0.03 ms` ticks in a small session. That conclusion did not cover the actual
production process. The live client/server environment still enabled physics
parity capture, continuously wrote large NDJSON traces, and repeatedly updated
summary files. A separate stale VXL probe also consumed a CPU core. The initial
representative 50-player run achieved only `15.37 Hz`, with `64.9 ms` average
ticks.

Profiling attributed roughly 84% of sampled time to synchronized bot target
acquisition and terrain line-of-sight raycasts. Staggering and bounding those
decisions, grouping WorldUpdate serialization, reducing its wire cadence to 30
Hz, disabling production movement snapshots, and bounding network/logging work
produced the latest measured 30-second result: `59.966 Hz`, `2.777 ms` average,
`5.088 ms` p99, zero gameplay-packet drops, and zero logging drops.

The 2026-07-11 stabilization pass added three more hard bounds:

- plugin event callbacks run under `network.plugin_event_budget_ms`;
- behavior `on_tick` work is capped by `network.entity_tick_batch_limit` and
  deferred round-robin;
- join mutation catch-up is capped by `network.max_map_mutation_journal`.

If the join journal cap is exceeded, the server disconnects the joining client
with a data error instead of admitting it with missing block edits. A reconnect
is cheaper than a silent terrain desync.

Lesson: benchmark the production configuration and include real framing and
ENet packet allocation. A synthetic tick with diagnostics forcibly disabled is
useful for comparison but cannot alone certify the launched server.

## Miner/Medic class-loadout split-brain

Reproduction: change from Medic to Miner, then attempt to place dynamite. The
server can receive or retain a Miner class ID while the active loadout still
contains the Medic tool. Packet 90 then follows the medpack path, so the user
sees a health pack where dynamite was expected.

The cause is independent pending fields and packet ordering:

- `ChangeClass(78)` changes or stages the class.
- `SetClassLoadout(13)` separately stages tools.
- Join, death, and round-reset paths historically applied those pieces at
  different times.

The fix must treat class, loadout, prefabs, and UGC tools as one normalized
selection and install it before spawn. It must also enforce deployable
authorization centrally. Checking only the packet ID or only `player.tool` is
insufficient; the active class and normalized loadout must both permit that
tool.

Live validation on 2026-07-11 switched class 17 -> 3, observed the normalized
Miner loadout, selected native `DynamiteWeapon` tool 21, and placed entity type
10 at the client's ghost voxel. The client continues emitting ClientData while
dead during this transition; those old-body frames are now ignored because the
new spawn deliberately establishes a new input-history anchor.

## Native-client crash hazards

These hazards come from live client failures and should be treated as protocol
invariants until new compiled-client evidence supersedes them:

- **Late entity IDs are dispatch-table indices:** `CreateEntity.type` indexes
  the retail `GameScene.ENTITIES` list. Cython class-registration order is not
  equivalent. The old inferred mapping sent C4 as type 30 (a medpack) and
  Medpack as type 31 (block goo). The verified late range is MedPack 30,
  BlockGoo 31, ChemicalBomb 32, GLGrenade 33, Sticky 34, AttachedSticky 35,
  Radar 36, ProjectileMine 37, C4 38, and RiotShield 39. Keep literal tests for
  this ABI; comparing a handler only to its symbolic constant cannot detect a
  consistently wrong alias.
- **Do not iterate native client vectors from the debug console:** converting
  `world_object.position` with `tuple(...)` terminated the retail Python 2
  client and produced dumps at 13:18:18 and 13:19:29 on 2026-07-12. Probe the
  three scalar indices individually (`float(position[0])`, etc.). This was an
  instrumentation crash, not a server packet or gameplay transition.

- **Map stream shape:** raw `.vxl` bytes without per-column `(x, y)` record
  framing crashed both development and stock clients. Re-encoding every filled
  underground voxel produced an oversized stream rejected by the stock client.
- **Entity lifetime:** repeated/unknown `DestroyEntity` IDs were printed as
  `invalid entity on destroy` immediately before a native crash during an
  end-round sequence. Destroy each server-owned ID at most once and clear stale
  owner references during reset.
- **Oriented explosive construction:** a retail crash in
  `process_packet_use_oriented_item` reached `GLGrenade.__init__`, where the
  render item was initialized with the wrong argument count. Do not invent a
  shortened `UseOrientedItem(10)` shape; validate its orientation/item fields
  against the generated packet layout and a live capture.
- **Join mutation gap:** a map snapshot alone is not sufficient. Built or
  destroyed blocks committed while the snapshot streams must be replayed from
  a retained mutation journal before the player enters the game. If the
  bounded journal overflows, do not synthesize a partial replay; reject the
  join and require a fresh snapshot.
- **Round-end ordering:** state/end packets, runtime entity destruction, map
  mutations, and respawn must be ordered consistently. A client showing its
  score screen is not evidence that it is safe to destroy arbitrary IDs or send
  a new spawn immediately.

## Rejected or misleading approaches

- **Raw VXL byte streaming:** lacks the record framing expected by the client
  builder and crashes.
- **Explicitly serializing the filled collision grid:** expands implicit solid
  underground into a tens-of-megabytes map stream and is rejected by stock
  clients.
- **Newest-input-only consumption:** applying the newest buffered input and
  clearing older inputs hides backlog but discards player history under jitter.
- **Consuming multiple inputs as multiple physics frames:** makes the server
  outrun the client's fixed step. Backpressure must not alter simulated time.
- **Synchronous DEBUG logging:** packet parsing, formatting, console output, and
  file writes on the event-loop thread turn observability into gameplay lag.
- **Synchronous self-row capture:** opening and flushing
  `logs/selfrow_samples.ndjson` from the WorldUpdate path perturbs the exact
  timing the capture is meant to measure. Use the bounded debug writer queue.
- **Periodic hard position snaps as the primary fix:** masks reconciliation
  errors with visible rollback and does not solve client/server map mismatch.
- **Sparse grounded self rows with urgent jump/build rows:** looked like a way
  to hide harmless aim-quantization lerps. The foreground A/B made the same
  turn+jump control worse (41 -> 73 corrections), raised loop lag to seven,
  and introduced a 1.0-block visible rollback. Keep the verified 30 Hz anchor.
- **Withholding every airborne self row:** reduced a flat jump from 29 soft
  corrections to 2 and a real block/jump run from 53 to 5, but a real
  Snowblower flight held the anchor for 112 loops and produced two visible
  rollbacks when correction resumed. Airborne suppression is therefore unsafe;
  every tool/state keeps the 30 Hz anchor.
- **Announcing jetpack activation one boundary before using it in physics:**
  attempted to align history with a server-originated action flag. The retail
  A/B worsened to 168 corrections and 0.429-block maximum matched error. The
  native client expects activation and physics in the same simulated frame.
- **Conditional -1 acknowledgement while aim changes:** offline candidate
  history made it look better than offset 0, but closing the feedback loop
  increased corrections from 165 to 246 and maximum error to 0.553 blocks.
  Candidate minima describe the already-corrected trajectory; they are not a
  safe dynamic stamp controller.
- **Synthesizing missing ClientData loop labels:** the retail renderer can skip
  labels during an ordinary frame hitch and never stores those labels in
  `movement_history`. A WorldUpdate acknowledging a fabricated label makes the
  client miss its history lookup and hard-SNAP. Simulate and acknowledge only
  observed labels; a later observed gap may advance bounded simulated time.
- **Using ENet arrival intervals as movement dt:** foreground retail A/B made
  straight movement errors much worse (up to 2.6 blocks). Transport bursts are
  not render-frame timing. The fixed step plus observed loop gap is the stable
  time source.

## Movement reconciliation decision (2026-07-11)

Production must keep `worldupdate_include_self = true`. The no-self-row A/B
looked clean when only native SNAP/ADJUST counters were checked, but a stronger
visible-position gate reproduced the real bug: repeated jump runs rolled the
player back toward the CreatePlayer spawn anchor by up to 25.15 blocks while
the counters remained at zero.

Root cause: with self rows omitted, the compiled client can retain
`network_position_loop_count == 0` and `network_position == spawn`. Pressing
jump may enter the correction path against that stale anchor. A fresh self row
keeps the anchor near the player's actual movement history. Never certify
movement solely from reconciliation counters; also scan sampled positions for
visible horizontal/vertical jumps.

The same rule applies while holding the block tool. Suppressing only that row
produced ten visible rollbacks and a 62.759-block maximum discontinuity in
`logs/feature-stress/block-tool-baseline`; restoring it produced no visible
rollback in `block-tool-selfrow` while native placement, palette state, and
selected colour remained correct.

The first orientation A/B was invalid. Its controller called
`world_object.set_orientation()` directly, but the native Character overwrote
that value on the same frame, so the advertised 88 -> 41 improvement did not
measure a turn. The corrected harness drives `Character.yaw` and records the
resulting degrees beside the orientation vector. Current evidence keeps button
flags latched but applies orientation from the current ClientData label. A
one-step replay of 336 grounded native-yaw frame pairs has about 0.0006-block
p95 error; the remaining roughly 0.1-block live correction boundary is a
history/scheduling issue, not a Cython formula-performance problem.

The corrected block segment also aims 60 degrees down so the stock preview has
a valid adjacent voxel after arbitrary prior movement. A clean isolated run
consumed four real blocks (2000 -> 1996), with zero SNAPs and zero visible
rollback; soft corrections remain an open gate and are not hidden by declaring
the action successful.

The safe compromise is a bounded airborne row interval of six simulation ticks
(10 Hz) while grounded rows and all observer snapshots remain 30 Hz. On the
same deterministic ArcticBase Engineer/Snowblower scenario, interval 2 caused
263 corrections with 0.342-block maximum matched error; interval 6 caused 39
with 0.214 and no rollback. Interval 10 reduced chatter further but introduced
one visible rollback, establishing six as the current measured bound.

A final 40-second combined retail run chained walk, repeated jumps, real block
placement, and real Snowblower entities. It recorded zero native SNAPs and zero
visible rollback, with 36 soft adjustments and loop lag capped at eight. A
3.9-block downward step initially looked like a vertical snap, but the sample
was airborne with positive downward velocity immediately after Snowblower
terrain destruction; the gate now distinguishes that legitimate fall from a
rollback while retaining the native SNAP/history-reset check.

An authenticated Engineer/Snowblower run created thirteen real server-owned
projectiles while the tick log reported 0.38 ms maximum packet drain, 0.90 ms
projectile work, and 1.17 ms maximum total tick. Flying-entity load did not
saturate Python; remaining correction clusters around movement/impact parity.

Fresh combined and landing runs reinforced that result: representative total
tick maxima remained below 2 ms (the combined baseline peaked at 1.86 ms),
well under the 16.67 ms simulation budget. Do not propose a broad Cython port
as a correction fix without a new profile proving CPU saturation. The current
failures have been exact native collision, packet-application, and scheduling
parity defects.

## Snowball impulse comes from Damage, not DestroyEntity

The native Snowball `delete` path only removes the visual and plays effects.
`SetHP(5)` updates health feedback but does not change velocity. IDA instead
shows `GameScene.process_packet_damage` at `0x1018C270` calling both the block
manager and `explosion_damage_manager.handle_damage`; that explosion manager
is the source of the retail client's predicted blast impulse.

The Snowball invariant is consequently narrow and order-sensitive:

1. Broadcast one reliable `Damage(37)` at the impact position with
   `type=20` (`SNOWBALL_DAMAGE`), `damage=0`, `face=0`, `chunk_check=0`,
   `seed=0`, and `causer_id` equal to the still-live projectile entity.
2. Then broadcast `DestroyEntity(19)` and remove the projectile.

Entity id 0 is a valid wire id and must not be treated as absent. A stale
explosion whose entity was already removed must send neither packet because
the retail client can no longer resolve its causer. The Damage event is
ephemeral, not a terrain mutation: it is sent only to in-game peers and is not
recorded in the MapSync catch-up journal. On disconnect, cancel the owner's
projectile-engine records and destroy their registered entities before the
player id can be reused.

Native receive/update order matters as much as packet order. Packet processing
reaches `process_packet_damage` (`0x1018C270`) before the frame's GameScene/
Character update core (`0x10149CF0`). The authoritative scheduler therefore
advances projectiles before player physics, while leaving generic entities and
turrets after players. Unit tests pin the Damage-before-Destroy bytes, id-zero
case, no-journal rule, disconnect cleanup, and friendly-fire policy.

### Failed Snowball application clocks

The remaining correction was not a wrong impulse magnitude. Diagnostic captures
showed the server computing the same blast against a state several retail frames
older than the state where `Damage(37)` entered Character physics. Three timing
approaches were tested and rejected:

- **server-loop/client-loop label:** targeting the impact's server loop produced
  two ADJUST and 0.373261 maximum error in the 1,077-sample first run. A shorter
  repetition happened to pass, but a fresh current-code repetition again
  produced two ADJUST and 0.400019 maximum error. The pass was timing luck, not
  a stable invariant. Evidence is under
  `logs/combined-replication/snowball-loop-label-live/20260714T013123/` and
  `logs/combined-replication/snowball-loop-label-live-current/20260714T013434/`;
- **fixed `server loop + 2`:** three ADJUST, 0.301891 maximum matched error, and
  0.0515445 maximum backward step in
  `logs/combined-replication/snowball-loop-plus2-live/20260714T014044/`;
- **two accepted ClientData frames plus application-time recomputation:** two
  ADJUST and 0.384826 maximum error in
  `logs/combined-replication/snowball-sequence-recompute-live/20260714T014615/`.

The server loop and sparse ClientData loop label are different clocks, and the
owner timeline sequence also includes WorldUpdate sends. None is a reliable
witness for how many retail Character frames have occurred after packet
delivery. Do not revive one of these variants based on the lucky zero-correction
server-loop repetition.

### Accepted Snowball application witness

Across six clean retail contacts, the matching authoritative pre-physics state
was consistently the third ClientData accepted after impact. Every accepted
ClientData now receives a dense, per-player input sequence that excludes
WorldUpdate sends and cannot skip like the wire loop label. Impact records
`current_sequence + 3` and queues the **origin, radius, and min/max falloff**,
not a velocity vector. Immediately before physics consumes the target frame,
the server recomputes the direction, distance falloff, and crouch multiplier
from the authoritative state at that moment. This mirrors when retail processes
Damage and avoids applying an impact-time direction after the player has moved.
It is an observed application clock, not an ENet acknowledgement.

The final two-client retail artifact is
`logs/combined-replication/snowball-sequence3-final-live/20260714T014849/scenario-run-1/movement-stress-20260713T224938.344225Z.json`.
It records 719 samples over 11.985602 seconds, zero ADJUST, SNAP, visible
rollback, stall, or unmatched sample, 0.000076 maximum and p95 matched error,
and 0.008209 maximum backward step. The pinned runner verified the same source
hashes before and after the run:

- `server/main.py`:
  `5FCE093AB5F18E45119B4D6C5F9E379A158AACD8ACB48B70DFA4F568774AC998`
- `server/player.py`:
  `A255EBE576236CE2FCA656A65A51B625DAFB37CA06F3BF8A0D8B950818416469`
- `server/simulation_runtime.py`:
  `7AA38B827B60B7BACAB228692BD110CF8598DEC0AE113EA21873F95FCC1EA217`

## Disconnect must retire an owner generation

The protocol reuses low numeric player ids immediately. A stale object that
survives disconnect can therefore become an active producer or damage-credit
source for a different human even though all of its integer ids still look
valid.

The first reproduction was a `UseOrientedItem` already in
`_pending_ingame_packets` when ENet delivered DISCONNECT. The next tick could
spawn its projectile after the id had been reassigned. Disconnect now purges
every queued row containing that exact Connection. Packet draining also checks
that `connections[connection.peer] is connection` and
`players[connection.player.id] is connection.player` before each delivery.
This second object-identity generation check is required because a local batch
is removed from the shared deque before processing, and an earlier packet's
`await` can disconnect or replace the connection while its FIFO tail remains in
that batch.

`RoundLifecycle.forget_player` then executes synchronously before
`server.players` releases the id:

- `WorldMutationService.cancel_owner` cancels pending terrain reservations whose
  closures still capture the departing Player;
- projectile-engine rows and live projectile entities are removed together;
  rocket turrets are removed through their controller and broadcast exactly one
  destroy;
- `FireController.forget_player` removes block fires owned by the player and
  extinguishes both the departing target and other burning targets whose damage
  credit names that owner;
- combat forgets pellet groups, assault bursts, and minigun runs; voting removes
  start cooldown/votes and cancels an active vote if the player is its starter
  or target; replication forgets per-recipient cadence/anchor state;
- registry kinds `deployable`, `medpack`, and `grave` owned by the player are
  destroyed. Ordinary construction (including placed flare blocks) and
  objectives deliberately survive because they do not continue executing under
  the owner id;
- machine guns are special because their wire `player_id` is `0xFF`: ownership
  comes from `MachineGunBehavior.owner_id`. An owned gun is unmounted and
  destroyed; a foreign gun whose carrier is departing is unmounted but retained;
- a removed `RadarStationBehavior` calls `_radar_station_removed(team)` before
  destruction. The reference count and `TeamMapVisibility` transition therefore
  remain correct when a team owns more than one radar. Player C4/radar/MG carrier
  convenience fields are cleared after registry cleanup.

This cleanup is intentionally narrower than round reset. It removes active
identity-bound producers and caches while retaining persistent world state.

The same validation process exposed a synchronous `NoDataLeft` traceback when
retail packet 13 omitted its trailing zero UGC-count byte. A bounded runtime
decoder now treats only that absent optional tail as an empty list; every
preceding count/string remains length-checked. After the fix, `/endround 1`
stayed in GameScene for 24 seconds, kept the loop advancing, respawned the
Engineer as class 12, rebuilt nine crates, peaked at 0.57 ms in respawn work,
and left the crash-dump count unchanged at 9.

## ClientData palette bit is not part of the player id

Retail overloads bit 7 of the ClientData player byte: bits 0–6 identify the
player and bit 7 reports `palette_enabled`. A stock Python 2 packet oracle
generated raw `0x83` for player 3 with the palette active and decoded it back
to `player_id=3, palette_enabled=True`. The reversed Cython reader previously
returned `131, False`; it now splits the byte exactly like retail.

The gameplay receive path already used `decode_client_data_payload`, which
performed this split correctly, so the Cython defect was a parity and fallback
hazard rather than a proven cause of live colour drift. The existing retail
artifact in `logs/palette-stability/` held `(159, 0, 0)` through standing,
walking, sprint/jump, observer replication, and reconnect. Do not attribute a
new colour flicker to this bit without a capture that includes both ClientData
and packet 11 (`SetColor`) traffic.

## A `FLARE BLOCK` log identifies flare tool 22

The ordinary and flare tools share the same block model, so the HUD/hand model
alone is misleading. Their wire paths are distinct: normal block tool 5 sends
`BlockLine(40)` and consumes one block; flare tool 22 sends
`PlaceFlareBlock(104)` and consumes ten. The flare handler also requires the
active selected tool to equal 22 and the normalized loadout to contain it.
Therefore a successful `FLARE BLOCK ... cost=10` log proves a real flare-tool
selection; it is not packet 40 being decoded as packet 104, and that handler
does not mutate the player's selected colour.

The accidental selection was made easier by a non-stock normalized carousel
order. Default selections now preserve the retail order with block first and
flare appended last. Keep this ordering covered per class; visually similar
tools cannot be safely reordered as if a loadout were an unordered set.

## Causal owner anchors and the missing application ACK (2026-07-13)

The owner-row history must preserve duplicate pong stamps. The native local
player path force-applies each row, so replacing a duplicate in a dictionary
erases a real cache transition. Production records an ordered bounded history
only after `connection.send()` and assigns monotonic sequences to both owner
sends and received ClientData. A launch may select only a row with `pong`
strictly older than the source ClientData label and a send sequence older than
that ClientData's receive sequence.

This filter proves only that rejected rows were impossible: a row queued after
the input was received could not have influenced that input. It does not prove
that an eligible row had already crossed ENet, been decoded, and been consumed
by GameScene. Reliable-transport completion also stops before that boundary.
Do not rename the queue event as a delivery or application ACK, and do not add
prediction to compensate: the predictive-row retail A/B created a raw
1.250061-block launch rollback.

A later validation-only `OwnerTransitionCoordinator` tried to gate owner
transitions on ENet's aggregate `reliableDataInTransit`. The counter can include
coalesced reliable commands and only proves transport acknowledgement after a
flush; it cannot identify when GameScene applied one particular WorldUpdate.
The experiment also regressed Engineer jetpack behavior and was removed. Do not
restore it or any aggregate-counter equivalent. The bounded causal send/receive
sequence filter above is the strongest signal available on this protocol.

The exact trace in `logs/causal-owner-live/anchor-trace/decisions.json` captured
seven production selections. Five matched the client's recorded cache stamp;
two were two loops newer but had equal stationary positions. There were zero
SNAPs, visible rollbacks, or backward steps. This validates the exclusion rule
without claiming an application signal the protocol does not provide.

## Engineer jetpack transition A/B (2026-07-13)

Eight clean class-12 spawns all exposed fuel 100 and all activated. Four
stationary reliable-transition cycles and four validation-only unreliable
cycles each produced two soft-correction runs and no SNAP; reliable had the
smaller worst error (0.097534 versus 0.112747). Four-second holds consumed
fuel 100 -> 24.90625 in both variants. Modest forward-flight pairs were
identical: zero ADJUST, zero SNAP, and 0.026672 maximum sub-threshold error.

Keep the reliable transition packet and immediate `host.flush()`. They reduce
socket queue latency but do not report when GameScene applies the jetpack bit.
The remaining intermittent soft phase error must be investigated at that
client scheduling boundary; making transition rows unreliable, applying
activation early, or treating ENet completion as an application ACK is not
supported by the measurements. Artifacts are under
`logs/jetpack-retail-audit/final-current/`.

The release edge is different from activation. Native GameScene performs its
update, calls `send_client_data` from `sub_10149CF0` at
`0x10151004-0x1015103F`, and flushes afterward at
`0x1015108B-0x10151168`. Engineer's equipped jetpack prevents the native
post-update jump clear at `world.pyd:0x10012D3F-0x10012D48`; ClientData later
reads that retained jump at `gameScene.pyd:0x1016B037`. The exact client trace
shows physical SPACE key-up stops thrust immediately even while the last
received action bit remains active. Production formerly kept
`_jetpack_physics_active` for one extra consumed recurrence; it now stops on
the consumed release frame.

Do not bypass the calibrated input latch with packet L's release bit. That
validation-only experiment reduced one immediate error but made all six retail
cycles diverge ballistically about 31 loops after activation. It was removed.
With the latch retained, the first eight fuel-valid activations in
`logs/jetpack-release-validation/exact-hold-release-final-12.json` produced six
exact cycles and two single soft corrections, with zero SNAP/rollback. Later
cycles in that diagnostic manually reset only the client meter and are invalid
once authoritative server fuel is insufficient.

### Bounded owner handoff closeout (2026-07-14)

The final IDA pass invalidated the remaining fixed-frame claim. Incoming
WorldUpdate is the only semantic runtime writer of native
`world.Player.jetpack_active` (`Player+0xB0`); ClientData has no active/fuel
echo, and its `ooo` nibble matched `(loop + 7) & 15` in all 448 captured rows.
ENet acknowledgement stops at transport and cannot prove GameScene applied the
row. The two-recurrence server physics delay is therefore an estimate, not an
exact synchronization primitive.

Production keeps the reliable transition plus immediate flush, then excludes
only that owner's ordinary position row for the active fuel burn. Observer
rows, hitboxes, entity interactions, fuel, and the authoritative server shadow
continue at 30 Hz. The former 30-input expiry was not a synchronization
primitive: the isolated run in `logs/movement/engineer_clean_20260714/`
resumed at that boundary and produced a 0.230469-block pre-correction error.
Fuel exhaustion starts a separately bounded handoff:
while the activation key remains held, a server-side ground contact is not a
release witness because the two native worlds can land/relaunch on adjacent
frames. Normal owner rows resume only after key release, a 30-input settle
period, and authoritative ground contact, with a 600-input hard cap.

The replacement release artifact
`logs/movement/engineer_exhaust_release_20260714/movement-stress-20260714T025324.993061Z.json`
contains 291 retail samples and 145 airborne samples through full exhaustion,
held-key fall, landing, release, and correction-row resumption: zero SNAP,
zero ADJUST, zero visible rollback/stall, and maximum matched error `0.024933`.
The deliberately aged cached owner-row loop
remains reported by the scenario but is excluded from its loop-lag failure
gate only for `engineer_jetpack_hold`; native correction and visible movement
gates remain active.

The block-transition A/B must not use Engineer for a generic block test because
its scripted held SPACE also activates the pack. The Soldier artifact
`logs/block-transition/soldier-post-flare/20260714T-current/` committed the
native block at frame 4, sprinted at frame 5, jumped at frame 6, and recorded
zero corrections in both block and no-block phases across 959 samples.

The current-tree regression gate after these changes is `584 passed in 70.12s`
under CPython 3.12. The 50-player/30-second gate reached 59.995 Hz with
4.209 ms tick p99, zero gameplay drops, and 1.59 MiB memory growth.

### Mixed-class movement artifact and scenario-clock trap (2026-07-14)

Do not reuse `logs/movement/live_20260714/` as a generic block/jump result. It
spawned class 12, so the scripted SPACE hold activated Engineer pack 68. Its
scenario elapsed time also accumulated Pyglet callback `dt`; callbacks split
across GameManager loops made a nominal 0.18-second hold last about 0.67
seconds. The runner now advances scenario phases from `perf_counter` and marks
Engineer-only segments with an explicit required class. Generic movement
defaults to Soldier. The apparent 1.208-block launch step in the contaminated
run was therefore not evidence for changing grounded reconciliation cadence.

- **Trusting the reversed Python client for packet bytes:** several layouts are
  incomplete. Use it to form hypotheses, then verify against IDA, generated
  packet classes, and the native client.

## Known incomplete reverse engineering

### Canonical terrain repair and rejected alternatives (2026-07-14)

The join mutation journal closes only the MapSync-to-first-ClientData window;
it cannot repair a settled native BlockManager that rejected or over-predicted
an edit. `TerrainRepairService` records cells, not raw mutation packets. Raw
packets become stale when the same cell is recolored or rebuilt before replay.
At send time the service emits the current canonical state through packet 33
or exact type-6 packet 37. It is delayed and rate-limited because replaying a
terrain packet in the movement frame that caused it recreates the historical
build/jump rollback, and large per-tick cell floods recreate server hitching.

Do not switch this path to `ServerBlockAction(39)` or an invented bulk packet.
Packet 39 is a native client no-op in this build and packet 38 remains
unverified. Do not rerun native collapse from repair: the original checked
Damage already owns effects and topology; repair uses `chunk_check=0`.

### Engineer HUD resource audit (2026-07-14)

HUD functions `set_jetpack_fuel` and `draw_jetpack_hud` reference one fuel
value/bar. WorldUpdate has one fuel short followed immediately by
spawn-protection and deployment-yaw. The second silver cylinder is baked into
the icon. `jetpack_passive` is a boolean native mode, not fuel; its action bit
must be marker-replayed before enabling it.

### Pyglet mouse input audit (2026-07-14)

The current and known-good clients have byte-identical Pyglet 1.2 trees and
movement PYDs. The real client-side difference was the missing raw Win32 mouse
shim. The port registers `WM_INPUT`, dispatches relative motion/drag deltas,
suppresses synthetic center-warp mouse moves while exclusive, and falls back
after repeated raw-input failure. Both patched clients compile and report the
handler installed under their bundled Python 2.7. A wholesale Pyglet 1.4 copy
was rejected because it changes much more than the input path. Modern-Windows
event-loop pacing remains a separate A/B candidate; do not combine it with the
raw-input release or loosen native reconciliation thresholds to hide errors.

The separate `character_jump_smoothing.py` compatibility shim wraps the public
native `Character.update` method rather than patching the PYD. It intervenes
only when a grounded frame becomes `jump_this_frame` and the native cached-row
restore moves position more than 0.25 blocks; velocity and airborne state stay
native. Install it after model loading and GameManager construction. Installing
it at module-import time caused an early `BLOCK_MODEL` `NameError`, which is a
Python startup abort rather than a native crash dump.

### Miner Super Spade footprint audit (2026-07-14)

Retail `handle_superspade_damage` wraps core `0x10082C90`. It subtracts one
from hit x/y/z and calls the native block-distance helper with extent 3. A
direct native packet-37 marker at `(320,256,228)` removed every existing voxel
within x `319..321`, y `255..257`, z `227..229`; 18 cells disappeared because
the nine z=227 candidates were already air. The footprint is therefore a
centered axis-aligned 3x3x3 cube, independent of view orientation. Normal spade
core `0x10082510` separately expands only z-1..z+1.

The server must mutate the full cube once and send one matching area Damage.
Sending type-3 Damage per cell recursively expands 27 overlapping cubes and is
both incorrect and flood-prone. Refund only cells that were actually solid.
The minimized console could select tool 3 but its synthetic click/direct scene
call did not emit ShootPacket(6); packet tracing showed only ClientData. Do not
count that automation attempt as either a pass or failure. The server entry
paths remain characterized by packet-level tests, and the native expansion is
proved by the direct packet-37 marker above. A future foreground physical-click
capture should close the end-to-end presentation gate.

### Late projectiles, Block Cannon, and native crash boundaries (2026-07-14)

The final retail strings identify tool 29 as **Block Cannon** and describe it
as rapid construction using blocks for ammo. Recovered `snowBlowerWeapon.py`
confirms that each shot subtracts `Character.block_count` and calls
`send_snowball(position, forward * 50)`. The old server implemented only its
10-damage Snowball blast, so no persistent voxel existed.

The authoritative transition snapshots firing palette and source loop,
colours the type-24 projectile, and on terrain contact commits the last free
supported voxel. It sends `BlockBuildColored(33)` before the existing
Damage(37)/DestroyEntity(19) pair. Packet 33 deliberately uses the ordinary
mutation broadcaster: active peers render exact RGB, an in-progress MapSync
retains it in the contiguous journal, and later full VXL snapshots include the
canonical cell. Do not charge again on impact; successful packet-10 admission
already spent one shared block.

The first live colour test remained grey even though the local palette changed.
That was not projectile serialization: `SetColor(11)` rejected tool 29 because
its gate recognized only block and flare. Both stock and UGC Block Cannons
activate the same retail palette, so tools 29/48 now share the palette gate.
After allowing the packet and waiting for selected-tool ClientData to reach the
server, live impact logged exact `0x2468AC`; a newly launched process received
the same RGB from full VXL sync. No dump newer than
`aos_crash_2026_07_14__13_41_09.dmp` appeared.

Late projectile presentation has two non-negotiable native boundaries:

- GL tool 55 cannot be echoed as `UseOrientedItem(10)`. The retail remote path
  constructs stale `GLGrenade`, which calls `Entity.initialize` with four
  arguments where five are required. CreateEntity type 33 is the validated
  safe flight path. Chemical 32, Sticky 34, and ProjectileMine 37 use the same
  server-owned entity strategy.
- BlockFire must serialize `face=FACE_TOP (4)` even for a side-attached fire.
  Base `Entity.set_face` rotates every other supported face. BlockFire has no
  model, so that rotation raises `AttributeError` and removes GameScene. Its
  internal anchor and serialized orientation are intentionally distinct.

The local Chemical and Sticky weapon classes already own `AnimThrowGrenade`;
CreateEntity supplies the world projectile their send methods do not create.
Observer hand-animation presentation still needs a foreground visual capture,
but it must not be "fixed" by reintroducing the GL packet-10 crash.

- Exact behavior and damage constants for every explosive and placeable entity
  still require native two-client validation.
- Round-end score-screen timing and safe entity teardown ordering need a clean
  repeated-cycle capture.
- Class-change packet ordering is observed to vary; the server must accept both
  orders rather than relying on a single UI trace.
- Some item/entity render constructors have stricter field requirements than
  the reversed Python implementation exposes.
- Engineer pack-68 thrust and jump-bit lifetime are now certified against the
  native world/Character oracle. The first stock velocities from rest are
  `-0.0032786874`, `-0.0065036258`, and `-0.0096756974`; the rebuilt mover
  matches them to float precision. The grounded launch reconciliation holds Z
  for one grounded tick only, never a sustained jetpack frame. A clean
  two-client hold/release flight test is still required for observer-side
  presentation, and explosion impulse timing remains open.

### Placement replay, Machete, and equipment-slot split (2026-07-14)

The delayed terrain repair service was initially attached as a global
`WorldManager` mutation listener. That made every successful normal build,
prefab cell, dig, and collapse replay after 120 ticks. Packet 33 and Damage 37
enter native visual callbacks, so this safety mechanism itself produced extra
placement/debris effects. The listener was removed. Only validation failures
and cancelled deferred predictions explicitly call `record_cells`; late join
state remains owned by MapSync plus the mutation journal.

The earlier claim that Machete's 2.0 was character-only damage was wrong.
IDA resolves `BlockManager.handle_machete_damage` to `0x1008AA60`, where it
loops over `range(z, z+2)` and calls `handle_single_block_damage`. The server
now accumulates two damage on both vertical cells and broadcasts one type-35
packet per strike. A compatibility BlockLiberate for Machete cannot also be
accepted, because the retail tool's ShootPacket already owns that swing.

The Engineer/jetpack bug was a data-model split, not intermittent fuel logic.
`CLASS_ITEMS` defines one equipment slot containing pack 68 and Disguise 64,
but normalization selected Disguise and then independently appended a class
jetpack; spawn repeated the fallback. Both append paths are gone. Rocketeer
retains Jetpack2 67 as the first choice in its separate equipment slot, and
the common pack physics table already drives its slower drain/refill model.

The first foreground Machete replay was a false negative caused by the test
harness assigning `Character.pitch` directly. `ClientData` then contained the
derived unit orientation, but the weapon emitted a malformed ShootPacket with
the literal 60-degree pitch in `ori_y`; the server correctly rejected it.
Foreground aim now uses `GameScene.mouse_move`, the same native route as real
mouse input. The corrected run emitted three normalized ShootPackets and three
type-35 Damage packets without reconciliation or a new crash dump. Keep this
distinction in future weapon tests: direct camera-field assignment is not a
valid substitute for native input when validating shot bytes.

### Three-prefab loss and the fake first prefab tile (2026-07-14)

Live packet traces showed that the client sent three valid prefab selections,
but the server logged values such as
`['prefab_caltrop', 'prefab_superpoleprefab_sfort_wall', '']`. The class
normalizer was initially suspected because it correctly filters unknown names.
The corruption had already occurred one layer earlier in `lzf_decompress`.

LZF back-references encode `distance - 1`. The decoder used the encoded value
directly, so overlapping copies read one byte too near the output cursor. Short
numeric fields often survived and hid the defect, while repeated `prefab_`
prefixes reliably triggered it. Restoring `+ 1` reproduces the retail
`shared.lzf.pyd` output exactly. The decoder now also rejects truncated literal
runs, truncated/invalid back-references, and output above one megabyte instead
of indexing arbitrary buffer positions.

The apparent one-block prefab was a separate native UI rule. Decompiled
`SelectClass.get_class_images` inserts `FLAREBLOCK_TOOL (22)` before real
prefabs unless that tool is present in `InitialInfo.disabled_tools`. It was not
safe to remove a name from `PREFAB_LISTS`, because no such prefab existed.
Advertising tool 22 as disabled and using the same default in class selection
removes the tile and prevents it from returning in CreatePlayer.loadout.

Evidence:

- `tests/test_lzf_codec.py` uses a compressed Engineer packet produced by the
  original 32-bit `shared.lzf.pyd` and asserts all three names and order.
- `tests/test_reversed_spawn_handshake.py` covers prefix handling through the
  actual pending-selection cache.
- `tests/test_initial_info_features.py` pins the native flare-tile suppression
  flag, while `tests/test_class_selection.py` pins its spawn-side equivalent.
- `logs/prefab-validation-27019.stderr.log` records a clean stock-client run
  with the non-default Engineer selection
  `prefab_superdome/prefab_superbridge/prefab_platform`. Packet 13, the pending
  transaction, and CreatePlayer all contain the same three strings; the spawned
  client console reported that same list and `disabled_tools=[22]`.

### Admin rollover and CTF BASE crash (2026-07-14)

Changing a live map/mode by mutating config and restarting mode objects is not
a supported retail transition. `InitialInfo` and `StateData` select native
scene tables during construction; applying a new mode or VXL underneath an old
`GameScene` can crash before Python reports a useful error. Map preflight also
cannot parse VXL synchronously in the 60 Hz packet-drain callback.

`MatchTransitionService` now separates same-map restart from full session
rollover. Map and mode requests retain a background task; VXL preflight runs
through `asyncio.to_thread` without making the packet-drain coroutine await it.
Invalid/path-shaped names leave the current match untouched. Mode change reloads
the current VXL so clearing the old mutation journal cannot retain server-only
construction, and mode-filtered metadata is rebuilt. A valid rollover gates old
connections before new mode startup, clears old queues and the terrain journal,
starts the new world, then disconnects the old sessions with reason 18. The
normal handshake owns rejoin. Simulation also skips a gated network player, so
the retiring body cannot be simulated once against the newly swapped VXL.

The remaining CTF rejoin freeze was a different packet invariant. The client
traceback ended at `GameScene.create_entity` with `KeyError: 1`. IDA core
`sub_10178B80` reaches `PyObject_GetItem` at the Cython source-line-2970 error
path. A live tracer query of `aoslib.scenes.main.gameScene.ENTITIES` showed
runtime keys `{2,3,4,5,7,8,9,10,11,13..25,27..39}`: `INTEL_PICKUP=16` exists,
but legacy `BASE=1` and `FLAG=0` do not. Sending the CTF tent as packet-21 BASE
therefore indexed the runtime constructor table with unsupported key 1.

Do not catch or patch over that client exception. CTF keeps its base anchor as
a server-only `wire_visible=False` registry marker and sends only the supported
intel entity. Join reveal, broadcast create, mode restart, and destroy cleanup
honor visibility symmetrically. The authored VXL/base zone represents the base
area until a separately validated minimap-zone/tent protocol is implemented.

Retail evidence in `logs/ctf-fixed-20260714-173357-*` covers fresh CTF join,
CTF restart, invalid map rejection, CTF map change/rejoin, CTF -> TDM/rejoin,
and kick. `logs/ctf-cleanup-20260714-174033-*` confirms the final CTF restart
has no traceback, `KeyError`, or invalid-entity destroy warning. The highest
observed transition tick was 4.23 ms, no slow tick exceeded 10 ms, and no dump
newer than `aos_crash_2026_07_14__13_41_09.dmp` appeared. The current full
regression suite is `627 passed in 72.72s`.

The first fresh-map `/mode` implementation awaited the worker from inside the
packet-drain handler and produced a 1337.49 ms tick even though the event loop
itself was not blocked. That design was rejected. The retained-request version
in `logs/mode-background-final-*` reloaded CityOfChicago for CTF, disconnected,
and rejoined cleanly with a 3.22 ms maximum transition window and no client
error.

## CTF minimap and dropped-intel ownership

The earlier CTF implementation treated a server-only base anchor as if it
were enough UI and assumed `DropPickup(71)` created a lasting world object.
Neither assumption matches the retail client. `GameScene.ENTITIES` has
`INTEL_PICKUP=16` but no `BASE=1`, while the native DropPickup handler clears
the carried tool without retaining a ground entity. This explained both the
missing capture destination and intel apparently disappearing after death.

IDA identified `GameScene.process_packet_minimap_zone` at `0x101A4A70`, its
billboard construction at `0x101A5B50`, and DropPickup at `0x1019AE10`. A live
packet probe against `hud.pyd` then fixed the opaque zone fields: key is stored
as `visible_team`; A2018/A2019, A2020/A2021, and A2022/A2023 are raw voxel
min/max pairs for X, Y, and Z; icon 6 constructs the CTF base billboard. The
same probe proved `ChangePlayer` action 8 toggles
`Player.high_minimap_visibility`, while type-16 `IntelPickup` owns a native
minimap marker. A tracking MinimapBillboard was rejected for carriers because
its tracking id resolves scene entities, not player ids.

The retained design gives each representation one owner: packet 43 owns base
zones, type-16 entities own ground-intel icons, and ChangePlayer action 8 owns
the carried-intel marker. The server sends mode state through `reveal_to` after
the generic late-join reveal. DropPickup is immediately followed by a
persistent CreateEntity, and the same visible base bounds are used for capture.
An isolated retail join and same-scene restart showed exactly two zones and
two ground intel objects without duplication, traceback, or a new crash.

## VIP is a mode state machine, not a generic visibility flag

The recovered `high_minimap_visibility` field is only the native presentation
primitive. Treating it as “VIP state” without owning class selection, lives,
disconnects, and sub-round reset leaves an unwinnable hybrid mode. The old
handwritten `aceofspades_decompiled/server/aosmodes/vip.py` was rejected as
ground truth: it ended immediately on a boss kill and reassigned a disconnect,
contradicting the retail sudden-death flow and recovered constants.

Native evidence establishes the stable boundary. `MAFIA_TEAM_CLASSES` contains
ordinary classes 6-9; `MAFIA_VIPS` maps playable teams to boss classes 10/11;
incoming boss damage is multiplied by 0.5; and ChangePlayer action 8 sets the
scoreboard/minimap crown state. SelectTeam bypasses SelectClass only when the
StateData team `locked_class` bit is set. An original Python 2 packet experiment
also proved that InitialInfo offset 150 is a null-terminated `texture_skin`:
empty is `00`, while `mafia` is `6d 61 66 69 61 00`. Keeping the old one-byte
placeholder shifted every later InitialInfo field for mafia mode.

Ownership is therefore explicit: `VIPMode` owns the phase and boss identities;
RoundLifecycle asks it whether a dead player may respawn; join normalization
owns gangster-only class/loadout coercion; StateData owns class-picker bypass;
InitialInfo owns the mafia presentation skin; and ChangePlayer action 8 owns
only the marker. Full match restart must demote old bosses before BaseMode's
outer respawn, or a stale boss class is briefly published into the new match.

The three-client run was normally 0.27-0.40 ms/tick, but recorded one 58.03 ms
`entities` spike when a timed grave/explosion lifecycle matured. No VIP mode
callback exceeded 1 ms and the spike did not affect the state transition, so it
is not folded into VIP logic. It remains a separate entity-batching/performance
follow-up; future work must reproduce it with grave-only instrumentation before
changing the VIP state machine.

## Zombie Infection and Glide Jetpack evidence (2026-07-14)

The video description was useful for presentation, but retail constants remain
the authority for timing and scoring: `ZOM_ROUND_TIME=600`, preparation time
60 seconds, two initial infected when population permits, and zero infected
respawn delay. The server must cap initial infection at `population - 1`; using
the literal count on a two-player server consumes every survivor and ends the
round immediately. The survival clock begins at outbreak, not mode activation,
because a server may wait indefinitely for its minimum population.

Mode id 2 already has native Zombie presentation. `CLASS_ZOMBIE=4` is the
stable melee-only class. Fast and Jump Zombie constants exist, but exposing
them through the ordinary class picker is unsafe in this client because their
picker artwork/path is incomplete. StateData therefore class-locks infected
players to class 4. Patient Zero crosses a KillAction/CreatePlayer boundary;
changing only the server-side team leaves the old human Character alive.
ChangePlayer action 8 drives the last-survivor heart/high-visibility marker,
not a new zombie-specific packet.

The user's “legacy Engineer” maps to retail class 2 Rocketeer, whose equipment
choice is tool 67 `JETPACK2` / “Glide Jetpack”. It is distinct from class 12
Engineer tool 68. IDA on the original
`G:/AoSRevival/AceOfSpades_no_steam_new/aoslib/world.pyd` located the per-pack
switch at `0x10012C47` in `sub_10012B80`. Its decoded thrust globals are:

- tool 66: `0x1001F930` = 0.0450000018;
- tool 67: `0x1001F928` = 0.0125000002;
- tool 68: `0x1001F920` = 0.0199999996;
- tool 69: `0x1001F918` = 0.0250000004.

At 60 Hz, 0.0125 is slightly weaker than gravity, so Glide slows descent but
does not climb from level ground. Its 17 fuel/s drain yields about 5.29 seconds
from the usable 90-unit reserve, longer than Engineer and over four times the
normal pack. The existing native-shaped mover already contained this branch;
changing it would have reduced parity. Tests now lock it, and a live retail
W+SPACE event activated pack 67 while moving horizontally with negligible
vertical gain.

The first live Patient Zero spawn also made a separate performance issue
visible: fallback TEAM2 candidate discovery ran lazily in `process_respawns`
and produced a 268.45 ms tick. Candidate discovery is map-derived work, so it
now runs for both teams inside `WorldManager.load_map`. Startup has no gameplay
loop yet, while map/mode transitions already execute candidate map loading in
their worker thread. Runtime spawn still revalidates the chosen cell after
terrain edits. On CityOfChicago, the prewarmed runtime selection measured
0.175 ms average and 0.270 ms maximum across 50 team-balanced samples.

## Bot process isolation and terrain restart state (2026-07-14)

The old `BotManager` looked inexpensive in small unit tests but performed
target scans and VXL LOS work synchronously on the simulation thread. It also
called a private hitscan helper, failed LOS open on errors, tracked hidden
players through live snapshots, bypassed mode join/leave lifecycle hooks, and
created a peerless connection that `SimulationRuntime` treated as inactive.
The apparent bot existed on the scoreboard while its native physics could be
skipped. The old 50-bot baseline attributed roughly 84% of sampled runtime to
synchronized bot LOS.

The retained design uses one supervised Windows-spawn child and a bridge
thread. The first native-map implementation cloned full `ServerVXL` state in
the worker and reached 267.8 MiB for CityOfChicago, failing the 256 MiB gate.
A collision-only column bitset map was substituted and compared against
`ServerVXL` across 20,000 random cells with zero mismatches; worker memory fell
to about 53 MiB.

A second subtle failure existed in the initial supervisor: after deltas were
successfully sent, restarting the worker resent only the original raw VXL.
Navigation could therefore forget old builds/destroys. Generating a current
VXL from `WorldManager` on the 60 Hz thread was rejected because the overflow
path would introduce unbounded serialization into gameplay. The bridge now
retains a canonical coalesced overlay and composes `base + overlay` into every
restart/overflow `MapSnapshot` off-thread. A characterization test sends and
clears deltas, simulates a fresh child input queue, and proves the replacement
snapshot still contains every committed cell.

Current implemented scope is deliberately smaller than the full bot roadmap:
process supervision, bounded versioned messages, dynamic tiled Recast/Detour,
persistent DetourCrowd steering, fallback layered A*, fair LOS/last-seen/sound/
team reports, natural aim, class-filtered movement affordances, TDM combat,
basic CTF/Zombie/VIP/Arena goals, stuck breach/bridge recovery, class
composition, shared deployables/projectiles/prefabs, bounded construction
reservations, resource seeking, and cover selection work. Prefab expansion is
spread across simulation ticks and reserves inventory before enqueueing; a
disconnect or round reset cancels/refunds unfinished ownership safely.

The first 900-second bot soak held 60 Hz but failed the strict bot budget by
0.0052 ms (`0.7552` versus `0.7500` ms p99). Transition-only input writes were
already active; the remaining waste was per-tick allocation and policy work:
awaiting a one-Hz population coroutine on all 60 ticks, copying the runtime
dictionary, checking unchanged class lifetimes, re-normalizing eight boolean
flags, and an unnecessarily expensive waypoint key. Removing only that
redundant work produced `0.4198` ms bot p99 in the final 900-second gate.

That soak audit also exposed a correctness issue hidden by the old pass/fail
rules. Server-owned bots submitted shared block actions with a server loop
stamp, but have no retail `last_applied_input_loop`; six valid builds waited
until timeout and were repaired away. Bot mutations are now ready at the
existing post-physics boundary, while human mutations still require their
client-history watermark. The capacity harness now fails on any rejected or
expired world mutation, map-journal overflow, or terrain-repair drop/failure.
The final strict soak committed two bot mutations with all those counters zero.

Richer projectile tactics, statistical hit-rate calibration, glider route
tuning, a rendered debug overlay, deterministic mode contributions, and clean
retail mode observation remain open. Headless movement/entity evidence does
not prove stock-client animations or complete objective play.

## Classic CTF scene-id trap and mode-policy fairness (2026-07-14)

The constants expose `MODE_CCTF=11`, which initially makes a new scene id look
reasonable. The shipped playlist contradicts that assumption: it declares
`modes ['ctf']` and `classic True`. Hex-Rays at gameScene.pyd `0x10126C20`
(`GameScene.is_in_classic_mode`) confirms the implementation returns the
GameManager's `classic` attribute. Therefore Classic CTF must retain native CTF
scene id 8 and vary the InitialInfo feature bit. This invariant is covered by
`tests/test_classic_ctf.py`.

The playlist also explicitly enables shooting with intel, disables intel
auto-return, and disables Classic SMG/shotgun. The common matchmaking defaults
leave return-on-touch and own-intel-at-base requirements off, so those were not
invented as Classic rules. Deuce selection is normalized with the disabled
tool set, preventing a forged loadout from re-enabling either weapon.

Mode objectives require the same information discipline as combat. Normal CTF
publishes a carrier marker, VIP publishes crowns, and Zombie publishes the last
survivor marker, so those exact positions are sanctioned inputs. Classic turns
the minimap off; its policy consequently ignores an exact stolen carrier or
unseen dropped intel supplied in the generic objective snapshot. Friendly
carrier escort remains legal because teammate state is shared. Policy unit
tests lock both sides of that distinction.

## Evidence required before changing an invariant

1. Add a characterization test that captures the current behavior.
2. Save the relevant IDA address/decompilation or a native packet capture in
   the replication findings document.
3. Make the smallest server-side change preserving packet bytes outside the
   target behavior.
4. Run:

```powershell
py -m pytest -q
py scripts\server_capacity.py --players 50 --seconds 30 --port 27016
```

5. For movement, class/loadout, entity, map-sync, or round lifecycle changes,
   also run a clean retail-client scenario and check that no new `.dmp` file was
   created. For a release candidate, extend the capacity run to 900 seconds and
   exercise reconnects and round transitions during the soak.

## Concurrent-join roster race and bot Player parity (2026-07-15)

Human invisibility was not a WorldUpdate range or ENet-loss problem. Both
clients could snapshot an empty roster during `send_connection_data`, finish
MapSync, and then submit NewPlayerConnection while the other receiver was still
gated from gameplay broadcasts. The old `reveal_world_to` assumed the handshake
snapshot plus gated CreatePlayer broadcasts were exhaustive; neither client
ever received the other's CreatePlayer, so no remote Character existed for
WorldUpdate to update.

Blindly replaying every CreatePlayer at reveal was rejected. Duplicate creation
is crash-sensitive in the compiled retail scene, and player IDs are reused.
Each connection now records `(id(Player), replication_generation)` by player ID
and catches up only missing concrete lives. It also replays a retained death or
departure that occurred during map loading. A reliable remote-only WorldUpdate
follows the roster catch-up; the local row is excluded so state initialization
cannot reconcile or move the joining owner.

Bots exposed a separate version of the same visual symptom. A retail human sets
`can_display_weapon` through ClientData, but peerless bots never send that
packet. Their action updater forced the flag false, making every remote weapon
invisible even when the tool byte was correct. Bot spawn and action transitions
now maintain display bit `0x10`. The public combat gateway also rejected melee
tools because it only recognized firearm profiles; Zombie/spade intents now use
the normal CombatSystem melee route. A strict worker smoke proves visible tool
state, Shoot replication, damage, KillAction, and lifecycle respawn rather than
accepting scoreboard presence as bot parity.

The first TDM policy also had no strategic destination beyond visual range, so
teams wandered locally and rarely met. All team modes now publish stable team
anchors and the default combat policy advances toward the enemy side with
formation offsets. Active Zombie bots select the nearest living survivor from
the mode roster regardless of visual distance, while the final firing decision
still requires fresh LOS. Repeatedly stuck Zombies may request a real authorized
breach or selected-prefab build step; they never mutate VXL directly.

## Bot row stamps and duplicate-name owner aliasing (2026-07-15)

The first three-retail-client observer rig separated owner reconciliation from
remote rendering. Human movement remained aligned, but bots diverged by up to
31.50 blocks between observers and snapped back to spawn. Every bot Character
reported `network_position_loop_count == 0` even while its server position
changed. This was not packet loss: WorldUpdate's per-player `pong` field is also
the remote Character dedupe key. Human pong advances from ClientData; a
peerless bot has no ClientData and therefore retained zero forever. Bot rows
now use `server.loop_count`. Do not apply this clock to a retail owner row;
owner prediction requires the exact consumed client loop.

After that fix, the same rig exposed a deterministic five-block owner move
while all movement keys were released. Parity showed client position, server
position, and the client's PositionData report change together from one fixed
spawn column to another. The change occurred immediately after another retail
client named `KikoTs` joined at that second location. The compiled client had
aliased its local Player to the later same-name CreatePlayer; subsequent no-id
ClientData/PositionData were validly attributed by the server connection but
contained the other local Character's state. This explains why ordinary
movement debugging alone could not find the cause.

Rejecting a second local test client would hide the problem, so join now
allocates unique case-insensitive names within the recovered 15-byte retail
limit. The corrected run deliberately launched three unchanged installations
with the same configured name and observed `KikoTs`, `KikoTs~2`, and
`KikoTs~3`. Artifact:
`logs/live-desync-20260715-botstamp-unique-names/remote-replication-20260714T223712.422140Z.json`.
It records zero missing players, zero human/bot teleports, zero stale moving
rows, stable local identity, and two-observer disagreement below 1.4 blocks.
Corpse-to-respawn position changes and a logged blast impulse are classified
separately from replication teleports.

## Drill contact was a compact expansion, not one voxel (2026-07-15)

The initial Drill implementation mixed two valid but incompatible models: the
server removed only the collision-trace cell, then sent native Damage type 10.
The retail client expanded that packet into a radius-2 bore, leaving most of
the visually absent cells solid on the server. Players consequently stood on
or collided with invisible terrain after drilling.

Replacing the live packet with an 81-packet exact-cell flood was rejected. It
would discard the Drill's native sound/particle path, amplify network and
BlockManager work, and introduce collapse ordering differences. The accepted
split is one compact type-10 packet for settled live clients, the same measured
81-cell canonical mutation on the server, and exact type-6 cells only in the
late-join journal. Reconnect replay cannot use type 10 because its required
projectile entity may already be destroyed.

The `causer_id` check exposed a second trap: entity id zero is valid. Code of
the form `projectile.entity_id or owner.id` silently changes zero into a player
id, causing the native Drill handler to reject the entity. Preserve explicit
`None` sentinel checks for every entity-id field.

Evidence before changing the footprint again:

- repeat direct calls to the compiled retail `handle_drill_damage` in a solid
  volume and record the complete removed-cell set;
- prove the result for multiple seeds and both contact/destroyed damage values;
- run a real Drill shot and confirm no `Drill entity ID not valid` client log;
- compare canonical VXL cells with a settled client and a late joiner.

## Bot combat stalls, stale decisions, and multikill lifetime state (2026-07-15)

The apparent late-match bot freeze combined two independent failures. With an
empty clip, combat considered oriented equipment before reload. Selecting that
grenade/tool calls the normal `Player.set_tool`, which correctly cancels an
in-progress weapon reload; subsequent worker frames could repeat the switch.
When both clip and reserve were empty, the policy continued submitting FIRE
instead of closing with an actually selected melee tool. Reload now has strict
priority, active reload frames cannot select another tool, and a truly dry bot
paths into melee range. No hidden ammunition or direct inventory mutation was
introduced.

The supervisor's old bounded FIFO could also leave every bot acting on stale
perception during a burst. Frames now coalesce by player id and replication
generation, and the worker coalesces each received batch again. The newest
frame for each concrete life survives without letting one noisy bot consume all
64 slots. Strategic decisions run at 8 Hz while the normal motor and server
simulation remain at 60 Hz.

Combat behavior now uses persistent 0.7-1.6 second strafes, grounded
cooldown-bounded jumps, real authorized block/prefab cover under pressure, and
proactive selected-melee breaching for Miner/Zombie route obstructions. Every
world action still passes through `BotActionGateway`, inventory checks,
construction safety, and the canonical mutation/replication path.

`KillAction.kill_count` was independently wrong: the server sent cumulative
scoreboard kills, so the native multikill presentation never returned to one
after its owner died. IDA of `GameScene.process_packet_kill_action`
(`gameScene.pyd`, implementation `0x10194940`) confirmed the packet field is
passed into the HUD. The server now tracks a separate current-life streak,
resets it on death and round transition, and retains cumulative `kills` only
for scoring.

Validation:

```text
focused bot/combat/round suite: 80 passed
bot_combat_smoke.py --seconds 25: both bots reloaded and fired afterward;
  8 ShootPackets, one authoritative death, one lifecycle respawn
bot_runtime_smoke.py --seconds 20 --bots 12 --mode tdm:
  every bot moved 86.86-138.04 blocks; clean worker shutdown
full suite: 716 passed in 89.21s
```

## Damage without remote sound: wrong shoot-packet direction (2026-07-15)

The server previously rebroadcast the complete client `ShootPacket(6)` after
validation. Authoritative hits and terrain damage therefore passed tests, but
the retail client printed packet 6 as unhandled. A human still heard their own
locally predicted weapon, while every remote player—and especially a peerless
bot with no predicting client—was silent. The old bot smoke made the same
mistake by accepting an observed packet 6 as proof of a replicated shot.

The native receive contract is packet 8. IDA of
`GameScene.process_packet_shoot_feedback` (`gameScene.pyd:sub_101935C0`, source
line 3642) recovers this sequence:

1. require `packet.shooter_id` in `self.players`;
2. obtain that player's `character` and require it to exist;
3. require `packet.tool_id == character.tool_id`;
4. call `character.shoot(packet.seed)`.

The last call selects the actual equipped firearm and owns its sound and visual
shot. The server sends packet 8 to observers and excludes the firing human to
avoid doubling local prediction. Do not replace this with generic
`PlaySound(23)`: that loses weapon selection, positional behavior, and
muzzle/tracer state. Do not send both packets 6 and 8; packet 6 remains C→S
only.

An isolated retail run then exposed the boundary hidden by decompilation: a bot
holding `SpadeTool` received packet 8, reached `Character.shoot`, and raised
`AttributeError: 'SpadeTool' object has no attribute 'shoot'`. Spade-family and
Machete classes implement `use_primary`, not `shoot`; their native remote path
is WorldUpdate primary-action bit `0x01` followed by authoritative
`Damage(37)`. The bot motor's old one-tick pulse could fall between 30 Hz
snapshots, so it is now latched through two future 60 Hz loops. Firearm packet
8 and melee action-bit replication must remain separate.

Evidence:

```text
focused combat/bot suite: 79 passed, including packet-8 firearm and melee latch
bot_combat_smoke.py --seconds 20: shots=12 packet-8 events, reloads=[0,1],
  post_reload_shots=[0,1], one death, one respawn
retail v2: 7.19 s in GameScene with 11 bots; no unhandled packet, traceback,
  crash dump, SNAP, ADJUST, or visible rollback
full suite: 719 passed in 93.84s
```

## Zombie stand-off, wrong held tool, and native breach geometry (2026-07-15)

The Zombie bots' apparent path failure was first a policy failure. Generic
combat treated Zombie hand 24 like a firearm whenever generic ammo counters
were nonzero, deliberately backed away inside firearm spacing, and allowed an
ammo-crate diversion. Detour could also finish at the nearest walkable polygon
without a final direct-contact step because the survivor's occupied cell is not
itself walkable. Zombie engagement now has its own motor: global survivor goal,
no firearm spacing/reload/cover branch, direct final approach inside six
blocks, continuous sprint, variant-aware jumps, and a fresh-LOS requirement
only at the actual claw strike.

Terrain behavior had three separate boundaries. IDA proved Zombie damage type
17 expands to the same centered 3x3x3 handler as Super Spade. The worker now
uses the stock 0.4-second cadence, while CombatSystem commits that exact cube
and emits one compact native area packet. Stuck Zombies may select native hand,
bone, or head prefabs through tool 28; the worker still cannot mutate VXL, and
the shared prefab service retains inventory, support, collision, reservation,
and replication checks.

The first end-to-end smoke exposed a lifecycle defect which decision tests did
not: `_select_spawn_weapon` searched firearm profiles only, so a Zombie whose
CreatePlayer loadout contained 24/28 initially advertised rifle 6 in
WorldUpdate. It now selects a normalized firearm when present, otherwise a
normalized melee primary, otherwise the first normalized utility. The initial
Zombie row therefore cannot describe an item absent from its loadout.

Fast/Jump Zombie are native hidden classes without picker icons. Humans remain
restricted to base Zombie, while a server-owned bot selects its deterministic
variant before Player construction, spawn, and CreatePlayer. Each variant also
receives the three native Zombie prefabs. This ordering prevents a base-model
CreatePlayer followed by a silent server-only class mutation.

Validation:

```text
focused bot/Zombie/construction/combat suite: 105 passed
bot_zombie_smoke.py --seconds 15:
  real worker PID 52760; Fast Zombie closed 10.00 -> 0.90 blocks,
  dealt stock 35 damage, and exposed hand 24 plus the primary swing
bot_worker_smoke.py --restart:
  intentional child termination recovered after one supervised restart
bot_runtime_smoke.py --seconds 12 --bots 12 --mode zombie:
  real worker PID 66460; all 12 moved 3.82-32.31 blocks, 3 mutations,
  replicated entities types 8/9/10/30, zero unplanned restarts
full suite: 729 passed in 93.84s
```

The `--inline-worker` flag is smoke-only for restricted Windows sandboxes where
`multiprocessing.Queue` fails at `_winapi.CreateFile` with WinError 5. Production
and normal validation omit it and require a real child PID. Scoped unsandboxed
validation now proves that production path; no in-process production fallback
was added.

## Accelerated City soak: what the first trace disproved (2026-07-15)

The first CityOfChicago Zombie trace reported 29 seconds of water exposure at
player z=235.75. That was a diagnostic error: a position at that height can be
supported by the lowest legal dry voxel. Only the authoritative physics
`wade` bit distinguishes it from the universal waterbed, so the monitor no
longer guesses water from position.

The same trace did prove a real worker defect. Several bots retained an
objective role while emitting a zero movement vector for tens of seconds.
Global navigation had no corridor; the generic recovery refused a zero heading
and, after three earlier attempts, never reopened its attempt window. Adding
more random patrol directions would have hidden the symptom and could steer
over water. The accepted invariant is layered: global navigation first,
bounded voxel action planning second, stationary recovery context third, and
authoritative breach/build as the only topology mutation.

Do not treat an accelerated soak as gameplay acceptance. It uses the exact VXL,
messages, policies, LOS, local planners, and terrain deltas, but its kinematic
adapter is intentionally small. A change to native movement, replication,
combat cadence, sound, or client-facing entity packets still requires the real
worker smoke and retail observers documented in `docs/RUNBOOK.md`.
