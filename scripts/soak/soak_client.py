"""Join one real (tracer-enabled dev) client to a soak server and idle.

Purpose: prove that nothing kicks a legitimate, mostly idle human except the
intended ``[conduct]`` AFK timer (warn at ``afk_warn_seconds``, kick at
``afk_kick_seconds`` of *alive, in-round* idling). The client is launched with
its tracer console on ``--console-port`` (default 32899, tracer 32900) so it
never collides with the 32896/32897 parity clients, then driven into a spawned
player by ``scripts/auto_join.py``. While holding, the console is polled every
``--poll`` seconds and each observation is appended to
``<out>/client_poll.jsonl``: connected?, scene, player alive, map. After a map
or mode rollover the client lands on team selection again; with
``--rejoin`` the helper re-runs auto_join (what a human would do: pick a team).

The server side of the check lives in the soak harness samples
(``humans_conduct``: idle seconds / warned / grief points) and events
(``player_disconnect_call`` with stack, ``conduct``/``anticheat`` log rows).

Example (soak server already running on 27020)::

    py -3.12 scripts/soak/soak_client.py --server 127.0.0.1:27020 \
        --hold-minutes 12 --out logs/soak/<run>

The client process is always terminated on exit.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scripts.parity_clients import ClientSpec, DEFAULT_CLIENT_DIR, launch_client, stop_client  # noqa: E402
from game_console import GameConsole, ConsoleError  # noqa: E402

POLL_CODE = (
    "c = getattr(manager, 'client', None)\n"
    "s = manager.scene\n"
    "p = getattr(s, 'player', None)\n"
    "gs = getattr(manager, 'game_scene', None)\n"
    "_ = repr({'connected': bool(c) and not c.disconnected,"
    " 'scene': type(s).__name__,"
    " 'menu': type(getattr(manager, 'menu', None)).__name__,"
    " 'player': p is not None,"
    " 'alive': bool(getattr(p, 'dead', True) is False) if p is not None else None,"
    " 'map': getattr(gs, 'map_name', None)})"
)


def run_auto_join(server: str, console_port: int, log_path: Path, wait: float) -> int:
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n==== auto_join {time.strftime('%H:%M:%S')}\n")
        handle.flush()
        return subprocess.call(
            [sys.executable, str(ROOT / "scripts" / "auto_join.py"),
             "--server", server, "--console-port", str(console_port),
             "--wait", str(wait)],
            stdout=handle, stderr=subprocess.STDOUT, cwd=ROOT,
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--server", default="127.0.0.1:27020")
    ap.add_argument("--console-port", type=int, default=32899)
    ap.add_argument("--tracer-port", type=int, default=32900)
    ap.add_argument("--hold-minutes", type=float, default=5.0)
    ap.add_argument("--poll", type=float, default=10.0)
    ap.add_argument("--rejoin", action="store_true",
                    help="re-run auto_join when a rollover leaves the client unspawned")
    ap.add_argument("--client-dir", default=str(DEFAULT_CLIENT_DIR))
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client_dir = Path(args.client_dir)
    spec = ClientSpec(
        index=9,
        client_dir=client_dir,
        python_path=client_dir / "python" / "python.exe",
        connect_target=args.server,
        console_port=args.console_port,
        tracer_port=args.tracer_port,
        capture_dir=out / "client",
        capture_enabled=False,
        minimized=False,
    )
    poll_log = (out / "client_poll.jsonl").open("a", encoding="utf-8")
    join_log = out / "client_autojoin.log"
    process = launch_client(spec)
    print("client pid", process.pid, flush=True)
    t0 = time.monotonic()

    def record(**row):
        row.update({"t": round(time.monotonic() - t0, 1), "wall": time.time()})
        poll_log.write(json.dumps(row) + "\n")
        poll_log.flush()
        print(row, flush=True)

    try:
        code = run_auto_join(args.server, args.console_port, join_log, 180.0)
        record(event="auto_join", exit=code)
        console = GameConsole(port=args.console_port)
        console.connect(wait_seconds=30.0)
        joined_at = time.monotonic()
        unspawned_since = None
        while time.monotonic() - joined_at < args.hold_minutes * 60.0:
            if process.poll() is not None:
                record(event="client_exited", code=process.returncode)
                break
            try:
                state = console.run(POLL_CODE)
            except (ConsoleError, OSError) as exc:
                record(event="poll_error", error=repr(exc))
                try:
                    console = GameConsole(port=args.console_port)
                    console.connect(wait_seconds=10.0)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(args.poll)
                continue
            record(event="poll", state=state)
            spawned = "'player': True" in state and "'connected': True" in state
            if not spawned:
                unspawned_since = unspawned_since or time.monotonic()
                if (args.rejoin and "'connected': True" in state
                        and time.monotonic() - unspawned_since > 20.0):
                    code = run_auto_join(args.server, args.console_port, join_log, 120.0)
                    record(event="rejoin", exit=code)
                    unspawned_since = None
            else:
                unspawned_since = None
            time.sleep(args.poll)
    finally:
        stop_client(process)
        record(event="client_stopped", code=process.returncode)
        poll_log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
