from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "scripts" / "scenarios" / "palette_stability.py"
SPEC = importlib.util.spec_from_file_location(
    "palette_stability_scenario",
    SCENARIO_PATH,
)
assert SPEC is not None and SPEC.loader is not None
palette_stability = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = palette_stability
SPEC.loader.exec_module(palette_stability)


def _sample(phase: str, color: tuple[int, int, int] = (159, 0, 0)) -> dict:
    return {
        "phase": phase,
        "owner": {
            "color": color,
            "tool": 5,
            "palette_active": True,
            "selector_active": True,
        },
        "observer": {"color": color, "tool": 5},
    }


def test_parse_packet_trace_decodes_palette_flag_tool_and_bgr_color():
    lines = [
        "2026-07-12 13:06:57 [DEBUG] server.connection: "
        "RECV packet_id=11 (SetColor) len=5 hex=0B 00 00 00 9F "
        "from 127.0.0.1:62013\n",
        "2026-07-12 13:10:37 [DEBUG] server.connection: "
        "RECV packet_id=4 (ClientData) len=18 "
        "hex=04 34 70 00 00 80 05 00 40 00 00 00 00 09 00 18 00 00 "
        "from 127.0.0.1:62013\n",
    ]

    trace = palette_stability.parse_packet_trace_lines(lines)

    assert trace["set_colors"] == [
        {
            "direction": "RECV",
            "endpoint": "127.0.0.1:62013",
            "player_id": 0,
            "rgb": (159, 0, 0),
            "wire_hex": "0B 00 00 00 9F",
        }
    ]
    assert trace["client_data"] == [
        {
            "direction": "RECV",
            "endpoint": "127.0.0.1:62013",
            "raw_player_id": 128,
            "player_id": 0,
            "palette_enabled": True,
            "tool": 5,
            "wire_hex": (
                "04 34 70 00 00 80 05 00 40 00 00 00 00 09 00 18 00 00"
            ),
        }
    ]


def test_analysis_accepts_stable_owner_observer_and_reconnect():
    expected = (159, 0, 0)
    report = {
        "selection_colors": [(95, 0, 0), expected],
        "samples": [
            _sample("stand"),
            _sample("walk"),
            _sample("jump"),
        ],
        "reconnect": {"color": expected, "tool": 5},
        "packet_trace": {
            "set_colors": [
                {"direction": "RECV", "rgb": (95, 0, 0)},
                {"direction": "RECV", "rgb": expected},
            ],
            "client_data": [
                {
                    "direction": "RECV",
                    "palette_enabled": True,
                    "tool": 5,
                }
            ],
        },
    }

    analysis = palette_stability.analyze_palette_report(report)

    assert analysis["passed"] is True
    assert analysis["failure_reasons"] == []
    assert analysis["expected_color"] == expected
    assert analysis["phases"] == ["jump", "stand", "walk"]


def test_analysis_rejects_walk_color_drift_even_if_reconnect_matches():
    expected = (159, 0, 0)
    report = {
        "selection_colors": [(95, 0, 0), expected],
        "samples": [
            _sample("stand"),
            _sample("walk", (47, 47, 47)),
            _sample("jump"),
        ],
        "reconnect": {"color": expected, "tool": 5},
        "packet_trace": {
            "set_colors": [
                {"direction": "RECV", "rgb": (95, 0, 0)},
                {"direction": "RECV", "rgb": expected},
            ],
            "client_data": [
                {
                    "direction": "RECV",
                    "palette_enabled": True,
                    "tool": 5,
                }
            ],
        },
    }

    analysis = palette_stability.analyze_palette_report(report)

    assert analysis["passed"] is False
    assert "owner color drift in walk" in analysis["failure_reasons"]
