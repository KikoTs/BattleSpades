# Bot locomotion rework — 21 September 2026

A live AncientEgypt TDM playtest (10 bots, 40 s) was stopped with the report
that bots "spin around", are slow, and do not look like players. Server ticks
were healthy (0.6 ms), so this was behaviour, not load. The earlier
[combat realism pass](BOT_COMBAT_REALISM_2026-09-21.md) measured fights and
never measured how travel looks; the numbers below were already present in
the untouched baseline.

Measure with `scripts/bot_motion_review.py <trace.jsonl>` (quiet travel only:
alive, no visible enemy, no terrain action).

## What the traces showed

| Quiet travel, AncientEgypt TDM, 10 bots | Baseline | First pass | Now |
| --- | ---: | ---: | ---: |
| Ground speed (blocks/s) | 4.4 | 5.8 | 7.5 |
| Time sprinting | 12 % | 36 % | 54 % |
| Time standing still | 8 % | 10 % | 6 % |
| Mean head turn rate (deg/s) | 84 | 76 | 51 |
| Head turns of 25 deg or more (per bot-minute) | 43 | 36 | 25 |
| There-and-back head wags (per bot-minute) | 25 | 17 | 11 |
| Travel leased 2.25 blocks or more ahead (share of quiet travel) | 23 % | 49 % | 66 % |
| Pacing inside 3 blocks for 3 s or more (share of bot time) | 7.0 % | 8.2 % | 4.3 % |
| 90 deg corner turn | up to 2 s | 0.25–0.4 s | 0.25–0.4 s |

Columns are single 120 s matches: `baseline-egypt`, `fix11-egypt` (the build
installed after the first pass) and `fix16-egypt` under `out/bot-fun-20260921/`.

London TDM moved the same way (4.7 to 5.2 blocks/s with a third of the time
swimming, wags 21.7 to 12.2, pacing 10.1 % to 6.8 %).

Walking measures 3.8 blocks/s and sprinting 7.1. Match-to-match variance is a
few tenths of a block per second, about 2 wags per minute and 2 points of
pacing; process scheduling makes equal seeds differ. Standing still is now
almost entirely deliberate: healing, watching, reloading in cover, digging.

## Causes and fixes

1. **One grid cell at a time.** The brain leased the motor a single waypoint
   (median 1.3 blocks away). The motor stops on reaching a leased waypoint and
   waits for the next 8 Hz decision, so bots stopped and restarted every
   block: over half of all standing-still samples had an ordinary moving role.
   `server/bot_ai/path_following.py` now picks the farthest waypoint of the
   current run of plain walking steps that a body-wide straight walk reaches
   over step-height terrain (`lookahead`), keeps it until within 5 blocks, and
   advances the route index by progress along the heading (`passed_index`)
   because a cut corner never touches the skipped cells. Jumps, drops, digs
   and builds end a run and keep exact cell-by-cell handling. A stall while
   steering at a far point disables it for 8 s on that stretch.
2. **Sprint only on perfectly straight, perfectly flat ground.**
   `_route_allows_sprint` required 3–5 dead-straight blocks with under 0.25
   of height change, which terraces almost never offer, and sprint was also
   refused uphill. With a far steering point the bot sprints unless a step
   needing an exact take-off is within 4.5 blocks. Uphill limits are the native
   movement model's business.
3. **A 90° turn took up to two seconds.** Movement keys are relative to the
   view, so while the travel gaze panned (capped near 100°/s, with jerk
   limits and leftover angular momentum) the body strafed and hopped in place.
   `_smooth_gaze_axis` is now a critically damped turn scaled by the profile's
   `turn_speed`: no overshoot at 1/60, 0.1 or 0.25 s steps. Combat aim, the
   difficulty model, is unchanged.
4. **The head faced every sidestep.** Terrace stairs and jump landings put
   single steps at right angles to the direction of travel; the head swung 90°
   and back for one block. `MovementIntent.gaze_waypoint` lets the eyes rest
   on the route about 8 blocks ahead while the view-relative keys take the step
   underfoot. A steering point at least 5 blocks away is itself the gaze.
5. **Every broken block invalidated every route.** `route_needs_replan`
   returned true for any route containing a jump, drop, dig, build or flight
   step, or a compacted stride over 2 blocks, on any edit anywhere on the map.
   In a firefight that meant all bots discarding routes, queueing for the
   24-per-second planner and standing in `planning_wait` on alternate
   decisions, each time freezing and restarting the head. Edits are stamped per
   column, so the check now compares the columns within 4 blocks of the body
   and the 3×3 neighbourhoods along the next 64 steps, including a dig edge's
   own cells, for every kind of step. A body that has not moved 4 blocks in 3 s
   falls back to the old any-edit rule for exact edges.
6. **Standing still to think.** While a replacement plan is queued, a bot on a
   plain walking step keeps following its current route
   (`SimpleVoxelWorld.planning_available`, `PlanningBudget.would_grant`); with
   no route it coasts along its last heading if the 4 blocks ahead are plain
   walkable ground (`_coast`); and a bounded segment is extended from its last
   cell before it runs out (`_extend_route`). `planning_wait` fell from 6.8 %
   to about 3 % of travel time.

