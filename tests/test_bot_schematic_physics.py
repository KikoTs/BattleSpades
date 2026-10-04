"""Bounded offline gates: bots build schematics with native physics and BlockLines."""

import asyncio

import pytest

from scripts.bot_schematic_build_physics import simulate


def _simulate_with_one_retry(scenario):
    # The simulation's CPU clock is fixed (see simulate), but bots and their
    # planner worker still interleave a little differently from run to run,
    # and about one run in ten finished a cell or a climb late. A real
    # regression fails both attempts; one unlucky interleaving does not.
    result = asyncio.run(simulate(scenario, seconds=75.0))
    if result.failures:
        result = asyncio.run(simulate(scenario, seconds=75.0))
    return result


@pytest.mark.parametrize("scenario", ["vip_shelter", "watchtower"])
def test_bots_build_schematics_together_with_authoritative_block_lines(scenario):
    result = _simulate_with_one_retry(scenario)
    assert not result.failures, (result.failures, result.events, result.trace[-6:])
    assert result.authoritative_cells_present == result.plan_cells
    assert sum(result.blocks_spent.values()) == result.plan_cells
    assert len(result.cells_by_builder) >= 2
    # Mostly drags: at least three cells by line for every single block.
    assert result.line_cells >= 3 * result.single_cells
    assert result.completion_seconds <= 60
