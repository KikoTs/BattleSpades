# Navigation progress and travel gaze, 2026-09-21

The reported local AncientEgypt TDM match showed repeated movement and vertical
aim changes. The navigation audit found two independently reproducible causes;
neither relies on reaching a public server.

## Repeated movement is not necessarily progress

The previous physical watchdog accepted every 0.5-block displacement as motion
and renewed its longer watchdog after moving four blocks from its latest
anchor. It also reset escape-attempt diversity at that point. A real
`SimpleBotBrain` following valid WALK edges around a 6-by-6 square for 40
seconds therefore completed ten laps without recording a failed edge or
attempting recovery, despite no lasting improvement toward its strategic goal.

`simple_worker.py` now retains up to 128 recently visited two-block spatial
cells per bot life. Eight seconds of ground navigation without either entering
a new cell or making four blocks of lasting goal-distance improvement invokes
bounded escape planning and temporarily excludes the current approach for 12
seconds. Goal swaps retain spatial memory. Useful detours through fresh cells
remain valid even when they initially lead away from the goal. Retracing old
cells toward another objective also remains valid when it makes real progress.
Escape attempts reset on strategic progress rather than arbitrary displacement.

Planning-admission delays pause this clock and do not count as geometry
failure. Ordinary jumps and drops remain in the coverage contract: treating
every airborne/terrace edge as a fresh exception hid repeated slope loops.
Combat/idle gaps, excavation and dedicated flight have separate
progress contracts. The memory is bounded and resets with bot life/map state.
The detector is a local repeated-ground-route safeguard; it is not a guarantee
that every possible long or changing tactical route is useful.

## Travel gaze is separate from body steering

The old ordinary navigation look target was the exact body waypoint. A nearby
waypoint could pass below or behind the eye while the actor continued moving
between worker frames. The body still follows the exact validated waypoint,
but ordinary route gaze now points six blocks ahead along travel direction at
eye height. Combat, excavation and the dedicated flight controller retain
their own aim targets. Director angular integration and held-aim momentum
changes are separate from this navigation patch.

## Regression evidence

```powershell
py -3.12 -m pytest tests/test_bot_navigation_progress.py tests/test_bot_planning_integration.py tests/test_bot_route_invalidation.py tests/test_simple_bot_tactics.py tests/test_simple_bot_navigation.py -q
```

Result: **155 passed in 3.03 seconds**. The new file includes the square-loop
reproduction with stable and changing goals, a useful long detour, bounded
memory, retracing toward a different goal, escape-attempt preservation,
near-waypoint gaze, and 120 frames of actual shared-budget admission denial.

The initial local native process trace at
`out/bot-field-repro-20260921/first-trace.jsonl` contains 9,600 samples across
13 lives over 120 seconds. Review found no suspected 20-second travel loops,
no ordinary ground-travel pitch samples over 45 degrees, and no abrupt
ordinary-travel pitch sign reversals. This run loaded a mixture of already
changed navigation, director and coordinator code: it is diagnostic evidence,
not a controlled before/after comparison or proof of overall playing quality.
Final source-frozen native map/seed gates are owned by the release workflow.

## Native terrace follow-up

The later 180-second `egypt-final.jsonl` trace exposed a real remaining defect:
bot 2 traveled approximately 200 blocks between seconds 60 and 90 but ended
only 2.7 blocks from its starting position. Bot 5 traveled approximately 265
blocks for 2.36 blocks of displacement before escaping after second 94.
Ordinary jumps/drops had renewed the first version's coverage clock, and the
initial diagnostic excluded too much airborne travel. These intervals are
failures, even though other bots fought and the server continued ticking.

Bot 2's worker snapshot at second 60.3 had already reached the next ordinary
terrace landing near `(212.803, 231.246, 226.251)`, but still targeted the older,
lower waypoint `(211.5, 231.68, 227.75)`. Sprint momentum and delayed movement
directions led it to turn back and orbit instead of proceeding along the route.

The route follower now searches at most eight remaining steps for an actually
occupied later ordinary landing. It requires the original destination column
and height tolerance, and cannot cross any unexecuted special traversal edge.
This advances along an already validated route without relaxing jump, drop,
excavation or landing collision requirements. Sprinting also requires 3–5
blocks of straight, level route ahead, based on the same native velocity scale
used by the live motor's braking probes. Short turns and terraces use walking;
long clear stretches retain sprinting.

The focused command above now reports **163 passed in 3.00 seconds**, including
the exact recorded terrace coordinates, special-edge catch-up exclusions,
unchanged landing tolerance, jump/drop loop detection and braking before turns
while preserving clear straight sprinting. Native acceptance must use the next
trace after these fixes; the earlier 180-second trace is retained as failure
evidence.

