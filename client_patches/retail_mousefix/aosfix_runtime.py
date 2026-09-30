"""Python 2.7 patch coordinator: module observers and atomic replacements.

Internal API only. The bootstrap owns feature selection and build validation.
"""
import sys
import traceback

API_VERSION = 1
MISSING = object()


class Runtime(object):
    def __init__(self, log):
        self.log = log
        self.pending = {}
        self.status = {}
        self.applied = set()

    def watch(self, name, tag, callback):
        if tag in self.status:
            return
        self.status[tag] = 'waiting'
        module = sys.modules.get(name)
        if module is not None:
            self._apply(module, tag, callback)
            return
        self.pending.setdefault(name, []).append((tag, callback))
        if self not in sys.meta_path:
            sys.meta_path.insert(0, self)

    def _apply(self, module, tag, callback):
        try:
            callback(module)
        except Exception:
            self.status[tag] = 'failed'
            self.log('%s skipped: %s' % (tag, traceback.format_exc()))
        else:
            self.status[tag] = 'active'
            self.log('%s active.' % tag)

    def find_module(self, fullname, path=None):
        return self if fullname in self.pending else None

    def load_module(self, fullname):
        # Keep observing OTHER watched modules imported by this module.
        callbacks = self.pending.pop(fullname)
        try:
            __import__(fullname)
        except BaseException:
            self.pending[fullname] = callbacks
            raise
        module = sys.modules[fullname]
        for tag, callback in callbacks:
            self._apply(module, tag, callback)
        if not self.pending and self in sys.meta_path:
            sys.meta_path.remove(self)
        return module

    def replace(self, tag, changes):
        """Commit (owner, name, expected value, replacement) as one unit."""
        if tag in self.applied:
            return
        for owner, name, expected, replacement in changes:
            actual = getattr(owner, name, MISSING)
            # Python 2 returns a new unbound method for each class lookup.
            if getattr(actual, 'im_func', actual) is not getattr(expected, 'im_func', expected):
                raise RuntimeError('%s changed during patch setup' % name)
        saved = []
        try:
            for owner, name, expected, replacement in changes:
                previous = owner.__dict__.get(name, MISSING)
                setattr(owner, name, replacement)
                saved.append((owner, name, previous))
        except BaseException:
            for owner, name, value in reversed(saved):
                if value is MISSING:
                    delattr(owner, name)
                else:
                    setattr(owner, name, value)
            raise
        self.applied.add(tag)


def method(owner, name):
    value = getattr(owner, name)
    if not callable(value):
        raise TypeError('%s must be callable' % name)
    return value
