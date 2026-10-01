# Bot schematics, block-line planning and cooperative building

Package: `server/bot_ai/schematics/` (`model.py`, `library.py`, `planner.py`,
`sites.py`), integrated into `server/bot_ai/cooperative_behavior.py` as the
`"schematic"` task kind. Everything is worker-local and pure; the action
gateway and `CombatSystem.handle_block_line` stay the only authority.

## API for strategy code (zombie / VIP agents)

All entry points take a `solid(x, y, z) -> bool` callable; inside the worker
use `brain.world.solid` (the `SimpleVoxelWorld` the coordinator plans on).

```python
from server.bot_ai.schematics import get, make_stair, fit, plan_build, structure_placement

# 1. Ask the team's bots to build a library schematic (or a generated one).
site = brain.cooperative.request_schematic(
    team, "stairs",            # name, Schematic (e.g. make_stair(6, 1)) or Placement
    anchor_body_position,       # where it goes (body/point position; ground found nearby)
    now,
    facing=(dx, dy),            # forward/climb direction; or rotation=0..3 for an exact fit
    builders=(bot_id, ...),     # optional: only these bots may work on it (default: any teammate)
    priority=0.95, ttl=60.0, requester="zombie_tower",
)                               # -> SchematicSite | None (None: no feasible plan)
brain.cooperative.schematic_status(site.site_id)  # dict: done/remaining/lines/singles/abandoned
brain.cooperative.cancel_schematic(site.site_id)

# 2. Arbitrary block-line structures (exact world cells, no terrain fitting).
placement = structure_placement("zombie_stair", cells)          # tuple of (x, y, z)
site = brain.cooperative.request_schematic(team, placement, anchor, now, builders=(...))

# 3. Pure planning without the coordinator (execute steps yourself).
plan = plan_build(world.solid, placement)   # BuildPlan: .steps (BuildStep), .feasible, .unbuildable
for step in plan.steps:                     # step.cells[0] is the supported drag start
    BotAction(BotActionKind.BUILD_LINE, int(C.BLOCK_TOOL),
              position=step.start, end_position=step.end)   # or BUILD for a single cell
```

Requested sites bypass the optional-construction cooldown, and bots listed in
`builders` (or any teammate within 28 blocks when empty) will work on them
even while holding a committed mode role (e.g. `zombie_*`), unless they are
in close combat. A request returns `None` when no plan is feasible, e.g. the
cells cannot be supported in any order or reached from a walkable stand.

## Schematic format

ASCII layers, bottom first; rows FRONT (toward the threat) to BACK. Legend:
`#` primary, `+` accent, `=` secondary (palette tokens per schematic),
`_` keep-clear (doors, firing slits, interior/headroom — must be air, never
built), `.` don't care. `anchor=(col,row)` is the structure's reference
column; `occupant=(u,f,h)` is where a VIP/sniper stands. Rotations are the
four quarter turns; `rotation_for_facing(dx, dy)` picks the one facing a
threat. `fit()` levels terrain (foundation fill up to two cells, one-cell
rises tolerated) and rejects blocked keep-clear cells, foreign obstructions,
out-of-map or out-of-build-band cells.

## Library

`server/bot_ai/schematics/library.py` (all validated in 4 rotations; plan
numbers are for one builder on flat ground, rotation 0):

| Name | W x D x H | Blocks | Steps | Lines/singles | Plan est. s | Purpose | Tags |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `sandbag_wall` | 5x1x2 | 10 | 2 | 2/0 | 1.5 | cover | cover, barrier, any |
| `cover_wall` | 7x1x3 | 19 | 6 | 4/2 | 4.1 | cover | two firing slits |
| `corner_cover` | 5x3x2 | 14 | 4 | 4/0 | 4.5 | cover | L-shaped |
| `small_hut` | 5x5x4 | 67 | 19 | 18/1 | 18.9 | shelter | back door, 3 windows, roof |
| `bunker` | 7x5x4 | 87 | 17 | 17/0 | 18.1 | defend | wide front slit, roof |
| `pillbox` | 5x5x4 | 65 | 17 | 15/2 | 14.0 | defend | front + side slits |
| `vip_shelter` | 5x5x4 | 67 | 21 | 17/4 | 22.2 | vip_shelter | occupant box, door, slits, gold trim |
| `watchtower` | 3x7x6 | 40 | 12 | 10/2 | 11.5 | overwatch | legs, platform, parapet, back stair |
| `sniper_nest` | 3x4x3 | 24 | 9 | 7/2 | 8.7 | overwatch | raised step + parapet |
| `stairs` | 2x4x4 | 20 | 7 | 7/0 | 7.4 | traversal | zombie, climb |
| `objective_ring` | 9x9x2 | 60 | 12 | 12/0 | 13.5 | objective | ring r4, two entrances |
| `base_ring` | 15x15x2 | 108 | 16 | 16/0 | 46.6 | objective | ring r7 (outside base protection) |
| `bridge_4`, `bridge_6` | 1xN | 4/6 | 1/3 | | | traversal | deck at walking level |

