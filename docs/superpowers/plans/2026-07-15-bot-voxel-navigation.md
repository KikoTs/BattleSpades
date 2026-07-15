# Bot Voxel Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make bots avoid water and unsafe edges, recover from water/enclosure, pursue elevated survivors through class-legal voxel actions, and build useful replicated cover.

**Architecture:** Recast remains a dry global corridor provider. A focused local voxel planner supplies height-aware movement actions, while the gameplay-thread director validates the immediate action against authoritative terrain and converts jump events into bounded pulses. All digging and construction still pass through normal server gameplay services.

**Tech Stack:** Python 3.12, immutable dataclasses, Cython VXL/Recast bindings, pytest, authoritative 60 Hz simulation.

## Global Constraints

- Production bot selection uses the base Zombie class by default.
- Fast Zombie and Jump Zombie require explicit experimental configuration until retail/video validation.
- Waterbed `z=239` is never ordinary walkable terrain.
- No path result may directly mutate VXL, inventory, physics, or replicated entities.
- Worker search is bounded and stale topology results fail closed.
- New behavior follows red-green-refactor; every regression test must fail before production code changes.

---

### Task 1: Canonical Dry-Surface Semantics

**Files:**
- Create: `server/bot_ai/voxel_navigation.py`
- Modify: `server/bot_ai/messages.py`
- Modify: `server/bot_ai/director.py`
- Modify: `server/bot_ai/worker.py`
- Test: `tests/test_bot_voxel_navigation.py`

**Interfaces:**
- Produces: `SurfaceSample`, `VoxelTerrain.classify()`, `VoxelTerrain.standing_node()`, and `PlayerSnapshot.wade`.
- Consumes: a fail-closed `Callable[[int, int, int], bool]` solid query.

- [ ] **Step 1: Write failing tests for waterbed rejection and snapshot wade state**

```python
def test_open_waterbed_is_not_an_ordinary_standing_node():
    terrain = VoxelTerrain(_solid_columns({(10, 10): {239}}))
    assert terrain.standing_node(10, 10, 236.75) is None


def test_waterbed_can_be_sampled_for_explicit_recovery():
    terrain = VoxelTerrain(_solid_columns({(10, 10): {239}}))
    sample = terrain.classify(10, 10, 236.75, allow_water=True)
    assert sample is not None
    assert sample.water is True
```

- [ ] **Step 2: Run the tests and confirm the missing-module/API failure**

Run: `python -m pytest tests/test_bot_voxel_navigation.py -q`

Expected: FAIL because `server.bot_ai.voxel_navigation` and `PlayerSnapshot.wade` do not exist.

- [ ] **Step 3: Implement typed surface classification**

```python
from collections.abc import Callable
from dataclasses import dataclass


SolidQuery = Callable[[int, int, int], bool]
WATERBED_SUPPORT_Z = 239
PLAYER_STANDING_OFFSET = 2.25


@dataclass(frozen=True, slots=True)
class SurfaceSample:
    x: int
    y: int
    support_z: int
    water: bool
    head_clear: bool


class VoxelTerrain:
    def __init__(self, solid: SolidQuery) -> None:
        self._solid = solid

    def classify(self, x: int, y: int, player_z: float, *, allow_water: bool = False) -> SurfaceSample | None:
        if not (0 <= x < 512 and 0 <= y < 512):
            return None
        expected = int(round(player_z + PLAYER_STANDING_OFFSET))
        candidates = range(max(2, expected - 3), min(240, expected + 4))
        for support_z in sorted(candidates, key=lambda value: abs(value - expected)):
            water = support_z >= WATERBED_SUPPORT_Z
            if water and not allow_water:
                continue
            head_clear = all(not self._solid(x, y, support_z - offset) for offset in (1, 2))
            if self._solid(x, y, support_z) and head_clear:
                return SurfaceSample(x, y, support_z, water, head_clear)
        return None
```

