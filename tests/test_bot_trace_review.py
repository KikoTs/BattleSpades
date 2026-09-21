"""Behavior review must flag motion in circles, not legitimate combat/holds."""

import math
import json
from pathlib import Path

from scripts.bot_trace_review import review


def circle():
    return [{"t": i / 10, "id": 1, "life": 1, "alive": True,
             "grounded": True, "affordance": "walk", "action": "none",
             "movement": [1, 0, 0], "orientation": [1, 0, 0],
             "position": [5 * math.cos(i * math.pi / 50),
                          5 * math.sin(i * math.pi / 50), 20],
             "goal": [100, 0, 20]}
            for i in range(401)]


def test_motion_alone_cannot_pass_repeated_loops():
    assert review(circle())["suspected_travel_loops"]


def test_new_ground_and_real_goal_progress_are_not_a_loop():
    rows = circle()
    for row in rows:
        row["position"][0] += row["t"] * 2
    assert not review(rows)["suspected_travel_loops"]


def test_combat_and_deliberate_holds_are_excluded():
    for alteration in ({"visible": True}, {"movement": [0, 0, 0]},
                       {"action": "mine"}, {"pending": "place_prefab"}):
        rows = [dict(row, **alteration) for row in circle()]
        assert not review(rows)["suspected_travel_loops"]


def test_lives_and_sample_gaps_do_not_form_a_loop():
    rows = circle()
    for row in rows:
        row["life"] = int(row["t"] // 10)
    assert not review(rows)["suspected_travel_loops"]
    assert not review(circle()[::10])["suspected_travel_loops"]


def test_vertical_travel_aim_counts_only_unowned_ground_travel():
    rows = circle()
    for row in rows:
        row["orientation"] = [0, 0, 1]
    result = review(rows)["bots"]["1"]
    assert result["travel_pitch_over_45_samples"] == len(rows)
    assert result["travel_pitch_max_degrees"] == 90
    for row in rows:
        row["pending"] = "mine"
    assert review(rows)["bots"]["1"]["ordinary_travel_samples"] == 0


def test_airborne_edges_and_navigation_pauses_do_not_hide_a_travel_loop():
    rows = circle()
    for index, row in enumerate(rows):
        row["grounded"] = index % 3 == 0
        row["affordance"] = ("walk", "jump", "drop")[index % 3]
        if index % 11 == 0:
            row.update(role="advance:planning_wait", movement=[0, 0, 0])
    result = review(rows)
    assert result["suspected_travel_loops"]
    assert result["bots"]["1"]["ordinary_travel_samples"] < len(rows) / 2


def test_short_combat_or_work_interruptions_break_travel_windows():
    for alteration in ({"visible": True}, {"action": "mine"},
                       {"pending": "place_prefab"}, {"movement": [0, 0, 0]},
                       {"intent_fresh": False}, {"wade": True}):
        rows = circle()
        for row in rows:
            if 19 <= row["t"] <= 21:
                row.update(alteration)
        assert not review(rows)["suspected_travel_loops"]


def test_intermediate_goal_change_cannot_be_hidden_by_equal_endpoints():
    rows = circle()
    for row in rows:
        if 19 <= row["t"] <= 21:
            row["goal"] = [-100, 0, 20]
    assert not review(rows)["suspected_travel_loops"]


def test_large_closed_return_is_reported_separately_from_repeated_coverage():
    rows = circle()
    for row in rows:
        angle = row["t"] * math.tau / 30
        row["position"] = [25 * math.cos(angle), 25 * math.sin(angle), 20]
    suspects = review(rows)["suspected_travel_loops"]
    assert any(row["kind"] == "closed_return" and row["start"] == 0
               and row["end"] == 30 for row in suspects)


def test_pitch_uses_direction_not_orientation_vector_magnitude():
    rows = circle()[:1]
    rows[0]["orientation"] = [0, 2, 2]
    assert review(rows)["bots"]["1"]["travel_pitch_max_degrees"] == 45


def test_recorded_native_terrace_loop_is_not_a_false_clean_result():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "bot_trace" /
                          "egypt_terrace_loop.json").read_text(encoding="utf-8"))
    rows = []
    for t, x, y, z, grounded, affordance, pause in fixture["samples"]:
        rows.append({"t": t, "id": 2, "life": 1, "alive": True,
                     "grounded": grounded, "affordance": affordance,
                     "action": "none", "pending": None, "visible": False,
                     "movement": [0, 0, 0] if pause else [1, 0, 0],
                     "role": "team_assault_enemy_side" + (":" + pause if pause else ""),
                     "orientation": [1, 0, 0], "position": [x, y, z],
                     "goal": fixture["goal"]})
    result = review(rows)
    suspects = [row for row in result["suspected_travel_loops"]
                if row["start"] == 60 and row["end"] == 90]
    assert len(suspects) == 1
    suspect = suspects[0]
    assert suspect["kind"] == "repeated_coverage"
    assert 199 < suspect["travel_distance"] < 201
    assert suspect["displacement"] < 3
    assert suspect["goal_progress"] < 2


