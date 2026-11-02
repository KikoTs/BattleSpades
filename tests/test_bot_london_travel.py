"""The infected cross London without standing on the Thames embankment.

London's river runs thirteen blocks below both banks: nobody enters or leaves
it where they stand, and the only way to a survivor on the far side is a long
walk round by the bridge. The bounded planner's best guess toward such a goal
is the bank cell nearest it (the same cell for every zombie), the map-wide
corridor that knows the bridge was judged "absurdly long" and dropped on every
swap of prey, and a follower halted dead behind any teammate a block ahead.
These fixtures reproduce each of those on a synthetic Thames.
"""

from __future__ import annotations

from dataclasses import replace
import math

from server.bot_ai.messages import BotAction, BotIntent, MovementAffordance, MovementIntent
from server.bot_ai.planning_budget import PlanningBudget
from server.bot_ai.simple_navigation import (
    RouteStep,
    SimpleVoxelWorld,
    _FinishedCorridor,
)
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState, _Goal
from server.bot_ai.traffic import Traffic
from server.game_constants import TEAM1
from tests.test_bot_water_crossing import _with_atlas
from tests.test_simple_bot_navigation import _world
from tests.test_simple_bot_tactics import _frame, _player

LAND = 226                 # embankment support: the body stands at 223.75
BODY_Z = LAND - 2.25
WATER_Z = 239
CHANNEL = range(40, 61)    # the river, x 40..60
BRIDGE_ROWS = range(5, 8)  # the only dry way across, y 5..7


def _thames(*, bridge: bool = True, river: bool = True,
            pillar: tuple[int, int] | None = None) -> SimpleVoxelWorld:
    """A plateau x 5..100, y 5..60 cut by a channel whose banks are 13 blocks high."""

    solids: set[tuple[int, int, int]] = set()
    for x in range(5, 101):
        for y in range(5, 61):
            if river and x in CHANNEL and not (bridge and y in BRIDGE_ROWS):
                solids.add((x, y, WATER_Z))
            else:
                solids.update((x, y, z) for z in range(LAND, WATER_Z + 1))
    if pillar is not None:   # an 8-block pillar a survivor stands on
        solids.update((pillar[0], pillar[1], z) for z in range(LAND - 8, LAND))
    world = _with_atlas(_world(solids))
    world.topology_version = 1
    return world


def _zombie(player_id: int, position, **fields):
    return _player(player_id, TEAM1, position, is_bot=True, **fields)


def _brain(world, observer, goal_position, role="zombie_hunt_survivor"):
    brain = SimpleBotBrain(world)
    brain.reset_for_map(1)
    state = _BotState(1, 1, observer.life_id)
    state.goal = _Goal(("objective", role), goal_position, role, 1.0, True)
    state.goal_since = state.goal_progress_at = 100.0   # a goal just taken, as _set_goal leaves it
    brain._states[(observer.player_id, observer.generation)] = state
    return brain, state


BANK = (38.5, 30.5, BODY_Z)          # the last dry column before the river
ACROSS = (62.5, 30.5, BODY_Z)        # the prey, 24 blocks east across it


def test_water_between_reports_the_river_and_the_height_of_its_banks():
    brain = SimpleBotBrain(_thames())
    barred = brain._water_between(BANK, ACROSS)
    assert barred is not None
    first, near, far = barred
    assert first == 2.0 and near == WATER_Z - LAND == 13 and far == 13
    # Along the bank, or on the far side, the line stays dry.
    assert brain._water_between(BANK, (38.5, 6.5, BODY_Z)) is None
    assert brain._water_between((70.5, 30.5, BODY_Z), ACROSS) is None
    # Water that runs on past the goal has no far bank to report.
    assert brain._water_between(BANK, (50.5, 30.5, BODY_Z))[2] is None
    assert brain._bank_dead_end(BANK, ACROSS)
    assert not brain._bank_dead_end(BANK, (38.5, 50.5, BODY_Z))      # dry way, no water
    assert not brain._bank_dead_end(BANK, (41.5, 30.5, BODY_Z))      # too close to matter
    # Another land mass altogether is not a dead end the corridor can solve.
    assert not SimpleBotBrain(_thames(bridge=False))._bank_dead_end(BANK, ACROSS)


def test_a_bank_with_the_prey_across_asks_for_the_corridor_at_once_and_follows_it():
    world = _thames()
    observer = _zombie(1, BANK)
    brain, state = _brain(world, observer, ACROSS)
    frame = _frame(observer)
    brain._navigation_intent(frame, observer, state, state.goal, 100.0)
    # No shoreline probe (twelve seconds each) and no escape: the atlas knows.
    assert state.guidance_wanted
    assert state.dry_detour_goal is None and state.dry_route_failures == 0
    # A best guess toward the bank need not be entered cell-exactly.
    assert state.route and not state.route_exact_end
    for tick in range(1, 40):
        intent = brain._navigation_intent(frame, observer, state, state.goal, 100.0 + tick * 0.125)
        if state.corridor:
            break
        # The body follows: half a block along the asked direction per decision.
        direction = intent.movement.direction
        if math.hypot(direction[0], direction[1]) > 0.1:
            observer = replace(observer, position=(observer.position[0] + direction[0] * 0.5,
                                                   observer.position[1] + direction[1] * 0.5,
                                                   observer.position[2]))
            frame = _frame(observer)
    assert state.corridor, "the map-wide search should have found the bridge"
    assert not state.guidance_wanted
    assert min(point[1] for point in state.corridor) <= BRIDGE_ROWS.stop
    assert state.walkless_goal is None and not state.corridor_futile
    # The long way round is what a player walks here, not an absurd corridor.
    assert not brain._corridor_is_absurd(state.corridor, observer.position, ACROSS)


