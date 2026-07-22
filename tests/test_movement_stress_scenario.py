from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "scripts" / "scenarios" / "movement_stress.py"
SPEC = importlib.util.spec_from_file_location("movement_stress_scenario", SCENARIO_PATH)
assert SPEC is not None and SPEC.loader is not None
movement_stress = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = movement_stress
SPEC.loader.exec_module(movement_stress)


def test_engineer_flight_segment_explicitly_selects_pack_not_disguise() -> None:
    segment = next(
        item
        for item in movement_stress.DEFAULT_SEGMENTS
        if item.name == "engineer_jetpack_hold"
    )

    assert segment.required_class_id == 12
    assert segment.required_loadout_tools == (68,)
    assert segment.tool_id is None


def test_rocketeer_flight_segment_owns_legacy_jetpack2() -> None:
    segment = next(
        item
        for item in movement_stress.DEFAULT_SEGMENTS
        if item.name == "rocketeer_jetpack2_hold"
    )

    assert segment.required_class_id == 2
    assert segment.required_loadout_tools == (67,)
    assert segment.tool_id is None


def test_specialist_machete_segment_uses_the_real_primary_fire_path() -> None:
    segment = next(
        item
        for item in movement_stress.DEFAULT_SEGMENTS
        if item.name == "specialist_machete_dig"
    )

    assert segment.required_class_id == 16
    assert segment.required_loadout_tools == (50,)
    assert segment.tool_id == 50
    assert segment.primary_period == 0.75


def sample(
    index: int,
    *,
    segment: str = "walk",
    history: int = 30,
    timer: float = 0.0,
    network_loop: int | None = None,
    client_loop: int | None = None,
    error: float | None = 0.01,
    airborne: bool = False,
    z: float = 20.0,
    monotonic_ns: int | None = None,
) -> dict:
    return {
        "segment": segment,
        "history_length": history,
        "lerp_timer": timer,
        "network_loop": network_loop if network_loop is not None else 100 + index,
        "client_loop": client_loop if client_loop is not None else 102 + index,
        "matched_loop_error": error,
        "position": (10.0 + index, 10.0, z),
        "network_position": (10.0 + index, 10.0, z),
        "velocity": (0.0, 0.0, 0.0),
        "orientation": (1.0, 0.0, 0.0),
        "airborne": airborne,
        "wade": False,
        "monotonic_ns": monotonic_ns if monotonic_ns is not None else index * 50_000_000,
    }


def test_clean_extended_run_passes_with_air_and_slope_coverage():
    rows = [sample(index) for index in range(10)]
    rows += [
        sample(10, segment="slope_diagonal", z=20.0),
        sample(11, segment="slope_diagonal", z=20.3),
        sample(12, segment="jump_run", airborne=True, z=19.8),
        sample(13, segment="fall_recovery", airborne=True, z=20.2),
    ]

    analysis, segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert analysis.passed is True
    assert analysis.snap_count == 0
    assert analysis.adjust_count == 0
    assert analysis.airborne_samples == 2
    assert analysis.slope_covered is True
    assert events == []
    assert {segment.name for segment in segments} == {
        "walk",
        "slope_diagonal",
        "jump_run",
        "fall_recovery",
    }


def test_snap_adjust_stamp_regression_and_stall_fail_the_gate():
    rows = [
        sample(0, history=30, timer=0.0, network_loop=100, client_loop=102),
        sample(1, history=0, timer=0.1, network_loop=99, client_loop=120),
        sample(
            2,
            history=4,
            timer=0.08,
            network_loop=101,
            client_loop=122,
            error=0.4,
            monotonic_ns=1_000_000_000,
        ),
    ]
    thresholds = movement_stress.StressThresholds(max_stalls=0)

    analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
        thresholds=thresholds,
    )

    assert analysis.passed is False
    assert analysis.snap_count == 1
    assert analysis.adjust_count == 1
    assert analysis.network_loop_regressions == 1
    assert analysis.stall_count == 1
    assert analysis.max_matched_error == 0.4
    assert analysis.max_abs_loop_lag == 21
    assert {
        "hard_snap_limit",
        "soft_adjust_limit",
        "matched_error_limit",
        "loop_lag_limit",
        "network_loop_regression",
        "sample_stall_limit",
    }.issubset(analysis.failure_reasons)
    assert {event["kind"] for event in events} == {
        "snap",
        "adjust",
        "network_loop_regression",
    }


