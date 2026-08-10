"""Production/native-physics navigation acceptance for all shipped maps."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json

import pytest

from scripts.bot_map_matrix import shipped_maps, simulate_map


@pytest.mark.parametrize("map_name", shipped_maps())
def test_shipped_map_avoids_bot_stalls_water_traps_and_team_piles(
    map_name: str,
) -> None:
    """Run every VXL through real server bots and authoritative physics."""

    result = asyncio.run(
        simulate_map(
            map_name,
            seed=0,
            seconds=35.0,
            bots=12,
        )
    )

    assert result.passed, json.dumps(
        asdict(result),
        indent=2,
        sort_keys=True,
    )
    assert set(result.traversal_style_by_bot.values()) == {
        "dry",
        "swim",
        "bridge",
    }
    assert not any(result.tactical_swim_jump_decisions_by_bot.values())


@pytest.mark.parametrize("seed", (0, 1, 7, 23, 101))
def test_london_long_water_crossings_reach_a_live_shore(seed: int) -> None:
    """Exercise London's sea, changed banks, and water-step exits long-term."""

    result = asyncio.run(
        simulate_map(
            "London",
            seed=seed,
            seconds=120.0,
            bots=12,
        )
    )

    assert result.passed, json.dumps(
        asdict(result),
        indent=2,
        sort_keys=True,
    )
    assert tuple(
        sorted(result.traversal_style_by_bot.values())
    ).count("dry") == 4
    assert tuple(
        sorted(result.traversal_style_by_bot.values())
    ).count("swim") == 4
    assert tuple(
        sorted(result.traversal_style_by_bot.values())
    ).count("bridge") == 4
    assert not any(result.tactical_swim_jump_decisions_by_bot.values())