7. **Every step down a terrace cancelled the far steering point.** Far
   steering required a grounded body, and a body is airborne for a moment on
   every one-block step down. A quarter of walking decisions fell back to the
   cell underfoot, which is also a different bearing: the head wagged and the
   body stopped. The point chosen from the ground is now held while airborne
   (`held_index`).
8. **Movement keys were quantized wrongly.** W/S and A/D were thresholded
   independently at 0.25, so a heading 20 degrees off the view became a 45
   degree strafe. Whenever the eyes were not exactly on the steering point,
   mid-turn included, the body veered off the line the worker had validated,
   sometimes off the side of a staircase. `BotDirector._movement_keys` presses
   the nearest of the eight key headings and carries the missed angle into the
   next press: exact on average, and straight ahead is still plain W. Sprint
   and acceleration are direction-independent in the native model, so the view
   is now free of the feet.
9. **Two gaze rules took turns.** Eyes went to the steering point when it was
   far and to a route point otherwise, and the hand-over swung the head on
   doglegs. There is one rule now (`_travel_gaze`): for two seconds the spot
   an enemy was last seen (losing and regaining sight was nearly half of all
   wags), otherwise the route ten blocks along, and when a route is about to
   run out the eyes stay where they were instead of on the last cell.
10. **Corridor guidance planned one corner at a time.** A map-wide corridor
    keeps every corner and height change, which on terraces is every block, and
    each detailed plan went to the next one: routes of one to three steps, a
    stop and a new bearing each, for the whole detour. This was the "dithers
    near a structure" defect. `_corridor_point_ahead` sends a plan eight blocks
    along the corridor (the straight section it already trusted) and skips
    whatever the body has come alongside; a stretch the planner cannot finish
    is retried corner by corner.
11. **Small ledges were treated as obstacles.** Falls hurt from ten blocks
    (six for the Classic soldier) but any descent over one block was an exact
    DROP step: stop, line up on the cell, step off. Drops of up to four blocks
    are part of a run now: `straight_walkable` accepts a dry ledge with open
    air below it, `run_end` keeps such steps, a lone one is walked off
    (`runs_off`), and `MovementIntent.walk_drop` tells the motor's live gate to
    allow that descent for that lease only. Climbs, longer drops and gap jumps
    stay exact; those line up first (`takeoff_alignment`) and are taken along
    the edge's own axis (`edge_axis`), which is 0.2 % of bot time.
12. **Smaller ones.** Jumps were authored through head-height openings under
    a slab (`_jump_gap_is_clear` now reserves the arc); a drop whose landing is
    overhead after a fall is replanned, not climbed back to; "go and look"
    errands are held for three seconds instead of swapping every second; two
    short plans in a row ask for corridor guidance at once; and the planner's
    request rate follows the roster (`PlanningBudget.scale_for`), because ten
    bots were having half their requests deferred.

## Tests

- New: `tests/test_bot_path_following.py` (22), `tests/test_bot_exact_edges.py` (2), and
  key-selection, walk-off gate and budget cases in `test_bot_live_travel_motor.py`
  and `test_bot_planning_budget.py`.
- Changed on purpose, because they encoded the behaviour being removed:
  `test_bot_gaze_dynamics.py` (a corner settles in 0.5 s, not 2 s; no
  overshoot at any cadence), `test_bot_route_invalidation.py` (distant edits
  no longer discard routes with special steps; nearby and dig-cell edits do;
  long routes are checked within a bound), `test_bot_planning_integration.py`
  (a busy planner no longer halts a bot on a plain step; an exact edge still
  waits with its clocks intact), a corridor test that expected the next
  corner as the planning target, the diagnostics field count, and one
  call-count assertion in `test_simple_bot_tactics.py`.
- The 36-case map matrix passed in full once on this code. A later batch run
  had two failures that both passed when re-run alone: `Frontier` failed to
  load its VXL (`not enough values to unpack`), and London `tc` uses a
  wall-clock base.
- `test_bot_escapes_excavated_map_pockets_with_native_physics[bot_mayan_corner_drop…]`
  was red for most of this work (27 s against a 15 s limit) and was first put
  down to replanning churn. It was two real defects, both now fixed, and all
  five pocket fixtures pass: the planner authored a DROP into a tunnel mouth
  whose roof is below the body's standing height (the motor rightly refused
  and the bot stood at the lip until a timeout), and `straight_walkable`
  accepted a one-block step down under a lower ceiling, which needs a crouch.
  Drops and straight walks now require the entered column to be open at the
  height the body walks in at.
- The 11 `test_simple_bot_navigation.py` and 5 `test_class_selection.py`
  failures that predated this work are fixed in the tests: their fake
  observers lacked `can_shoot`, and they counted a class change as a
  scoreboard death, which the server deliberately stopped doing.

## Still visible in traces

- About 11 there-and-back head movements per bot-minute remain. Most follow a
  real change of mind (a new goal, a replaced route); a few are doglegs.
- Steep structures are climbed with two-block jumps, each of which takes one
  to two seconds while the native body settles against the wall.
- London bots see enemies across the river far more than they shoot: they
  close the distance first. That predates this work and was not touched.
- None of this has been judged in a rendered client yet.
