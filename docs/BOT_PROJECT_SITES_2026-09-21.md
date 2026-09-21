# Bounded cooperative project geometry

`server/bot_ai/project_sites.py` proposes small local projects to the shared
coordinator. It does not mutate the map, load files during decisions, query
an enemy registry, or run a global search. The caller supplies an observed
lane or public objective and nearby friendly reservations. The ordinary
gateway and domain services still validate every committed action.

| Helper | Proposal and hard bounds |
| --- | --- |
| `find_sniper_outpost` | At most 12 nearby candidates; equipped scoped weapon; reachable dry body position, clear known firing lane, two exits |
| `find_prefab_cover` | At most 12 placements from three equipped prefabs; at most 128 authored cells, actual stock, six-block anchor reach, supported shape, body/route exclusions, clear firing gap and a physically occluded chest-height approach |
| `find_bridge_project` | Complete crossing of at most six new cells, face support from the near bank, full price, dry landing and usable far exit |
| `find_breach_project` | At most six columns, 64 actual removals and eight seconds of recovered melee work; three-cell body clearance, preserved floor and known dry exit |
| `find_mine_approach` | Five side/rear candidates; equipped mine and authoritative deployable stock; walkable approach, thick 3x3x3 terrain, friendly blast separation and reserved-route exclusions |
| `find_decorative_site` | Four corner candidates; one supported block, no destruction, body separation and at least three builder exits |

Short approach checks use at most two 16-cell Manhattan walks with three-cell
headroom. They intentionally accept only level dry paths; ordinary movement
planning remains responsible for more complex traversal. Ray checks are
limited to a known lane within 96 blocks. Missing geometry, dead or airborne
lives, wading, exhausted stock, and uncertain supports fail closed.
Friendly safety checks cover all 32 supplied positions; optional sites reject
larger input lists instead of silently ignoring a potentially trapped player.

`ProjectSite` separates action `position` from body `approach`, records exits,
authored/new-cell costs, exact footprint, support cells and crossing landing.
Yaw is radians, matching `BotAction`. Breaches additionally report actual tool
ID and estimated work. Helpers never assume that returning a proposal means
it was built or used.

`load_bot_prefab_geometry` runs once at map/setup time through the same
`server.prefabs.PrefabRegistry` as authoritative placement. It reads at most
20 named models and excludes models over 256 cells. The primitive immutable
metadata contains all four quarter-turn expansions and bounds, with no native
handles. `MapSnapshot.prefab_geometry` transfers it and `SimpleVoxelWorld.load`
replaces the worker cache on every map, including empty snapshots. This covers
16 small shipped models. The original KV6 expansion uses `invscale=1` and the
recovered roll/pitch/yaw transforms. Cover anchors deliberately match the
current gateway's top-surface snapping; cave/roof-mismatched anchors are not
proposed.

`tests/test_bot_project_sites.py` validates exact real-KV6 expansion in every
yaw, pickle transfer/reset, no decision-time file access, resource/terrain
failure cases, complete bridge/breach route use, and bounded occupancy reads.
One real `BotActionGateway` → `PrefabActionService` test places exactly the
planned six cover cells, debits six blocks and verifies owner/observer packets.

`tests/test_bot_cooperative_projects.py` adds complete outpost cover → equipped
mine/radar → occupation scenarios through the actual gateway and placement
services. Other cases cover authoritative confirmation, builder/partner route
use, retained participant roles, failure backoff, loadout/life reset and human
edits. Movement completion is represented by snapshots after a production
route is validated. The separate native gate below tests actual movement.

## Offline native construction gate

`scripts/bot_cooperative_project_physics.py` creates two production server
instances without starting a network listener. Each has one Miner and one
Soldier partner, a supported debug floor, and either a four-block gap or a
two-block-deep wall. Only initial spawn positions, a public strategic lane,
and the accelerated monotonic clock are controlled. There are no position
assignments after spawning: the real brain/coordinator, planner, director
motor, gateway, mutation commits, inventory and native 60 Hz physics execute.
The partner begins four blocks behind and three blocks beside the builder,
so reaching the crossing also requires a real approach and alignment.

Run:

```
py -3.12 scripts/bot_cooperative_project_physics.py --json tmp/cooperative-project-physics-20260921/report.json
py -3.12 -m pytest tests/test_bot_project_sites.py tests/test_bot_cooperative_projects.py tests/test_bot_cooperative_safety.py tests/test_bot_cooperative_project_physics.py tests/test_prefabs.py -q
```

The 2026-09-21 run passed both scenarios:

| Scenario | Authoritative change | Simulated / wall time | Final Miner / partner body position |
| --- | --- | --- | --- |
| Bridge | Four added cells `(101..104,100,62)`; stock 20 → 16 | 5.88 s / 6.72 s | `(122.144,100.172,59.749)` / `(105.410,100.426,59.749)` |
| Breach | 18 Super Spade removals; walking floor preserved | 3.62 s / 6.93 s | `(118.668,101.089,59.749)` / `(104.945,100.730,59.749)` |

The companion test requires completed-builder and partner-uptake events,
both native bodies on the far side, actual walking distance, exact geometry
commits, no deaths/water/falls, correct bridge inventory payment, and native
melee requests for the tunnel. All 71 tests in the command above passed in
14.19 seconds. This is a two-scenario gate, not an all-map endurance claim.

The gate exposed and fixed a route-ownership defect: confirmation changed a
bridge project to `USE` while its advertised position still pointed to the
near bank. A partner reached that position, falsely recorded route uptake,
returned to unrelated navigation and fell beside the narrow bridge. Both
bridge and breach projects now publish the actual landing as soon as their
geometry is ready. Builder and partner completion require a grounded body in
the landing's horizontal cell; approach radii no longer mark a last bridge
cell as successful use. Snapshot regressions also cover partner arrival before
the builder has finished and attempted completion one cell short of the bank.
