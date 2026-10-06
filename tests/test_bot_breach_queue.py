"""A teammate's excavation is waited for only by the bots it is opening the way for."""

from dataclasses import replace

import shared.constants as C
from server.bot_ai.messages import BotActionKind, ObjectiveSnapshot
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState, _Goal
from server.game_constants import TEAM1
from tests.test_simple_bot_navigation import _sealed_wall_solids, _world
from tests.test_simple_bot_tactics import _frame, _player

HEAD = 17.75
# The wall stands at x = 13; its cell on the bots' row at leg height.
FACE = (13.5, 10.5, 18.5)
ACROSS = (17.5, 10.5, HEAD)
SPADE_KIT = (int(C.SMG_TOOL), int(C.SPADE_TOOL))


def _bot(player_id, position, **kwargs):
    return _player(player_id, TEAM1, position, is_bot=True, loadout=SPADE_KIT,
                   weapon_tool=int(C.SMG_TOOL), **kwargs)


def _digging(digger, at, target=FACE):
    return replace(digger, last_action_kind=BotActionKind.MELEE.value,
                   last_action_accepted=True, last_action_position=target,
                   last_action_at=at)


def _goal(position, role="wall_test"):
    return _Goal(("wall-test",), position, role, 0.5, True)


def _queued(intent) -> bool:
    return intent.debug_role.endswith(":breach_assist_queue")


def test_a_bot_on_its_way_through_the_wall_waits_behind_the_digger():
    brain = SimpleBotBrain(_world(_sealed_wall_solids()))
    follower = _bot(3, (11.5, 10.5, HEAD))
    digger = _digging(_bot(1, (12.5, 10.5, HEAD)), 100.0)
    state = _BotState(1, 1, follower.life_id)
    intent = brain._navigation_intent(
        _frame(follower, digger, created_at=100.1), follower, state, _goal(ACROSS), 100.1)
    assert _queued(intent)


def test_a_bot_bound_elsewhere_walks_past_a_digging_teammate():
    brain = SimpleBotBrain(_world(_sealed_wall_solids()))
    passer = _bot(3, (11.5, 10.5, HEAD))
    digger = _digging(_bot(1, (12.5, 9.5, HEAD)), 100.0, target=(13.5, 9.5, 18.5))
    state = _BotState(1, 1, passer.life_id)
    # Along the wall, not through it: the hole two blocks away is not its way.
    along = _goal((11.5, 12.5, HEAD))
    for tick in range(4):
        now = 100.1 + tick * 0.125
        intent = brain._navigation_intent(
            _frame(passer, replace(digger, last_action_at=now), created_at=now),
            passer, state, along, now)
        assert not _queued(intent)
    assert state.breach_wait_digger is None


def test_a_zombie_with_voxels_of_its_own_to_claw_goes_to_them():
    brain = SimpleBotBrain(_world(_sealed_wall_solids()))
    hand = int(C.ZOMBIEHAND_TOOL)
    zombie = _player(3, TEAM1, (11.5, 10.5, HEAD), class_id=int(C.CLASS_ZOMBIE), is_bot=True,
                     loadout=(hand,), weapon_tool=hand)
    digger = _digging(replace(zombie, player_id=1, position=(12.5, 10.5, HEAD)), 100.0)
    order = ObjectiveSnapshot("zombie_order", TEAM1, (12.5, 11.5, HEAD), carrier_id=3,
                              state=2, cells=((13, 11, 18), (13, 11, 17)), attacker=9)
    frame = replace(_frame(zombie, digger, created_at=100.1), mode_id="zom", objectives=(order,))
    state = _BotState(1, 1, zombie.life_id)
    intent = brain._navigation_intent(frame, zombie, state, _goal(ACROSS, "zombie_siege_dig"), 100.1)
    assert not _queued(intent)
    # Without an order of its own the same zombie is a follower like any other.
    plain = replace(frame, objectives=())
    intent = brain._navigation_intent(plain, zombie, _BotState(1, 1, zombie.life_id),
                                      _goal(ACROSS, "zombie_hunt_survivor"), 100.1)
    assert _queued(intent)


def test_a_follower_gives_the_digger_a_cells_worth_of_swings_then_stops_waiting():
    brain = SimpleBotBrain(_world(_sealed_wall_solids()))
    follower = _bot(3, (11.5, 10.5, HEAD))
    digger = _bot(1, (12.5, 10.5, HEAD))
    state = _BotState(1, 1, follower.life_id)

    def decide(now):
        frame = _frame(follower, _digging(digger, now - 0.05), created_at=now)
        return brain._navigation_intent(frame, follower, state, _goal(ACROSS), now)

    assert _queued(decide(100.0))
    assert _queued(decide(101.5))
    assert not state.blocked_edges
    # Two seconds on, the wall is still not open: enough.
    released = decide(102.1)
    assert not _queued(released)
    assert state.breach_wait_released_until > 102.1
    # It does not fall back into the queue while the teammate digs on.
    for now in (102.3, 103.0, 105.0, 107.5):
        assert not _queued(decide(now))
    # Much later the patience is whole again.
    assert _queued(decide(120.0))


def test_the_cell_it_stopped_waiting_for_is_left_to_its_digger():
    brain = SimpleBotBrain(_world(_sealed_wall_solids()))
    follower = _bot(3, (12.5, 10.5, HEAD))
    state = _BotState(1, 1, follower.life_id)
    # Alone at the wall it plans the breach of the cell in front of it.
    brain._navigation_intent(_frame(follower, created_at=100.0), follower, state,
                             _goal(ACROSS), 100.0)
    breach = next(step.breach for step in state.route if step.breach is not None)
    edge = (breach.source, breach.destination)
    digger = _bot(1, (12.6, 10.5, HEAD))

    def decide(now):
        frame = _frame(follower, _digging(digger, now - 0.05, breach.target), created_at=now)
        return brain._navigation_intent(frame, follower, state, _goal(ACROSS), now)

    assert _queued(decide(100.2))
    assert _queued(decide(101.9))
    assert edge not in state.blocked_edges
    assert not _queued(decide(102.3))
    # That exact cell is the digger's: the next plan is another hole or the
    # way round.
    assert edge in state.blocked_edges
