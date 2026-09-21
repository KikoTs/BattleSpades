"""Task ownership must not create follow/orbit loops or suppress live combat."""

from dataclasses import replace

import pytest
import shared.constants as C

from server.bot_ai.cooperative_behavior import CooperativeBehavior
from server.bot_ai.messages import BotActionKind
from server.bot_ai.policies import ModeBotDecision
from server.bot_ai.simple_worker import SimpleBotBrain
from tests.test_bot_cooperative_medical import frame
from tests.test_bot_cooperative_projects import setup_crossing, decide, move
from tests.test_bot_project_sites import make_world, player


def formation():
    bot = player(player_id=1, loadout=(int(C.RIFLE_TOOL),), prefabs=(),
                 deployable_stock=(), position=(20.5, 20.5, 17.75))
    human = move(replace(bot, player_id=20, is_bot=False), (30.5, 20.5, 17.75))
    return CooperativeBehavior(make_world()), bot, human


def test_follower_holds_formation_instead_of_alternating_with_objective_navigation():
    coordinator, bot, human = formation()
    first = coordinator.decide(frame(bot, human, friendly_mischief=False), bot, None, None)
    assert first.role == "join_player_push"
    bot = move(bot, first.goal)
    strategic = ModeBotDecision((45.5, 20.5, 17.75), "tdm_assault")
    for now in (100.2, 100.4, 100.7, 101.0, 107.5):
        order = coordinator.decide(frame(bot, human, now=now, friendly_mischief=False),
                                   bot, None, strategic)
        assert order is not None and order.role == "join_player_push" and order.hold
    moved = move(human, (35.5, 20.5, 17.75))
    order = coordinator.decide(frame(bot, moved, now=107.7, friendly_mischief=False), bot, None, strategic)
    assert order is not None and not order.hold


def test_stationary_leader_turning_to_look_around_does_not_make_follower_orbit():
    coordinator, bot, human = formation()
    first = coordinator.decide(frame(bot, human, friendly_mischief=False), bot, None, None)
    for index, orientation in enumerate(((0., 1., 0.), (-1., 0., 0.), (0., -1., .7))):
        turned = replace(human, orientation=orientation)
        order = coordinator.decide(frame(bot, turned, now=100.2 + index * .2,
                                         friendly_mischief=False), bot, None, None)
        assert order is not None and order.goal == first.goal


def test_formation_hysteresis_and_visible_enemy_keep_one_movement_owner():
    coordinator, bot, human = formation()
    brain = SimpleBotBrain(coordinator.world)
    first = brain.decide(frame(bot, human, friendly_mischief=False))
    assert first.debug_role == "join_player_push"
    bot = move(bot, first.debug_goal)
    holding = brain.decide(frame(bot, human, now=100.2, frame_id=2, friendly_mischief=False))
    assert holding.debug_role == "join_player_push"
    assert holding.movement.direction == (0., 0., 0.)
    # Small movement at the edge of spacing must not hand control back and
    # forth between the formation and an unrelated strategic goal.
    bot = move(bot, (first.debug_goal[0] + 2.5, first.debug_goal[1], first.debug_goal[2]))
    holding = brain.decide(frame(bot, human, now=100.4, frame_id=3, friendly_mischief=False))
    assert holding.debug_role == "join_player_push"
    assert holding.movement.direction == (0., 0., 0.)
    enemy = move(replace(bot, player_id=9, team=3), (43.5, 20.5, 17.75))
    fighting = brain.decide(frame(bot, human, enemy, now=100.6, frame_id=4, friendly_mischief=False))
    assert fighting.action.kind is BotActionKind.FIRE
    assert fighting.debug_role != "join_player_push"


@pytest.mark.parametrize("kind", ["bridge", "breach"])
def test_ready_route_support_yields_to_visible_enemy_between_evaluation_ticks(kind):
    world, bot, partner, coordinator = setup_crossing(kind)
    first = decide(coordinator, bot, 100., partner)
    task = coordinator.lives[(bot.player_id, bot.generation)].task
    if kind == "bridge":
        world._vxl.solids.update(task.site.cells)
        bot = replace(bot, last_action_request_id=first.action.request_id, last_task_accepted=True)
    else:
        world._vxl.solids.difference_update(task.site.cells)
    decide(coordinator, bot, 100.2, partner)
    following = decide(coordinator, partner, 100.3, bot, strategic=False)
    assert following is not None and following.role == "squad_advance"
    enemy = move(replace(partner, player_id=9, team=3), (35.5, 23.5, 17.75))
    result = coordinator.decide(frame(partner, bot, enemy, now=100.4, friendly_mischief=False),
                                partner, enemy, None)
    assert result is None


def test_project_approach_yields_to_visible_combat_outside_ten_block_emergency_range():
    world, bot, partner, coordinator = setup_crossing("breach")
    bot = replace(bot, loadout=(*bot.loadout, int(C.RIFLE_TOOL)))
    assert decide(coordinator, bot, 100., partner).role == "squad_breach"
    enemy = move(replace(partner, player_id=9, team=3), (20.5, 35.5, 17.75))
    order = coordinator.decide(frame(bot, partner, enemy, now=100.2, friendly_mischief=False),
                               bot, enemy, None)
    assert order is None
    assert coordinator.lives[(bot.player_id, bot.generation)].task is None


@pytest.mark.parametrize("recent_damage", [False, True])
def test_distant_visible_enemy_only_interrupts_construction_when_it_recently_damaged_builder(recent_damage):
    world, bot, partner, coordinator = setup_crossing("breach")
    bot = replace(bot, loadout=(*bot.loadout, int(C.SHOTGUN_TOOL)))
    assert decide(coordinator, bot, 100., partner).role == "squad_breach"
    enemy = move(replace(partner, player_id=9, team=3), (65.5, 20.5, 17.75))
    if recent_damage:
        bot = replace(bot, last_damage_source_id=enemy.player_id, last_damage_at=100.1)
    order = coordinator.decide(frame(bot, partner, enemy, now=100.2, friendly_mischief=False),
                               bot, enemy, None)
    if recent_damage:
        assert order is None and coordinator.lives[(bot.player_id, bot.generation)].task is None
    else:
        assert order is not None and order.role == "squad_breach"
        assert coordinator.teams.metrics["tasks_failed"] == 0


def test_visible_target_is_not_replaced_by_a_new_sniper_position_project():
    coordinator = CooperativeBehavior(make_world())
    bot = player(prefabs=(), deployable_stock=())
    enemy = move(replace(bot, player_id=9, team=3), (45.5, 20.5, 17.75))
    order = coordinator.decide(frame(bot, enemy, friendly_mischief=False), bot, enemy, None)
    assert order is None
    assert coordinator.teams.metrics["tasks_started"] == 0


def test_already_occupied_outpost_remains_a_combat_position():
    coordinator = CooperativeBehavior(make_world())
    bot = player(loadout=(int(C.SNIPER_TOOL),), prefabs=(), deployable_stock=())
    lane = ModeBotDecision((45.5, 20.5, 17.75), "known_lane")
    initial = coordinator.decide(frame(bot, friendly_mischief=False), bot, None, lane)
    assert initial is not None and initial.role == "outpost_watch"
    enemy = move(replace(bot, player_id=9, team=3), lane.position)
    defending = coordinator.decide(frame(bot, enemy, now=100.2, friendly_mischief=False), bot, enemy, lane)
    assert defending is not None and defending.role == "outpost_watch" and defending.hold
