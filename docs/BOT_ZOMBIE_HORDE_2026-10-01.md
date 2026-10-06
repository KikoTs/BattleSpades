# Zombie horde strategy, structure collapse and VIP roles (2026-10-01)

Kiril's goals: zombies always know where the survivors are and keep after
them without getting stuck; survivors on a pillar, a sky platform or a tower
must not collect the horde in one heap under their feet: the horde goes for
the structure's roots and drops it, or climbs; in VIP every bot pushes the
enemy VIP, at most one bodyguard per team stays home.

## Design

The 2026-10-07 section at the end changed three things described here: the
walk-flood (now an approach flood that also follows jumps and drops), the
ground orders (everybody hunts; flank is only the watchdog's fallback) and
the watchdog (5 s, another side first). Where the two disagree, that section
is the current one.

Strategy runs once per perception snapshot on the gameplay thread (director),
so every infected bot gets a consistent, team-level order. Policies stay pure
and per-bot; the worker executes.

1. **Structure collapse model** (`server/bot_ai/structure_collapse.py`, pure).
   Mirrors `WorldManager.find_unsupported_chunks` exactly: 18-neighbour
   face+edge connectivity, grounded at z > 238.
   - `iter_isolation`: walk-flood (one-block steps, crouch headroom) from the
     survivor's floor. A small closed region that no hunter stands in is a
     pillar top / platform / tower; open ground outgrows the 400-cell bound.
   - `iter_plan_collapse`: bounded BFS of the survivor's solid component
     (2500 voxels), then a minimum-cost **vertex cut** (max-flow on the
     vertex-split graph) between the survivor's support voxels (uncuttable)
     and the ground. Ground = base plane, deep terrain three blocks under the
     horde's floor, and the exploration frontier; treating extra cells as
     ground can only make a cut more expensive, never invalid. Dig cost per
     voxel: 1 when a zombie standing on the horde's floor reaches it from an
     open face (0-1 blocks up), 2 at 2-3 blocks up or buried at claw height, 25 when it hangs out of
     reach. Plans above cost 48 are rejected (a hill, a massive keep).
   - `claw_sites`: greedy cover of the cut by the zombie hand's 3x3x3 dig
     cube (`ZOMBIEHAND_TOOL`, `DIG_CUBE`), restricted to exposed voxels a
     swing can actually hit.
   Both are generators that yield every ~100 operations.
2. **Horde coordinator** (`server/bot_ai/horde_strategy.py`, pure).
   - Target assignment: closest hunters claim first, cost = distance x
     (1 + 0.35 x load), soft cap ceil(Z/S)+1 per survivor, 30 % + 8 block
     hysteresis; human zombies count toward a survivor's load.
   - Ground targets: the two nearest hunters go straight in (`hunt`), the
     rest approach from evenly spaced sides (`flank`, only at 12-48 blocks and
     only to a spot with an open floor).
   - Isolated targets: `dig_root` diggers (one per claw site, two per site
     when the horde is large, max 6 sites) stand on an open floor on the
     outside of the footprint; `climb` (two bots) when no cheap cut exists;
     everyone else `surround`s on a 7-9 block ring, never under the target.
     Dug voxels drop out of the orders as soon as the world shows them gone.
   - Progress watchdog per bot: no 1.5-block goal progress for 7 s (and less
     than 4 blocks of displacement, so a chase is not a stall) raises the
     stuck level: 1 = flank from another side, 2 = `tunnel` (claw the solid
     voxels on the body-height line toward the target, ramping up for a
     higher target; only within 20 blocks of the prey), 3 = climb (also near
     only). Sustained progress lowers it again.
3. **Siege service** (`server/bot_ai/zombie_siege.py`). Director glue: reads
   the mode's live survivors/infected (ACTIVE phase only), keeps each
   survivor's isolation (2.5 s TTL) and collapse plan (6 s TTL, replanned on
   2-block movement or when the cut is gone) fresh with one generator job at
   a time under a **1 ms budget per objective snapshot**, only for survivors with a hunter within 48 blocks, publishes one
   `zombie_order` objective per infected bot (`carrier_id` zombie, `attacker`
   target, `state` role code, `cells` voxels to claw, `progress` stuck
   level). Any exception resets the service and returns no orders (the old
   nearest-survivor hunt); logged once a minute.
