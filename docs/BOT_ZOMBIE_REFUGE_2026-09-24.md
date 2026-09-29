# Zombie survivors: refuge election and ramparts (2026-09-24)

Kiril's report: in Zombie the survivor bots stop, never spread over the map,
never build, and never climb to high ground to get away from the horde. Retail
humans do the opposite: they run for a defensible spot, wall it, and hold it
together until it is breached.

## What was wrong

- `ZombieBotPolicy` only knew a loose formation around the spawn anchor with a
  `fortify` directive that nothing in the production worker acted on: the
  simple worker's prefab-cover intent is gated to the non-cooperative
  behaviour version, and cooperative behaviour treated every `zombie_*` role as
  a committed objective that suppresses construction.
- `find_prefab_cover` did fire occasionally (two prefabs in 150 s) but the
  smoke's `world_mutations` counter only counts BlockBuild/BlockLine commits,
  so it looked like nothing was ever built.

## Design

1. **Refuge election** (`server/bot_ai/zombie_refuge.py`, pure functions).
   The director samples a 4-block grid within 56 blocks of the living
   survivors' centroid using the map height function and scores each column:
   local high point (higher than the ring 4 blocks out, or the wider ring 8
   blocks out for small plateaus), flat 3x3 top, dry (no water in the ring),
   short walk, farther from the horde than the survivors already are. One
   `zombie_refuge` objective per survivor team is published every snapshot
   (`state` = election generation). It is re-elected when a zombie stands on
   it (`refuge_breached`) or when nobody has reached it in 45 s; breached
   spots are excluded for 90 s. Everything is try/except guarded: a failure
   degrades to the old spawn regroup and can never stall the gameplay tick.
2. **Policy** (`ZombieBotPolicy` in `policies.py`). Survivors in countdown and
   active phases route to formation spots 1.0-2.5 blocks around the refuge
   (inside the wall ring), `directive="fortify"`, posture BUILD, engagement
   28 blocks, `watch_position` = nearest zombie or the horde anchor. The last
   survivor runs for the refuge when it is farther from the nearest zombie
   than they are; otherwise the old flee vector. Infected are unchanged.
3. **Cooperative fortification** (`cooperative_behavior.py`,
   `project_sites.py`). A `fortify` directive is no longer "critical", so the
   squad may build. At the refuge every bot with the block tool asks
   `find_rampart_segment` for the next straight run of a 9x9 ring, two blocks
   above the plateau top (a standing player sees and shoots over it, a
   crouched one is covered). Only cells resting on something solid are
   offered, so the ring rises one layer at a time and every BlockLine cell
   passes the retail client's face-contact gate. Columns three or more blocks
   below the plateau are a natural cliff and stay open; body columns and
   cells reserved by teammates split the runs. The nearest run on the lowest
   unfinished layer wins. Runs are `rampart` team projects: several per team
   at once, disjoint cells, one per builder (`TeamTasks.reserve`). A run is
   confirmed when the cells appear solid in the worker map, then the builder
   re-evaluates 2.5 s later for the next run. Combat still interrupts:
   visible zombies within weapon range stop construction, as before.

## Measurement

`py -3.12 scripts/bot_runtime_smoke.py --mode zom --map ArcticBase --bots 10
--seconds 150 --full-runtime --seed 7 --progress-every 30 --trace-jsonl ...`

| Metric | Before | After |
|---|---|---|
| Survivor travel per bot | ~80 blocks (refuge already in) | 74-85 blocks |
| BlockLine commits (`world_mutations`) | 0 | 22 (25 requested, 3 rejected) |
| Rampart tasks started / built | - | 31 / 22 |
| Survivor deaths | 0 | 0 (zombies `route_breach`, cannot climb) |
| Gameplay tick p99 | 1.0 ms | 1.0 ms |

Tests: `tests/test_zombie_refuge.py` (election, breach, policy routing, last
survivor) and `tests/test_zombie_rampart.py` (ring rises layer by layer from
grounded straight runs within reach; runs split around reserved cells and
bodies and skip cliffs; two survivors share the ring and finish runs through
the cooperative task lifecycle). Bot suites: 226 passed.

## Known limits

- The ring has no door. Survivors dig out with their spade when the refuge is
  breached and re-elected; if that proves too slow in play, leave one gap on
  the side facing away from the horde.
- On steep mountain tops most ring columns are cliffs, so the wall is short
  runs on the flat sides only. Tower building (stacking under oneself) is not
  possible through the bot gateway: construction safety rejects cells that
  overlap a living body.
- Prefab cover still uses the class prefab list; the counter that proves
  building happened is `world_mutations` plus per-bot block wallets in the
  trace.

## Reachability filter (2026-09-25)

A 5-minute MayanJungle run built no walls: the elected temple top could only
be reached by digging, so survivors sat in breach queues. The election now
only considers columns in a walkable region the survivors already occupy,
using the region labels from the map's cached navigation atlas
(`maps/<name>.botnav`, loaded once per map by the director, verified against
the map digest; without a cache the election falls back to heights only).

| 5-min Zombie smoke, 10 bots | Wall runs before | After |
|---|---|---|
| MayanJungle | 0 | 39 |
| CityOfChicago | 36 | (unchanged path) |

Test: `tests/test_zombie_refuge.py::test_election_skips_refuges_outside_the_survivors_walkable_region`.
