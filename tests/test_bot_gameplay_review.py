"""Audit outcomes must describe gameplay, not a process exiting successfully."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from scripts.bot_audit_harness import instrument_mode
from scripts.bot_gameplay_matrix import cases
from scripts.bot_gameplay_review import review


def test_full_matrix_covers_every_playable_family_and_true_full_roster_targets():
    selected = cases(scope="all", seeds=(0, 7, 19), maps_per_mode=1, targets=True)
    modes = {case.mode for case in selected if not case.scenario}
    assert len(modes) == 35
    assert {"tdm", "ctf", "cctf", "vip", "zom", "czom", "arena"} <= modes
    assert len([code for code in modes if code.startswith("hc_")]) == 10
    challenges = [case for case in selected if case.scenario]
    assert len(challenges) == 81
    assert {c.scenario for c in challenges} == {"rooftop", "hidden", "water", "buried",
        "highroof", "skybridge", "stacked", "moving", "repairing"}
    assert all(c.map_name == "ArcticBase" for c in challenges)


def test_no_damage_or_no_objective_progress_is_not_a_gameplay_pass():
    assert review({"mode": "tdm", "simulated_seconds": 360})["gameplay_issues"] == ["no combat kills"]
    assert review({"mode": "dia", "simulated_seconds": 360})["gameplay_issues"] == ["no diamond cashed in"]
    challenge = {"mode": "czom", "scenario": {"kind": "rooftop", "target_started_alive": True}}
    assert review(challenge)["gameplay_issues"] == ["target remains undefeated"]


def test_old_reports_without_custom_telemetry_are_not_reported_as_failed_races():
    assert review({"mode": "crumbling_run", "simulated_seconds": 360})["gameplay_issues"] == []


def test_only_successful_diamond_cash_ins_are_counted():
    class Mode:
        def __init__(self):
            self.server = SimpleNamespace(teams={2: SimpleNamespace(score=0)})

        async def _cash_in_loose(self, accepted):
            if accepted:
                self.server.teams[2].score += 1
            return accepted

    mode, events = Mode(), []
    instrument_mode(mode, events, lambda: 1.0)
    assert not asyncio.run(mode._cash_in_loose(False))
    assert asyncio.run(mode._cash_in_loose(True))
    assert len(events) == 1