def support_trace():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "bot_trace" /
                          "london_support_stagnation.json").read_text(encoding="utf-8"))
    rows = []
    for t, x, y, z, gx, gy, gz, role, affordance, age, escapes, corridor in fixture["samples"]:
        rows.append({"t": t, "id": 3, "life": 1, "alive": True, "spawned": True,
                     "intent_fresh": True, "wade": False, "action": "none",
                     "pending": None, "visible": False, "affordance": affordance,
                     "role": role, "position": [x, y, z], "goal": [gx, gy, gz],
                     "movement": [1, 0, 0] if role == "tdm_squad_support" else [0, 0, 0],
                     "navigation": {"goal_progress_age": age, "escape_attempts": escapes,
                                    "corridor_remaining": corridor}})
    return rows


def test_recorded_optional_support_commitment_cannot_hide_behind_goal_jitter():
    result = review(support_trace())
    assert not result["suspected_travel_loops"]
    stalls = result["suspected_support_stagnation"]
    assert len(stalls) == 1
    assert stalls[0]["id"] == 3
    assert stalls[0]["max_goal_progress_age_seconds"] > 47
    assert stalls[0]["age_basis"] == "goal_progress_age"
    assert stalls[0]["max_support_stall_seconds"] > 47
    assert stalls[0]["escape_attempts"] == 3
    # This field did not exist in the diagnostic. A stored endpoint failure
    # is not evidence of a match to the currently moving support goal anyway.
    assert stalls[0]["corridor_endpoint_failure"] is None
    assert result["bots"]["3"]["support_goal_progress_max_age_seconds"] > 47


def test_brief_work_combat_and_sampling_interruptions_do_not_erase_support_age():
    rows = support_trace()
    for row in rows:
        if 71 <= row["t"] <= 72:
            row["visible"] = True
        if 73 <= row["t"] <= 74:
            row["action"] = "melee"
        if 77 <= row["t"] <= 78:
            row["role"] = "medic_patient"
    rows = [row for row in rows if not 75 <= row["t"] <= 76]
    stalls = review(rows)["suspected_support_stagnation"]
    assert len(stalls) == 1
    assert stalls[0]["start"] < 71 and stalls[0]["end"] > 78
    assert stalls[0]["max_goal_progress_age_seconds"] > 47


def test_support_stagnation_requires_current_unowned_non_arrived_support():
    for change in ({"visible": True}, {"action": "melee"}, {"pending": "place_prefab"},
                   {"intent_fresh": False}, {"alive": False}, {"wade": True},
                   {"role": "tdm_squad_support:arrived"}, {"role": "team_assault_enemy_side"},
                   {"role": "tdm_squad_support:breach_assist_queue"}, {"navigation": {}}):
        rows = [dict(row, **change) for row in support_trace()]
        assert not review(rows)["suspected_support_stagnation"]
    rows = support_trace()
    for row in rows:
        row["position"] = row["goal"]
    assert not review(rows)["suspected_support_stagnation"]


def test_meaningful_goal_progress_resets_support_age_not_local_escape_movement():
    rows = support_trace()
    for row in rows:
        row["navigation"]["goal_progress_age"] = row["t"] % 20
    assert not review(rows)["suspected_support_stagnation"]
    rows = support_trace()
    for row in rows:
        row["navigation"]["coverage_age"] = row["t"] % 2
        row["navigation"]["corridor_endpoint_failure"] = False
    assert review(rows)["suspected_support_stagnation"]


def test_isolated_support_sample_does_not_satisfy_stagnation_evidence():
    assert not review(support_trace()[-1:])["suspected_support_stagnation"]


def test_own_actor_support_clock_catches_jitter_while_general_goal_age_resets():
    rows = support_trace()
    for row in rows:
        row["navigation"]["support_no_progress_time"] = row["navigation"]["goal_progress_age"]
        row["navigation"]["goal_progress_age"] = 0
    result = review(rows)
    stall = result["suspected_support_stagnation"][0]
    assert stall["age_basis"] == "support_no_progress_time"
    assert stall["max_support_stall_seconds"] > 47
    assert stall["max_goal_progress_age_seconds"] == 0
    assert result["bots"]["3"]["support_age_bases"] == ["support_no_progress_time"]
    assert result["bots"]["3"]["support_stall_max_seconds"] > 47


def test_zero_new_support_clock_does_not_fall_back_to_old_stale_age():
    rows = support_trace()
    for row in rows:
        row["navigation"]["support_no_progress_time"] = 0
    result = review(rows)
    assert not result["suspected_support_stagnation"]
    assert result["bots"]["3"]["support_stall_max_seconds"] == 0
    assert result["bots"]["3"]["support_goal_progress_max_age_seconds"] > 47


