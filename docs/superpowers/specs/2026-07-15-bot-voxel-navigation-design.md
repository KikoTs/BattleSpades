# Bot Voxel Navigation and Survival Design

**Date:** 2026-07-15
**Status:** Approved for implementation

## Objective

Make bots traverse the live voxel world without walking into water, stepping
off unsafe edges, jumping forever, entombing themselves, or idling below an
elevated target. Zombies must pursue survivors aggressively through walking,
jumping, digging, and class-legal construction while retaining human-visible,
server-authoritative actions.

## Diagnosed Failures

The existing worker builds walkable Recast polygons on the universal waterbed
at `z=239`. The gameplay-thread motor validates body obstruction but does not
validate supporting ground, water, or drop height. The worker records full 3D
displacement as progress, so jumping in place and water bobbing continuously
reset its stuck timer.

Native Recast steering currently returns only an XY direction. It discards the
next waypoint's height, the required movement ability, and whether the returned
path actually reaches the requested endpoint. Close Zombies bypass navigation
entirely and steer directly toward a survivor, which makes them stand below an
elevated target and repeatedly jump.

Bot construction exposes only a single-block action even though the shared
combat service already implements validated, atomic, color-stable BlockLine
placement. Single-block cover is too small, and ordinary construction does not
reject every cell overlapping the builder's body.

## Architecture

Navigation is hierarchical:

1. Recast provides a dry, long-distance corridor across the map. It is a route
   hint and never directly authorizes a movement input.
2. A bounded local voxel-action planner searches the next 16-24 blocks. Its
   nodes represent a standable body position and its edges represent timed
   actions: `WALK`, `CROUCH`, `JUMP`, `DROP`, `BREACH`, `BUILD_STEP`,
   `BUILD_BRIDGE`, and `PLACE_PREFAB`.
3. The gameplay-thread motor checks the next action against the authoritative
   VXL at 60 Hz. Stale or unsafe actions fail closed and request a new plan.

Every route and action carries the topology version that produced it. Canonical
world mutations dirty affected navigation tiles and invalidate a local plan
whose immediate cells changed.

## Terrain Semantics

The worker exposes one canonical surface classifier. A position is standable
only when it has solid support, two cells of body clearance, valid map bounds,
and is not an open-water column. Surfaces additionally record head clearance,
drop depth, destructibility, and nearby edge exposure.

Ordinary routes reject water and drops beyond the active class profile. The
live motor probes the intended center, left shoulder, and right shoulder before
committing forward motion. Sprint is disabled around uncertain ground, jumps,
construction, and breaches.

## Dynamic Local Action Planning

The local planner uses actual class movement limits and estimates action time,
not just geometric distance. Jump edges require a clear simulated arc and a
standable landing. Drop edges require a safe landing depth. Breach edges include
block health/tool time. Construction edges include stock, support, protected
zones, body clearance, and placement time.

A visible moving target produces an intercept region based only on delayed,
observed velocity. The plan refreshes when the target changes surface, the
topology version changes, the next edge fails live validation, or projected
progress stops. Replanning is local; normal target movement does not rebuild
the entire map or block the server thread.

Close-range direct steering is allowed only when the target is on a compatible
surface and within the current movement action's vertical reach. Otherwise the
Zombie continues through the voxel-action planner. A partial global path below
an elevated survivor escalates through jump, climb construction, breach, and an
alternate approach instead of repeated vertical jumping.

## Water and Stuck Recovery

`PlayerSnapshot` includes the authoritative `wade` state. Entering water cancels
combat strafing and activates a dedicated recovery goal:

1. Search outward for the nearest dry, body-clear standable surface.
2. Move toward it with bounded jump pulses.
3. Breach an obstructed shore when the held class tool permits it.
4. Place a supported step/ramp or Zombie prefab when no natural exit exists.
5. Resume the prior objective only after multiple stable dry-ground frames.

There is no recovery teleport.

Progress is measured as reduction in distance to the current waypoint/objective
plus horizontal displacement projected along the requested route. Rotation,
vertical bobbing, jumping in place, and knockback do not count. Recovery
escalates through local avoidance, fresh local plan, dig/build route, and an
alternate approach. A repeatedly failing cell/action pair is temporarily
blacklisted.

Jump requests are events, not leased Boolean states. The director holds jump
for a small fixed number of simulation ticks, forces a release, and requires a
new planner event before another topology jump. Combat-evasion jumps use a
separate cooldown.

## Construction and Cover

The bot action gateway adds a BlockLine action with two endpoints. It reserves
the exact generated cells and delegates to `CombatSystem.handle_block_line`;
it never mutates VXL or inventory directly.

Defensive cover is built as supported 3-5-cell horizontal segments oriented
across the threat direction. A later action may add a second row when stock,
support, and combat pressure permit. Bots immediately move behind accepted
cover instead of waiting several seconds after one voxel.

Construction rejects cells intersecting any living player's body/head volume,
the builder's immediate escape corridor, protected spawn/objective zones, or a
reserved friendly path. The same checks apply to local climbing steps and
prefabs.

## Zombie Class Policy

Production bot selection uses the base Zombie class by default. Fast Zombie and
Jump Zombie remain available only behind an experimental configuration switch
until their movement constants, frequency, and intended roles are validated
against original gameplay footage and the retail client. Recovered physics
constants are not silently changed.

## Performance and Failure Rules

All voxel search, route scoring, target interception, and action selection run
inside the AI worker. The server thread performs only bounded live probes and
normal gameplay action validation. Search horizons and node counts remain
bounded even though early development prioritizes behavior quality over tight
CPU targets.

Worker failure leaves bots safely idle. Invalid maps, stale topology versions,
failed collision queries, and incomplete paths all fail closed. No planner
result may bypass normal movement, combat, inventory, construction, replication,
or class authorization.

## Acceptance Tests

- A dry bot refuses an input leading onto the `z=239` waterbed or across an
  unsafe drop.
- A wading bot selects a dry exit, uses bounded jump pulses, and escalates to a
  legal breach/build action when the shore is blocked.
- Jumping in place does not reset the progress timer or prevent stuck recovery.
- Jump input releases between planner events at the 60 Hz motor boundary.
- A Zombie below an elevated survivor chooses a reachable jump, step, prefab,
  breach, or alternate route rather than direct steering and jump spam.
- A topology mutation affecting the immediate action invalidates that action.
- Bot cover uses replicated BlockLine placement with exact inventory and color.
- No cover, step, or prefab may overlap a player or seal the builder's escape.
- Base Zombie is the default bot class; experimental variants require explicit
  configuration.
- The focused bot suite and the complete server suite remain green, followed by
  a live retail-client soak covering water, cliffs, towers, and terrain edits.