4. **Policy** (`ZombieBotPolicy._horde_order_decision`). Infected bots in the
   active phase follow their order: `zombie_hunt_survivor`,
   `zombie_hunt_flank` (engages within 14 blocks), `zombie_siege_dig` /
   `zombie_hunt_tunnel` (directive `siege`), `zombie_siege_climb` (directive
   `climb`), `zombie_siege_surround`. Siege roles only fight within claw reach
   (engagement 4.5): a survivor shooting from his platform no longer pulls
   the diggers off the cut into a pile. `ModePolicyMemory` ignores other
   zombies' orders in its commitment signature and treats `siege` roles as
   live-tracking.
5. **Worker** (`simple_worker.py`, one small hook). Directive `siege` reuses
   the Demolition dig machinery (`_objective_block_work_intent`): nearest
   assigned voxel in reach, first solid on the swing ray, retail swing
   cadence, skip a cell whose swings never land.
6. **VIP** (`VIPBotPolicy`). Exactly one bodyguard per team (lowest bot id,
   only when a second bot can attack); role hysteresis now only holds an
   attacker, so roster churn can briefly leave the VIP unguarded but never
   with two bodyguards. Attackers' priority 0.84 -> 0.95 so optional supply,
   medic or construction work cannot outrank the push. The VIP bot rallies
   behind its own team (DEFEND, fights within 20 blocks, no sprint) and
   carries directive `vip_shelter`, the request the construction layer's
   `_vip_shelter_hold` / schematic `vip_shelter` serves.

## Retail loadout facts used

Zombie, Fast Zombie and Jump Zombie spawn with `ZOMBIEHAND_TOOL` (24),
`ZOMBIE_PREFAB_TOOL` (28) and `PREFAB_TOOL` (23) plus the zombie
hand/bone/head prefabs and 200 blocks; no `BLOCK_TOOL`. So the horde digs with
the hand's 3x3x3 cube and can only "build up" with zombie prefabs. The
coordinator gives no tool; the `climb` directive is consumed by the movement
layer's `LocomotionSkillDriver` (`skill_driver.SKILL_DIRECTIVES`, ascent /
staircase planning within 14 blocks); farther away a climber walks in.

## Tests

`tests/test_zombie_horde.py` (21): collapse cuts for a pillar, a 4-leg sky
platform, a 3x3 tower and a 5x5 keep validated against the real
`WorldManager.find_unsupported_chunks` (survivor support falls; one voxel
short of the cut it does not); a natural hill is open ground with no cheap
cut; claw sites are exposed; target spread, hysteresis, human load; watchdog
flank -> tunnel through a wall, no false stall during a chase, no tunnel from far away, no flank spot inside a wall; siege orders
(diggers at every site, ring with distinct spots, zero pile in the goals);
climbers when no cut; dug voxels leave the orders; policy role mapping;
commitment signature; the worker claws the assigned voxel with the zombie
hand; the service lands a plan within its budget and its published cut drops
the platform under the real collapse rule; silent outside the outbreak and
fail-safe on errors. `tests/test_bot_mode_commitment.py`: one bodyguard at
most (also under roster churn), VIP holds and requests a shelter.

## Scenarios

`scripts/bot_zombie_siege_scenario.py` runs the real director, thread worker,
motor, physics, dig damage and collapse on an authored map. Idle survivor
bodies stand on a synthetic structure; `--baseline` disables the coordinator.
Idle bodies are client-authoritative (the server does not simulate their
fall), so the harness drops a survivor to the ground once its support voxels
are gone. 8 zombies, 2 survivors, ArcticBase, seed 7, 120 s, real time:

| Structure | Base falls (s) before / after | Both survivors infected (s) | Pile mean / max before | after |
|---|---|---|---|---|
| 2 pillars 1x1x12 | 6.5 / 2.7 | 8.5 / 4.7 | 1.76 / 4 | 0.70 / 3 |
| 7x7 sky platform on four 2x2 legs, 14 up | never / 7.1 | never / 7.9 | 4.69 / 8 | 2.00 / 5 |
| 3x3 tower, 14 up | 13.9 / 6.3 | 14.8 / 7.1 | 3.20 / 8 | 0.80 / 3 |
| 5x5 keep, 16 up | 27.3 / 9.6 | 28.2 / 10.8 | 3.82 / 8 | 1.18 / 4 |

Before, the horde heaps under the survivor and swings upward; the zombie
hand's 3x3x3 cube makes thin bases fall by accident, but the platform (legs
away from the heap) stood for the whole run. After, diggers go to the cut.

Ground pursuit, 3 survivors far from the zombie spawn, 150 s, 2 seeds per map:

| Map / seed | Survivors infected before | after | Last infection (s) before / after |
|---|---|---|---|
| MayanJungle 7 | 0 | 3 | - / 140 |
| MayanJungle 11 | 1 | 2 | - / 67 |
| CityOfChicago 7 | 3 | 3 | 61 / 33 |
| CityOfChicago 11 | 2 | 2 | 80 / 49 |
| SpookyMansion 7 | 3 | 3 | 63 / 74 |
| SpookyMansion 11 | 0 | 0 | - |