def test_visible_jump_rollback_fails_even_without_reconciliation_counters():
    rows = [
        sample(0, segment="jump_in_place", z=20.0),
        sample(1, segment="jump_in_place", z=19.9),
        sample(2, segment="jump_in_place", z=19.8),
    ]
    rows[0]["position"] = (100.0, 50.0, 20.0)
    rows[1]["position"] = (100.1, 50.0, 19.9)
    rows[2]["position"] = (96.5, 50.0, 19.8)

    analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert analysis.passed is False
    assert analysis.visible_rollback_count >= 1
    assert analysis.max_backward_step >= 3.0
    assert "visible_rollback_limit" in analysis.failure_reasons
    assert any(event["kind"] == "visible_rollback" for event in events)


def test_jetpack_handoff_loop_age_is_diagnostic_not_a_failure() -> None:
    rows = [
        sample(
            index,
            segment="engineer_jetpack_hold",
            client_loop=300 + index,
            network_loop=100,
            airborne=True,
        )
        for index in range(4)
    ]
    for index, row in enumerate(rows):
        row["jetpack_active"] = True
        row["jetpack_fuel"] = 100.0 - index

    analysis, _segments, _events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert analysis.passed is True
    assert analysis.max_abs_loop_lag >= 200


def test_respawn_gap_between_repeat_cycles_is_not_movement_or_sample_jitter():
    rows = [
        sample(
            0,
            segment="block_sprint_jump",
            monotonic_ns=1_000_000_000,
        ),
        sample(
            1,
            segment="block_sprint_jump",
            monotonic_ns=7_000_000_000,
        ),
    ]
    rows[0].update(
        cycle=1,
        position=(300.0, 200.0, 220.0),
        reconciliation_snap_count=3,
        reconciliation_adjust_count=4,
    )
    rows[1].update(
        cycle=2,
        position=(100.0, 100.0, 180.0),
        reconciliation_snap_count=9,
        reconciliation_adjust_count=12,
    )

    analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
        thresholds=movement_stress.StressThresholds(max_stalls=0),
    )

    assert analysis.visible_rollback_count == 0
    assert analysis.snap_count == 0
    assert analysis.adjust_count == 0
    assert analysis.stall_count == 0
    assert analysis.max_sample_gap_seconds == 0.0
    assert not any(
        event["kind"]
        in {"visible_teleport", "visible_rollback", "visible_vertical_snap"}
        for event in events
    )


def test_large_downward_airborne_step_is_a_fall_not_a_vertical_snap():
    rows = [
        sample(0, segment="flying_entity_jump", airborne=False, z=198.0),
        sample(1, segment="flying_entity_jump", airborne=True, z=202.0),
    ]
    rows[1]["velocity"] = (0.0, 0.0, 0.7)

    _analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert not any(event["kind"] == "visible_vertical_snap" for event in events)


def test_normal_jump_ascent_is_normalized_by_native_client_loops():
    rows = [
        sample(0, segment="jump_in_place", airborne=True, z=225.5),
        sample(4, segment="jump_in_place", airborne=True, z=224.8),
    ]
    rows[1]["position"] = (10.0, 10.0, 224.8)
    rows[1]["network_position"] = rows[1]["position"]
    rows[0]["velocity"] = (0.0, 0.0, -0.38)
    rows[1]["velocity"] = (0.0, 0.0, -0.30)

    analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert analysis.visible_rollback_count == 0
    assert not any(event["kind"] == "visible_vertical_snap" for event in events)


def test_high_thrust_jump_pack_climb_is_not_a_vertical_snap():
    rows = [
        sample(
            0,
            segment="rocketeer_jump_pack_hold",
            airborne=True,
            z=225.5,
        ),
        sample(
            3,
            segment="rocketeer_jump_pack_hold",
            airborne=True,
            z=223.8,
        ),
    ]
    for index, row in enumerate(rows):
        row.update(
            position=(10.0, 10.0, 225.5 - index * 1.7),
            velocity=(0.0, 0.0, -0.65),
            jetpack_id=66,
            jetpack_active=True,
            jetpack_fuel=90.0 - index * 5.0,
        )

    analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert analysis.visible_rollback_count == 0
    assert not any(event["kind"] == "visible_vertical_snap" for event in events)