`classify()` scans only the bounded vertical window around
`round(player_z + PLAYER_STANDING_OFFSET)`, rejects invalid coordinates, requires
two clear body cells, and rejects support `z >= 239` unless `allow_water=True`.

- [ ] **Step 4: Publish `wade`, delegate worker standing queries, and omit waterbed quads from Recast tiles**

```python
@dataclass(frozen=True, slots=True)
class PlayerSnapshot:
    wade: bool = False
```

The director copies `player.wade`; `_rebuild_native_tile()` skips `z >= 239`.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/test_bot_voxel_navigation.py tests/test_bot_architecture.py tests/test_recast_navigation.py -q`

Expected: PASS.

Commit: `git commit -am "fix: classify dry bot navigation surfaces"`

---

### Task 2: Live Edge Guard and Jump Event Pulses

**Files:**
- Modify: `server/bot_ai/director.py`
- Modify: `server/bot_ai/messages.py`
- Test: `tests/test_bot_architecture.py`
- Test: `tests/test_bot_voxel_navigation.py`

**Interfaces:**
- Produces: `BotDirector._movement_is_live(runtime, movement)` and `_RuntimeBot.jump_release_loop`.
- Consumes: `MovementIntent`, `WorldManager.get_height()`, `WorldManager.is_water_column()`.

- [ ] **Step 1: Write failing motor tests**

```python
def test_dry_motor_rejects_open_water_ahead():
    runtime, director = _motor_fixture(surface_z=239, wade=False)
    assert director._movement_is_live(runtime, _walk_east()) is False


def test_jump_affordance_pulses_then_releases_before_retrigger():
    runtime, director = _jump_fixture()
    director._apply_motor(runtime, now=1.0, dt=1 / 60)
    assert runtime.player.last_input.jump is True
    director.server.loop_count += director.JUMP_PULSE_TICKS + 1
    director._apply_motor(runtime, now=1.1, dt=1 / 60)
    assert runtime.player.last_input.jump is False
```

- [ ] **Step 2: Run both tests and confirm they fail on water acceptance and held jump**

Run: `python -m pytest tests/test_bot_architecture.py tests/test_bot_voxel_navigation.py -q`

Expected: FAIL at the asserted safety and release behavior.

- [ ] **Step 3: Implement authoritative support/drop probes**

`_movement_is_live()` validates body clearance plus center/left/right support
samples. Dry movement rejects water. `WALK` rejects drops greater than one
voxel; `DROP` and `JUMP` use their own bounded limits. The probe cache key
includes topology version, wade state, and affordance.

- [ ] **Step 4: Convert leased jump Booleans into frame-keyed pulses**

```python
if jump_requested and runtime.jump_event_frame != intent.frame_id:
    runtime.jump_event_frame = intent.frame_id
    runtime.jump_release_loop = loop + JUMP_PULSE_TICKS
jump = loop <= runtime.jump_release_loop
```

The motor forces at least one released frame before accepting another event.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/test_bot_architecture.py tests/test_bot_voxel_navigation.py tests/test_movement_jitter.py -q`

Expected: PASS.

Commit: `git commit -am "fix: guard bot edges and pulse jumps"`

---

### Task 3: Progress Tracking and Water Recovery

**Files:**
- Modify: `server/bot_ai/worker.py`
- Modify: `server/bot_ai/voxel_navigation.py`
- Test: `tests/test_bot_voxel_navigation.py`
- Test: `tests/test_bot_architecture.py`

**Interfaces:**
- Produces: `ProgressTracker.observe()`, `VoxelActionPlanner.water_exit()`, and worker survival-intent selection.
- Consumes: `PlayerSnapshot.wade`, topology version, current route direction.

- [ ] **Step 1: Write failing progress and water-exit tests**

