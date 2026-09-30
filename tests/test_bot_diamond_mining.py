"""Bots that mine in Diamond Mine must actually remove blocks.

Measured 2026-09-29 (scripts/bot_runtime_smoke.py --mode dia, six bots, two
minutes): 669 swings, no block removed, no diamond. The mining target moved
on every half second, so the aim never arrived, and a swing goes where the
body looks. Afterwards, with nothing left in reach, the bot stood still.
"""

from __future__ import annotations

from dataclasses import replace
import math

import pytest
import shared.constants as C

from server.bot_ai.messages import BotActionKind, ObjectiveSnapshot
from server.bot_ai.simple_worker import SimpleBotBrain
from server.dig_profiles import PRIMARY_DIG_PROFILES, melee_dig_positions
from server.game_constants import TEAM1
from tests.test_simple_bot_tactics import _frame, _player, _TacticalWorld

DROPOFF = ObjectiveSnapshot(
    "dia_dropoff", int(C.TEAM_NEUTRAL), (80.0, 90.0, 20.0), state=1
)
# Standing at z 20 the ground under the feet starts at z 22.


class _Ground(_TacticalWorld):
    """Flat ground below z 22 with the cells dug out so far removed."""

    def __init__(self):
        super().__init__()
        self.dug: set[tuple[int, int, int]] = set()

    def solid(self, x, y, z):
        return int(z) >= 22 and (int(x), int(y), int(z)) not in self.dug


def _bot(position=(40.5, 40.5, 19.75), player_id=1):
    return _player(player_id, TEAM1, position, is_bot=True)


def _decide(brain, observer, at, *objectives, frame_id=1):
    frame = replace(
        _frame(observer, objectives=(DROPOFF, *objectives), created_at=at),
        mode_id="dia", frame_id=frame_id,
    )
    return brain.decide(frame)


def _cell(intent):
    return tuple(int(math.floor(value)) for value in intent.look.target)


def test_the_same_block_is_worked_until_it_breaks():
    world = _Ground()
    brain = SimpleBotBrain(world)
    observer = _bot()

    first = _decide(brain, observer, 100.0)
    assert first.debug_role == "diamond_mine_blocks"
    assert first.action.kind is BotActionKind.MELEE
    target = _cell(first)
    assert world.solid(*target)
    assert (target[0], target[1]) != (40, 40)   # never the column it stands on

    # Between swings the aim stays on that block and nothing is swung.
    waiting = _decide(brain, observer, 100.2, frame_id=2)
    assert waiting.debug_role == "diamond_mine_blocks"
    assert waiting.action.kind is BotActionKind.NONE
    assert _cell(waiting) == target
    assert waiting.movement.crouch is True

    # Half a second later the old code had moved on to another block.
    again = _decide(brain, observer, 101.0, frame_id=3)
    assert again.action.kind is BotActionKind.MELEE
    assert _cell(again) == target
    assert again.action.position == first.action.position


def test_a_broken_block_is_followed_by_the_next_one():
    world = _Ground()
    brain = SimpleBotBrain(world)
    observer = _bot()
    first = _cell(_decide(brain, observer, 100.0))

    world.dug.update((first[0], first[1], first[2] + dz) for dz in (-1, 0, 1))
    following = _decide(brain, observer, 101.0, frame_id=2)

    assert following.debug_role == "diamond_mine_blocks"
    assert _cell(following) != first
    assert world.solid(*_cell(following))


def test_every_swing_target_is_inside_the_terrain_swing_range():
    world = _Ground()
    brain = SimpleBotBrain(world)
    observer = _bot()
    reach = float(C.MELEE_WORLD_RANGE)
    seen = set()
    at = 100.0
    for frame_id in range(1, 40):
        intent = _decide(brain, observer, at, frame_id=frame_id)
        if intent is None or intent.debug_role != "diamond_mine_blocks":
            break
        cell = _cell(intent)
        hit = brain._first_solid_hit(observer.eye, intent.look.target, reach)
        assert hit is not None and hit[0] == cell
        assert hit[1] < reach
        seen.add(cell)
        world.dug.update((cell[0], cell[1], cell[2] + dz) for dz in (-1, 0, 1))
        at += 1.0
    assert len(seen) >= 8


def test_a_block_that_never_breaks_is_left_after_a_while():
    world = _Ground()
    brain = SimpleBotBrain(world)
    observer = _bot()
    stubborn = _cell(_decide(brain, observer, 100.0))

    later = _decide(brain, observer, 100.0 + brain._BLOCK_WORK_PATIENCE + 1.0,
                    frame_id=2)
    following = _decide(brain, observer, 100.0 + brain._BLOCK_WORK_PATIENCE + 2.0,
                        frame_id=3)

    cells = {_cell(intent) for intent in (later, following)
             if intent.debug_role == "diamond_mine_blocks"}
    assert cells and stubborn not in cells


