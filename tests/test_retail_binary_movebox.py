"""Exact voxel-collision regressions generated from the shipped x86 code.

Regenerate with scripts/reverse_movebox.py and the SHA-pinned original PE.
Normal test runs require neither that proprietary binary nor an emulator.
"""
import json
import struct
from pathlib import Path

import pytest

from aoslib import world
from shared.glm import Vector3


FIXTURE = json.loads(
    (Path(__file__).parent / 'fixtures' / 'retail_movebox.json').read_text()
)


class SparseMap:
    def __init__(self, voxels):
        self.voxels = {tuple(point) for point in voxels}

    def get_solid(self, x, y, z):
        return (x, y, z) in self.voxels


TERRAINS = {name:SparseMap(points) for name,points in FIXTURE['terrain'].items()}


@pytest.mark.parametrize('case', FIXTURE['cases'])
def test_voxel_collision_matches_original_x86(case):
    position = Vector3(*case['position'])
    velocity = Vector3(*case['velocity'])
    step = struct.unpack('<f', struct.pack('<f', case['dt']))[0]
    climbed, _, airborne, wade, crouch = world._move_box(
        position, velocity, step, TERRAINS[case['terrain']],
        case['crouch'], case['hover'], case['sprint'], case['can_uphill'],
        case['airborne'], case['wade'],
    )
    actual = dict(position=list(position), velocity=list(velocity), crouch=crouch,
                  airborne=airborne, wade=case['wade'] if wade is None else wade,
                  climbed=climbed)
    assert actual == case['expected']
