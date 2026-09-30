"""Preserve native jump displacement for the local player on every server.

Python 2.7 runtime patch; no executable bytes or files are rewritten. Walking,
collision, input timing, movement history and authoritative corrections remain
native. Server-authoritative corrections still apply normally.
"""
import ctypes
import gc
import hashlib
import os
import sys
import types
try:
    from thread import get_ident
except ImportError:
    from _thread import get_ident


NATIVE_HASHES = {
    'aoslib.character': '52ec520d83fe9e0ed8338a1038b176c272752e81d65e9f923fe90036aaa107f7',
    'aoslib.world': 'ae45ec007e312c8d650620bc2779169f7b7461c74192b7a7480342c21237c1a0',
}


def check_abi():
    if (sys.platform != 'win32' or sys.version_info[:2] != (2, 7)
            or ctypes.sizeof(ctypes.c_void_p) != 4
            or type(str.__dict__).__name__ != 'dictproxy'):
        raise RuntimeError('Movement hooks require retail CPython 2.7 x86')


def verify_module(module, cls):
    name = module.__name__
    path = os.path.abspath(module.__file__)
    if cls.__module__ != name or not path.lower().endswith('.pyd'):
        raise RuntimeError('Unexpected native movement module: ' + name)
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            block = stream.read(65536)
            if not block:
                break
            digest.update(block)
    if digest.hexdigest() != NATIVE_HASHES[name]:
        raise RuntimeError('Unsupported or already modified movement binary: ' + name)


def native_method(cls, name):
    original = getattr(cls, name)
    function = getattr(original, 'im_func', original)
    if isinstance(function, types.FunctionType) or not callable(original):
        raise RuntimeError('Another patch already replaced ' + name)
    if type(function).__name__ not in ('method_descriptor', 'cython_function_or_method'):
        raise RuntimeError('Unexpected native method: ' + name)
    return original


def set_method(cls, name, value):
    """Replace a verified method, including CPython's static extension types.

    gc exposes the dictproxy's referent without guessed object-memory offsets.
    Always invalidate CPython's type cache after a direct dictionary update.
    """
    try:
        setattr(cls, name, value)
        return
    except (TypeError, AttributeError):
        check_abi()
    proxy = cls.__dict__
    referents = gc.get_referents(proxy)
    if len(referents) != 1 or type(referents[0]) is not dict:
        raise RuntimeError('Native type dictionary is unavailable')
    table = referents[0]
    if name not in table or table[name] is not proxy[name]:
        raise RuntimeError('Unexpected native type dictionary')
    modified = ctypes.pythonapi.PyType_Modified
    modified.argtypes = [ctypes.py_object]
    modified.restype = None
    table[name] = value
    modified(cls)


class JumpFrame(object):
    def __init__(self, character):
        self.character = character
        self.world = character.world_object
        self.updates = 0
        self.pending = False


class JumpFix(object):
    def __init__(self, log):
        self.log = log
        self.thread = get_ident()
        self.active = None
        self.suppressed = 0
        self.frames = 0
        self.physics_steps = 0
        self.journal = []

    def selected(self, character):
        if get_ident() != self.thread or not getattr(character, 'main', False):
            return False
        scene = getattr(character, 'scene', None)
        manager = getattr(scene, 'manager', None)
        player = getattr(scene, 'player', None)
        client = getattr(manager, 'client', None)
        return bool(
            getattr(manager, 'scene', None) is scene
            and getattr(player, 'character', None) is character
            and not getattr(client, 'disconnected', True)
            and getattr(character, 'world_object', None) is not None
        )

    def wrappers(self, alive, update, position):
        fix = self

        def update_alive(character, *args, **kwargs):
            if get_ident() != fix.thread:
                return alive(character, *args, **kwargs)
            previous = fix.active
            fix.active = JumpFrame(character) if fix.selected(character) else None
            try:
                if fix.active is not None:
                    fix.frames += 1
                    if fix.frames == 1:
                        fix.log('movement.jump: observing the local character update.')
                return alive(character, *args, **kwargs)
            finally:
                fix.active = previous

        def update_world(world, *args, **kwargs):
            result = update(world, *args, **kwargs)
            frame = fix.active
            if frame is not None and world is frame.world and get_ident() == fix.thread:
                fix.physics_steps += 1
                frame.updates += 1
                # Only the first live physics step may precede the launch
                # restore. Replay steps must never arm this exception.
                frame.pending = frame.updates == 1 and bool(world.jump_this_frame)
            return result

        def set_position(world, *args, **kwargs):
            frame = fix.active
            if (frame is not None and frame.pending and world is frame.world
                    and get_ident() == fix.thread):
                # The verified native launch block is the first position call
                # after physics. A different call consumes the opportunity too.
                frame.pending = False
                cached = frame.character.network_position
                if len(args) == 3 and not kwargs and args == (cached.x, cached.y, cached.z):
                    fix.suppressed += 1
                    if fix.suppressed == 1:
                        fix.log('movement.jump: first stale launch restore suppressed.')
                    return None
            return position(world, *args, **kwargs)

        return update_alive, update_world, set_position

    def attach(self, character_cls, world_cls, setter=set_method, validator=native_method):
        if self.journal:
            return
        targets = [(character_cls, 'update_alive'), (world_cls, 'update'),
                   (world_cls, 'set_position')]
        originals = [validator(cls, name) for cls, name in targets]
        replacements = self.wrappers(*originals)
        try:
            for (cls, name), replacement in zip(targets, replacements):
                self.journal.append((cls, name, cls.__dict__[name]))
                setter(cls, name, replacement)
        except BaseException:
            self.detach(setter)
            raise

    def detach(self, setter=set_method):
        for cls, name, descriptor in reversed(self.journal):
            setter(cls, name, descriptor)
        self.journal[:] = []
        self.active = None


def install(runtime):
    def character_loaded(module):
        check_abi()
        from aoslib import world
        verify_module(module, module.Character)
        verify_module(world, world.Player)
        fix = JumpFix(runtime.log)
        fix.attach(module.Character, world.Player)
        runtime.movement = fix
        runtime.log('movement.jump: enabled for the local character on all servers.')

    runtime.watch('aoslib.character', 'movement.jump', character_loaded)
