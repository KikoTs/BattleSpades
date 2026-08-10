"""Cross-round/map bot lifecycle acceptance."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json

from scripts.bot_transition_soak import run_transition_soak


def test_retained_server_survives_repeated_map_mode_and_round_boundaries() -> None:
    result = asyncio.run(
        run_transition_soak(
            maps=("London", "MayanJungle"),
            modes=("tdm", "zom"),
            cycles=1,
            games_per_session=4,
            bots=4,
            settle_ticks=4,
        )
    )

    assert result.passed, json.dumps(asdict(result), indent=2, sort_keys=True)
    assert result.session_count == 4
    assert result.game_boundaries == 15
    assert result.clean_slate_resets == 5
