"""Embedded entry point, executed by the retail Python 2.7 main thread."""

def _load_retail_fixes():
    import os
    import sys
    import time

    root = os.path.dirname(os.path.abspath(sys.executable))

    def log(message):
        try:
            with open(os.path.join(root, 'aos_mousefix_loader.log'), 'a') as stream:
                stream.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), message))
        except (IOError, OSError):
            pass

    def load_source(name):
        import types
        path = os.path.join(root, name + '.py')
        module = types.ModuleType(name)
        module.__file__ = path
        with open(path, 'rb') as stream:
            source = stream.read()
        exec(compile(source, path, 'exec'), module.__dict__)
        sys.modules[name] = module
        return module

    try:
        if sys.platform != 'win32' or sys.version_info[:2] != (2, 7):
            log('Unsupported Python runtime; keeping stock behavior.')
            return
        arguments = set(arg.lower().lstrip('+-/') for arg in sys.argv[1:])
        pending = []
        for name, switch in [('aos_mousefix', 'legacymouse'),
                             ('aos_equipmentfix', 'legacyequipment'),
                             ('aos_uifix', 'legacyui'),
                             ('aos_movementfix', 'legacymovement'),
                             ('aos_networkfix', 'legacynetwork')]:
            if getattr(sys, '_' + name + '_loaded', False):
                continue
            if switch in arguments or os.path.isfile(os.path.join(root, name + '.disabled')):
                log('%s: Disabled; stock behavior retained.' % name)
            elif not os.path.isfile(os.path.join(root, name + '.py')):
                log('%s.py missing; stock behavior retained.' % name)
            else:
                pending.append(name)
        if not pending:
            return
        import hashlib
        with open(os.path.join(root, 'aos.pkg'), 'rb') as stream:
            digest = hashlib.sha256(stream.read()).hexdigest()
        if digest != 'c0d0cdc6f61f4b58172f74faf036c6f323b1cdbe59193f595fcce7d2a524e52c':
            log('Unsupported aos.pkg SHA256 %s; keeping stock behavior.' % digest)
            return
        runtime = getattr(sys, '_aos_retail_runtime', None)
        if runtime is None:
            runtime = load_source('aosfix_runtime').Runtime(log)
            sys._aos_retail_runtime = runtime
        for name in pending:
            try:
                if name == 'aos_networkfix':
                    load_source('aos_steam_bridge')
                module = load_source(name)
                module.install(runtime)
                setattr(sys, '_' + name + '_loaded', True)
                log('%s registered; individual patch status: %r' % (name, runtime.status))
            except BaseException:
                import traceback
                log('%s unavailable; game startup continues.\n%s' %
                    (name, traceback.format_exc()))
    except BaseException:
        import traceback
        log('Retail fixes unavailable; game startup continues.\n' + traceback.format_exc())


_load_retail_fixes()
del _load_retail_fixes