def test_a_long_way_round_high_banks_is_not_absurd_but_the_same_walk_on_dry_land_is():
    corridor = tuple((38.5 + (index % 2) * 10.0, 6.5, BODY_Z) for index in range(31))
    length = sum(math.dist(a, b) for a, b in zip(corridor, corridor[1:]))
    assert length == 300.0
    goal = (78.5, 30.5, BODY_Z)  # 40 blocks away: the old limit is 280
    assert not SimpleBotBrain(_thames())._corridor_is_absurd(corridor, BANK, goal)
    assert SimpleBotBrain(_thames(river=False))._corridor_is_absurd(corridor, BANK, goal)


def _corridor_state(route_steps=2):
    state = _BotState(1, 1, 1)
    state.goal = _Goal(("enemy", 9, 1), ACROSS, "combat_pursuit", 1.5, True)
    state.corridor = tuple((38.5, float(y) + 0.5, BODY_Z) for y in range(30, 5, -1)) + tuple(
        (float(x) + 0.5, 6.5, BODY_Z) for x in range(39, 99)) + ((98.5, 30.5, BODY_Z),)
    state.corridor_index = 2
    state.route = tuple(RouteStep((38.5, 30.5 - step, BODY_Z), MovementAffordance.WALK)
                        for step in range(1, route_steps + 1))
    state.route_index = 0
    return state


def test_a_new_prey_beside_the_old_one_keeps_the_corridor_and_the_route():
    position = (38.5, 31.5, BODY_Z)
    # The horde's order names the survivor the bot can already see: one errand.
    state = _corridor_state()
    SimpleBotBrain._set_goal(state, _Goal(("objective", "zombie_hunt_survivor"),
                                          (65.5, 33.5, BODY_Z), "zombie_hunt_survivor", 1.0, True),
                             position, 100.0)
    assert state.corridor and state.route and state.route_index == 0
    # Another survivor twenty blocks from the first shares the bridge.
    state = _corridor_state()
    SimpleBotBrain._set_goal(state, _Goal(("objective", "zombie_hunt_flank"),
                                          (62.5, 50.5, BODY_Z), "zombie_hunt_flank", 1.0, True),
                             position, 100.0)
    assert state.corridor and state.route
    # So does one far along the corridor's own direction (across the river).
    state = _corridor_state()
    SimpleBotBrain._set_goal(state, _Goal(("objective", "zombie_hunt_flank"),
                                          (98.5, 40.5, BODY_Z), "zombie_hunt_flank", 1.0, True),
                             position, 100.0)
    assert state.corridor
    # Prey behind the bot, on this side, does not: the corridor leads away from it.
    state = _corridor_state()
    SimpleBotBrain._set_goal(state, _Goal(("objective", "zombie_hunt_flank"),
                                          (8.5, 55.5, BODY_Z), "zombie_hunt_flank", 1.0, True),
                             position, 100.0)
    assert not state.corridor and not state.route


def test_a_sighting_across_the_river_does_not_replace_the_hunt_in_hand_at_two_hertz():
    world = _thames()
    observer = _zombie(1, BANK)
    brain, state = _brain(world, observer, ACROSS)
    state.corridor = _corridor_state().corridor
    state.goal_since = 100.0
    frame = _frame(observer)
    seen = _Goal(("enemy", 9, 1), (62.5, 58.5, BODY_Z), "combat_pursuit", 1.5, True)
    brain._navigation_intent(frame, observer, state, seen, 101.0)
    assert state.goal.key == ("objective", "zombie_hunt_survivor")
    # Markedly nearer prey still wins at once.
    near = _Goal(("enemy", 9, 1), (30.5, 30.5, BODY_Z), "combat_pursuit", 1.5, True)
    brain._navigation_intent(frame, observer, state, near, 101.5)
    assert state.goal.key == ("enemy", 9, 1)


def test_a_failed_edge_away_from_the_corridor_keeps_it_and_one_on_it_rebuilds_it():
    state = _corridor_state()
    beside_the_bank = ((37, 40, LAND), (38, 40, LAND))
    SimpleBotBrain._remember_blocked_edge(state, beside_the_bank, 100.0)
    assert state.corridor and beside_the_bank in state.blocked_edges
    on_the_way = ((38, 27, LAND), (38, 26, LAND))
    SimpleBotBrain._remember_blocked_edge(state, on_the_way, 100.0)
    assert not state.corridor and state.corridor_retry_at == 101.0


