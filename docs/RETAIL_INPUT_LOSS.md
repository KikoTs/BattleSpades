# Lost ClientData at internet ping: why the retail client kept correcting

Measured 2026-09-24 with the patched retail client (docs/RETAIL_JUMP_RESTORE.md)
through `scripts/udp_lag_proxy.py` (40 ms one-way delay, ±8 ms jitter, 0.5 %
loss per direction, i.e. ~80 ms RTT).

## Symptom

On a loss-free local link the movement gate was clean (0 ADJUST, 0.0014
blocks max error). Through the lossy proxy, stationary jumps, straight sprint
and turning stayed clean, but from some point in `slope_diagonal` onward every
self row produced an ADJUST with a constant error of one frame of motion
(~0.25 blocks at sprint) that never cleared, through `jump_run` as well:
75 + 70 corrections in 12 s. That is the "rollback / lag while running" feel.

## Evidence

A per-tick input trace on the server (`debug_selfrow` now also writes
`logs/input_samples.ndjson`) next to a per-frame sample of the client's native
`movement_history` showed:

- the client's history labels were contiguous (3005, 3006, 3007, …);
- the server's consumed labels went 3005 → 3007: packet 3006 never arrived
  (four such holes in 636 packets, matching the proxy's 0.5 % loss);
- until 3005 every server row equalled the client's history entry for that
  label; from 3007 on the server position equalled the client's entry for the
  *previous* label, i.e. authority was one frame behind for good;
- each ADJUST rewinds the client to that late position and replays, so the
  next row is off by one frame again — the correction chain is self-sustaining.

`scripts/udp_lag_proxy.py --sniff` classified the client's 60 Hz upstream as
ENet `SEND_UNSEQUENCED`: no retransmission, possible reordering. `docs/PROTOCOL.md`
previously claimed the stream was reliable.

IDA of `gameScene.pyd`: `GameScene.update` does `self.loop_count += 1` once
per update (one physics step, one history row, one ClientData per label);
`process_packet_clock_sync` computes `server_loop_count + latency` and only
assigns it to `loop_count` when `abs(loop_count - estimate) >
MAX_CLOCK_SYNC_DIFFERENCE` (module constant, value 10). So labels never skip
inside the client except for clock jumps of more than ten loops. The earlier
"labels skip by two in one 17 ms frame" observation came from the tracer,
which samples once per *render* frame while the game loop can run two
updates in one render frame; both updates do send a packet.

## Fix (server)

`Player.simulate_tick`: when the smallest buffered label is more than one past
the last applied label and the gap is at most `[debug] input_gap_fill_limit`
(default 8, hard-capped at 9), the server simulates the missing frame with the
held input (`_synthesize_missing_frame`: previous packet's locomotion buttons,
crouch and orientation, exactly what the one-frame latch would have applied)
and acknowledges it under the missing label; the real packet is consumed on
the next tick, keeping the one-frame-per-tick pacing. A late-arriving copy of
that label is dropped as stale. Wider gaps are clock jumps and are never
refilled. `tick stats:` reports `synth=`.

Tests: `tests/test_movement_jitter.py` (gap refill, late duplicate, clock-jump
exclusion, starvation + gap, fixed dt), `tests/test_reversed_world_update.py`
(latched composition across refilled frames), `tests/test_simulation_order.py`.

## Result

See the gate table at the end of docs/RETAIL_JUMP_RESTORE.md for the lossy
proxy run with the refill enabled: 145 corrections became 0 and the maximum
matched error fell from 0.618 to 0.0003 blocks.

## Residual: a lost packet that carried an input change

The refill guesses "same input as the previous packet". When the lost packet
carried a change the guess is wrong for that one frame. Traced example: the
client released W and crouch in frame 3565, that packet was lost, the server
refilled 3565 still crouched, and because the client applies crouch geometry
before it records history row L, the self row stamped 3565 was 0.9 blocks
away from the client's entry and forced a correction whose replay popped the
player a block up for one frame. Two mitigations are now in place:

- no owner self row is ever stamped with a refilled label
  (`Player.last_applied_input_synthesized`, checked in
  `ReplicationService.broadcast_world_updates`); the next real label carries
  the exact state, so a wrong guess costs at most a few millimetres of drift;
- the real packet after a refill applies the midpoint of the last known and
  its own aim, the best estimate of the lost packet's orientation for a
  continuous mouse turn (the scripted turn segment jumped 57 degrees in the
  lost frame and drifted 4 mm per frame with the stale aim).

A lost *button edge* (press/release in the lost frame) can still put the two
simulations one step apart for that key; at 0.5 % loss that happened three
times in a 32 s run with rough terrain and produced a few 0.1-0.4 block
corrections. Only reliable ClientData plus a burst catch-up on the server
would remove it entirely (see "Not done").

## Not the cause: ENet throttle, and the 69-loop self-row gap

One run showed a 69-loop stretch with no accepted self row while the server
trace proves it consumed input at one frame per tick throughout. ENet's
per-peer unreliable throttle was suspected; `tick stats:` now prints
`thr=<n>/32 rtt=<ms>` and it stayed at 32/32 for the whole run, so
`[network] unreliable_throttle_deceleration` (default 0, keeps the throttle
from ever lowering) is a harmless guard, not the fix. The instant recovery
(the accepted stamp jumped from 4012 to 4076 in one sample) matches ENet
head-of-line blocking: a lost *reliable* packet on channel 0 (the once-per-
second round timer is the only steady reliable traffic in a solo run) holds
every later packet on that channel, unreliable rows included, until the
retransmission lands, then delivers them in one burst. The client applies the
newest row, so the local player is unaffected; remote players freeze for that
interval. Nothing server-side can change that without a second ENet channel,
which the stock client does not read.

## Mitigation (2026-09-24)

The once-per-second `DisplayCountdown(84)` refresh is now sent unreliably
(`send_round_timer(..., reliable=False)` from the simulation runtime's
per-second scheduler); round starts and mode transitions keep the reliable
send. The HUD counts down locally from an absolute value and ENet drops a
stale unreliable refresh that arrives behind a newer one, so nothing is lost,
and a quiet round no longer has any steady reliable traffic that a single
lost packet could hold the WorldUpdate stream behind. Event packets (kills,
scores, sounds, entity moves) remain reliable by necessity, so the stall can
still occur behind a lost event packet during busy play.

## Head-of-line stall removed (2026-09-29)

"Nothing server-side can change that without a second ENet channel" above was
wrong: `ENET_PACKET_FLAG_UNSEQUENCED` is dispatched on arrival whatever the
channel's reliable sequence says. Measured on this build with one datagram
carrying a reliable command dropped: flag 0 held three snapshots for 172 ms,
unsequenced held none.

`[network] worldupdate_delivery = "split"` (default) sends the rows of other
players, entities and turrets unsequenced, in parts of at most `mtu - 28`
wire bytes, and keeps each recipient's own row on the ordered stream. The
second half matters: a snapshot of 24 rows is 1397 wire bytes, and above 1372
ENet fragments it **reliably** whatever its flags, so a full server was
sending every WorldUpdate as reliable fragments.

Neither client orders WorldUpdates, so the server does:

- it measures each link from the arrival order of that player's own
  ClientData and spaces the unsequenced snapshots wider than the measured
  displacement (`worldupdate_reorder_guard`);
- a player's row is withheld from a peer until ENet reports that life's
  CreatePlayer acknowledged, and an entity's or turret's row until its
  CreateEntity is (the client keeps simulating both meanwhile).

`scripts/worldupdate_stall_probe.py`, 100 ms round trip, 2 % loss, about 18
reliable packets per second to the observer, gaps of 150 ms or more between
snapshots carrying remote rows:

| Link | Delivery | Gaps | Frozen | Longest | Stale |
|---|---|---|---|---|---|
| ±5 ms, 40 s | sequenced | 7 | 1498 ms | 437 ms | 0 |
| ±5 ms, 40 s | split | 0 | 0 | 110 ms | 0 |
| ±20 ms, 40 s | sequenced | 8 | 1687 ms | 266 ms | 0 |
| ±20 ms, 40 s | split, guard off | 0 | 0 | 110 ms | 16 |
| ±20 ms, 60 s | split, guard on | 1 and 9 | 157 and 1422 ms | 157 and 171 ms | 0 |

On the last row (two seeds) the guard paces the stream to about 15 Hz, so
one lost snapshot is already 133 ms and two are over the 150 ms mark; those
gaps are lost datagrams, not holds, and none is longer than 171 ms. A link
that reorders datagrams 33 ms apart is unusual; the ±5 ms rows are the
ordinary case and keep 30 Hz.

When a link stops reordering beyond 100 ms and its stream returns from
ordered to unsequenced delivery, the observer stream pauses until every
reliable packet sent before the last ordered snapshot is acknowledged (about
one round trip, once): an ordered snapshot still held at the receiver would
otherwise be released after a newer one.

## Late and out-of-order ClientData (2026-09-29)

A label that was refilled as lost often arrives a moment later (any link
with jitter). It used to be dropped as stale. `Player._salvage_late_frame`
now uses it once: if nothing newer has been simulated, the next step latches
its buttons and aim exactly as the client's did; otherwise a jump or gadget
tap that lived only in that packet is honoured on the next consumed frame. A
button still held in a newer frame is left to that frame, so nothing fires
twice. `handle_client_data` no longer lets an older label replace the aim,
buttons and tool of a newer one, and the crouch edge that scores a teabag is
taken on the arrival timeline only (the buffered replay repeated it once per
tick of queue depth: one held crouch counted as three).

A packet that never arrives still leaves its frame a guess, and when it
carried an edge the guess is wrong half the time. Only redundancy from the
client can remove that.

## Not done

Making the Revival client send ClientData reliably (a two-address patch in
`gameScene.pyd`'s `send_client_data`) would remove lost-edge divergence, but
the server would then need to consume input bursts faster than one frame per
tick after each retransmission or it stays permanently behind; that is a
larger change to `simulate_tick`/replication ordering and was not attempted.
