"""Peer collision vectors executed from the SHA-pinned original x86 binary."""
import json
from pathlib import Path

import pytest

from aoslib import world


FIXTURE = json.loads(
    (Path(__file__).parent / 'fixtures' / 'retail_peer_collisions.json').read_text()
)


@pytest.mark.parametrize('case', FIXTURE['cases'])
def test_peer_collision_matches_original_x86(case):
    player = world.Player(None)
    player.set_crouch(case.get('crouch', False), (), 0)
    player.hover = case.get('hover', False)
    player.set_position(*case['position'])
    player.set_velocity(*case['velocity'])
    player.set_dead(not case.get('alive', True))
    player.set_exploded(case.get('exploded', False))
    count = world._collide_with_players(player, case['peers'], case['dt'], case['resolve'])
    assert dict(velocity=list(player.velocity), count=count) == case['expected']
