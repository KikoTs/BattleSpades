"""Objective checks must distinguish useful guarding from abandoning the win."""

import math

import pytest

from scripts.bot_objective_review import review


def _rows(*, enemy_vip_alive=True, guard_role="vip_guard_formation:arrived",
          guard_goal=(5., 0., 20.), boss_role="vip_retreat:arrived", enemy_x=80.):
    rows = []
    for tick in range(121):
        state = {"mode": "vip", "phase": "ACTIVE", "vips": {"2": 0, "3": 2},
                 "vip_alive": {"2": True, "3": enemy_vip_alive},
                 "respawn_enabled": {"2": True, "3": enemy_vip_alive},
                 "team_scores": {"2": 0, "3": 0}}
        for bot, team, pos, goal, role, alive in (
                (0, 2, (0., 0., 20.), (0., 0., 20.), boss_role, True),
                (1, 2, (5., 0., 20.), guard_goal, guard_role, True),
                (2, 3, (100., 0., 20.), (100., 0., 20.), "vip_retreat", enemy_vip_alive),
                (3, 3, (enemy_x, 0., 20.), (0., 0., 20.), "vip_flank_attack", True)):
            rows.append({"id": bot, "life": 1, "t": tick / 10, "mode_state": state,
                         "team": team, "position": pos, "goal": goal, "role": role,
                         "alive": alive, "spawned": True, "intent_fresh": True,
                         "visible": False, "wade": False, "action": "none", "pending": None,
                         "orientation": (1., 0., 0.), "score": 0})
    return rows


def test_stationary_vip_and_close_escorts_are_valid_objective_work():
    result = review(_rows())
    assert result["objective_state_recorded"] and not result["findings"]
    assert result["bots"]["1:1"]["guard_distance_median"] == 5.
    assert result["objective_success"] == {"vip_kills": 0, "round_score_changes": 0}


def test_vip_generic_patrol_and_far_escort_goals_are_flagged_despite_healthy_runtime():
    result = review(_rows(guard_goal=(50., 0., 20.), boss_role="patrol"))
    assert {f["kind"] for f in result["findings"]} == {
        "escort_goal_abandons_live_vip", "vip_abandons_mode_for_generic_task"}


def test_enemy_vip_death_cannot_send_every_attacker_home_indefinitely():
    result = review(_rows(enemy_vip_alive=False))
    assert [f["kind"] for f in result["findings"]] == ["no_mop_up_after_enemy_vip_death"]
    assert result["findings"][0]["enemy_survivors"] == [3]


@pytest.mark.parametrize("role", ["vip_mop_up", "vip_mop_up:planning_wait", "combat_pursuit"])
def test_survivor_pursuit_or_combat_preserves_mop_up_ownership(role):
    assert not review(_rows(enemy_vip_alive=False, guard_role=role))["findings"]


def test_protected_objective_combat_keeps_mode_role_without_false_quiet_abandonment():
    result = review(_rows(enemy_vip_alive=False, guard_role="combat_objective:vip_mop_up"))
    assert not result["findings"]
    assert result["bots"]["1:1"]["roles"] == {"vip_mop_up": 121}


def test_defending_an_immediately_threatened_vip_does_not_abandon_mop_up():
    assert not review(_rows(enemy_vip_alive=False, enemy_x=12.))["findings"]


def test_sampling_gaps_do_not_invent_ten_seconds_of_continuous_failure():
    rows = [r for r in _rows(boss_role="patrol") if r["t"] < 5 or r["t"] > 8]
    assert not review(rows)["findings"]


def test_pitch_direction_reversal_is_not_just_crossing_zero():
    rows = _rows()
    for row in rows:
        pitch = math.radians(10 if int(row["t"] * 10) % 2 else 20)
        row["orientation"] = (math.cos(pitch), 0., math.sin(pitch))
    assert review(rows)["bots"]["1:1"]["pitch_reversals"] == 119


def test_missing_objective_telemetry_is_unknown_not_success():
    assert not review([{"id": 1, "life": 1, "t": 0}])["objective_state_recorded"]
    assert not review([{"mode_state": {"mode": "vip"}}])["objective_state_recorded"]