def test_stationary_navigation_reports_attempts_and_terrain_evidence_without_failing_gate():
    rows = circle()
    for i, row in enumerate(rows):
        row.update(position=[10., 10., 20. + (i % 2) * .9], intent_fresh=True,
                   role="advance:route_breach" if i < 250 else "advance:planning_wait",
                   movement=[0, 0, 0], action="melee" if i < 250 else "none",
                   feedback=["melee", True, 10] if i < 100 else
                            ["melee", False, 11] if i < 200 else ["melee", True, 12],
                   topology_version=0 if i < 150 else 1,
                   terrain_target={"cell": [11, 10, 20], "solid": i < 180,
                                   "damage": 0 if i < 80 or i >= 180 else 10})
    result = review(rows)
    physical = result["bots"]["1"]["longest_stationary_navigation"]
    assert physical["seconds"] == 40
    assert physical["max_distance_from_start"] == .9
    # The first accepted feedback predates the observed run; do not count it
    # repeatedly as new useful work while its stale tuple remains in the trace.
    assert physical["new_accepted_feedback"] == {"melee": 1}
    assert physical["new_rejected_feedback"] == {"melee": 1}
    assert physical["topology_changes"] == 1
    assert physical["terrain_target_state_changes"] == 2
    assert not result["suspected_travel_loops"]
    assert not result["suspected_support_stagnation"]


def test_stationary_navigation_excludes_deliberate_holds_combat_and_sample_gaps():
    rows = circle()
    for row in rows:
        row.update(position=[10, 10, 20], intent_fresh=True, role="advance:planning_wait")
    for change in ({"role": "advance:arrived"}, {"visible": True}, {"intent_fresh": False}):
        result = review([dict(row, **change) for row in rows])
        assert result["bots"]["1"]["longest_stationary_navigation"] is None
    assert review(rows[::10])["bots"]["1"]["longest_stationary_navigation"] is None
    for row in rows:
        row["position"][0] = row["t"]
    assert review(rows)["bots"]["1"]["longest_stationary_navigation"]["seconds"] <= 1


def water_trace():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "bot_trace" /
                          "london_water_recovery.json").read_text(encoding="utf-8"))
    return [{"t": t, "id": 3, "life": 1, "alive": True, "spawned": True,
             "position": [x, y, z], "wade": wade, "grounded": grounded,
             "role": role, "topology_version": topology, "terrain_target": target}
            for t, x, y, z, wade, grounded, role, topology, target in fixture["samples"]]


def test_recorded_water_recovery_keeps_brief_dry_flashes_and_real_work_in_one_episode():
    rows = water_trace()
    assert any(not row["wade"] and row["grounded"] for row in rows)
    result = review(rows)
    assert len(result["water_recovery_episodes"]) == 1
    episode = result["suspected_water_recovery_stagnation"][0]
    assert episode["start"] == 42.7 and episode["end"] == 119.9
    assert episode["seconds"] > 75
    assert episode["wade_fraction"] > .95
    assert episode["recovery_role_fraction"] > .65
    assert episode["bbox_diagonal"] < 32
    assert episode["travel_distance"] > 400
    assert episode["terrain_target_state_changes"] == 2
    assert episode["topology_changes"] > 0
    assert not episode["recovered"]
    assert episode["dry_footing_basis"] == "grounded_and_not_wading"


def test_long_swim_that_covers_new_ground_is_not_confined_water_stagnation():
    rows = water_trace()
    for row in rows:
        row["position"][0] += (row["t"] - rows[0]["t"]) * 2
    result = review(rows)
    assert result["water_recovery_episodes"][0]["seconds"] > 75
    assert not result["suspected_water_recovery_stagnation"]


def test_water_exit_requires_a_full_second_of_dry_safe_footing_when_recorded():
    rows = water_trace()[:30]
    for i, row in enumerate(rows):
        row["dry_safe"] = False
        if i >= 5:
            row.update(wade=False, grounded=True)
        if i >= 15:
            row["dry_safe"] = True
    episode = review(rows)["water_recovery_episodes"][0]
    assert episode["recovered"]
    assert episode["dry_footing_basis"] == "dry_safe"
    assert episode["end"] >= rows[15]["t"] + 1
    assert episode["end"] < rows[15]["t"] + 1.2


def test_sparse_water_samples_cannot_prove_stagnation_or_continuous_dry_footing():
    rows = water_trace()[::10]
    result = review(rows)
    assert not result["suspected_water_recovery_stagnation"]
    for row in rows[1:]:
        row.update(wade=False, grounded=True)
    assert not review(rows)["water_recovery_episodes"][0]["recovered"]
