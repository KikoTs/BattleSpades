"""Bounded offline gates: bots build schematics with native physics and BlockLines."""

import asyncio

import pytest

from scripts.bot_schematic_build_physics import simulate


@pytest.mark.parametrize("scenario", ["vip_shelter", "watchtower"])
def test_bots_build_schematics_together_with_authoritative_block_lines(scenario):
    result = asyncio.run(simulate(scenario, seconds=75.0))
    assert not result.failures, (result.failures, result.events, result.trace[-6:])
    assert result.authoritative_cells_present == result.plan_cells
    assert sum(result.blocks_spent.values()) == result.plan_cells
    assert len(result.cells_by_builder) >= 2
    # Mostly drags: at least three cells by line for every single block.
    assert result.line_cells >= 3 * result.single_cells
    assert result.completion_seconds <= 60
