"""Regress against vectors executed from shipped x86, not our implementation.

Regenerate with scripts/reverse_movement_core.py and the SHA-pinned retail PE.
These vectors stop before collision; collision has separate branch tests.
"""
import json
from pathlib import Path

import pytest

from aoslib.vxl import VXL
from aoslib.world import Player, World


FIXTURE = json.loads(
    (Path(__file__).parent / 'fixtures' / 'retail_movement_core.json').read_text()
)


@pytest.fixture(scope='module')
def water_shelf():
    terrain = VXL(-1, b'', 0, 2)
    for x in range(98, 104):
        for y in range(98, 104):
            terrain.set_point(x, y, 240, True, 0x7f00ff00)
    return World(terrain)


@pytest.mark.parametrize('case', FIXTURE['cases'])
def test_velocity_and_jump_flags_match_original_x86(case, water_shelf):
    body = Player(water_shelf)
    body.set_orientation((1.0, 0.0, 0.0))
    if case['wade']:
        # Set read-only contact flags using an actual submerged landing,
        # then move to empty space so collision cannot alter the test vector.
        body.set_position(100.5, 100.5, 235.0)
        for _ in range(100):
            body.update(1 / 60, [])
        assert body.wade and not body.airborne
    body.set_position(100.5, 100.5, 100.0)
    if case['airborne']:
        body.update(case['dt'], [])
    assert body.airborne == case['airborne']
    assert body.wade == case['wade']
    body.set_crouch(case.get('crouch', False), [], 0)
    body.set_position(100.5, 100.5, 100.0)
    body.set_velocity(*case['velocity'])
    body.set_class_accel_multiplier(0.7)
    body.set_class_sprint_multiplier(1.4)
    body.set_class_jump_multiplier(1.2)
    body.set_class_crouch_sneak_multiplier(0.5)
    body.set_class_water_friction(8.0)
    body.jetpack = 65 + case['pack']
    body.jetpack_active = case['active']
    body.jetpack_passive = case['passive']
    body.parachute = int(case['parachute'])
    body.parachute_active = case['parachute_active']
    for name in ('jump', 'hover', 'sneak', 'sprint', 'burdened'):
        setattr(body, name, case.get(name, False))
    body.set_walk(*(case.get(name, False) for name in ('up', 'down', 'left', 'right')))
    body.update(case['dt'], [])
    # Exact float32 values, deliberately no approximation tolerance.
    assert list(body.velocity) == case['expected']['velocity']
    assert body.jump == case['expected']['jump']
    assert body.jump_this_frame == case['expected']['jump_this_frame']
