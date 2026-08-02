# Direct VXL bot navigation

BattleSpades production bots navigate the VXL directly in one bounded owner
thread:

- A bounded surface A* plans short, concrete segments over walkable VXL
  columns. Search radius and expansion count are hard-capped.
- Solid body-height columns are costed as explicit breach edges when the bot
  owns a digging tool. The edge records the exact voxel, retail footprint,
  mouse button, cadence, and estimated swing count.
- A map-wide semantic atlas describes connected primary ground, narrow
  passages, shorelines, water escape flow, and upper/underground layers.
- The brain owns one goal, one route, one progress deadline, and a small set of
  recently blocked edges. A failed edge is invalidated and replanned instead
  of entering another recovery state machine.

Recast/Detour and the old multi-state worker remain in the repository only as
a rollback/reference path; the production thread does not load them. The
atlas and planner do not replace authoritative physics. Every immediate
movement, jump, dig, and build is revalidated by the gameplay process before
it can affect the world.

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