```python
def test_vertical_bobbing_does_not_count_as_route_progress():
    tracker = ProgressTracker((1.0, 0.0, 0.0), (20.0, 5.0, 20.0), 0.0)
    assert tracker.observe((5.0, 5.0, 18.8), 0.4) is False


def test_water_exit_selects_nearest_dry_body_clear_surface():
    planner = VoxelActionPlanner(_water_with_shore_fixture())
    step = planner.water_exit((8.5, 8.5, 236.75))
    assert step is not None
    assert step.goal == (11.5, 8.5, 234.75)
```

- [ ] **Step 2: Run tests and confirm current 3D-distance behavior fails**

Run: `python -m pytest tests/test_bot_voxel_navigation.py -q`

Expected: FAIL because vertical motion reports progress and no recovery planner exists.

- [ ] **Step 3: Implement projected horizontal progress**

Track reduction in waypoint distance and XY displacement projected onto the
requested route. Reset stuck state only after meaningful positive progress;
rotation, knockback perpendicular to the route, and Z-only movement do not count.

- [ ] **Step 4: Implement bounded water recovery selection**

When `observer.wade` is true, recovery takes priority over combat and objectives.
Search a bounded ring for dry, body-clear surfaces, path through recovery-only
water samples, and return movement plus jump events. A blocked shore returns a
breach/build escalation result rather than ordinary direct steering.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/test_bot_voxel_navigation.py tests/test_bot_architecture.py tests/test_bot_policies.py -q`

Expected: PASS.

Commit: `git commit -am "feat: recover bots from water and real stalls"`

---

### Task 4: Height-Aware Zombie Voxel Actions

**Files:**
- Modify: `server/bot_ai/voxel_navigation.py`
- Modify: `server/bot_ai/worker.py`
- Test: `tests/test_bot_voxel_navigation.py`
- Test: `tests/test_bot_architecture.py`

**Interfaces:**
- Produces: immutable `VoxelActionStep` and `VoxelActionPlanner.plan_local()`.
- Consumes: class affordances, observed target position/velocity, live topology version.

- [ ] **Step 1: Write failing elevated-target and topology tests**

```python
def test_zombie_below_survivor_chooses_climb_instead_of_direct_walk():
    result = planner.plan_local(zombie, survivor_on_three_block_platform)
    assert result.first.affordance in {MovementAffordance.JUMP, MovementAffordance.BUILD_STEP, MovementAffordance.BREACH}


def test_changed_immediate_cell_invalidates_local_step():
    result = planner.plan_local(zombie, survivor)
    changed = VoxelChange(*result.first.cells[0], solid=True)
    world.apply(WorldDelta(map_epoch=1, topology_version=2, changed_cells=(changed,)))
    assert result.first.is_valid(world) is False
```

- [ ] **Step 2: Run tests and confirm close-range direct steering fails**

Run: `python -m pytest tests/test_bot_voxel_navigation.py tests/test_bot_architecture.py -q`

Expected: FAIL because close Zombies always select `WALK` regardless of height.

- [ ] **Step 3: Implement bounded local action search**

Search standable nodes within 24 blocks and a fixed expansion cap. Edges encode
`WALK`, `CROUCH`, validated `JUMP`, safe `DROP`, `BREACH`, `BUILD_STEP`, and
`PLACE_PREFAB`, each with duration/resource/exposure costs. Results include
goal reachability, topology version, immediate cells, waypoint height, and first
affordance.

- [ ] **Step 4: Remove unconditional close Zombie steering**

Direct contact steering remains available only when vertical separation is
within one walkable surface step and the local planner confirms compatible
support. Otherwise use the local action result and escalate partial routes.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/test_bot_voxel_navigation.py tests/test_bot_architecture.py tests/test_zombie.py -q`

Expected: PASS.

Commit: `git commit -am "feat: plan zombie climbing and breaches"`

---

### Task 5: Replicated BlockLine Cover and Body-Safe Construction