def test_one_client_loop_vertical_restore_is_still_reported():
    rows = [
        sample(0, segment="jump_in_place", airborne=True, z=225.5),
        sample(1, segment="jump_in_place", airborne=True, z=224.8),
    ]
    rows[1]["position"] = (10.0, 10.0, 224.8)
    rows[1]["network_position"] = rows[1]["position"]

    analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert analysis.visible_rollback_count == 1
    assert any(event["kind"] == "visible_vertical_snap" for event in events)


def test_one_voxel_grounded_collision_step_is_not_a_vertical_snap():
    rows = [
        sample(0, segment="block_sprint_jump", airborne=True, z=208.044754),
        sample(1, segment="block_sprint_jump", airborne=False, z=207.480408),
    ]
    rows[1].update(
        matched_loop_error=0.002485,
        velocity=(0.21, -0.44, 0.0),
    )

    analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert analysis.visible_rollback_count == 0
    assert not any(event["kind"] == "visible_vertical_snap" for event in events)


def test_missing_terrain_coverage_is_reported():
    rows = [
        sample(0, segment="slope_diagonal", z=20.0),
        sample(1, segment="slope_diagonal", z=20.02),
        sample(2, segment="jump_run", airborne=False),
        sample(3, segment="fall_recovery", airborne=False),
    ]

    analysis, _segments, _events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert analysis.passed is False
    assert "slope_path_not_covered" in analysis.failure_reasons
    assert "airborne_path_not_covered" in analysis.failure_reasons


def test_report_write_is_valid_json(tmp_path):
    report = {
        "schema_version": 1,
        "analysis": {"passed": True},
        "samples": [],
    }

    path = movement_stress.write_report(report, tmp_path)

    assert path.suffix == ".json"
    assert '"passed": true' in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


def test_yaw_ramp_uses_one_client_clock_schedule_and_one_cleanup_rpc():
    class FakeConsole:
        def __init__(self):
            self.calls = []

        def run(self, code):
            self.calls.append(code)
            return repr("ok")

    console = FakeConsole()
    movement_stress._start_yaw_ramp(console, 0.25, 90.0, 2.0)

    assert len(console.calls) == 1
    start = console.calls[0]
    assert "clock.schedule(_stress_yaw_tick)" in start
    assert "_stress_yaw_elapsed += max(0.0, float(dt))" in start
    assert "nonlocal" not in start  # generated code runs under Python 2.7
    assert "_player.character.yaw = _yaw" in start
    assert "if _player is None" in start
    assert "unschedule(_stress_yaw_tick)" in start
    assert "_stress_yaw_delta = 90.000000000" in start
    assert "set_orientation" not in start

    movement_stress._stop_yaw_ramp(console)

    assert len(console.calls) == 2
    assert "clock.unschedule(_stress_yaw_tick)" in console.calls[1]


def test_client_sample_sweeps_adjacent_reconciliation_history_labels():
    code = movement_stress.CLIENT_SAMPLE

    assert "for _offset in range(-3, 4):" in code
    assert "get_old_movement_data(_loop + _offset)" in code
    assert "'candidate_loop_errors': _candidate_errors" in code
    assert "'matched_error_vector': _matched_error_vector" in code
    assert "'yaw_degrees': round(float(_c.yaw), 6)" in code


def test_exact_block_jump_ring_captures_vector_and_adjacent_loop_errors():
    code = movement_stress.START_BLOCK_SPRINT_JUMP

    assert "for _offset in range(-3, 4):" in code
    assert "'candidate_loop_errors': _candidate_errors" in code
    assert "'matched_error_vector': _matched_error_vector" in code
    assert "manager.scene.send_block_line(" in code
    assert "for _stress_dx, _stress_dy, _stress_dz in (" in code
    assert "_stress_face_supported" in code
    assert "_stress_forward_projection <= -1.5" in code
    assert "_stress_horizontal_distance >= 2.0" in code
    assert "'block_target': _stress_sequence_target" in code
    assert "'block_target_outside_route_hull':" in code
    assert "'block_target_solid_before':" in code
    assert "'block_target_solid':" in code
    assert "_stress_time.clock()" in code
    assert "_stress_time.time()" not in code
    assert "wall_time_ns" not in code


