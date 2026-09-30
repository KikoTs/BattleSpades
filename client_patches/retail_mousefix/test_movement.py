"""Jump-boundary regressions, compatible with Python 2.7 and Python 3."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aos_movementfix as movement


class Box(object):
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def fixture():
    class World(object):
        def __init__(self):
            self.position = (10., 20., 30.)
            self.velocity = (0., 0., 0.)
            self.jump_this_frame = False
            self.want_jump = True
            self.positions = []
        def update(self, dt, players):
            self.position = (10.1, 20., 29.8)
            self.jump_this_frame = self.want_jump
            self.velocity = (0.1, 0., -0.4)
            return 'physics'
        def set_position(self, x, y, z):
            self.positions.append((x, y, z))
            self.position = (x, y, z)
            return 'position'

    class Character(object):
        def __init__(self):
            self.main = True
            self.world_object = World()
            self.network_position = Box(x=9., y=20., z=30.)
            self.scene = Box(player=Box(character=self))
            self.scene.manager = Box(scene=self.scene, client=Box(
                ip='127.0.0.1', port=28630, disconnected=False))
            self.before = None
            self.after = None
            self.corrections = 0
        def update_alive(self, dt, players):
            if self.before:
                self.before()
            self.world_object.update(dt, players)
            if self.after:
                self.after()
            if self.world_object.jump_this_frame:
                n = self.network_position
                self.world_object.set_position(n.x, n.y, n.z)
            else:
                self.corrections += 1
                self.world_object.set_position(9., 20., 30.)
            return 'alive'
    return Character, World


class JumpTests(unittest.TestCase):
    def setUp(self):
        self.Character, self.World = fixture()
        self.originals = [cls.__dict__[name] for cls, name in (
            (self.Character, 'update_alive'), (self.World, 'update'), (self.World, 'set_position'))]
        self.logs = []
        self.fix = movement.JumpFix(self.logs.append)
        self.fix.attach(self.Character, self.World, validator=getattr)
        self.character = self.Character()

    def step(self):
        return self.character.update_alive(1./60, [])

    def test_launch_keeps_post_physics_displacement_and_velocity(self):
        self.assertEqual(self.step(), 'alive')
        self.assertEqual(self.character.world_object.position, (10.1, 20., 29.8))
        self.assertEqual(self.character.world_object.velocity, (0.1, 0., -0.4))
        self.assertEqual(self.fix.suppressed, 1)
        self.assertEqual(len(self.logs), 2)
        self.step()
        self.assertEqual(len(self.logs), 2)

    def test_walking_keeps_native_reconciliation(self):
        self.character.world_object.want_jump = False
        self.step()
        self.assertEqual(self.character.world_object.position, (9., 20., 30.))
        self.assertEqual(self.character.corrections, 1)
        self.assertEqual(self.fix.suppressed, 0)

    def test_any_server_is_supported_without_endpoint_list(self):
        self.character.scene.manager.client.ip = '192.0.2.9'
        self.step()
        self.assertEqual(self.character.world_object.position, (10.1, 20., 29.8))

    def test_remote_character_is_untouched(self):
        self.character.main = False
        self.step()
        self.assertEqual(self.fix.suppressed, 0)

    def test_stale_scene_is_untouched(self):
        self.character.scene.manager.scene = Box()
        self.step()
        self.assertEqual(self.fix.suppressed, 0)

    def test_previous_life_is_untouched(self):
        self.character.scene.player.character = Box()
        self.step()
        self.assertEqual(self.fix.suppressed, 0)

    def test_disconnected_client_is_untouched(self):
        self.character.scene.manager.client.disconnected = True
        self.step()
        self.assertEqual(self.fix.suppressed, 0)

    def test_pre_physics_and_later_position_updates_remain_native(self):
        world = self.character.world_object
        self.character.before = lambda: world.set_position(9., 20., 30.)
        self.step()
        self.assertEqual(world.positions, [(9., 20., 30.)])
        self.assertEqual(world.set_position(9., 20., 30.), 'position')
        self.assertEqual(world.position, (9., 20., 30.))

    def test_different_position_call_consumes_the_exception(self):
        world = self.character.world_object
        self.character.after = lambda: world.set_position(100., 200., 300.)
        self.step()
        self.assertEqual(world.positions, [(100., 200., 300.), (9., 20., 30.)])
        self.assertEqual(self.fix.suppressed, 0)

    def test_keyword_position_is_not_mistaken_for_native_restore(self):
        world = self.character.world_object
        self.character.after = lambda: world.set_position(x=9., y=20., z=30.)
        self.step()
        self.assertEqual(len(world.positions), 2)
        self.assertEqual(self.fix.suppressed, 0)

    def test_replay_step_disarms_launch_exception(self):
        self.character.after = lambda: self.character.world_object.update(1./60, [])
        self.step()
        self.assertEqual(self.fix.suppressed, 0)

    def test_other_world_cannot_arm_or_consume_local_exception(self):
        other = self.World()
        self.character.before = lambda: other.update(1./60, [])
        self.character.after = lambda: other.set_position(9., 20., 30.)
        self.step()
        self.assertEqual(self.fix.suppressed, 1)
        self.assertEqual(other.position, (9., 20., 30.))

    def test_nested_remote_call_restores_local_scope(self):
        other = self.Character()
        other.main = False
        self.character.after = lambda: other.update_alive(1./60, [])
        self.step()
        self.assertEqual(self.fix.suppressed, 1)
        self.assertEqual(other.world_object.position, (9., 20., 30.))

    def test_exception_restores_scope(self):
        def fail():
            raise ValueError('original failure')
        self.character.after = fail
        with self.assertRaises(ValueError):
            self.step()
        self.assertIsNone(self.fix.active)
        self.assertEqual(self.character.world_object.set_position(9., 20., 30.), 'position')

    def test_unsupported_thread_does_not_suppress_restore(self):
        self.fix.thread = -1
        self.step()
        self.assertEqual(self.fix.suppressed, 0)

    def test_detach_restores_exact_descriptors(self):
        self.fix.detach()
        for expected, (cls, name) in zip(self.originals, (
                (self.Character, 'update_alive'), (self.World, 'update'), (self.World, 'set_position'))):
            self.assertIs(cls.__dict__[name], expected)
        self.step()
        self.assertEqual(self.character.world_object.position, (9., 20., 30.))

    def test_partial_install_rolls_back(self):
        self.fix.detach()
        calls = []
        def fail_once(cls, name, value):
            setattr(cls, name, value)
            calls.append(name)
            if len(calls) == 2:
                raise ValueError('failure after mutation')
        with self.assertRaises(ValueError):
            self.fix.attach(self.Character, self.World, setter=fail_once, validator=getattr)
        self.assertIs(self.World.__dict__['update'], self.originals[1])
        self.assertIs(self.Character.__dict__['update_alive'], self.originals[0])
        self.assertEqual(self.fix.journal, [])

    def test_rejects_previously_wrapped_method(self):
        with self.assertRaises(RuntimeError):
            movement.native_method(self.Character, 'update_alive')


if __name__ == '__main__':
    unittest.main(verbosity=2)
