"""Windows-spawn smoke test for the isolated bot bridge.

Run with ``py -3.12 scripts/bot_worker_smoke.py``.  The script intentionally
uses an empty VXL: perception must fail closed yet the worker should still
return a harmless patrol intention and shut down without orphaning a process.
"""

from __future__ import annotations

import argparse
import os
import signal
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.bot_ai.messages import MapSnapshot, PerceptionFrame, PlayerSnapshot
from server.bot_ai.profiles import ProfileFactory
from server.bot_ai.supervisor import AIWorkerSupervisor
from server.game_constants import DEFAULT_WEAPON_TOOL


def _player(player_id: int, team: int, x: float, *, bot: bool) -> PlayerSnapshot:
    return PlayerSnapshot(
        player_id=player_id,
        generation=1,
        team=team,
        class_id=0,
        alive=True,
        spawned=True,
        position=(x, 0.0, 0.0),
        eye=(x, 0.0, 0.0),
        orientation=(1.0, 0.0, 0.0),
        velocity=(0.0, 0.0, 0.0),
        health=100,
        tool=DEFAULT_WEAPON_TOOL,
        blocks=50,
        ammo_clip=10,
        ammo_reserve=50,
        is_bot=bot,
    )


def main(*, restart: bool = False) -> int:
    """Start, exercise, and cleanly reap one spawned worker."""

    supervisor = AIWorkerSupervisor(seed=11)
    supervisor.start(MapSnapshot(1, 0, b"", "tdm", "smoke"))
    deadline = time.monotonic() + 8.0
    try:
        while time.monotonic() < deadline and not supervisor.status().running:
            time.sleep(0.02)
        if not supervisor.status().running:
            raise RuntimeError("AI worker did not start")
        original_pid = supervisor.status().process_id
        if restart:
            if original_pid is None:
                raise RuntimeError("worker has no process id")
            # The PID came from this supervisor; never enumerate or terminate
            # unrelated Python processes in this acceptance probe.
            os.kill(original_pid, signal.SIGTERM)
            restart_deadline = time.monotonic() + 10.0
            while time.monotonic() < restart_deadline:
                status = supervisor.status()
                if (
                    status.running
                    and status.restarts >= 1
                    and status.process_id is not None
                    and status.process_id != original_pid
                ):
                    break
                time.sleep(0.02)
            else:
                raise RuntimeError(
                    f"AI worker did not restart: {supervisor.status()}"
                )

        observer = _player(1, 2, 0.0, bot=True)
        enemy = _player(2, 3, 10.0, bot=False)
        supervisor.submit_frame(
            PerceptionFrame(
                frame_id=1,
                map_epoch=1,
                mode_epoch=1,
                topology_version=0,
                observer_id=1,
                observer_generation=1,
                created_at=time.monotonic(),
                mode_id="tdm",
                players=(observer, enemy),
                profile=ProfileFactory(seed=11).create("normal"),
            )
        )
        while time.monotonic() < deadline:
            intents = supervisor.drain_intents()
            if intents:
                intent = intents[0]
                print(
                    "worker_ok",
                    f"pid={supervisor.status().process_id}",
                    f"restarts={supervisor.status().restarts}",
                    f"intent={intent.action.kind.value}",
                )
                return 0
            time.sleep(0.02)
        raise RuntimeError("AI worker returned no intention")
    finally:
        supervisor.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--restart", action="store_true", help="terminate and verify one supervised restart"
    )
    args = parser.parse_args()
    raise SystemExit(main(restart=args.restart))
