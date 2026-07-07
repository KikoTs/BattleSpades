# -*- coding: utf-8 -*-
"""testbot/run.py — Python 2.7 32-bit only.

Usage:
    py2 testbot/run.py --scenario <name> [--port 27015] [--host 127.0.0.1]
                        [--name TestBot] [--list]

Stdout: one JSON event per line (machine-readable; the harness consumes this).
Stderr: human-readable bot log.
Exit:    0 on scenario success, 1 on failure.
"""
from __future__ import print_function

import argparse
import sys
import traceback

# Make sibling imports work when invoked as `py2 testbot/run.py`
import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from testbot.client import Client, EventLog, TimeoutError  # noqa: E402
from testbot import scenarios  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--scenario', help='scenario name (see --list)')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=27015)
    ap.add_argument('--name', default='TestBot')
    ap.add_argument('--list', action='store_true', help='list scenarios and exit')
    args = ap.parse_args(argv)

    if args.list:
        for name in scenarios.names():
            mod = scenarios.get(name)
            print('{:<24} timeout={:>5}s  {}'.format(
                name, getattr(mod, 'TIMEOUT', '?'),
                (mod.__doc__ or '').strip().splitlines()[0] if mod.__doc__ else ''))
        return 0

    if not args.scenario:
        ap.error('--scenario is required (use --list to see available)')

    mod = scenarios.get(args.scenario)
    if mod is None:
        ap.error('unknown scenario: {} (use --list)'.format(args.scenario))

    log = EventLog()
    log.emit('bot_start',
             scenario=args.scenario,
             host=args.host,
             port=args.port,
             name=args.name,
             python=sys.version.splitlines()[0])

    client = Client(host=args.host, port=args.port, name=args.name, log=log)
    try:
        mod.script(client)
        log.emit('scenario_done', result='ok')
        log.emit('exit', code=0)
        return 0
    except TimeoutError as e:
        log.emit('scenario_failed', kind='timeout', error=str(e))
        log.emit('exit', code=1)
        try:
            client.disconnect(drain=0.2)
        except Exception:
            pass
        return 1
    except Exception as e:
        log.emit('scenario_failed', kind='exception',
                 error='{}: {}'.format(type(e).__name__, e),
                 traceback=traceback.format_exc().splitlines()[-8:])
        log.emit('exit', code=1)
        try:
            client.disconnect(drain=0.2)
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
