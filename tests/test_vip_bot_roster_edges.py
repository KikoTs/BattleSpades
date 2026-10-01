"""VIP rounds keep a boss and keep bots busy when the roster thins out.

Regression for "VIP mode: only the bots are left, 1v1. There are no leaders
and both bots just stand still": a lone VIP bot held at home (``vip_rally``
behind an empty team) so two lone bosses never met, and a departed VIP was
never replaced.
"""

import asyncio
from dataclasses import replace
import math

import shared.constants as C
from modes.vip import VIPPhase
from server.bot_ai.director import _BotConnection
from server.bot_ai.messages import ObjectiveSnapshot
from server.bot_ai.policies import ModeBotDecision, ModePolicyMemory, objective_decision_for
from server.game_constants import TEAM1, TEAM2
from tests.test_bot_policies import _frame, _player as _snapshot
from tests.test_vip import VIPMode, _new_mode, _player, _Server


def _bot(server, player_id, team):
    player = _player(server, player_id, team)
    connection = _BotConnection(server)
    connection.player = player
    player.connection = connection
    player.send = connection.send
    return player


def _leave(mode, server, player):
    """The ordinary disconnect order: mode hook while the id is still known."""
    asyncio.run(mode.on_player_leave(player))
    server.players.pop(player.id, None)
    server.teams[player.team].remove_player(player)


def test_bots_only_one_versus_one_both_bots_become_vips():
    server, mode = _new_mode()
    blue = _bot(server, 1, TEAM1)
    green = _bot(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))

    assert mode.phase is VIPPhase.ACTIVE
    assert mode.vips == {TEAM1: blue, TEAM2: green}
    assert blue.class_id == int(C.MAFIA_VIPS[TEAM1])
    assert green.class_id == int(C.MAFIA_VIPS[TEAM2])


def test_bots_only_round_resolves_and_the_next_round_crowns_again():
    server, mode = _new_mode()
    blue = _bot(server, 1, TEAM1)
    green = _bot(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))

    blue.alive = blue.spawned = False
    asyncio.run(mode.on_player_death(blue, green, 0))

    assert mode.rounds_played == 1
    assert server.teams[TEAM2].score == 1
    assert mode.phase is VIPPhase.INTERMISSION

    async def next_round():
        # The intermission task belonged to the finished event loop; run the
        # restart it would have run, then the reset drain and selection.
        mode._round_task = None
        await mode._begin_round(reset_players=True)
        for tick in range(2, 12):
            await mode.on_tick(tick)
            if mode.phase is VIPPhase.ACTIVE:
                return

    asyncio.run(next_round())
    assert mode.phase is VIPPhase.ACTIVE
    assert mode.vips == {TEAM1: blue, TEAM2: green}
    assert mode.vip_alive == {TEAM1: True, TEAM2: True}


def test_vip_leaving_mid_round_crowns_a_teammate_without_sudden_death():
    server, mode = _new_mode()
    human = _player(server, 1, TEAM1)
    bot = _bot(server, 3, TEAM1)
    _bot(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    mode.vips[TEAM1] = human  # deterministic: the human holds the crown
    vip_before = mode.vips[TEAM2]

    _leave(mode, server, human)

    assert mode.phase is VIPPhase.ACTIVE
    assert mode.vips[TEAM1] is bot
    assert mode.vip_alive[TEAM1] is True
    assert mode.respawn_enabled[TEAM1] is True
    assert bot.class_id == int(C.MAFIA_VIPS[TEAM1])
    assert mode.vips[TEAM2] is vip_before
    assert mode.rounds_played == 0


def test_last_humans_leaving_hand_both_crowns_to_the_bots():
    server, mode = _new_mode()
    blue_human = _player(server, 1, TEAM1)
    green_human = _player(server, 2, TEAM2)
    blue_bot = _bot(server, 3, TEAM1)
    green_bot = _bot(server, 4, TEAM2)
    asyncio.run(mode.on_tick(1))
    mode.vips = {TEAM1: blue_human, TEAM2: green_human}

    _leave(mode, server, blue_human)
    _leave(mode, server, green_human)

    assert mode.phase is VIPPhase.ACTIVE
    assert mode.vips == {TEAM1: blue_bot, TEAM2: green_bot}
    assert mode.vip_alive == {TEAM1: True, TEAM2: True}
    assert mode.respawn_enabled == {TEAM1: True, TEAM2: True}


def test_vip_leaving_an_otherwise_empty_team_ends_the_round_for_the_other_team():
    server, mode = _new_mode()
    blue = _bot(server, 1, TEAM1)
    _bot(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))

    _leave(mode, server, blue)

    assert mode.rounds_played == 1
    assert server.teams[TEAM2].score == 1
    assert mode.phase is VIPPhase.INTERMISSION


