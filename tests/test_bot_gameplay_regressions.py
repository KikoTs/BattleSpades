"""Regressions found by full-roster native gameplay audits."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

from server.bot_ai.director import BotDirector
from server.bot_ai.messages import ObjectiveSnapshot
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState, _Goal
from server.bot_ai.schematics import get
from server.bot_ai.schematics.sites import find_schematic_site
from tests.test_bot_project_sites import make_world
from tests.test_simple_bot_tactics import _frame, _player
from tests.test_construction_actions import _server


def test_mode_owned_population_does_not_starve_every_perception_and_motor_tick():
    director = object.__new__(BotDirector)
    director.server = SimpleNamespace(mode=SimpleNamespace(owns_bot_population=True))
    director._reconnect_count = None
    director._next_population_at = 0.0
    asyncio.run(director._maintain_population(100.0))
    assert director._next_population_at == 101.0


def test_published_construction_exclusions_match_the_authoritative_gate():
    server = _server()
    zone = SimpleNamespace(xy_bounds=lambda: (20, 30, 20, 30))
    server.world_manager.map_metadata = SimpleNamespace(spawn_zones={2: [zone]}, base_zones={})
    server.mode = SimpleNamespace(base_bounds={3: (50, 60, 50, 60)},
                                  intel_positions={2: (80.5, 80.5, 10)})
    bounds, circles = server.construction.protected_areas()
    assert bounds == ((50, 60, 50, 60), (19, 31, 19, 31))
    assert circles == ((80.5, 80.5, 2),)
    assert server.construction._protected((19, 31, 10))
    assert not server.construction._protected((18, 31, 10))
    assert server.construction._protected((82, 80, 10))
    assert not server.construction._protected((83, 80, 10))


def test_schematic_planner_relocates_or_rejects_an_exact_protected_spawn_footprint():
    world = make_world(thick=True)
    protected = ((20, 28, 16, 24),)
    choice = find_schematic_site(world.solid, get("pillbox"), (25.5, 20.5, 17.75),
                                 keep_out_bounds=protected)
    assert choice is None or all(not (20 <= x <= 28 and 16 <= y <= 24)
                                 for x, y, _z in choice.placement.cells)


def test_public_buried_vip_can_be_breached_downward_but_hidden_roster_is_not_a_marker():
    world = make_world(thick=True)
    bot = _player(1, 2, (25.5, 20.5, 17.75), is_bot=True)
    target = (25.5, 20.5, 24.75)
    goal = _Goal(("vip", 3), target, "vip_flank_attack", 2, True)
    frame = _frame(bot, objectives=(ObjectiveSnapshot("vip", 3, target, carrier_id=9),))
    brain = SimpleBotBrain(world)
    state = _BotState(1, 1, 0)
    intent = brain._buried_objective_breach(frame, bot, state, goal, 100)
    assert intent is not None and intent.debug_role.endswith(":route_breach")
    assert intent.action.position[2] >= 20
    assert brain._buried_objective_breach(replace(frame, objectives=()), bot, state, goal, 100) is None
    assert brain._buried_objective_breach(frame, replace(bot, can_dig=False), state, goal, 100) is None


def test_descent_refuses_a_shaft_without_a_safe_solid_landing():
    world = make_world(thick=True)
    bot = _player(1, 2, (25.5, 20.5, 17.75), is_bot=True)
    # The only surface is a thin slab over a deep void.
    world._vxl.solids = {cell for cell in world._vxl.solids if cell[2] == 20 or cell[2] >= 40}
    goal = _Goal(("vip", 3), (25.5, 20.5, 28.75), "vip_flank_attack", 2, True)
    frame = _frame(bot, objectives=(ObjectiveSnapshot("vip", 3, goal.position, carrier_id=9),))
    assert SimpleBotBrain(world)._buried_objective_breach(frame, bot, _BotState(1, 1, 0), goal, 100) is None


def test_crouched_descent_aims_at_the_visible_floor_instead_of_the_layer_below_it():
    world = make_world(thick=True)
    bot = _player(1, 2, (25.5, 20.5, 18.65), is_bot=True)
    goal = _Goal(("vip", 3), (25.5, 20.5, 24.75), "vip_flank_attack", 2, True)
    frame = _frame(bot, objectives=(ObjectiveSnapshot("vip", 3, goal.position, carrier_id=9),))
    intent = SimpleBotBrain(world)._buried_objective_breach(frame, bot, _BotState(1, 1, 0), goal, 100)
    assert intent is not None
    assert intent.action.position[2] == 20.5


def test_sixteen_native_bots_reach_a_buried_last_vip(tmp_path):
    root = Path(__file__).resolve().parents[1]
    report = tmp_path / "buried-vip.json.gz"
    env = dict(os.environ)
    env.pop("BOT_AUDIT_ROOT", None)
    result = subprocess.run([sys.executable, "scripts/bot_audit_harness.py", "--map", "ArcticBase",
        "--mode", "vip", "--seed", "0", "--bots", "16", "--seconds", "180",
        "--scenario", "buried", "--out", str(report)], cwd=root, env=env,
        capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(gzip.decompress(report.read_bytes()))
    assert not data["error"] and data["source_unchanged_during_run"]
    challenge = data["scenario"]
    assert challenge["bots"] == 16 and challenge["target_team_size"] == 1
    assert challenge["target_started_alive"]
    assert challenge["first_damage_s"] < challenge["target_defeated_s"] < 180
    assert challenge["structure_cells_removed"] > 0


def test_native_audit_rejects_a_caught_and_logged_lifecycle_error(tmp_path):
    root = Path(__file__).resolve().parents[1]
    report = tmp_path / "logged-error.json.gz"
    program = """
import asyncio
import logging
from pathlib import Path
import sys
from scripts.bot_audit_harness import run_case, uninstall_clock

async def scenario(server, director, clock, state):
    logging.getLogger('modes.test').error('injected lifecycle failure')

try:
    result = asyncio.run(run_case(map_name='Classic', mode_name='tdm', seed=0,
        seconds=1, bots=4, out=Path(sys.argv[1]), scenario=scenario))
finally:
    uninstall_clock()
assert 'injected lifecycle failure' in result['error']
assert result['runtime_errors']
"""
    env = dict(os.environ)
    env.pop("BOT_AUDIT_ROOT", None)
    result = subprocess.run([sys.executable, "-c", program, str(report)],
        cwd=root, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(gzip.decompress(report.read_bytes()))
    assert "injected lifecycle failure" in data["error"]
