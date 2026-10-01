# Bot locomotion skills: climb out, pillar up, fast-bridge (2026-10-01)

Kiril's report: on DragonIsland bots that fall into the sea under the cliffs
do not recover well. Humans dig a staircase into the wall, pillar up under
themselves, or bridge a gap by walking backwards off the edge laying blocks.
Bots now do the same, through the ordinary input path: tool selection,
aim, held primary (MELEE / BlockBuild / BlockLine), native jumping and
walking. The server validates every swing and block exactly as for a
client (reach, face support, body overlap at commit, wallet, protection).

Code: `server/bot_ai/recovery_skills.py` (pure planning + executor),
`server/bot_ai/skill_driver.py` (per-bot ownership, triggers, re-planning),
a few hooks in `server/bot_ai/simple_worker.py`. Scenario gate:
`scripts/bot_recovery_scenarios.py`. Tests: `tests/test_bot_recovery_skills.py`.

## API for strategy code (zombie / VIP / cooperative)

### 1. Directive (preferred; no import needed)

Set `ModeBotDecision.directive` and put the target in `position`:

| directive | what the bot does | `position` means |
| --- | --- | --- |
| `climb` | reach the point by any mix of dug stairs, built stairs, pillars and short bridges | the elevated spot (e.g. survivor's feet) |
| `pillar_up` | jump-and-place blocks under itself until at that height | only `z` is used |
| `dig_staircase_up` | dig (and where needed build) a straight staircase toward the point, up to its height | direction + height |
| `bridge` | fast-bridge (backwards, short BlockLines) toward the point | direction + length |

Rules: beyond 14 blocks horizontally the bot first walks there with normal
navigation; within 14 it plans the primitive. If it cannot be planned
(no tool, no blocks, protected cells, too far), ordinary navigation toward
`position` continues as the fallback, so the directive is always safe to
set. Re-sending the same directive every frame is idempotent (targets
within 2.5 blocks count as the same order). A visible enemy inside the
decision's `engagement_radius` still pre-empts it (combat).

`climb` is what `zombie_siege_climb` already sends.

### 2. Direct call (tests, tools)

```python
from server.bot_ai.skill_driver import SkillRequest
intent = brain.request_locomotion_skill(frame, SkillRequest("pillar_up", levels=5))
intent = brain.request_locomotion_skill(frame, SkillRequest("dig_staircase_up",
                                                            direction=(1, 0, 0), levels=6))
intent = brain.request_locomotion_skill(frame, SkillRequest("bridge",
                                                            direction=(0, 1, 0), length=8))
intent = brain.request_locomotion_skill(frame, SkillRequest("climb", target=(x, y, z)))
```

Returns the first `BotIntent` or `None` (cannot plan). Subsequent decisions
continue it automatically (`brain.skills.active(observer)`).

### 3. Pure planners (feasibility checks, no side effects)

```python
from server.bot_ai.recovery_skills import (
    ClimbAbilities, AscentGoal, plan_ascent, plan_pillar_up,
    plan_staircase_up, plan_fast_bridge, find_gap_bridge, node_of)
abilities = ClimbAbilities.from_observer(player_snapshot)   # dig profile, wallet
plan = plan_ascent(world, node_of(pos), AscentGoal.toward(target), abilities)
plan.seconds, plan.blocks, plan.rise, plan.steps            # None if not found
```

`world` is the worker's `SimpleVoxelWorld` (anything with `solid(x, y, z)`).

## Design

- **Planner** (`AscentSearch`, resumable bounded weighted A*). Nodes are
  standing positions `(x, y, support_z)`; a swimmer stands on the waterbed
  (239). Moves: walk/swim (same level), walk down one, stair (up one into a
  neighbour), pillar (up one in place), bridge (walk over a laid floor).
  Each move lists the cells to dig (body corridor + head room) and to build
  (missing floor). Costs are seconds: recovered per-tool swings x cadence,
  footprint-aware (spade column clears a stair step in one swing, Super
  Spade cube in one, pickaxe needs four), 0.85 s per pillar level, and a
  wallet price per block (high when an instant-dig tool is owned, so a
  Miner digs and keeps blocks; low otherwise, so a knife Soldier pillars).
  Faces must be supported (client-parity rule) ignoring cells the plan
  itself digs. A search gets 12 ms per worker decision and continues on the
  next decision, so a hard climb never stalls the roster thread.
- **Executor** (`LocomotionSkill`). Per step: dig what is still solid
  (first-solid-on-ray aim selection within melee reach, footprint never
  touching the floor it stands on or the next floor), lay the floor
  (stair floors at foot height are placed at the top of a hop, because the
  server refuses a block that intersects the body), then move. Pillar:
  jump, look down, place when the feet clear the cell (2.24-block native
  jump gives about 0.6 s), skilled bots place two cells in one jump with a
  vertical BlockLine. Bridge: brake at the lip, crouch-place a 2-4 cell
  BlockLine (longer for skilled bots), walk onto it; skilled bots face back
  the way they came (backwards bridging), casual ones look ahead. Short
  reaction pauses between distinct actions, swing cadence jitter from the
  profile, sneaking only for the final approach. Re-validated against the
  live world every frame; anything unexpected is a `failed` status and
  the driver re-plans from the body (3 times, then backs off 8 s so the
  legacy recovery keeps going). A whole-skill watchdog (no finished step
  and under 0.75 blocks of motion for 4 s afloat / 7 s on land) and any
  step timeout while afloat hand a swimmer straight back to the shore flow,
  which keeps the London long-crossing matrix gates green.
- **Triggers** (`LocomotionSkillDriver`).
  - Water: after 0.6 s afloat (6 s on a deliberate crossing), compare the
    atlas swim to a native one-block exit onto *main ground* (beaches cut
    off from the island count as no exit) with a quick climb estimate;
    when climbing wins, search a climb to the top of a main-ground column
    within 28 columns (swimming along the cliff to a better spot is part of
    the plan). A swim that has taken 8 s longer than its estimate stops
    being trusted.
  - Stuck on dry ground: no 2.5-block displacement for 6 s with the goal 3+
    blocks above, or 5 s in a low pocket (sea-level ledge outside the main
    region, a pit below its column's top, or tunnelling at sea level under
    the island): search toward the goal, then to main ground.
  - Gap: when the dry route cannot advance and a gap of 2-8 columns (16
    for bridge builders, or after one failed lateral detour) lies straight
    toward the goal with a landing at the same level (+-1) and a clear body
    corridor, fast-bridge it.
- **Water-plane detail.** Intents within 4.25 blocks of the water plane are
  labelled `water_*`, so the director's swimmer override does not steer a
  digging/building body.

## Results

All runs: production brain + director motor + gateway + native 60 Hz
physics on an accelerated clock (`scripts/bot_recovery_scenarios.py`).
"Before" is the same tree with `--disable-skills` (the driver stubbed out).

**DragonIsland, bots dropped into the sea beside main-ground cliffs**
(16 mixed-class bots, seeds 7/11/23/31, 120 s cap; recovered = standing on
the top of a main-ground column, or inside an island building):

| | recovered | median | mean | p90 | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| before | 62/64 | 19.6 s | 25.0 s | 48.3 s | 107.9 s |
| after | **64/64** | 17.5 s | **21.2 s** | **35.5 s** | **63.1 s** |
| before, far exit (no native exit or > 15 s swim, n=50) | 48/50 | 25.1 s | 29.9 s | 55.1 s | 107.9 s |
| after, far exit | **50/50** | 25.6 s | **24.9 s** | **36.8 s** | **63.1 s** |

Runs vary by a few seconds per seed (equal seeds are not bit-identical:
worker timing differs); one earlier run of this build gave 63/64.

The median barely moves: most cliffs are 25-45 blocks, and a climb (about
1 s per level by pillar, 1-2.5 s by dug staircase) is no faster than a
60-cell swim. The gain is in the tail and in failures: specialists that
tunnelled along at sea level forever, bots bouncing between two banks of a
cut-off outcrop, and swimmers sent to beaches that are not connected to the
island.

**Chasm with no detour (flat plateau, trench to the sea), 4 bots
(Soldier, Scout, Medic, Engineer):**

| gap | before | after |
| --- | --- | --- |
| 6 columns | 3/4, median 26.6 s | **4/4, median 9.0 s** |
| 10 columns | 1/4, 26.3 s | **3/4, median 21.1 s** |

Gaps up to 8 columns are bridged at once by everyone. Wider gaps (up to 16)
are bridged at once only by the bridge-builder temperament; dry-route
temperaments first try their usual 12 s lateral detour along the lip
(with immediate bridging for all, the 10-column case measured 4/4, median
14.2 s). A Miner starts with 0 blocks and cannot bridge; it falls back to
the old behaviour.

**3x3 shaft dug into solid island ground, 6 mixed bots:**

| depth | before | after |
| --- | --- | --- |
| 6 | 5/6, median 3.3 s | **6/6**, median 5.9 s |
| 10 | 3/6, median 12.2 s | **4-5/6**, median 15-21 s |

Pit climbing only starts after 6 s without progress, so pits the old
breach logic already escapes are unchanged.

**Directive primitives (no "before"; the old brain had none):**
`pillar_up` 10 levels on open ground: 4/4, median 9.5 s (about 0.95 s per
level incl. landing). `dig_staircase_up` 8 levels out of a shaft
(Miner/Medic/Engineer/Soldier): 3/4, 7.7-15 s (the Miner stops two levels short:
its empty wallet cannot re-lay a floor its 3x3x3 swing removed).

Worker cost: plan searches are sliced at 12 ms per decision; the slowest
single bot decision in the water runs is 80-170 ms, the same range as
before (baseline 160-470 ms, dominated by the legacy planner).

## Commands

```
py -3.12 scripts/bot_recovery_scenarios.py --scenario water --bots 16 --seconds 120 --seed 7 --json tmp/recovery/water_after_s7.json
py -3.12 scripts/bot_recovery_scenarios.py --scenario water --bots 16 --seconds 120 --seed 7 --disable-skills
py -3.12 scripts/bot_recovery_scenarios.py --scenario gap --gap-width 10 --bots 4 --classes 0,1,17,12 --seconds 40
py -3.12 scripts/bot_recovery_scenarios.py --scenario pit --pit-depth 10 --bots 6 --seconds 40
py -3.12 scripts/bot_recovery_scenarios.py --scenario pillar --pit-depth 10 --bots 4 --classes 0,1,17,12
py -3.12 scripts/bot_recovery_scenarios.py --scenario staircase --pit-depth 8 --bots 4 --classes 3,17,12,0
py -3.12 -m pytest -q tests/test_bot_recovery_skills.py
```

`--trace-every 0.25 --trace-bot N` prints one bot's position, role and
skill step; the JSON report carries `skill_metrics` and the last 64
`skill_events` (failure reason, step, body position).

## Known limits

- Pillaring from open water is not planned: the swim bob rarely lifts the
  feet clear of layer 238, so a swimmer lays a block beside itself and
  steps onto it instead.
- A Super Spade's 3x3x3 swing removes the next stair floor; the executor
  re-lays it from the refunded blocks and the planner keeps cube
  staircases straight (a turn would lose the floor's support).
- Plans read the worker's map; a teammate's edit, structural collapse or
  falling blocks can invalidate a step. The executor then re-plans (3
  times) and finally backs off 8 s to the legacy recovery.
- Not yet observed in a rendered client. Gaze while bridging backwards and
  the hop that places a stair step are the parts worth a visual check.
- Bots still occasionally walk off DragonIsland cliff edges during ordinary
  navigation; this pass makes the recovery better, it does not prevent the
  fall.