def test_no_block_control_uses_identical_scheduler_with_packet_branch_disabled(
    monkeypatch,
):
    class FakeConsole:
        def __init__(self):
            self.calls = []

        def run(self, code):
            self.calls.append(code)
            return repr("ok")

    console = FakeConsole()
    monkeypatch.setattr(movement_stress.time, "monotonic_ns", lambda: 123_000)

    movement_stress._start_scripted_sequence(
        console,
        "no_block_sprint_jump",
        control_delay_frames=5,
    )

    assert len(console.calls) == 1
    code = console.calls[0]
    assert "_stress_block_action_enabled = bool(0)" in code
    assert "_stress_control_delay_frames = int(5)" in code
    assert "_stress_controller_monotonic_anchor_ns = float(123000)" in code
    assert "__BLOCK_ACTION_ENABLED__" not in code
    assert "__CONTROL_DELAY_FRAMES__" not in code


def test_scripted_frame_clock_is_normalized_from_monotonic_client_delta():
    """Scripted frames must never mix wall time into movement durations."""

    controller_monotonic_ns = 500_100_000_000
    client_clock_anchor = 120.0

    class FakeConsole:
        def run(self, code):
            if code == movement_stress.STOP_BLOCK_SPRINT_JUMP:
                return repr(
                    {
                        "frames": [
                            {"client_clock_seconds": client_clock_anchor},
                            {"client_clock_seconds": client_clock_anchor + 0.016},
                        ],
                        "events": [],
                        "client_clock_anchor": client_clock_anchor,
                        "controller_monotonic_anchor_ns": controller_monotonic_ns,
                    }
                )
            return repr("ok")

    frames = movement_stress._stop_scripted_sequence(
        FakeConsole(),
        "block_sprint_jump",
        segment="block_sprint_jump",
        repeat=1,
    )

    assert [frame["monotonic_ns"] for frame in frames] == [
        500_100_000_000,
        500_116_000_000,
    ]
    assert all("client_clock_seconds" not in frame for frame in frames)


