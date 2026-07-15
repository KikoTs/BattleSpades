from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "scripts" / "scenarios" / "block_transition_ab.py"
SPEC = importlib.util.spec_from_file_location("block_transition_ab_scenario", SCENARIO_PATH)
assert SPEC is not None and SPEC.loader is not None
block_transition_ab = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = block_transition_ab
SPEC.loader.exec_module(block_transition_ab)


def _row(segment: str, loop: int, position=(224.5, 189.5, 219.75)) -> dict:
    return {
        "segment": segment,
        "cycle": 1,
        "client_loop": loop,
        "network_loop": loop - 2,
        "position": position,
        "orientation": (1.0, 0.0, 0.0),
        "yaw_degrees": 0.0,
        "sequence_phase": "sustained",
        "matched_loop_error": 0.0,
        "matched_error_vector": (0.0, 0.0, 0.0),
        "airborne": False,
        "wade": False,
    }


def test_comparison_aligns_corrections_to_each_segment_frame_clock():
    samples = [
        _row("block_sprint_jump", 100),
        _row("block_sprint_jump", 101),
        _row("no_block_sprint_jump", 200),
        _row("no_block_sprint_jump", 201),
    ]
    events = [
        {
            "kind": "adjust",
            "segment": "block_sprint_jump",
            "sample_index": 1,
            "count": 1,
            "matched_loop_error": 0.2,
        },
        {
            "kind": "adjust",
            "segment": "no_block_sprint_jump",
            "sample_index": 3,
            "count": 1,
            "matched_loop_error": 0.1,
        },
    ]

    comparison = block_transition_ab.build_comparison(
        samples,
        events,
        block_commit_frame=3,
    )

    assert comparison["block_commit_frame"] == 3
    assert comparison["control_delay_frames"] == 3
    assert comparison["start_position_delta"] == 0.0
    assert comparison["start_yaw_delta"] == 0.0
    assert comparison["block_corrections"][0]["relative_frame"] == 2
    assert comparison["control_corrections"][0]["relative_frame"] == 2
    assert comparison["block_corrections"][0]["matched_loop_error"] == 0.2


def test_commit_frame_requires_a_real_block_commit_event():
    rows = [_row("block_sprint_jump", 100)]
    rows[-1]["sequence_events"] = [{"name": "block_sent", "frame": 1}]

    with pytest.raises(RuntimeError, match="block_committed"):
        block_transition_ab.block_commit_frame(rows)

