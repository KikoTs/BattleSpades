# Active worker planning budget

`server/bot_ai/planning_budget.py` enforces `path_requests_per_second` in both
the production thread supervisor and the simple process entry point. It uses
perception timestamps and one shared FIFO for `(observer_id, generation)`.
Requests coalesce; a denied observer keeps its position rather than repeatedly
competing in sorted bot-ID order.

## Bounds and deferral contract

- The default 24 requests/second at 8 decisions/second permits a burst of
  three jobs. Idle credits cannot exceed `ceil(rate / decision_hz)`, capped at
  eight jobs; admission is bounded by `rate * elapsed + burst`.
- One observer can start at most one job at a decision timestamp. An admitted
  route includes up to two ordinary/breach searches and 512 total expansions.
  Each retained corridor slice also consumes one job and remains capped at
  512 expansions. Already completed corridor searches spend nothing.
- `RoutePlan.deferred` means scheduling pressure, not inaccessible geometry.
  Callers must retain valid routes and avoid failed-site/edge learning or
  fallback retries. Deferred corridor advances keep their exact frontier.
- The active brain retains up to four completed queries only for the current
  unchanged planning attempt. This lets a failed dry query yield to its detour
  and water fallback on later grants instead of repeating the first query
  forever. Position, topology, movement contract or goal changes discard it.
  Untried detours do not increment geometry failure counts. Escape recovery
  separately resumes an index into at most 51 candidates, allowing later water
  and digging choices to receive a turn. Completed corridor joins retain their
  candidate index while waiting. Scheduling time pauses physical stall timers
  without inventing goal progress or erasing a retained breach.
- The pending queue and recent-grant table hold at most 128 observers. A
  departed requester expires after `max(1 second, 2 / decision_hz)`. A batch
  refreshes its alive waiters before execution so an overloaded worker does
  not expire later bot IDs before serving them. Completed combat/non-planning
  decisions cancel unused pending turns; decision-frequency skips retain them.
- Map snapshots reset scheduling state. Existing per-query geometry bounds
  still apply when standalone tests intentionally omit a planning budget.

The aggregate admission cap covers detailed `SimpleVoxelWorld.plan` and
incremental corridor expansion. Cheap LOS/body probes, bounded corridor
endpoint setup, and the existing separate water-recovery search are not new
rate-controlled jobs in this change. It does not claim a bound on authoritative
terrain collapse, rendering, replication, or total server tick time.

## Local terrain invalidation

`SimpleVoxelWorld.route_needs_replan()` retains simple WALK/CROUCH routes
across distant edits instead of spending every bot's next planning grant when
one teammate builds. A fixed 512x512 signed 64-bit column revision table uses
2 MiB and does not grow with terrain history. Validation checks at most 64
remaining steps plus the observer's current position, each with its adjacent
column neighborhood. Any edit in those columns conservatively invalidates the
route at every height, including support removal and nearby walls.

Jump, breach, build, swim and flight routes, long/sparse routes, nonfinite
coordinates and unknown revisions retain global invalidation. Map loading
resets history. The authoritative motor continues checking actual collision;
this optimization does not authorize movement through an unchecked obstacle.

`tests/test_bot_route_invalidation.py` includes 5000 mutations with fixed memory,
map resets, unknown history, complex-route fallbacks, and real-brain movement
while another observer owns the only grant. With the existing navigation,
tactics and budget suites, 163 tests passed in 3.08s after this change.

## Profiling and validation

`AIThreadSupervisor.planning_metrics()` returns its last worker-owned snapshot
under the supervisor lock. The process worker logs the same snapshot every
ten seconds while processing messages. `PlanningBudget.snapshot()` contains
requested/granted/deferred/expired/queue-full counts, pending observers, actual
search/expansion counts, and work duration p95/max in milliseconds over the
last 256 completed jobs. Duration measures worker wall time, not server-main
tick time; it includes bounded route setup, search and reconstruction.

`AIThreadSupervisor.behavior_metrics()` separately returns detached counters,
the last 256 coordinator events, active project totals/kinds, and active life
count. The owner thread copies these at batch end, so readers never inspect a
mutable live brain. Process workers log the same bounded snapshot every 10s.

```powershell
py -3.12 -m pytest tests/test_bot_planning_integration.py tests/test_bot_planning_budget.py tests/test_bot_liveness.py tests/test_simple_bot_navigation.py tests/test_simple_bot_tactics.py -q
```

The initial combined run passed 155 tests; an additional full-brain dispatch
regression brought the focused integration file to nine passing tests.
Regressions cover fixed-order fleets of 12/24/48 observers,
48-observer batches delayed by two seconds, absent/combat requester cleanup,
bounded queues and profiling samples, real traversal/breach searches, retained
corridor work, and the real thread dispatch/process entry configuration hooks.
Real-brain scenarios cover deferred dry/detour/wet and wet/dry fallback chains,
topology changes during admission waits, retained route/breach/edge state,
an island escape reaching a later water heading, completed corridor joining,
and detached bounded behavior diagnostics.
These prove admission/fairness invariants, not live tactical quality or a
0.75 ms main-thread performance claim. The broader bot behavior rollout owns
the final local soak measurements.

## Broader regression and clock replay

The broader serial run covered 29 bot/navigation/mode-related files and
reported 637 passed, 3 failed in 522.02s. Full stdout, JUnit and the exact file
list are in `out/bot-beta-1.1/regression-bots.*` and
`out/bot-beta-1.1/regression-bots-files.txt`.

Two failures were stale A2S test expectations: public browser category 1 is
independent of TDM/CTF session IDs 6/8, as recovered in `STEAM_DISCOVERY.md`.
The tests now parse the actual EDF keyword field and assert public category,
classic presence/absence and the required protocol/game identity while allowing
additional tags. Production discovery and flight negotiation were unchanged.
The third failure was London Diamond Mine seed 19's 120s navigation scenario.
The original result and geometry remain in `london-dia-failure.json`.

The matrix seeded random choices but selected its initial monotonic clock from
host uptime. Surface mining uses `int(now * 2) & 7`, so identical seeds could
excavate different cells. `simulation_clock_base()` now chooses a repeatable
default phase 0 after setup. `--clock-phase` selects another phase;
`--clock-base` replays a recorded phase, moving an old epoch forward by whole
eight-second cycles so actor initialization cooldowns remain in the past.
Results record requested base, actual base and phase. Eleven clock regressions
include matching every mining choice over the original failure's 7200 ticks.
This normalizes the known clock-phase input; it does not claim bit-for-bit
determinism of all native floating-point or asynchronous behavior.

After the local invalidation change, both the original phase 292988.734 and
default phase 0 passed the unchanged London DIA 120s gate. The original-phase
replay's maximum navigation trap was 4.93s, below the existing 8s limit. Evidence:
`out/bot-beta-1.1/london-dia-replay.{json,log}` and
`out/bot-beta-1.1/london-dia-normalized.{json,log}`. A separate final-source
focused rerun passed 120 A2S/Steam/coordinator/project/integration/DIA tests in
12.49s before the local invalidation optimization. Runtime performance baselines
are recorded separately by the release smoke workflow; these tests do not
claim a main-thread timing improvement.
