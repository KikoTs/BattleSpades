"""A live flag carrier must keep returning to its actual capture base."""

from scripts.bot_ctf_review import review


def _rows(goal=(0., 0., 20.), role="ctf_capture", gap=False):
    return [{"id": 1, "life": 1, "team": 2, "t": tick / 10,
             "position": (50., 0., 20.), "goal": goal, "role": role,
             "alive": True, "intent_fresh": True, "visible": False,
             "action": "none", "pending": None, "wade": False,
             "mode_state": {"mode": "ctf", "team_scores": {"2": int(tick >= 100)},
                "objectives": [{"kind": "ctf_base", "team": 2, "position": (0., 0., 20.)},
                               {"kind": "ctf_intel", "team": 3, "position": (50., 0., 20.),
                                "carrier_id": 1 if tick >= 5 else -1, "state": 2}]}}
            for tick in range(121) if not gap or tick < 50 or tick > 80]


def test_actual_carrier_goal_and_scoring_transitions_are_measured_once():
    report = review(_rows())
    assert report["objective_state_recorded"] and not report["findings"]
    assert len(report["pickups"]) == len(report["captures"]) == 1
    assert report["carrier_samples"] == 116


def test_live_healthy_carrier_pursuing_enemy_instead_of_base_fails():
    report = review(_rows(goal=(100., 0., 20.), role="chase_last_seen"))
    assert report["findings"][0]["kind"] == "flag_carrier_abandons_home_base"


def test_protected_combat_can_keep_capture_goal_without_failure():
    assert not review(_rows(role="combat_objective:ctf_capture"))["findings"]


def test_sampling_gap_and_missing_telemetry_do_not_imply_verified_success():
    assert not review(_rows(goal=None, gap=True))["findings"]
    assert not review([{"id": 1}])["objective_state_recorded"]


def _carrier_breach_rows():
    rows = _rows(role="ctf_capture:route_breach")[:51]
    for row in rows:
        tick = round(row["t"] * 10)
        row.update(affordance="breach", action="melee" if tick % 5 == 0 else "none",
                   feedback=["melee", False, tick // 5],
                   terrain_target=({"cell": [167, 267, 225], "solid": True, "damage": 3.}
                                   if tick % 3 == 0 else None))
    return rows


def test_correct_capture_goal_cannot_hide_stationary_rejected_breach():
    finding = review(_carrier_breach_rows())["findings"][0]
    assert finding["kind"] == "carrier_repeated_rejected_breach"
    assert finding["unique_rejections"] >= 3
    assert finding["target_cells"] == [[167, 267, 225]]


def test_repeated_feedback_snapshot_is_only_one_rejected_action():
    rows = _carrier_breach_rows()
    for row in rows:
        row["feedback"][2] = 19
    assert not review(rows)["findings"]


def test_actual_target_progress_and_accepted_work_break_rejected_episode():
    rows = _carrier_breach_rows()
    for row in rows:
        if row["terrain_target"]:
            row["terrain_target"]["damage"] += int(row["t"] // 2)
    assert not review(rows)["findings"]
    rows = _carrier_breach_rows()
    for row in rows:
        if row["t"] in {2., 4.}:
            row["feedback"] = ["melee", True, 100 + int(row["t"])]
    assert not review(rows)["findings"]


def test_body_progress_or_missing_samples_do_not_invent_continuous_stall():
    rows = _carrier_breach_rows()
    for row in rows:
        row["position"] = (50. + int(row["t"] // 2) * 2, 0., 20.)
    assert not review(rows)["findings"]
    rows = [r for r in _carrier_breach_rows() if r["t"] < 2 or r["t"] > 3]
    assert not review(rows)["findings"]