Generators: `make_stair(height, width)`, `make_ring(radius, height)`,
`make_bridge(length)`. Palette tokens: `team` (keep the bot's colour),
`sand`, `concrete`, `wood`, `dark`, `olive`, `brick`, `gold`, `stone`.
Doors are three high (bots do not crouch through two-high gaps); every
enclosed structure keeps a walkable exit, which also satisfies the
server's sole-friendly-exit rule for a VIP standing inside.

## Block-line planner (`planner.py`)

Mirrors `CombatSystem.handle_block_line`: a straight run is valid exactly
when its drag start face-touches solid (later cells rest on earlier ones),
cells are generated like the client's `cube_line`, already-solid cells are
skipped. Plan rules:

* runs along x/y (and upward z) through same-colour cells, at most
  `MAX_LINE_CELLS = 8` (server cap 64); single blocks only where no run fits;
* order: foundation (terrain levelling) first, then the lowest unfinished
  layer, longer and horizontal drags preferred, short walks between stands;
* every step has a stand: a body-clear support (terrain or built structure)
  reachable by walking with 1-block step-ups from the site boundary or the
  builder's position, both endpoints within `PLAN_REACH = 5.5` of the eye and
  visible (voxel DDA), no planned cell in the builder's body column, and the
  builder can still walk out after the step;
* cells that cannot be supported or reached are returned as `unbuildable`
  (`plan.feasible` is false); bounded by region and 400 cells.

`validate_plan` replays a plan (support in line order, reach, visibility,
coverage exactly once, keep-clear untouched, no walled-in builder, final
structure face-connected to terrain so the floating-chunk collapse cannot
drop it).

## Cooperative flow (`sites.py` + `cooperative_behavior.py`)

A site is a shared plan. Each builder claims one ready step (drag start
supported now, not done/claimed/backed off), gets a stand that avoids other
builders' claimed cells, stand columns and bodies, walks there, switches to
the block tool with a varied 0.2-0.6 s delay, aims (the director's aim
model converges on the start cell before executing) and sends one
`BUILD_LINE`/`BUILD` (`argument="rgb:RRGGBB"` selects a palette colour via
the gateway like SetColor). Authoritative world deltas confirm the step;
then a 0.2-0.5 s human pause precedes the next claim. Rejections back a step
off (3 s x failures), five failures skip it, sixteen abandon the site.

Triggers:

* **VIP mode**: the VIP (`vip_rally`, not hurt for 6 s, a teammate within
  14, not within 9 of a spawn anchor) creates `vip_shelter` around itself,
  facing the enemy anchor, and builds from inside (it never steps outside
  the interior, first trying its current spot). Escorts
  (`vip_guard_formation`) join it; after completion they add up to two
  `sandbag_wall`/`corner_cover` barriers in front. The finished VIP holds
  inside (`vip_sheltered`) while at least 70% of the box stands.
* **Defenders** holding a post (DEFEND posture within arrival+3, creativity
  >= 0.45) dig in: `objective_ring` around an own intel/hill/territory, else
  a defensive schematic facing the watched approach.
* **Optional** (non-committed roles): join any team site within 28 blocks;
  rarely start one (creativity > 0.55, holding position, recent contact,
  30% per 20 s window, team cooldown 45 s, at most 6 per team per map,
  at most 3 active, 10 blocks apart). Sniper loadouts prefer
  `sniper_nest`/`watchtower`, and finished overwatch posts are climbed
  (tread-by-tread over the built stair) and held for up to 25 s.
* **Requests** from strategy code (see API).

Abandonment: no progress for 45 s, lease expiry, too many rejections, or 4
incoming hits on builders within 15 s (`under_fire`). Combat contact still
interrupts a builder exactly like other cooperative tasks.

(Verification results follow below.)