The next 180-second `egypt-terrace-fix.jsonl` run improved the original interval:
bot 2 moved 54.32 blocks and bot 5 moved 77.72 blocks between seconds 60 and 90.
However, two other bot-5 intervals still returned near their starts after more
than 100 blocks of travel. This run also fails behavioral acceptance. Sparse
new neighboring cells can renew a local novelty timer, so that timer alone
cannot establish sustained useful movement.

The remaining motor defect was a frozen movement direction computed from an
older worker snapshot: native movement could travel several blocks before the
next decision, leaving the old heading pointed past or away from the waypoint.
Ordinary route intents now carry explicit `travel_source` and `travel_waypoint`
control fields. The motor can refresh its heading toward this authorized
landing on each physics tick while preserving the worker's crowd-separation
rotation and all live collision checks. These are internal control fields;
diagnostic paths are not movement authority. Combat, excavation, swimming and
dedicated flight intents do not acquire this ordinary-route control.
The worker's focused regressions remain **163 passed in 2.97 seconds** after
metadata wiring. Motor regressions and the next native trace are recorded by
the integration workflow; neither earlier failed run is a release pass.

## Shared-budget planning priority

The following `egypt-live-steering.jsonl` run resolved the previous orbit
cases, but bot 1's seconds 20–40 still produced a return interval with 50.65
blocks traveled and 2.2 blocks displacement. It was not 20 seconds of continuous
circling: seconds 28–36 were fresh `planning_wait` intents with an essentially
stationary authoritative body, followed by a backtrack.

Coarse corridor search ran before detailed local routing and requested the
observer's admitted job on every decision. An incomplete map-wide search could
therefore monopolize that observer's grants while its immediate route remained
empty or needed terrain revalidation. The novelty clock correctly paused
admission waits; treating those waits as bad geometry would mask the cause.

After a coarse slice, a bot with an empty, exhausted or stale route now reserves
its next grant for detailed local routing. Denied admission retains this
reservation until a local query actually completes. Existing routes continue
moving while background guidance advances. Rate, burst, per-job search and
expansion caps are unchanged.

The production regression uses a real 512-by-512 `SurfaceCorridorSearch`, a
shared `PlanningBudget`, and `SimpleBotBrain`: a distant inaccessible coarse
target cannot starve an executable immediate route. It covers both empty and
terrain-stale retained routes, an intervening competing observer, and resumed
coarse work after local movement begins. With the navigation and budget suites,
**181 tests passed in 3.09 seconds**. The next sealed native trace remains the
acceptance check for the complete behavior.

## Optional support on an inaccessible floor

Candidate 4 passed the 180-second Egypt loop gate but exposed London support
loops. The recovery handler also discarded unfinished corridor search work
when detecting a local cycle. It now retains a pending frontier that has
already learned the failed edge; a regression verifies both preservation and
the exclusion. This is a general recovery fix, not the cause attributed to
every London interval.

The source-only `london-diagnostic-5` run added fixed-size internal navigation
diagnostics to each worker intent and the local JSONL trace. It completed
without a geometric loop failure, but bot 3 still spent more than 40 seconds
without strategic progress on an optional `tdm_squad_support` destination.
Diagnostics showed no corridor endpoint, repeated escape attempts and returns
to the upper shelf. This geometric pass alone was not accepted as useful play.

The original London VXL reproduces the problem directly: from
`(297.5, 260.5, 225.75)` to the support offset near
`(280.44, 260.42, 236.65)`, corridor endpoint selection returns no route and the
bounded wet local query returns no steps after 256 expansions. The policy's
formation offset was on an inaccessible lower layer.

Only this optional TDM support role now has a bounded fallback. After a
confirmed endpoint failure, at least one escape attempt, and 15 seconds
without goal progress, the bot advances toward the existing authored enemy
anchor. The same support point is retried after 30 seconds, or earlier if it
moves eight blocks. Actual dry arrival clears old failure/cooldown evidence.
Reachable and untried support, recent enemy contacts, and critical objectives
retain their existing priority and behavior.

The final focused command includes `tests/test_bot_planning_budget.py` in
addition to the earlier five-file navigation coverage: **191 passed in 4.24
seconds**. Tests include the real London map endpoint failure, cooldown/retry,
moving support, actual arrival, untried/reachable support and critical-objective
preservation. Diagnostics are a fixed 15-field tuple of immutable scalar/vector
values, with no mutable path/search data or client protocol changes.

## Candidate 6: moving-target progress and recovery starvation

Both candidate-6 geometric loop checks passed, but the runs were rejected on
their trace evidence. London bot 3 remained on the upper shelf while its
general goal-progress age repeatedly dropped: the squad point moved toward
the stationary bot, falsely counting as actor progress. The earlier optional
support fallback could therefore fail to reach its deadline.