def test_death_leave_policy_keeps_quit_as_vip_death():
    server = _Server()
    server.config.mode_settings["vip"]["leave_policy"] = "death"
    mode = VIPMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    blue = _bot(server, 1, TEAM1)
    _bot(server, 3, TEAM1)
    _bot(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    mode.vips[TEAM1] = blue

    _leave(mode, server, blue)

    assert mode.vip_alive[TEAM1] is False
    assert mode.respawn_enabled[TEAM1] is False


def test_vip_dropped_without_a_leave_hook_is_replaced_by_the_audit():
    server, mode = _new_mode()
    blue = _bot(server, 1, TEAM1)
    blue_mate = _bot(server, 3, TEAM1)
    _bot(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    mode.vips[TEAM1] = blue
    # A roster path that forgot the mode hook: the crown would be a ghost.
    server.players.pop(blue.id)
    server.teams[TEAM1].remove_player(blue)
    mode._next_roster_audit = 0.0

    asyncio.run(mode.on_tick(2))

    assert mode.vips[TEAM1] is blue_mate
    assert mode.vip_alive[TEAM1] is True


def test_vip_death_is_still_sudden_death_not_a_reassignment():
    server, mode = _new_mode()
    blue = _bot(server, 1, TEAM1)
    _bot(server, 3, TEAM1)
    green = _bot(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    mode.vips[TEAM1] = blue

    blue.alive = blue.spawned = False
    asyncio.run(mode.on_player_death(blue, green, 0))

    assert mode.vips[TEAM1] is blue
    assert mode.vip_alive[TEAM1] is False
    assert mode.respawn_enabled[TEAM1] is False


# --- bot policy -----------------------------------------------------------

def _lone_vip_frame():
    blue = _snapshot(1, 2, (40., 40., 10.))
    green = _snapshot(2, 3, (250., 40., 10.))
    objectives = (
        ObjectiveSnapshot("vip", 2, blue.position, carrier_id=1),
        ObjectiveSnapshot("vip", 3, green.position, carrier_id=2),
        ObjectiveSnapshot("team_anchor", 2, (20., 40., 10.)),
        ObjectiveSnapshot("team_anchor", 3, (260., 40., 10.)),
    )
    return replace(_frame("vip", blue, green, objectives=objectives),
                   created_at=100.), blue, green


def test_lone_vip_bots_hunt_each_other_instead_of_holding_at_home():
    observation, blue, green = _lone_vip_frame()
    for bot, enemy in ((blue, green), (green, blue)):
        decision = objective_decision_for(observation, bot)
        assert decision.role == "vip_lone_hunt"
        assert decision.position == enemy.position
        assert decision.directive != "vip_shelter"
        assert decision.objective_priority >= .9


def test_lone_vip_mops_up_when_the_enemy_vip_is_already_dead():
    observation, blue, _ = _lone_vip_frame()
    observation = replace(observation, objectives=tuple(
        o for o in observation.objectives if not (o.kind == "vip" and o.team == 3)))
    decision = objective_decision_for(observation, blue)
    assert decision.role == "vip_mop_up"
    assert decision.position == (260., 40., 10.)


def test_vip_with_a_teammate_still_holds_and_requests_a_shelter():
    observation, blue, green = _lone_vip_frame()
    mate = _snapshot(5, 2, (30., 40., 10.))
    observation = replace(observation, players=(blue, green, mate))
    decision = objective_decision_for(observation, blue)
    assert decision.role == "vip_rally"
    assert decision.directive == "vip_shelter"
    # ...and the teammate goes for the enemy boss.
    assert objective_decision_for(observation, mate).role == "vip_flank_attack"


def test_no_vips_at_all_still_gives_every_bot_a_hunt():
    observation, blue, green = _lone_vip_frame()
    observation = replace(observation, objectives=tuple(
        o for o in observation.objectives if o.kind != "vip"))
    for bot, anchor in ((blue, (260., 40., 10.)), (green, (20., 40., 10.))):
        decision = objective_decision_for(observation, bot)
        assert decision is not None and decision.role == "vip_mop_up"
        assert decision.position == anchor


def test_strike_arrives_only_on_top_of_the_boss():
    observation, blue, _ = _lone_vip_frame()
    assert objective_decision_for(observation, blue).arrival_radius <= 1.0


def test_strike_does_not_idle_on_a_stale_held_point_after_arriving():
    memory = ModePolicyMemory()
    observation, blue, green = _lone_vip_frame()
    first = memory.decide(observation, blue)
    # The hunter reached the old point; the boss sidestepped 2.5 blocks
    # (inside the 3-block hold) behind cover.
    moved = (green.position[0], green.position[1] + 2.5, green.position[2])
    arrived = replace(blue, position=first.position)
    later = replace(
        observation, created_at=100.4,
        players=(arrived, replace(green, position=moved)),
        objectives=tuple(replace(o, position=moved) if o.kind == "vip" and o.team == 3
                         else replace(o, position=arrived.position)
                         if o.kind == "vip" else o
                         for o in observation.objectives))
    assert memory.decide(later, arrived).position == moved


def test_worker_searches_the_enemy_side_after_a_mop_up_arrival():
    from server.bot_ai.simple_worker import SimpleBotBrain, _BotState
    from tests.test_simple_bot_tactics import _frame as _tactics_frame
    from tests.test_simple_bot_tactics import _player as _tactics_player
    from tests.test_simple_bot_tactics import _TacticalWorld

    observer = _tactics_player(1, TEAM1, (10.0, 10.0, 20.0), is_bot=True)
    brain = SimpleBotBrain(_TacticalWorld())
    state = _BotState(1, 1, observer.life_id)
    decision = ModeBotDecision(observer.position, "vip_mop_up",
                               objective_priority=.72)
    frame = replace(_tactics_frame(observer), mode_id="vip")
    first = brain._select_goal(frame, observer, state, 100.0, decision=decision)
    assert first is not None
    later = brain._select_goal(replace(frame, created_at=103.5), observer, state,
                               103.5, decision=decision)
    assert later.role == "team_assault_search"
    assert math.dist(later.position, observer.position) > 10.0