13 vs 9 infections in total. The harness's stationary-zombie count (8 s
windows moving < 1.5 blocks, > 3 blocks from survivors, not clawing) is about
the same (174 after, 166 before). It is dominated by locomotion (spawns in
water, `water_exit`, `planning_wait` on SpookyMansion's edge), which the
coordinator does not own.

VIP, Alcatraz, 16 bots, 150 s (`bot_runtime_smoke --mode vip` trace):
team samples with two bodyguards dropped from 264 to 0, and the share of
non-VIP bot samples in attack/combat roles rose from 0.39 to 0.61. At 10 bots
the old n/3 rule already gave one guard, so before and after match there.

Gameplay tick p99 (whole tick): open-ground pursuit 1.6 -> 2.6 ms; an active
siege 2.5 -> 3.8-4.9 ms (the analysis runs at most 1 ms per 10 Hz objective
snapshot; the largest single generator step is 0.23 ms).

## 2026-10-07: crossing water, the way in, the claw and the cut by estimated time

Measured with `scripts/bot_zombie_horde_scenario.py`: the production decision
batch and planning budget on a simulated clock, 8 zombies, the same seeds
before and after. "Before" is `bots-integration` 5262705 (this branch's base
plus six locomotion fixes); those fixes alone moved the rush numbers by about
a second (first contact 30.7 s to 29.6 s), so what follows is this branch.
The harness charges the siege analysis by the step (0.1 ms each, measured
0.07-0.15 ms), so an analysis arrives as late there as on a live server; the
"before" service is charged the same way.

### Water (every bot, not only the infected)

Why zombies stayed on SpookyMansion's islets, and why TDM teams did not meet
on island maps:

1. `simple_worker._route_step_reached` held a swim waypoint to the walking
   height tolerance; a swimming body hops above it and never "arrived".
2. The water branch of `_decide` treated every swim as an emergency. After
   the first route segment (about thirteen cells of water) the body was
   handed to the nearest-shore flow, which only knows banks one block high:
   on Atlantis 171 of 178 water entries ended on the bank they had left, and
   SpookyMansion's sea-spawned zombies were carried to the islets (the
   mainland's beaches are two blocks high).
3. Combat and goal refresh were skipped while committed to water, so a
   zombie beside a wading survivor did not claw him.
4. Two of three infected bots carried a dry or a builder identity
   (`_traversal_personality`) that keeps a bot out of the water; a Zombie has
   no block tool to bridge with.

Now a swim is carried to the far bank on the live bearing to the goal
(`_water_crossing_intent`), a swim waypoint counts at any height of the hop,
the infected hunt and claw in water as on land, a builder with nothing to
build with swims, and a dry identity swims at once when its goal is on
another land mass (`_dry_detours_before_swim`). Dry-identity zombies still
take a bridge where there is one: sending the whole horde through London's
river cost a third more time there (32.7 s to 46.4 s), so that was undone.

| SpookyMansion, seeds 7 / 11 | Before | After |
|---|---|---|
| Spawned on islets: zombies on the mainland | 1 of 8 / 1 of 8 | 8 of 8 in 24.5 s / 23.0 s |
| Spawned in the sea ring (retail spawn) | 0 of 8 / 0 of 8 | 8 of 8 in 10 s / 11 s |
| Survivors wading: both infected | never / never (150 s) | 6.3 s / 5.2 s |
| Survivors on an islet: both infected | never / never (150 s) | 39 s / 66 s |

TDM kills per minute, seeds 0-4, 240 s each (`scripts/bot_audit_harness.py`):
Atlantis 0.15 to 0.65, DoubleDragon 1.45 to 2.65, CastleWars 1.10 to 2.70.
Atlantis is still far below the other maps: what is left there is not the
swim (routes over the central plateau's levels, `route_breach` into it, the
skill driver's `water_climb_out` loops) and was left alone.

### Rush

Eight maps, four seeds each, two survivors who cannot die, 90 s; and three
survivor bots far from the spawn, 150 s:

| | Before | Water only | After |
|---|---|---|---|
| First contact (s) | 29.6 | 22.6 | 23.1 |
| Median zombie reaches a survivor (s) | 50.3 | 42.8 | 42.7 |
| Zombies that reached one, of 8 | 5.8 | 7.0 | 6.8 |
| Share of time standing or wandering | 0.23 | 0.13 | 0.14 |
| Order changes per bot per minute | 2.2 | 2.4 | 1.2 |
| Ground: survivors infected, of 3 | 2.46 | 2.88 | 2.96 |
| Ground: last one infected (s, 150 when not) | 76.4 | 63.3 | 58.1 |
| Ground: share of time closing in | 0.57 | 0.68 | 0.68 |

Nearly all of the gain is the water crossing (SpookyMansion 71 s to 17 s to
first contact, CastleWars two more infections). The coordinator's own share
is the halved order churn and a few seconds on the ground runs; on dry open
ground the horde is as fast as before, not faster. What still holds a zombie
back is below the coordinator: sprint is refused on slopes and short runs
(`_route_allows_sprint`, sprint share 0.63), the route planner never uses a
Zombie's four-block jump (its jump edges are two), and bots queue behind a
teammate's breach. Those live in shared locomotion code and are not changed
here.

Coordinator changes: every hunter runs at its prey (`hunt`); the old "two
hunt, the rest flank" split and the surround ring on walkable ground are
gone. The watchdog fires after 5 s instead of 7 (3, 5 and 7 s reach the
survivors equally fast; 3 s raises twice the alarms), measures a hunter
against its prey, credits landed claw swings as progress for 8 s, and sends
a stalled walker round another side before it claws. Clawing on the first
alarm was tried and dug at every terrace of MayanJungle (3 of 8 zombies
reached the survivors instead of 7).

### The way in, the claw and the cut

`structure_collapse.iter_approach` floods outward from a survivor who has
stayed put (0.75 s) along the edges the bots' planner really takes: a step,
a two-block jump, a four-block drop, wading. From it the coordinator
estimates each hunter's walk (exact inside the flood, a lower bound outside
it, plus twice the time already spent getting nowhere) and compares:

- **The way in.** A hunter whose way is a detour or a climb is sent ten
  moves along it at a time (`_via`): under a deck it runs to the stairs
  instead of standing below.
- **The claw** (`tunnel`, `_breach`): the walls on the straight line
  (`wall_line`), 1.2 s for the first column and 0.4 s for each further one.
  A sealed room on the horde's own level is clawed at once instead of being
  besieged.
- **The cut** (`dig_root`, `_besiege`): the collapse plan's sites at 1.6 s a
  site plus the fall, for a footing three or more blocks above the horde,
  whether or not something walks up to it. The plan is only paid for when
  nothing walks in, no near hunter gets up within 4 s, or the hunters sent
  up are stuck. The rest wait on a ring just outside the measured footprint.
- `horde_floor` is now the ground at the structure's foot (it used to follow
  far or jumping hunters, and cuts were planned six to ten blocks up).