**Files:**
- Modify: `server/bot_ai/messages.py`
- Modify: `server/bot_ai/gateway.py`
- Modify: `server/bot_ai/worker.py`
- Modify: `server/construction.py`
- Test: `tests/test_construction_actions.py`
- Test: `tests/test_bot_architecture.py`

**Interfaces:**
- Produces: `BotActionKind.BUILD_LINE`, `BotAction.end_position`, and `BotActionGateway.build_line()`.
- Consumes: `CombatSystem.handle_block_line()` and its canonical cube-line generator.

- [ ] **Step 1: Write failing gateway and safety tests**

```python
def test_bot_block_line_uses_shared_combat_service():
    accepted = gateway.execute(bot, BotAction(BotActionKind.BUILD_LINE, tool_id=5, position=(10, 10, 20), end_position=(14, 10, 20)))
    assert accepted is True
    assert combat.block_line_endpoints == (10, 10, 20, 14, 10, 20)


def test_construction_rejects_player_body_overlap():
    token, reason = construction.reserve_construction(bot.id, bot.team, {(int(bot.x), int(bot.y), int(bot.z))})
    assert token is None
    assert reason == "player body overlap"
```

- [ ] **Step 2: Run tests and confirm BUILD_LINE is unavailable**

Run: `python -m pytest tests/test_construction_actions.py tests/test_bot_architecture.py -q`

Expected: FAIL because the action and overlap rule do not exist.

- [ ] **Step 3: Add the gateway action without direct world mutation**

Generate the exact cell list using the combat service, reserve those cells, fill
a stock `BlockLine` packet with the current loop stamp, and call
`handle_block_line()`. Release the reservation on rejection.

- [ ] **Step 4: Plan threat-oriented cover segments**

Select a supported 3-5-cell line perpendicular to the threat vector, exclude
player bodies/escape paths/protected zones, build the lower row first, then
permit an upper row on a later decision. After acceptance, movement routes to
the protected side of the segment.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest tests/test_construction_actions.py tests/test_bot_architecture.py tests/test_join_mutation_catchup.py -q`

Expected: PASS.

Commit: `git commit -am "feat: build replicated bot block-line cover"`

---

### Task 6: Zombie Variant Gate and End-to-End Verification

**Files:**
- Modify: `server/config.py`
- Modify: `server/bot_ai/director.py`
- Modify: `config.example.toml`
- Modify: `docs/ENGINEERING_NOTES.md`
- Modify: `HANDOFF.md`
- Test: `tests/test_bot_architecture.py`
- Test: `tests/test_bot_runtime_smoke.py`

**Interfaces:**
- Produces: `[bots].experimental_zombie_variants = false`.
- Consumes: normalized base Zombie class selection.

- [ ] **Step 1: Write a failing default-selection test**

```python
def test_bot_zombie_defaults_to_base_class():
    selection = director._choose_zombie_selection(profile)
    assert selection.class_id == int(C.CLASS_ZOMBIE)
```

- [ ] **Step 2: Run the test and confirm hidden variants are still selected**

Run: `python -m pytest tests/test_bot_architecture.py::test_bot_zombie_defaults_to_base_class -q`

Expected: FAIL when random selection returns Fast or Jump Zombie.

- [ ] **Step 3: Gate hidden variants and document validation evidence**

Default to base Zombie. Include hidden variants only when the experimental flag
is true. Record that their recovered constants remain unchanged pending original
YouTube and retail-client validation.

- [ ] **Step 4: Run focused, full, and inline-worker smoke verification**

Run: `python -m pytest tests/test_bot_voxel_navigation.py tests/test_bot_architecture.py tests/test_construction_actions.py tests/test_zombie.py -q`

Run: `python -m pytest -q`

Run: `python scripts/run_bot_smoke.py --inline-worker --bots 12 --duration 30`

Expected: all tests pass; smoke exits zero with bounded queues and no worker or simulation exception.

- [ ] **Step 5: Commit the integration**

Commit: `git commit -am "feat: stabilize dynamic voxel bot navigation"`
