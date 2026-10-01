# Lag compensation (target rewind)

Module: `server/lag_compensation.py`. Tests: `tests/test_lag_compensation.py`.

## Problem

`CombatSystem.handle_shot` resolves a ShootPacket against the targets' CURRENT
server hitboxes. A player with 100 ms ping aimed at the body their client
showed, and that body has moved on the server by the time the shot arrives.
Hits that looked clean missed.

## What the shooter saw (retail client contract)

- `GameScene.send_shoot_packet` (gameScene.pyd `0x101702A0`, source line 2753)
  sets `shoot_packet.shot_on_world_update = self.last_world_update`.
- `GameScene.process_packet_world_update` stores the WorldUpdate HEADER loop in
  `scene.last_world_update` (`0x10182C93`). Our header is `server.loop_count` of
  the tick whose end state the snapshot carries (`server/replication.py`).
- Remote players are not buffered for interpolation.
  `Character.apply_interpolations` snaps the remote `world_object` to the
  newest `network_position`/`network_velocity` and keeps simulating it forward,
  which is extrapolation.

The view the shooter aimed at is therefore about one downstream trip old.
The shot then needs one upstream trip to reach the server. Measured from the
tick that processes the shot, the view is about **one RTT** old, not RTT/2.
`shot_on_world_update` is a lower bound: the client extrapolates forward from
that snapshot, so it never shows anything older.

```
now        = server.loop_count - 1        # label of the newest published state
view_age   = min(RTT + view_delay, (now - shot_on_world_update) * tick)
allowed    = min(RTT + extra, max)
rewind     = clamp(view_age, 0, allowed)  # < half a tick -> no rewind
```

- **RTT** is ENet's smoothed `peer.roundTripTime`, the value the health log
  prints. The 1.x protocol has no application-level ping to use instead,
  because the client's ClockSync measures RTT locally and never reports it.
- **Snapshot floor.** ENet starts each peer at a 500 ms default. A genuine
  `shot_on_world_update` corrects that early over-estimate. A forged ancient
  snapshot gains nothing, because the RTT term bounds the rewind.
- **Absolute and per-RTT clamps** stop the rewind being used to backtrack.

## History

- `Player.simulate_tick` calls `lag_compensation.record_player(self)` first
  thing each tick. This runs for humans and bots, alive or dead, and labels
  the sample `loop_count - 1`, which is the state the previous tick's
  WorldUpdate published.
- A shot handled during the packet drain of tick `L` sees the live body as
  label `L-1` and history for older labels.
- Each sample is a tuple:
  `(tick, x, y, z, o_x, o_y, o_z, crouched, live, life, epoch)`.
- Samples are stored in a 64-slot ring on `player._lag_history`, about
  1.07 s at 60 Hz.
- Rewinds to fractional ticks interpolate linearly between samples. Position
  and aim are lerped; crouch comes from the nearer sample.

## Never rewound

- **Across a death or respawn.** `replication_generation` changes on spawn,
  and samples from the previous life are ignored.
- **To a dead sample** (`alive and spawned` was false at that tick).
- **Across a teleport.** A per-tick jump larger than
  `TELEPORT_BLOCKS_PER_TICK` (5 blocks) starts a new epoch. A live body that
  is discontinuous with the newest sample is not rewound.
- **For bot shooters, or humans with an RTT of 0.** Targets that are bots are
  still recorded and rewound.
- **Terrain and line of sight.** These always use the current world: the
  terrain raycast caps the ray first, then the rewound hitboxes are tested.
- **Anything other than the hitbox test.** Damage, knockback and riot-shield
  facing use the live target. `RewoundBody` is a read-only view: the live
  Player and its native world object are never moved, so nothing has to be
  restored.

## Hook (combat_runtime)

- `lag_compensation.compensated(owner, server, shooter, packet)` sets
  `owner._lag_rewind` for the duration of a shot and always clears it.
- `lag_compensation.body_for(owner, target)` returns the rewound hitbox, or
  the live target when there is no rewind.
- The player-hit loop in `_find_first_player_hit` is shared by hitscan,
  pellets and spade/melee, so rewinding it covers all three.

## Config (read with getattr)

| key | default | meaning |
|---|---|---|
| `lag_compensation_enabled` | `True` | master switch |
| `lag_compensation_max_ms` | `250` | absolute rewind cap |
| `lag_compensation_extra_ms` | `50` | allowance above measured RTT |
| `lag_compensation_view_delay_ms` | `0` | extra client render delay (retail extrapolates, so 0) |
| `lag_compensation_late_shot_ms` | `400` | most extra rewind for a retransmitted (late) shot, see below |

## Late (retransmitted) shots (2026-10-01)

A ShootPacket is reliable; its frame's ClientData is unsequenced. When the
datagram carrying both is lost, ENet resends the shot one retransmission
time-out later (about 270 ms in the lab at 300 ms ping) while later
ClientData keeps arriving. Rewound by the round trip alone, such a shot was
tested against a body 1.3-2.6 blocks away from the one the shooter saw
(`scripts/shot_registration_lab.py`, sniper and rifle misses at 300 ms).

`Player.label_arrival_tick(label)` records the server tick on which every
ClientData label arrived. `late_shot_ms` dates the shot's label by it (or by
the nearest label within 8 frames when its own ClientData was lost with
it), ignores one tick of phase skew, and adds the delay to both the view
age and the allowance, capped by `lag_compensation_late_shot_ms`:

```
late     = min(cap, (loop_count - arrival_tick(label) - 1) * tick)
view_age = min(RTT + view_delay + late, age of the claimed snapshot)
allowed  = min(RTT + extra, max) + late
```

The snapshot floor still applies. Every late shot is reported log-only as
`lag_comp_late_shot` with its delay: an honest link shows it at about its
loss rate, a client holding every shot back ("backtrack") on every shot.
The history ring is 128 ticks so the largest default rewind (650 ms) fits.

## Anti-abuse (log-only)

`anticheat.report(..., enforced=False)` is called for:

- `lag_comp_stale_snapshot`: the claimed snapshot is older than
  `allowed + (2 * worldupdate_broadcast_interval + 1)` ticks.
- `lag_comp_future_snapshot`: the claimed snapshot is more than one tick in
  the future.

ENet throttling can suppress the unreliable WorldUpdate stream for about a
second, so honest clients can trip the stale report. Keep it log-only.

## Cost

Measured on the dev machine with 24 real Players:

- Recording: about 0.010 ms per tick.
- Rewind decision plus 23 hitbox views: about 0.026 ms per shot. Views are
  cached per shot, so shotgun pellets reuse them.

## Remaining risks

- **RTT noise and asymmetry.** ENet's RTT includes the client servicing the
  socket once per 16 ms frame. Rewinds can overshoot by up to about a frame,
  which the snapshot floor partly corrects.
- **Extrapolation mismatch.** When a target changes direction, the shooter's
  extrapolated picture differs from the server's true past. No server rewind
  can reproduce a view the client predicted wrongly.
- **Being shot behind cover.** A target that has just stepped behind a wall
  can still be hit where the shooter saw them, for up to RTT + 50 ms. This is
  the standard lag-compensation trade-off; `lag_compensation_max_ms` bounds it.
- **Explosions, grenades and projectiles are not rewound.**

## Measured under simulated ping (2026-09-29)

`scripts/lag_compensation_link.py` replays the stock client's picture of a
moving target over a link with delay, jitter and loss, in virtual time, and
runs the real `rewind_targets` and hitbox test. The client model is the
contract above (newest snapshot received, simulated forward), not the client
binary. `tests/test_lag_compensation_link.py` keeps it in the suite.

Seed 11, 20 s, 140 shots per row, ±20 ms jitter per datagram and direction,
1 % loss, targets at sprint speed (7.5 blocks/s):

| Target | RTT | Registered | Without rewind | Position error p95 | Hit behind cover, max |
|---|---|---|---|---|---|
| strafe | 50 / 100 / 200 ms | 100 / 100 / 100 % | 76 / 14 / 14 % | 0.25 / 0.24 / 0.26 | 0.47 / 0.85 / 1.61 blocks |
| jump | 50 / 100 / 200 ms | 100 / 99.3 / 99.3 % | 74 / 6 / 5 % | 0.34 / 0.32 / 0.35 | 0.77 / 1.32 / 2.25 |
| jetpack | 50 / 100 / 200 ms | 99.3 / 100 / 100 % | 89 / 13 / 0 % | 0.60 / 0.52 / 0.63 | 1.81 / 3.32 / 6.31 |
| parachute | 50 / 100 / 200 ms | 100 / 100 / 100 % | 100 / 81 / 30 % | 0.33 / 0.31 / 0.27 | 0.91 / 1.69 / 3.07 |

Over 10,000 shots per setting (four seeds, all four targets, 1 % loss) the
first-attempt registration is 99.7 % at ±20 ms jitter and 99.9 % at ±5 ms.

- **Rewind limits.** The largest rewind was 62 / 113 / 214 ms at 50 / 100 /
  200 ms: the smoothed round trip, never above `RTT + extra` or the 250 ms
  cap. A claim of a snapshot one second old changed nothing (largest rewind
  113 ms, as for the honest client); a claim of a fresh one only shortens
  the rewind.
- **The round trip overstates the view age by about 12 ms**, at every ping
  and without any jitter: a shot waits up to a tick before it is handled
  while the newest state label is a tick old (half a tick on average), and
  ENet times the round trip to an acknowledgement the client sends from its
  10 ms socket poll. Subtracting it was tried and rejected: the mean
  position error halved (0.113 to 0.063 blocks) but fewer shots registered
  (99.93 to 99.78 %; 99.08 to 97.90 % against a 12 blocks/s target at ±20 ms
  jitter). The misses are direction changes, where the client's
  extrapolation overshoots the turning point, and a slightly older body is
  the nearer one.
- **A lost shot packet** arrives one retransmission time-out later, which is
  beyond the allowance at any ping above about 50 ms. It is rewound as far
  as allowed and usually misses a moving target; that is the whole cost of
  loss (at most the share of shots that needed a retransmission). Honest
  clients trip `lag_comp_stale_snapshot` this way, so it stays log-only.
- **Paced snapshots.** When the WorldUpdate reorder guard spaces a player's
  snapshots four or six ticks apart, registration was 98.5 % and 97.9 %.
- **`shot_on_world_update` and split delivery.** The client stores the header
  loop of every WorldUpdate, the own-row packet included. That packet
  therefore repeats the loop of the newest observer snapshot sent to the
  recipient, so the claim keeps dating the bodies on screen.