ArcticBase, synthetic structures, seeds 7 / 11 / 3, seconds until both
survivors are infected:

| Structure | Before | After |
|---|---|---|
| 7x7 platform on four legs | 7.7 / 22.3 / 8.2 | 8.0 / 7.5 / 8.4 |
| 5x5 keep | 12.5 / 13.9 / 21.3 | 9.3 / 8.3 / 8.1 |
| 3x3 tower | 6.6 / 11.4 / 7.9 | 8.0 / 8.2 / 6.7 |
| Deck with stairs at one end | 6.2 / 17.1 / 4.0 | 4.0 / 9.2 / 4.0 |
| Pillars | 4.4 / 5.7 / 4.0 | 4.1 / 6.5 / 4.3 |
| Roof with a walk up | 7.0 / 4.6 / 8.1 | 7.9 / 5.4 / 7.0 |
| Sealed room | 4.9 / 5.0 / 3.5 | 5.5 / 4.1 / 3.9 |
| Walled yard | 6.1 / 4.5 / 5.7 | 5.0 / 4.7 / 5.8 |

The cut and the way in pay off where the walk is long or missing (platform,
keep, deck: the keep falls at 7-8 s instead of 12-20 s). The claw does not
show: the worker's own `route_breach` already went through a wall in the
way, so a room or a yard is opened as fast as before. The choice is now made
by the coordinator and tested, but it is not a measured gain.

### Cost

Real budgets, 300 snapshots of a ground chase: the whole service call is
0.31 ms on average (0.23 ms before), 1.5 ms at worst (1.4). During a siege
it is 1.04 ms on average and 1.7 ms at worst (1.29 and 2.0 before): the
orders are now inside the snapshot's millisecond and the analysis gets what
they leave. A flood of 600 floors costs about 6 ms (1200 for a survivor
above the horde, 13 ms), spread over the snapshots; it is repeated only when
the ground it covered was edited (the service listens to the world's
mutations), otherwise only who stands inside it is brought up to date. The
collapse planner looks for its paths depth first: the same cuts (identical
on 600 random structures) in 20-30 ms instead of 50-125 ms, so a keep's cut
is known about three seconds after the survivor settles instead of nine.

### Tests

`tests/test_bot_water_crossing.py` (13): swim arrival, the strait and its
beach, clawing and chasing in the sea, the soldier's bearing, dry and builder
identities, and SpookyMansion end to end (islets, sea ring, wading and islet
survivors). `tests/test_zombie_horde.py` (49): the flood's edges, the way in,
room, wall and gate, the cut against the walk, the fallen survivor, the
ring, the horde's floor, the service's budget, edit tracking and listener.
One older test was replaced: "watchdog escalates a stalled hunter to flank
then tunnel" asserted that a hunter at a wall walks to another side of the
same wall before clawing; it now claws at once (the estimate), and the
flank-first step is kept for a hunter the estimate did not send to claw.
