# Direct VXL bot navigation

Current implementation: `server/bot_ai/simple_worker.py`,
`simple_navigation.py`, `surface_corridor.py`, `director.py` and the thread/process
supervisors. Configuration lives in `server/config.py`; reproduction commands
are below. The cached atlas and reports are derived data; authored VXL maps
and `tests/fixtures/` remain source inputs.

BattleSpades production bots navigate the VXL directly in one bounded owner
thread by default. The optional process backend runs the same planner:

- A bounded surface A* plans short, concrete segments over walkable VXL
  columns. Search radius and expansion count are hard-capped.
- An incremental layered-surface search remembers longer dry detours when
  local segments stop advancing the goal. It spends at most 512 heap pops per
  decision, with 32,768 total pops and discovered cells per attempt. Its
  corners are guidance for the live voxel planner, never direct motor orders.
  It follows the bot's current floor beneath roofs and bridges. Joining a
  completed search requires a short validated connection; a nearby waypoint
  across a wall cannot skip the detour.
- Solid body-height columns are costed as explicit breach edges when the bot
  owns a digging tool. The edge records the exact voxel, retail footprint,
  mouse button, cadence, and estimated swing count.
- A map-wide semantic atlas describes connected primary ground, narrow
  passages, shorelines, water escape flow, and upper/underground layers.
- The brain owns one strategic goal, a concrete route, physical progress
  deadlines, and a small set of recently blocked edges. Empty plans trigger
  a short validated escape segment; water pocket recovery briefly commits
  to an open swim direction before reconsidering the shore.

Recast/Detour and the old multi-state worker remain in the repository only as
a rollback/reference path; the production thread does not load them. The
atlas and planner do not replace authoritative physics. Every immediate
movement, jump, dig, and build is revalidated by the gameplay process before
it can affect the world.

Completing a final walking waypoint requires entering its destination cell or
passing the waypoint between observations. Intermediate corners retain a
small tolerance for smooth traversal. Jump completion requires the correct
height and a dry landing when leaving water. A long detour is allowed to move
away from its goal without having valid edges blacklisted.

Local steering tries an exact forward-facing grid axis when angled probes
cannot fit a narrow passage. A body already balanced over a water column can
use validated swim movement to recover onto the bank before the native wade
flag changes. Water probes use the water plane beneath bridges and after
waterbed excavation, rather than requiring the column's topmost floor.

Escape planning preserves freshly failed edges for at least four seconds.
Only when ordinary alternatives are exhausted does it revalidate older
failures, and it releases only exclusions used by the selected live route.
Repeated escapes vary direction and search farther. Their walking destination
must be far enough away to leave the physical watchdog's previous region.
When excavation strands a bot on an isolated dry cell, recovery can choose a
validated swim or a concrete breach. An already-occupied path placeholder is
not treated as useful progress.

Engineer and extended Rocketeer packs can cross short, same-height gaps of up
to three missing columns. A fuel reserve and clear takeoff/landing volume are
required. The worker owns takeoff, crossing and thrust release until native
physics lands the body; visible enemies cannot interrupt that sequence.
Normal short-duration packs and unrestricted vertical flight are not exposed
as navigation edges. Ordinary jumping/crouching uses the existing motor.

Each full game/map transition disconnects the old bot roster and rejoins fresh
players with zero match stats, new profiles and increasing internal generations.
Humans retain their player objects. Arena elimination subrounds retain their
match roster and scores; full match restarts use the fresh-roster boundary.

Bot construction includes fractional foot clearance and is checked again after
movement before committing. A body trapped below solid, immutable waterbed for
six seconds uses normal death and mode-controlled respawn as a last resort.
This does not trigger on normal swimming, defensive holds or long excavations.

Bridge builders check for a dry landing within their remaining inventory
before extending a floor. Placement still uses ordinary reach, protection,
body intersection, resource, and mutation rules. Assault bots search nearby
dry positions after reaching an enemy-side anchor; wounded TDM/arena bots
resume their goals after a short regroup if no new damage arrives.

Motor phases advance on actual motor passes so periodic perception work cannot
starve one group of bot IDs. Fresh intents can cross an unrelated terrain
update; live motor/action validation still checks the current world. Each
observer decision is isolated at both thread and process worker boundaries.
An unexpectedly exited thread restarts from the current map and terrain
overlay, while shutdown retains ownership until the old thread really exits.

## Why it exists

The older water recovery cached the result of each bot's independent BFS in
one shared dictionary. Later searches could overwrite cells already used by
another route with an incompatible direction. After enough play, merged
routes could point backward or form cycles.

The atlas builds water flow once with a reverse, multi-source BFS. Every water
edge has a strictly decreasing integer distance, so shared routes can merge
but cannot cycle. A normal two-block shore is preferred for the whole
connected water component. If an authored basin has only a tall bank, the
flow leads to a bounded build/climb bank; the planner stops before the
impossible final jump and lets normal physical recovery build upward.

