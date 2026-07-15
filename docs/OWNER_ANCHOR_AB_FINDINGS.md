# Owner-anchor transport A/B (2026-07-12)

## Conclusion

Keep ordinary unreliable WorldUpdates with owner rows every 2 grounded ticks
and every 6 airborne ticks.  Do not merge the reliable split, current-packet
jump, or grounded-only variants.  The 2/6 control was the safest live retail
result: 5 soft adjustments, no SNAP, and no visible rollback over five full
`jump_run` cycles.

## 2026-07-13 causal-history follow-up

IDA later proved two assumptions in the earlier experiment were wrong:

- the retail local-player path force-applies duplicate row pong stamps, so an
  ordered send history is required; and
- `Character.network_position_loop_count` comes from each player row's pong,
  not the WorldUpdate header loop.

Production now retains every duplicate send and rejects candidate launch rows
that were queued after the source ClientData reached the gameplay thread. This
ordering removes impossible anchors but still does not acknowledge GameScene
application. A fresh foreground run produced a perfect ordinary `jump_run`
(zero adjustments/error/rollback); the real block mutation also completed and
the only remaining corrections occurred after Engineer jetpack activation.
Keep the reliability conclusion below: a transport ACK is still not an
application ACK, and predictive owner-row extrapolation was separately
rejected after it created a raw 1.25-block launch rollback.

The exact decision trace captured seven launches. Selected row stamps were
`2095, 2338, 2418, 2500, 2580, 2662, 2742`; the client cache stamps at those
launches were `2095, 2336, 2418, 2498, 2580, 2662, 2742`. The two two-loop
mismatches occurred while both candidate positions were stationary/equal.
That run recorded zero SNAP, zero visible rollback, and zero backward steps;
its two soft terrain-route corrections occurred before later launch decisions.
Artifacts:
`logs/causal-owner-live/anchor-trace/decisions.json` and
`logs/causal-owner-live/anchor-trace/movement/movement-stress-20260712T214713.922090Z.json`.

## Retail results

| Variant | ADJUST | Max matched error | p95 error | SNAP | Visible rollback |
|---|---:|---:|---:|---:|---:|
| Ordinary owner rows, ground 2 / air 2 | 271 | 0.787679 | 0.301922 | 0 | 0 |
| Reliable split, ground 2 / air 2 (first run) | 61 | 0.249047 | 0.008728 | 0 | 0 |
| Reliable split, ground 2 / air 2 (repeat) | 200 | 0.787679 | 0.308578 | 0 | 0 |
| **Ordinary owner rows, ground 2 / air 6** | **5** | **0.195527** | **0.008728** | **0** | **0** |
| Reliable split, ground 2 / air 6 | 12 | 0.195760 | 0.000000 | 0 | 0 |
| Ordinary 2/6 + current-packet jump | 40 | 0.333721 | 0.158295 | 0 | 0 |
| Ordinary, grounded-only owner rows | 189 | 0.762724 | 0.149170 | 0 | 15 |

The stationary control was already exact: ten `jump_in_place` cycles, 1,585
samples, zero adjustment, zero matched error, and zero rollback.  The residual
2/6 errors are moving/wading launch-phase errors, not generic jump impulse
errors.  They recur about once per 75-83-loop pulse and commonly carry the
one-frame vector `(-0.1147, 0, +0.158295)`.

Grounded-only failed because a held/repeated launch can occur before a landed
player receives a fresh ground row.  Retail then restores the stale preflight
`network_position`, producing backward steps up to 3.088 blocks.

## Reliable transport experiment

The validation-only service split replication into:

- the ordinary unreliable observer/entity/turret snapshot, excluding only its
  recipient's local row; and
- a 67-byte WorldUpdate containing one owner row, tool sentinel `0xFF`, and no
  entity/turret tail, sent reliable with stop-and-wait.

An anchor was admitted to `Player._owner_anchor_history` only after ENet's
aggregate `reliableDataInTransit` was observed positive and later returned to
zero.  In the complete trace, 1,866 of 1,867 rows confirmed one server tick
after queueing, with zero ambiguous zero-byte sends.  A normal 67-byte payload
appeared as 71 reliable bytes after framing/compression.  Counts of 78, 89, 110,
and the first 853-byte sample proved that other queued reliable commands can be
coalesced into the same aggregate.

This signal is conservative but not packet-specific.  A zero count before
`Host.flush()` does not prove the host has no queued reliable commands, and a
later zero proves transport acknowledgement, not that GameScene already
consumed the WorldUpdate.  At the production 2/6 cadence, reliability increased
adjustments from 5 to 12, so confirmed delivery is not the remaining root cause.

## Evidence artifacts

- `logs/owner-anchor-baseline-live/movement-stress-20260712T094828.109420Z.json`
- `logs/owner-anchor-prototype-live/movement-stress-20260712T095341.450775Z.json`
- `logs/owner-anchor-prototype-traced-live/movement-stress-20260712T095931.872474Z.json`
- `logs/owner-anchor-prototype-delivery-27041.json`
- `logs/owner-anchor-prototype-delivery-27043.json`
- `logs/owner-anchor-stationary-baseline-live/movement-stress-20260712T095656.474518Z.json`
- `logs/owner-anchor-cadence6-baseline-live/movement-stress-20260712T100304.651745Z.json`
- `logs/owner-anchor-cadence6-prototype-live/movement-stress-20260712T100515.428836Z.json`
- `logs/owner-anchor-currentjump-ordinary-live/movement-stress-20260712T100836.670139Z.json`
- `logs/owner-anchor-groundedonly-ordinary-live/movement-stress-20260712T101208.093951Z.json`

The isolated prototype, launcher, and its eight unit tests are in
`tmp/reliable_owner_anchor_prototype.py`,
`tmp/run_reliable_owner_anchor_validation.py`, and
`tmp/test_reliable_owner_anchor_prototype.py`.  They do not change production
gameplay behavior.