The optional-role commitment now measures actor distance toward a fixed support
anchor. Four blocks of real improvement, actual dry arrival, or a target
relocation of at least sixteen blocks renews that commitment. Smaller target
movement does not. Its fifteen-second no-progress time accumulates only on
active support decisions, pauses gaps longer than a second and current
excavation, and retains confirmed endpoint/escape evidence through transient
goal ownership. A recent endpoint failure within three blocks of the current
point is still required before fallback. Critical objectives and contacts
remain ahead of this policy. Cooldown duration and earlier release are unchanged.

Egypt bot 0 exposed a separate planning continuation bug: during seconds
77.2–91.7, its body stayed at exactly `(229.4929, 267.4671, 221.7424)` while
113 goal-coordinate changes repeatedly reset its escape search to candidate
zero or one. The search stored exact goal coordinates in its continuation key.
This was scheduling starvation rather than fourteen seconds of failed digging.

Escape searches now retain their bounded candidate sequence for the same goal
owner while the actor remains within one block and the target within sixteen
blocks of the original query. Every remaining query uses current terrain and
current failed-edge exclusions; no successful route is cached by this
continuation. Actor relocation, meaningful target relocation, ownership or
movement-capability changes restart it. Completed failed headings may be tried
again in the next bounded attempt, so a terrain edit neither repeats the first
query forever nor authorizes stale movement.

The shared-budget regression now reaches the ninth, wet recovery candidate
under repeated four-block target jitter and per-tick terrain edits using the
same nine grants as the stable-target case. Additional tests cover current
local terrain/new edge exclusions, actor/target/owner changes, target jitter
versus real physical support progress, combat/dig pauses and priority rules.
The navigation/budget suites plus the native melee-authority regression passed
**202 tests in 5.04 seconds**. Independent review also caught re-rejection of
an old edge while a relaxed escape query was deferred; renewed recent failures
now remain excluded and have a dedicated regression. Native acceptance is
recorded by the integration workflow. Fixed diagnostics now
contain twenty fields, including `support_no_progress_time`, the stable anchor,
failed endpoint/age and completed escape evidence. The next sealed traces are
required; candidate 6 is not a release pass.

## Candidate 7: excavation must create a dry bank

Candidate 7 passed the geometric loop and optional-support checks, but London's
bot 3 was still wading in 721 of 750 samples from seconds 45–119.9. The run was
rejected. Its two brief dry contacts at approximately seconds 63.7 and 88.6
did not establish a sustained exit. Native landing retirement/braking is
covered separately in `tests/test_bot_water_landing_motor.py`.

The navigation defect reproduces on the authored London bank at
`(296.5, 228.5, 236.75)`, beside column `(297, 228)` with solid terrain from
height 228 through the waterbed at 239. Assisted bank excavation targeted a
waterbed-level entrance, removing cells 237 and 238 and leaving floor 239.
Successful digging therefore extended a flooded tunnel. It could produce real
terrain mutations indefinitely without yielding a dry standing surface.

Assisted excavation now preserves a floor at 238 and clears all three standing
head/body cells at 235–237. Shore selection requires that full headroom before
handing movement to the jump motor. Tool targets use the authoritative digging
footprint, choose the lower safe faces in a stable order, and never remove the
preserved floor. In particular, the machete clears the 236/237 pair before 235,
instead of leaving a final target whose downward footprint would erase 238.

Moving to the next face retains the same tool's existing cooldown. Previously
that transition could immediately issue an early swing and mistake the normal
cooldown rejection for an unusable bank. The swim progress deadline also renews
once when the exact targeted exit cell actually disappears. Slow finite
excavation can finish, while unchanged geometry, unrelated map edits and merely
accepted swings do not extend that deadline.

`tests/test_bot_shore_landing.py` runs the real London VXL, worker, action
gateway, mutation replication, motor and 60 Hz native physics, including the
post-physics observation boundary. Spade, machete, pickaxe and the recorded
class-3 superspade all reach a safe dry standing body for two consecutive
seconds, preserve floor 238, clear 235–237, and require neither deaths nor
terrain recovery. Tests isolate survival with no strategic destination after
landing; complete-match behavior remains the next native acceptance gate.

Validation after these changes:

- `py -3.12 -m pytest tests/test_bot_shore_landing.py tests/test_simple_bot_navigation.py tests/test_simple_bot_tactics.py tests/test_bot_navigation_progress.py tests/test_bot_planning_integration.py tests/test_bot_planning_budget.py tests/test_bot_route_invalidation.py -q --tb=short`: **208 passed in 9.43 seconds**.
- `py -3.12 -m pytest tests/test_bot_gameplay_overhaul.py -k 'escapes_excavated_map_pockets or london_waterbed' -q --tb=short`: **7 passed in 14.19 seconds**, with the existing pocket deadlines unchanged.

Candidate 7 is retained as a failed water-recovery counterexample. Source is
frozen for candidate 8; these focused passes alone do not certify the full match.
