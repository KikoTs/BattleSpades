"""Headless check against the actual retail world.pyd on Python 2.7 x86.

Usage: python -B smoke_movement.py PATH_TO_UNPACKED_CLIENT
The Character shell reproduces only the independently identified launch
statement. This is native-physics integration, not a full game/network test.
"""
from __future__ import print_function
import ctypes
import math
import os
import sys
import time
import types

import aos_movementfix as movement


class Box(object):
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def vector(v):
    return (v.x, v.y, v.z)


def main(root):
    movement.check_abi()
    root = os.path.abspath(root)
    ctypes.windll.kernel32.SetDllDirectoryW(unicode(root))
    sys.path.insert(0, root)
    # Skip aoslib.__init__, which initializes graphics/fonts. Physics needs
    # neither a display nor an active game, and this is a fresh test process.
    package = types.ModuleType('aoslib')
    package.__path__ = [os.path.join(root, 'aoslib')]
    sys.modules['aoslib'] = package
    from aoslib import world
    from aoslib.vxl import VXL
    from shared.glm import Vector3
    movement.verify_module(world, world.Player)
    print('Loading native test terrain.')
    # The original VXL loads asynchronously. Supply complete VXL bytes and
    # wait for its loader; set_point requires renderer state absent headlessly.
    color = '\x00\xff\x00\x7f'
    raw = '\x00\xef\xef\x00' + color + ('\x00\x3e\x3e\x00' + color) * (512 * 512 - 1)
    terrain = VXL(1, raw, len(raw), 2)
    deadline = time.clock() + 10.
    while not terrain.done_processing():
        if time.clock() >= deadline:
            raise RuntimeError('Native test terrain did not load')
        time.sleep(.01)
    print('Creating native bodies.')
    space = world.World(terrain)

    def body():
        p = space.create_object(world.Player)
        p.set_dead(False)
        p.set_position(100.5, 100.5, 59.75)
        for field, value in [('accel_multiplier', .7), ('sprint_multiplier', 1.4),
                             ('jump_multiplier', 1.2), ('crouch_sneak_multiplier', .5),
                             ('water_friction', 8.), ('can_sprint_uphill', True)]:
            getattr(p, 'set_class_' + field)(value)
        p.set_orientation(Vector3(1., 0., 0.))
        return p

    class Character(object):
        def update_alive(self, dt, players):
            result = self.world_object.update(dt, players)
            if self.world_object.jump_this_frame:
                n = self.network_position
                self.world_object.set_position(n.x, n.y, n.z)
            return result

    original_update = world.Player.update
    descriptors = (world.Player.__dict__['update'], world.Player.__dict__['set_position'])
    reference, hooked = body(), body()
    character = Character()
    character.main = True
    character.world_object = hooked
    character.network_position = Vector3(100.5, 100.5, 59.75)
    character.scene = Box(player=Box(character=character))
    character.scene.manager = Box(scene=character.scene, client=Box(
        ip='127.0.0.1', port=28630, disconnected=False))
    fix = movement.JumpFix(print)
    fix.attach(Character, world.Player, validator=getattr)
    print('Native method hooks installed.')
    jumps = 0
    try:
        for frame in range(480):
            space.update(1./60)
            angle = max(0, frame - 180) * .04
            for p in (reference, hooked):
                p.set_orientation(Vector3(math.cos(angle), math.sin(angle), 0.))
                p.set_walk(frame >= 30, False, False, 240 <= frame < 300)
                p.sprint = 90 <= frame < 150
                p.jump = (frame % 60) in (35, 36, 37)
                p.set_crouch(300 <= frame < 350, [], 0)
            original_update(reference, 1./60, [])
            character.update_alive(1./60, [])
            jumps += bool(reference.jump_this_frame)
            assert vector(hooked.position) == vector(reference.position), frame
            assert vector(hooked.velocity) == vector(reference.velocity), frame
            assert hooked.airborne == reference.airborne, frame
        assert jumps >= 3, jumps
        assert fix.suppressed == jumps, (fix.suppressed, jumps)
        print('480 native frames matched exactly; %d launch restores suppressed.' % jumps)
        # A teleport outside the launch boundary must still take effect.
        hooked.set_position(101., 102., 59.75)
        assert vector(hooked.position) == (101., 102., 59.75)
    finally:
        fix.detach()
    assert world.Player.__dict__['update'] is descriptors[0]
    assert world.Player.__dict__['set_position'] is descriptors[1]
    print('Native method descriptors restored; ordinary position updates passed.')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