def test_with_nothing_in_reach_the_bot_walks_on_instead_of_standing():
    world = _Ground()
    world.solid = lambda x, y, z: int(z) >= 30     # the ground is far below
    brain = SimpleBotBrain(world)
    observer = _bot()

    intent = _decide(brain, observer, 100.0)

    state = brain._states[(observer.player_id, observer.generation)]
    assert intent.action.kind is not BotActionKind.MELEE
    assert state.mine_site is not None
    walked = math.dist(state.mine_site[:2], observer.position[:2])
    assert abs(walked - brain._MINE_SITE_STEP) < 1e-6
    # Toward the open drop-off, give or take the fan.
    toward = math.atan2(DROPOFF.position[1] - 40.5, DROPOFF.position[0] - 40.5)
    heading = math.atan2(state.mine_site[1] - 40.5, state.mine_site[0] - 40.5)
    assert abs(heading - toward) <= 0.91
    assert state.goal is not None
    assert state.goal.position == state.mine_site
    # The site is kept while the bot is on its way.
    site = state.mine_site
    _decide(brain, observer, 101.0, frame_id=2)
    assert state.mine_site == site


def test_the_spawn_is_not_dug_up():
    world = _Ground()
    brain = SimpleBotBrain(world)
    observer = _bot()
    spawn = ObjectiveSnapshot("team_anchor", TEAM1, (44.5, 44.5, 19.75))

    intent = _decide(brain, observer, 100.0, spawn)

    state = brain._states[(observer.player_id, observer.generation)]
    assert intent.action.kind is not BotActionKind.MELEE
    assert state.mine_site is not None

    far = ObjectiveSnapshot("team_anchor", TEAM1, (70.5, 40.5, 19.75))
    mining = _decide(brain, observer, 101.0, far, frame_id=2)
    assert mining.action.kind is BotActionKind.MELEE


def test_the_water_line_is_not_mined():
    class _Shore(_Ground):
        def solid(self, x, y, z):
            return int(z) >= 238

    brain = SimpleBotBrain(_Shore())
    observer = _bot((40.5, 40.5, 235.75))

    intent = _decide(brain, observer, 100.0)

    assert intent.action.kind is not BotActionKind.MELEE


@pytest.mark.parametrize("tool", (C.SPADE_TOOL, C.SUPERSPADE_TOOL, C.MACHETE_TOOL))
def test_mining_footprint_preserves_floor_above_water(tool):
    world = _Ground()
    world.solid = lambda x, y, z: int(z) >= 237
    brain = SimpleBotBrain(world)
    observer = replace(_bot((40.5, 40.5, 234.75)), loadout=(int(tool),))

    target = brain._mine_cell(observer, observer.eye, {})

    assert target is None or all(
        cell[2] < int(C.Z_ABOVE_WATERPLANE)
        for cell in melee_dig_positions(target, PRIMARY_DIG_PROFILES[int(tool)].pattern)
    )


def test_area_mining_footprint_does_not_remove_own_support():
    world = _Ground()
    brain = SimpleBotBrain(world)
    tool = int(C.SUPERSPADE_TOOL)
    observer = replace(_bot(), loadout=(tool,))

    target = brain._mine_cell(observer, observer.eye, {})

    # Every floor cell in reach overlaps the occupied support column. Walk
    # toward another mining site instead of collapsing the bot's own footing.
    assert target is None
    floor = world.solid
    world.solid = lambda x, y, z: floor(x, y, z) or (int(x) == 42 and 18 <= int(z) < 22)
    target = brain._mine_cell(observer, observer.eye, {})
    assert target is not None  # The neighboring wall is safe and reachable.
    assert all(
        cell[:2] != (40, 40)
        for cell in melee_dig_positions(target, PRIMARY_DIG_PROFILES[tool].pattern)
    )


def test_knockback_onto_cached_mining_target_reselects_safe_ground():
    brain = SimpleBotBrain(_Ground())
    first = _cell(_decide(brain, _bot(), 100.0))
    moved = _bot((first[0] + 0.5, first[1] + 0.5, 19.75))

    following = _decide(brain, moved, 101.0, frame_id=2)

    assert following.action.kind is BotActionKind.MELEE
    assert _cell(following)[:2] != first[:2]


def test_bots_of_one_team_start_on_different_blocks():
    world = _Ground()
    cells = set()
    for player_id in (1, 2, 3, 4):
        brain = SimpleBotBrain(world)
        cells.add(_cell(_decide(brain, _bot(player_id=player_id), 100.0)))
    assert len(cells) >= 3
