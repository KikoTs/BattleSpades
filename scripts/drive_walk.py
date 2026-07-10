"""Drive a REAL walk through the live client's own input pipeline.

Presses/releases a movement key via pyglet's event dispatch so the input flows
through the client's normal path (and therefore into ClientData -> the server),
exactly as a human keypress would. Used to reproduce/measure the walk-rollback
desync (task R1) with the server's input-drain telemetry.

Usage:
    py scripts/drive_walk.py --seconds 10 --key W [--console-port 32896]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from game_console import GameConsole  # noqa: E402

PRESS = """from pyglet.window import key as K
manager.keyboard[K.{key}] = True
manager.window.dispatch_event('on_key_press', K.{key}, 0)
_ = 'down'"""

RELEASE = """from pyglet.window import key as K
manager.keyboard[K.{key}] = False
manager.window.dispatch_event('on_key_release', K.{key}, 0)
_ = 'up'"""

POS = ("_p = manager.scene.player.get_world_object().position\n"
       "_ = (round(_p[0], 3), round(_p[1], 3), round(_p[2], 3))")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--key", default="W")
    ap.add_argument("--console-port", type=int, default=32896)
    ap.add_argument("--tag", default="r1_walk")
    args = ap.parse_args()

    c = GameConsole(port=args.console_port, timeout=15)
    print("scene:", c.run("repr(manager.scene.__class__.__name__)"))
    print("pos before:", c.run(POS))
    c.run("repr(tag(%r))" % args.tag)

    c.run(PRESS.format(key=args.key))
    print("walking %.1fs with %s ..." % (args.seconds, args.key))
    time.sleep(args.seconds)
    c.run(RELEASE.format(key=args.key))

    time.sleep(0.4)
    print("pos after :", c.run(POS))
    c.run("repr(tag(''))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