def test_block_sequence_preflight_respawns_when_no_build_face_is_reachable(
    monkeypatch,
):
    states = iter(
        [
            {"dead": False, "wade": True, "target": None},
            {"dead": True, "wade": True, "target": None},
            # CreatePlayer can make the character alive one rendered frame
            # before the native map/player occupancy view is ready to scan.
            {"dead": False, "wade": False, "target": None},
            {"dead": False, "wade": False, "target": (225, 189, 220)},
        ]
    )

    class FakeConsole:
        def __init__(self):
            self.calls = []

        def run(self, code):
            self.calls.append(code)
            if code == movement_stress.BLOCK_TARGET_PROBE:
                return repr(next(states))
            return repr("ok")

    console = FakeConsole()
    monotonic_values = iter((10.0, 10.1, 10.2, 10.3, 10.4))
    monkeypatch.setattr(movement_stress.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(movement_stress.time, "sleep", lambda _seconds: None)

    result = movement_stress._prepare_block_sequence(
        console,
        timeout=2.0,
        poll_interval=0.01,
    )

    assert result == {
        "dead": False,
        "wade": False,
        "target": (225, 189, 220),
        "respawned": True,
    }
    assert movement_stress.REQUEST_RESPAWN in console.calls


def test_block_sequence_preflight_keeps_a_dry_reachable_position():
    class FakeConsole:
        def __init__(self):
            self.calls = []

        def run(self, code):
            self.calls.append(code)
            return repr(
                {"dead": False, "wade": False, "target": (225, 189, 220)}
            )

    console = FakeConsole()
    result = movement_stress._prepare_block_sequence(console)

    assert result["respawned"] is False
    assert movement_stress.REQUEST_RESPAWN not in console.calls


def test_block_sequence_preflight_can_force_a_matched_control_respawn(
    monkeypatch,
):
    states = iter(
        [
            {"dead": False, "wade": False, "target": (225, 189, 220)},
            {"dead": True, "wade": False, "target": None},
            {"dead": False, "wade": False, "target": (225, 189, 220)},
        ]
    )

    class FakeConsole:
        def __init__(self):
            self.calls = []

        def run(self, code):
            self.calls.append(code)
            if code == movement_stress.BLOCK_TARGET_PROBE:
                return repr(next(states))
            return repr("ok")

    console = FakeConsole()
    monotonic_values = iter((20.0, 20.1, 20.2, 20.3))
    monkeypatch.setattr(movement_stress.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(movement_stress.time, "sleep", lambda _seconds: None)

    result = movement_stress._prepare_block_sequence(
        console,
        force_respawn=True,
        timeout=2.0,
        poll_interval=0.01,
    )

    assert result["respawned"] is True
    assert movement_stress.REQUEST_RESPAWN in console.calls


def test_key_pulse_uses_client_clock_and_cleanup_releases_the_key():
    class FakeConsole:
        def __init__(self):
            self.calls = []

        def run(self, code):
            self.calls.append(code)
            return repr("ok")

    console = FakeConsole()
    movement_stress._start_key_pulse(console, "SPACE", 1.35, 0.16, 4.0)

    assert len(console.calls) == 1
    start = console.calls[0]
    assert "clock.schedule(_stress_pulse_tick)" in start
    assert "_StressKey.SPACE" in start
    assert "dispatch_event('on_key_press'" in start
    assert "dispatch_event('on_key_release'" in start
    assert "nonlocal" not in start

    movement_stress._stop_key_pulse(console)

    assert len(console.calls) == 2
    assert "clock.unschedule(_stress_pulse_tick)" in console.calls[1]
    assert "_stress_set_pulse_key(False)" in console.calls[1]


def test_primary_pulse_uses_native_character_input_and_releases_it():
    class FakeConsole:
        def __init__(self):
            self.calls = []

        def run(self, code):
            self.calls.append(code)
            return repr("ok")

    console = FakeConsole()
    movement_stress._start_primary_pulse(console, 1.0, 0.30, 4.0)

    assert len(console.calls) == 1
    start = console.calls[0]
    assert "clock.schedule(_stress_primary_tick)" in start
    assert "character.set_primary_shoot(_stress_primary_down)" in start
    assert "nonlocal" not in start

    movement_stress._stop_primary_pulse(console)

    assert len(console.calls) == 2
    assert "clock.unschedule(_stress_primary_tick)" in console.calls[1]
    assert "_stress_set_primary(False)" in console.calls[1]


def test_stress_catalog_separates_generic_and_engineer_segments():
    segments = {segment.name: segment for segment in movement_stress.DEFAULT_SEGMENTS}

    assert segments["block_build_jump"].tool_id == 5
    assert segments["block_build_jump"].pitch_degrees == 60.0
    assert segments["block_build_jump"].primary_period is not None
    assert segments["block_sprint_jump"].tool_id == 5
    assert segments["block_sprint_jump"].scripted_sequence == "block_sprint_jump"
    assert segments["flying_entity_jump"].tool_id == 29
    assert segments["flying_entity_jump"].primary_period is not None
    assert segments["flying_entity_jump"].primary_duration >= 0.30
    assert segments["flying_entity_jump"].required_class_id == 12
    assert segments["flying_entity_jump"].include_by_default is False
    assert segments["engineer_jetpack_hold"].keys == ("SPACE",)
    assert segments["engineer_jetpack_hold"].duration >= 4.0
    assert segments["engineer_jetpack_hold"].required_class_id == 12
    assert segments["engineer_jetpack_hold"].include_by_default is False
    assert segments["rocketeer_jump_pack_hold"].required_class_id == 2
    assert segments["rocketeer_jump_pack_hold"].required_loadout_tools == (66,)
    assert segments["rocketeer_jetpack2_hold"].required_class_id == 2
    assert segments["rocketeer_jetpack2_hold"].required_loadout_tools == (67,)


def test_client_sample_records_equipment_and_palette_state():
    code = movement_stress.CLIENT_SAMPLE

    assert "'tool_id': int(manager.scene.player.tool_id)" in code
    assert "'block_count': int(_c.block_count)" in code
    assert "'jetpack_active': bool" in code
    assert "'jetpack_fuel': round(float" in code
    assert "'palette_active':" in code


def test_movement_stress_launches_foreground_unless_explicitly_minimized():
    normal = movement_stress.parse_args([])
    minimized = movement_stress.parse_args(["--minimized-client"])

    assert normal.minimized_client is False
    assert minimized.minimized_client is True
    assert normal.class_id == 0


def test_scripted_block_clock_uses_accepted_render_frame_delta():
    assert "_stress_sequence_elapsed += _frame_clock_dt" in (
        movement_stress.START_BLOCK_SPRINT_JUMP
    )
    assert "_stress_sequence_elapsed += max(0.0, float(dt))" not in (
        movement_stress.START_BLOCK_SPRINT_JUMP
    )


def test_engineer_segment_requires_explicit_engineer_class():
    wrong = movement_stress.parse_args(
        ["--segments", "engineer_jetpack_hold", "--class-id", "0"]
    )
    with pytest.raises(ValueError, match="requires --class-id 12"):
        movement_stress._selected_segments(wrong)

    correct = movement_stress.parse_args(
        ["--segments", "engineer_jetpack_hold", "--class-id", "12"]
    )
    selected = movement_stress._selected_segments(correct)
    assert [segment.name for segment in selected] == [
        "engineer_jetpack_hold"
    ]


def test_feature_segments_fail_when_the_requested_action_never_executes():
    rows = [
        sample(0, segment="block_build_jump"),
        sample(1, segment="block_build_jump"),
    ]
    for row in rows:
        row.update(tool_id=5, block_count=200, cycle=1)

    analysis, _segments, _events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert "block_build_jump_action_not_exercised" in analysis.failure_reasons


def test_feature_segment_records_real_ammo_or_block_consumption():
    rows = [
        sample(0, segment="flying_entity_jump"),
        sample(1, segment="flying_entity_jump"),
    ]
    rows[0].update(tool_id=29, block_count=200, cycle=1)
    rows[1].update(tool_id=29, block_count=198, cycle=1)

    analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert "flying_entity_jump_action_not_exercised" not in analysis.failure_reasons
    assert any(
        event["kind"] == "feature_action" and event["count"] == 2
        for event in events
    )


def test_block_sprint_jump_requires_the_exact_target_to_change_air_to_solid():
    rows = [
        sample(0, segment="block_sprint_jump"),
        sample(1, segment="block_sprint_jump"),
    ]
    rows[0].update(
        tool_id=5,
        block_count=200,
        cycle=1,
        block_target=(225, 189, 220),
        block_target_solid_before=False,
        block_target_solid=False,
        block_target_forward_projection=-2.0,
        block_target_horizontal_distance=2.5,
        block_target_outside_route_hull=True,
        block_send_attempted=True,
    )
    rows[1].update(
        tool_id=5,
        block_count=199,
        cycle=1,
        block_target=(225, 189, 220),
        block_target_solid_before=False,
        block_target_solid=True,
        block_target_forward_projection=-2.0,
        block_target_horizontal_distance=2.5,
        block_target_outside_route_hull=True,
        block_send_attempted=True,
    )

    analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert "block_sprint_jump_action_not_exercised" not in analysis.failure_reasons
    assert (
        "block_sprint_jump_map_mutation_not_observed"
        not in analysis.failure_reasons
    )
    assert any(
        event["kind"] == "block_mutation"
        and event["target"] == (225, 189, 220)
        and event["solid_before"] is False
        and event["solid_after"] is True
        and event["outside_route_hull"] is True
        and event["inventory_consumed"] == 1
        for event in events
    )


def test_inventory_change_without_target_map_change_fails_block_gate():
    rows = [
        sample(0, segment="block_sprint_jump"),
        sample(1, segment="block_sprint_jump"),
    ]
    for index, row in enumerate(rows):
        row.update(
            tool_id=5,
            block_count=200 - index,
            cycle=1,
            block_target=(225, 189, 220),
            block_target_solid_before=False,
            block_target_solid=False,
            block_target_forward_projection=-2.0,
            block_target_horizontal_distance=2.5,
            block_target_outside_route_hull=True,
            block_send_attempted=True,
        )

    analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert (
        "block_sprint_jump_map_mutation_not_observed"
        in analysis.failure_reasons
    )
    assert not any(event["kind"] == "block_mutation" for event in events)


def test_block_target_inside_forward_route_hull_fails_isolation_gate():
    rows = [
        sample(0, segment="block_sprint_jump"),
        sample(1, segment="block_sprint_jump"),
    ]
    for index, row in enumerate(rows):
        row.update(
            tool_id=5,
            block_count=200 - index,
            cycle=1,
            block_target=(225, 189, 220),
            block_target_solid_before=False,
            block_target_solid=bool(index),
            block_target_forward_projection=0.5,
            block_target_horizontal_distance=1.0,
            block_target_outside_route_hull=False,
            block_send_attempted=True,
        )

    analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert (
        "block_sprint_jump_target_intersects_route_hull"
        in analysis.failure_reasons
    )
    assert not any(event["kind"] == "block_mutation" for event in events)


def test_engineer_jetpack_segment_requires_activation_and_fuel_drain():
    rows = [
        sample(0, segment="engineer_jetpack_hold", airborne=False),
        sample(1, segment="engineer_jetpack_hold", airborne=True),
    ]
    rows[0].update(jetpack_id=68, jetpack_active=False, jetpack_fuel=100.0)
    rows[1].update(jetpack_id=68, jetpack_active=True, jetpack_fuel=80.0)

    analysis, _segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert "engineer_jetpack_hold_not_activated" not in analysis.failure_reasons
    assert "engineer_jetpack_hold_fuel_not_drained" not in analysis.failure_reasons
    assert any(event["kind"] == "jetpack_activation" for event in events)


def test_engineer_jetpack_segment_fails_without_real_thrust():
    rows = [sample(0, segment="engineer_jetpack_hold")]
    rows[0].update(jetpack_id=68, jetpack_active=False, jetpack_fuel=100.0)

    analysis, _segments, _events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert "engineer_jetpack_hold_not_activated" in analysis.failure_reasons
    assert "engineer_jetpack_hold_fuel_not_drained" in analysis.failure_reasons


def test_reconciliation_counter_deltas_catch_events_between_samples():
    rows = [sample(0, history=30, timer=0.0)]
    rows[0]["reconciliation_snap_count"] = 10
    rows[0]["reconciliation_adjust_count"] = 20
    # By the next 50ms sample a SNAP has already regrown three history rows,
    # and the lerp timer no longer exposes both intervening ADJUST rearms.
    rows.append(sample(1, history=3, timer=0.0))
    rows[1]["reconciliation_snap_count"] = 11
    rows[1]["reconciliation_adjust_count"] = 22

    analysis, segments, events = movement_stress.analyze_stress_samples(
        rows,
        interval=0.05,
    )

    assert analysis.snap_count == 1
    assert analysis.adjust_count == 2
    assert segments[0].snap_count == 1
    assert segments[0].adjust_count == 2
    assert [(event["kind"], event["count"]) for event in events] == [
        ("snap", 1),
        ("adjust", 2),
    ]


def test_foreground_restore_uses_win32_best_effort_console_code():
    class FakeConsole:
        def __init__(self):
            self.calls = []

        def run(self, code):
            self.calls.append(code)
            return repr("ok")

    console = FakeConsole()
    movement_stress._restore_foreground(console)

    assert len(console.calls) == 1
    code = console.calls[0]
    assert "getattr(manager.window, '_hwnd', 0)" in code
    assert "ShowWindow(_stress_hwnd, 9)" in code  # SW_RESTORE
    assert "SetWindowPos(" in code
    assert "BringWindowToTop(_stress_hwnd)" in code
    assert "SetForegroundWindow(_stress_hwnd)" in code
    assert "except Exception:" in code  # non-Windows/no-hwnd is a no-op
