"""Bounded offline gates: production bots must really walk what they build."""

import asyncio

import pytest

from scripts.bot_cooperative_project_physics import simulate_project


@pytest.mark.parametrize("kind,committed", [("bridge", 4), ("breach", 18)])
def test_miner_and_partner_cross_using_authoritative_native_physics(kind, committed):
    result = asyncio.run(simulate_project(kind))
    assert not result.failures, (result.failures, result.events, result.trace)
    assert result.simulated_seconds <= 24
    assert result.metrics["tasks_completed"] == 1
    assert result.metrics["routes_used_by_partner"] == 1
    assert len(result.project_cells) == len(result.committed_cells) == committed
    assert set(result.project_cells) == {(x, y, z) for x, y, z, _ in result.committed_cells}
    assert all(solid == (kind == "bridge") for x, y, z, solid in result.committed_cells)
    assert all(distance > 7 for distance in result.distance_walked.values())
    assert any(role.startswith("squad_advance") for role in result.roles)
    if kind == "bridge":
        assert result.blocks_before - result.blocks_after == committed
        assert result.metrics["actions_confirmed"] == 1
    else:
        assert result.actions["melee"] > 0
        assert result.blocks_after - result.blocks_before == committed