Construction also treats a narrow cell in the main ground component as a
passage to preserve, and fortification scoring avoids switching accidentally
between upper-ground and underground layers.

## Planned excavation

Excavation is part of the same bounded A* search, not a blind stuck fallback.
The planner compares estimated mining time with ordinary walk/jump detours. A
short open route therefore wins over digging, while a sealed wall can become a
costed tunnel. The production brain stops at the wall, selects the owned melee
tool, aims at the planned voxel centre, and waits for the authoritative terrain
delta before replanning.

Climbs also check the ceiling above the source body: the trailing half of
the native capsule rises before leaving its original column. If that space
is solid, the planner clears it through an ordinary owned melee tool first.
Overhead digging keeps the bot standing. The motor derives movement keys
from horizontal facing independently of aim pitch, so looking sharply up
or down cannot turn a valid movement command into four released keys.
Water braking lookahead grows from the immediate body probe with speed;
stationary bots can enter a safe corner without testing beyond its turn.
The shore progress watchdog continues through airborne false-wade frames
until the existing dry-landing checks release water recovery.
A verified grounded dry landing also clears the director's short water
observation lease immediately, preventing a completed crossing from
requesting another shore jump.
Jump routes retain their original takeoff edge across airborne movement and
unrelated terrain replans, so recovery excludes the edge A* actually used
instead of reconstructing a different source from a body over the gap.

Dig costs and aim footprints share the same recovered profiles as combat:
single-cell tools account for block health and repeated hits, ordinary spades
target a three-cell vertical column, the Machete targets its vertical pair,
and Super Spades use their area footprint. UGC Super Spade navigation uses its
retail secondary 3x3x3 action. Planned breach swings execute only when the live
server raycast hits the selected cell, preventing aim tolerance from removing
a neighboring layer or the tunnel floor.

## Cache format and safety

Each `maps/<name>.botnav` file is derived from the exact VXL bytes and contains
only fixed-width integer arrays compressed with zlib. It contains no Python,
pickle, DLL, or executable payload.

At load time the worker verifies:

- format magic and version;
- fixed decoded length and a hard size ceiling;
- BLAKE2s digest of the matching VXL;
- bounded array indices;
- dry route goals; and
- strictly decreasing water distances.

A stale, missing, oversized, or corrupt cache is ignored. The bot thread builds a
fresh atlas from its private VXL copy instead. Restart snapshots containing
live terrain edits also build from the patched snapshot rather than reusing
the authored-map cache.

## Build and validate

Build, serialize, read back, and simulate the longest routes for every map:

```powershell
py -3 scripts/build_bot_navigation.py
```

Verify all existing caches without rebuilding:

```powershell
py -3 scripts/build_bot_navigation.py --check
```

Select specific maps or run without writing:

```powershell
py -3 scripts/build_bot_navigation.py --map CastleWars --map TokyoNeon
py -3 scripts/build_bot_navigation.py --map GreatWall --no-write --json
```

The release packager copies matching `.botnav` files beside the VXL maps.
Custom maps remain supported without a precomputed cache.

Run an accelerated continuous-combat endurance matrix with real native
movement, projectiles, terrain edits, corpses, and respawns:

```powershell
py -3.12 -m scripts.bot_map_matrix --map London --map GreatWall --map MayanJungle --seconds 1800 --bots 12 --seed 7 --respawns --json tmp/bot-validation/endurance.json
```

The report measures actual displacement, shore escape, team congestion, and
zero-motion unfinished segments. It saves each completed case during the
run. Add `--fail-fast` while diagnosing a failure to stop each case at its
first stall and preserve the terrain at that point; the report records the
actual simulated duration. Intentional objective holds and active firing are distinct from a
navigation stall. This accelerates simulation time; use the runtime smoke
for actual thread/process scheduling:

```powershell
py -3.12 -m scripts.bot_runtime_smoke --map London --seconds 120 --bots 12 --worker thread --full-runtime --restart-worker-at 60 --json tmp/bot-validation/runtime.json
```

Add `--water-spawn-bots 4` to inject four swimmers. That check records each
bot's first supported dry landing, so entering water again later does not
erase a successful escape.

Focused regressions are in `tests/test_bot_gameplay_overhaul.py`,
`tests/test_surface_corridor.py`, `tests/test_bot_liveness.py`,
`tests/test_bot_voxel_navigation.py`, and `tests/test_simple_bot_navigation.py`.
`tests/fixtures/` contains the captured terrain used by those tests; keep these
fixtures with source, even when removing disposable validation reports.

Long vertical jetpack routes, deliberate two-cell crouch tunnels, and richer
prefab tactics remain outside the enabled navigation contract. The six-second
buried-waterbed recovery is a normal death, not a successful route.

No finite matrix guarantees recovery on every destructible or custom map.
Unreachable objectives, fully enclosed bodies, and multi-level terrain still
need explicit regressions when discovered. These changes do not add packets,
alter retail input semantics, or require a custom client.