def _travel(actor, frame, now=100.0):
    return BotIntent(actor.player_id, actor.generation, frame.frame_id, frame.map_epoch,
                     frame.mode_epoch, 0, now, now + 0.4,
                     MovementIntent((0.0, -1.0, 0.0), sprint=True, travel_source=actor.position,
                                    travel_waypoint=(38.5, 10.5, BODY_Z)),
                     tool_id=actor.weapon_tool, debug_role="zombie_hunt_survivor",
                     life_id=actor.life_id)


def test_a_follower_keeps_running_behind_a_leader_who_is_running_on():
    world = _thames()
    actor = replace(_zombie(2, (38.5, 30.5, BODY_Z)), orientation=(0.0, -1.0, 0.0))
    running = replace(_zombie(1, (38.5, 29.6, BODY_Z)), orientation=(0.0, -1.0, 0.0),
                      velocity=(0.0, -0.35, 0.0))          # about 11 blocks a second
    frame = _frame(actor, running)
    result = Traffic(world).adjust(frame, _travel(actor, frame))
    assert result.movement.direction == (0.0, -1.0, 0.0) and result.movement.sprint
    # Behind a leader who is only walking, the follower drops to a walk too.
    walking = replace(running, velocity=(0.0, -0.13, 0.0))
    result = Traffic(world).adjust(_frame(actor, walking), _travel(actor, frame))
    assert result.movement.direction == (0.0, -1.0, 0.0) and not result.movement.sprint
    # A leader standing still is still queued behind, as before.
    standing = replace(running, velocity=(0.0, 0.0, 0.0))
    result = Traffic(world).adjust(_frame(actor, standing), _travel(actor, frame))
    assert result.movement.direction == (0.0, 0.0, 0.0) and result.social_cue == "yield_queue"


def test_separation_never_bends_a_step_toward_the_river():
    world = _thames()
    observer = _zombie(1, (39.3, 30.5, BODY_Z))             # on the last dry column
    crowding = _zombie(2, (38.2, 30.5, BODY_Z))             # a shoulder to the west
    frame = _frame(observer, crowding)
    brain = SimpleBotBrain(world)
    north = MovementIntent((0.0, -1.0, 0.0), sprint=True, affordance=MovementAffordance.WALK)
    straight = brain._crowd_adjusted_movement(frame, north, action=BotAction())
    assert straight.direction == (0.0, -1.0, 0.0)
    # Away from the water the same crowd still bends the step.
    inland = replace(observer, position=(30.3, 30.5, BODY_Z))
    beside = replace(crowding, position=(29.2, 30.5, BODY_Z))
    bent = brain._crowd_adjusted_movement(_frame(inland, beside), north, action=BotAction())
    assert bent.direction[0] > 0.3 and bent.direction[1] < -0.5


def test_the_horde_shares_one_corridor_search():
    world = _thames()
    world.planning_budget = PlanningBudget(24.0, decision_hz=8.0)
    world.begin_planning((1, 1), 100.0)
    first = world.begin_corridor(BANK, ACROSS, blocked_edges=frozenset())
    assert not isinstance(first, _FinishedCorridor)
    while not first._search.done:
        first._search.advance()
    first._remember()
    assert first.path
    # A teammate two blocks along the same bank gets the finished answer.
    world.begin_planning((2, 1), 100.1)
    second = world.begin_corridor((36.5, 32.5, BODY_Z), ACROSS, blocked_edges=frozenset())
    assert isinstance(second, _FinishedCorridor) and second.path == first.path
    # A different exclusion set is a different question.
    other = world.begin_corridor(BANK, ACROSS, blocked_edges=frozenset({((38, 29, LAND), (38, 28, LAND))}))
    assert not isinstance(other, _FinishedCorridor)
    # Terrain edits forget every cached corridor.
    world._corridor_cache.clear()
    assert not isinstance(world.begin_corridor(BANK, ACROSS, blocked_edges=frozenset()),
                          _FinishedCorridor)


def test_a_corridor_to_a_survivor_on_a_pillar_leads_to_the_ground_beside_it():
    world = _thames(pillar=(62, 30))
    perch = (62.5, 30.5, LAND - 8 - 2.25)
    search = world.begin_corridor(BANK, perch, blocked_edges=frozenset())
    assert search is not None
    while not search.done:
        search.advance()
    # The search used to be aimed at the pillar top, a boxed-in perch, and
    # finish at once with nothing; the brain then held "no walk there" for
    # two minutes. It now ends on the embankment beside the pillar.
    assert search.path, "the walk round by the bridge should be found"
    end = search.path[-1]
    assert math.hypot(end[0] - 62.5, end[1] - 30.5) <= 1.5 and abs(end[2] - BODY_Z) < 0.1
    assert min(point[1] for point in search.path) <= BRIDGE_ROWS.stop
