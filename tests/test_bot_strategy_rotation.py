"""Full-team role variety must retain quotas, stability and objective ownership."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace

import shared.constants as C
from server.bot_ai.messages import ObjectiveSnapshot
from server.bot_ai.policies import TeamDeathmatchBotPolicy, _rotating_team_slot, objective_decision_for
from tests.test_bot_policies import _frame, _player
from tests.test_bot_schematics import PROFILE
from server.bot_ai.horde_strategy import HordeCoordinator, HordeMember, SiegeInfo, SurvivorTarget
from server.bot_ai.fun_messages import FunState, FunPlayer
from server.bot_ai.unofficial import decision_for


def roster():
    # Alternating IDs reproduce the actual director's two-team joins.
    return tuple(_player(index, 2 if index % 2 == 0 else 3,
                         (20.5 + index * 2, 30.5, 17.75)) for index in range(16))


def team_frame(observer, players, elapsed=0.0, seed=19):
    return replace(_frame("tdm", observer, *(p for p in players if p is not observer),
        objectives=(ObjectiveSnapshot("team_anchor", 2, (20.5, 30.5, 17.75)),
                    ObjectiveSnapshot("team_anchor", 3, (180.5, 30.5, 17.75)))),
        behavior_version="cooperative", strategy_seed=seed, strategy_elapsed=elapsed,
        profile=replace(PROFILE, teamwork=.9, caution=.4, aggression=.7))


def test_rotation_is_consistent_across_both_full_teams_and_fair_over_a_match():
    players = roster()
    owners = {team: Counter() for team in (2, 3)}
    for period in range(8):
        for team in (2, 3):
            members = [p for p in players if p.team == team]
            slots = [_rotating_team_slot(team_frame(p, players, period * 45), p) for p in members]
            assert sorted(slots) == list(range(8)), "every observer agrees on one team allocation"
            for player, slot in zip(members, slots):
                if slot == 0:
                    owners[team][player.player_id] += 1
    assert all(len(counts) == 8 and set(counts.values()) == {1} for counts in owners.values())


def test_match_seed_changes_assignments_without_per_frame_role_churn():
    players = roster()
    assignments = []
    for seed in (0, 7, 19):
        slots = []
        for player in players:
            frame = team_frame(player, players, 12.0, seed)
            slot = _rotating_team_slot(frame, player)
            assert _rotating_team_slot(replace(frame, strategy_elapsed=12.1), player) == slot
            slots.append(slot)
        assignments.append(tuple(slots))
    assert len(set(assignments)) == 3


def test_full_tdm_squads_rotate_support_without_losing_the_assault():
    players = roster()
    policy = TeamDeathmatchBotPolicy()
    by_player = {p.player_id: set() for p in players}
    for period in range(8):
        counts = {2: Counter(), 3: Counter()}
        for player in players:
            decision = policy.decide(team_frame(player, players, period * 45), player)
            assert decision is not None
            counts[player.team][decision.role] += 1
            by_player[player.player_id].add(decision.role)
        for team in (2, 3):
            assert counts[team]["tdm_squad_support"] == 3
            assert counts[team]["team_assault_enemy_side"] == 5
    assert all(len(roles) == 2 for roles in by_player.values())


def test_a_ctf_carrier_keeps_the_capture_job_across_rotation_boundaries():
    players = roster()
    carrier = replace(players[0], carried_entity_id=99)
    home = ObjectiveSnapshot("ctf_base", 2, (20.5, 30.5, 17.75))
    for elapsed in (0, 44.9, 45, 90, 180):
        frame = replace(team_frame(carrier, players, elapsed), mode_id="ctf", objectives=(home,))
        decision = objective_decision_for(frame, carrier)
        assert decision is not None and decision.role == "ctf_capture"
        assert decision.position == home.position


def test_vip_bodyguard_rotates_across_full_roster_without_replacing_attackers():
    players = roster()
    own = ObjectiveSnapshot("vip", 2, players[0].position, carrier_id=0)
    enemy = ObjectiveSnapshot("vip", 3, players[1].position, carrier_id=1)
    guards = set()
    for period in range(8):
        roles = {}
        for player in players[2::2]:
            frame = replace(team_frame(player, players, period * 45), mode_id="vip",
                            mode_phase="active", objectives=(own, enemy))
            roles[player.player_id] = objective_decision_for(frame, player).role
        chosen = [pid for pid, role in roles.items() if role == "vip_guard_formation"]
        assert len(chosen) == 1
        assert list(roles.values()).count("vip_flank_attack") == 6
        guards.update(chosen)
    assert len(guards) == 7


def test_full_horde_has_seeded_approaches_without_churning_orders_every_tick():
    members = [HordeMember(index, (120.5, 128.5, 227.75)) for index in range(16)]
    target = SurvivorTarget(30, (128.5, 128.5, 207.75))
    siege = SiegeInfo(True, 230)
    signatures = []
    for seed in (0, 7, 19):
        coordinator = HordeCoordinator(seed=seed)
        first = coordinator.plan(members, [target], {30: siege}, 100)
        previous = first
        changes = Counter()
        for tick in range(1, 11):
            again = coordinator.plan(members, [target], {30: siege}, 100 + tick * .1)
            for pid, order in again.items():
                assert (order.role, order.target_id) == (first[pid].role, first[pid].target_id)
                if order != previous[pid]:
                    # Waiting actors pace on separate four-second beats; one
                    # may cross its beat in this window without role churn.
                    assert order.role == "surround"
                    changes[pid] += 1
            previous = again
        assert all(count <= 1 for count in changes.values())
        assert len(first) == 16
        assert any(order.role == "climb" for order in first.values())
        assert any(order.role == "surround" for order in first.values())
        assert all(order.target_id == 30 for order in first.values())
        signatures.append(tuple((pid, order.role, order.goal) for pid, order in sorted(first.items())))
    assert len(set(signatures)) == 3


def test_ctf_home_guard_relief_rotates_but_keeps_the_team_raid_quota():
    players = roster()
    objectives = (
        ObjectiveSnapshot("ctf_base", 2, (20.5, 30.5, 17.75)),
        ObjectiveSnapshot("ctf_intel", 2, (20.5, 30.5, 17.75)),
        ObjectiveSnapshot("ctf_base", 3, (180.5, 30.5, 17.75)),
        ObjectiveSnapshot("ctf_intel", 3, (180.5, 30.5, 17.75)),
    )
    guards = set()
    for period in range(8):
        chosen = []
        for player in players[::2]:
            frame = replace(team_frame(player, players, period * 45), mode_id="ctf", objectives=objectives)
            if objective_decision_for(frame, player).role == "ctf_defend":
                chosen.append(player.player_id)
        assert len(chosen) == 2
        guards.update(chosen)
    assert len(guards) == 8


def test_full_survival_squad_splits_human_guards_repairs_and_fighters():
    players = tuple(replace(p, team=2, loadout=(int(C.BLOCK_TOOL),))
                    for p in roster())
    humans = (replace(players[0], player_id=20, is_bot=False),
              replace(players[1], player_id=21, is_bot=False))
    f = FunState(1, 2, (80.5, 30.5, 17.75),
                 ((2, (20.5, 30.5, 17.75)), (3, (160.5, 30.5, 17.75))),
                 tuple(FunPlayer(p.player_id, True) for p in (*players, *humans)),
                 stage=1, wave=1, repair_cells=((40, 32, 19),))
    protected = set()
    for period in range(4):
        roles = Counter()
        for player in players:
            frame = replace(team_frame(player, (*players, *humans), period * 45),
                            mode_id="night_shift", mode_phase="live", fun=f)
            decision = decision_for(frame, player)
            roles[decision.role] += 1
            if decision.role == "fun_guard_survivor":
                protected.add(decision.position)
                assert decision.watch_position == dict(f.homes)[3]
        assert roles == {"fun_guard_survivor": 4, "fun_repair_barricade": 2,
                         "fun_contest_objective": 10}
    assert protected == {p.position for p in humans}
    # Survival has no ordinary mid-wave allied respawn. A fallen guard's
    # lease must be filled by a living bot immediately.
    living = tuple(p for p in players if p.player_id not in {0, 1, 2, 3})
    remaining = tuple(replace(p, alive=False) if p.player_id < 4 else p for p in players)
    roles = Counter(decision_for(replace(team_frame(p, (*remaining, *humans), 10),
                                        mode_id="night_shift", mode_phase="live", fun=f), p).role
                    for p in living)
    assert roles["fun_guard_survivor"] == 4
    assert roles["fun_repair_barricade"] == 2
    assert roles["fun_contest_objective"] == 6


def test_full_team_assigns_one_builder_to_an_elevated_capture_volume_then_holds_it():
    players = tuple(replace(p, team=2, position=(104.5 + i * .2, 104.5, 75.75),
                            loadout=(int(C.BLOCK_TOOL), int(C.SPADE_TOOL)))
                    for i, p in enumerate(roster()))
    volume = ObjectiveSnapshot("tc_territory", 1, (106., 106., 55.),
                               bounds=(100, 112, 100, 112, 50, 60))
    decisions = [objective_decision_for(replace(team_frame(p, players), mode_id="tc",
                                               objectives=(volume,)), p) for p in players]
    builders = [decision for decision in decisions if decision.role == "territory_restore_access"]
    assert len(builders) == 1 and builders[0].directive == "climb"
    assert builders[0].position[2] == 59
    inside = replace(players[0], position=builders[0].position)
    decision = objective_decision_for(replace(team_frame(inside, players), mode_id="tc",
                                               objectives=(volume,)), inside)
    assert decision.role == "territory_hold_capture" and decision.directive == "hold_capture"
    assert decision.position == inside.position
