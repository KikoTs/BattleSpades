# Zombie horde strategy, structure collapse and VIP roles (2026-10-01)

Kiril's goals: zombies always know where the survivors are and keep after
them without getting stuck; survivors on a pillar, a sky platform or a tower
must not collect the horde in one heap under their feet: the horde goes for
the structure's roots and drops it, or climbs; in VIP every bot pushes the
enemy VIP, at most one bodyguard per team stays home.

## Design

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
