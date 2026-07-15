"""Measure the live client's reconciliation outcome while it walks.

Ground truth: docs/PROTOCOL.md (recovered from the native character module).
Each tick the client runs apply_player_network_correction():

    md = get_old_movement_data(network_position_loop_count)   # EXACT loop match
    d2 = |server_pos - md.position|^2                          # squared, no sqrt
    d2 > 16.0   -> SNAP   (set position, movement_history = [])   "rollback"
    d2 > 0.01   -> ADJUST (set position, position_lerp_timer = 0.1) "chunky"
    else        -> NO-OP  (butter)

We can't hook the function, so we watch its observable side effects:
  * SNAP   -> movement_history collapses to ~0 entries
  * ADJUST -> position_lerp_timer jumps to ~0.1
  * d2     -> compare network_position (last server row) vs the client's
              CURRENT position, which bounds the error the client is seeing.

Usage:
    py scripts/recon_monitor.py --seconds 12 --key W
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from game_console import GameConsole  # noqa: E402

SNAP_D2 = 16.0
ADJUST_D2 = 0.010000000000000002

PRESS = """from pyglet.window import key as K
manager.keyboard[K.{key}] = True
manager.window.dispatch_event('on_key_press', K.{key}, 0)
_ = 'down'"""

RELEASE = """from pyglet.window import key as K
manager.keyboard[K.{key}] = False
manager.window.dispatch_event('on_key_release', K.{key}, 0)
_ = 'up'"""

# One round-trip per sample: history length, lerp timer, server row, our position.
# NOTE: cast every int through float() — the client is Python 2 and reprs longs
# as `0L`, which is a SyntaxError to eval() on the py3 side.
SAMPLE = (
    "_c = manager.scene.player.character\n"
    "_w = _c.world_object.position\n"
    "_n = _c.network_position\n"
    "_ = (float(len(_c.movement_history)), round(float(_c.position_lerp_timer), 4),\n"
    "     float(_c.network_position_loop_count),\n"
    "     round(_w[0],4), round(_w[1],4), round(_w[2],4),\n"
    "     round(_n[0],4), round(_n[1],4), round(_n[2],4))"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--key", default="W")
    ap.add_argument("--console-port", type=int, default=32896)
    ap.add_argument("--interval", type=float, default=0.05)
    args = ap.parse_args()

    c = GameConsole(port=args.console_port, timeout=15)
    scene = c.run("repr(manager.scene.__class__.__name__)")
    if "GameScene" not in scene:
        print("client not in game (scene=%s)" % scene)
        return 1

    c.run(PRESS.format(key=args.key))
    samples = []
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        try:
            samples.append(eval(c.run(SAMPLE)))
        except Exception as exc:  # console hiccup shouldn't abort the run
            print("sample failed: %r" % (exc,))
        time.sleep(args.interval)
    c.run(RELEASE.format(key=args.key))

    if not samples:
        print("no samples")
        return 1

    snaps = adjusts = 0
    prev_hist = None
    prev_timer = 0.0
    max_d2 = 0.0
    over_adjust = 0
    loops = sorted({int(s[2]) for s in samples})
    print("network_position_loop_count seen: %s%s" % (
        loops[:4], " ... %s" % loops[-2:] if len(loops) > 6 else ""))
    if loops == [0]:
        print("!! client never accepted a self-row stamp (loop_count stuck at 0)")
    for (hist, timer, loop, wx, wy, wz, nx, ny, nz) in samples:
        hist = int(hist)
        # SNAP wipes movement_history
        if prev_hist is not None and prev_hist > 8 and hist <= 1:
            snaps += 1
        # ADJUST (re)arms the lerp timer
        if timer > prev_timer + 1e-6:
            adjusts += 1
        prev_hist, prev_timer = hist, timer
        # error the client is carrying vs the last server row it accepted
        if (nx, ny, nz) != (0.0, 0.0, 0.0):
            d2 = (nx - wx) ** 2 + (ny - wy) ** 2 + (nz - wz) ** 2
            max_d2 = max(max_d2, d2)
            if d2 > ADJUST_D2:
                over_adjust += 1

    n = len(samples)
    hist_lens = [s[0] for s in samples]
    print("samples            : %d over %.1fs" % (n, args.seconds))
    print("movement_history   : min=%d max=%d (SNAP wipes it to 0)" % (min(hist_lens), max(hist_lens)))
    print("SNAPs  (rollback)  : %d" % snaps)
    print("ADJUSTs (chunky)   : %d" % adjusts)
    print("max |server-client|: %.4f blocks  (d2=%.5f)" % (max_d2 ** 0.5, max_d2))
    print("samples over ADJUST threshold (0.1 block): %d/%d" % (over_adjust, n))
    verdict = "BUTTER" if (snaps == 0 and adjusts == 0) else ("CHUNKY" if snaps == 0 else "ROLLBACK")
    print("VERDICT            : %s" % verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
