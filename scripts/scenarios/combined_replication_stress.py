"""Run block->sprint->jump while a second retail client emits projectiles.

This is the interaction gate missing from the older serial stress scenario.
Client A remains foreground and records every rendered movement frame. Client B
uses an independent tracer/console endpoint and continuously fires real
Snowblower entities before, during, and after A's block transition.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from game_console import GameConsole  # noqa: E402
from parity_clients import (  # noqa: E402
    DEFAULT_CLIENT_DIR,
    ClientSpec,
    launch_client,
    stop_client,
)
from scenarios.movement_stress import (  # noqa: E402
    DEFAULT_SEGMENTS,
    _auto_join,
    _read_mapping,
    _start_primary_pulse,
    _stop_primary_pulse,
    run_scenario,
    write_report,
)


def parse_args(argv=None) -> argparse.Namespace:
    """Parse isolated two-retail-client stress options."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="127.0.0.1:27016")
    parser.add_argument("--duration", type=float, default=12.0)
    parser.add_argument("--wait", type=float, default=120.0)
    parser.add_argument("--mover-team", type=int, default=2)
    parser.add_argument("--emitter-team", type=int, default=3)
    parser.add_argument("--client-dir", type=Path, default=DEFAULT_CLIENT_DIR)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=ROOT / "logs" / "combined-replication",
    )
    return parser.parse_args(argv)


def _specs(args: argparse.Namespace) -> tuple[ClientSpec, ClientSpec]:
    python_path = args.client_dir / "python" / "python.exe"
    return (
        ClientSpec(
            index=1,
            client_dir=args.client_dir,
            python_path=python_path,
            connect_target=args.server,
            console_port=32896,
            tracer_port=32895,
            capture_dir=args.artifact_dir / "client-1",
            capture_enabled=False,
            stack_sampler_enabled=False,
            minimized=False,
        ),
        ClientSpec(
            index=2,
            client_dir=args.client_dir,
            python_path=python_path,
            connect_target=args.server,
            console_port=32897,
            tracer_port=32898,
            capture_dir=args.artifact_dir / "client-2",
            capture_enabled=False,
            stack_sampler_enabled=False,
            minimized=True,
        ),
    )


def main(argv=None) -> int:
    """Launch, drive, validate, and clean up two owned retail processes."""

    args = parse_args(argv)
    if args.duration <= 0:
        raise ValueError("duration must be positive")
    mover_spec, emitter_spec = _specs(args)
    processes = []
    mover_console = emitter_console = None
    try:
        processes = [launch_client(mover_spec), launch_client(emitter_spec)]
        # Spawn order is part of this test: the mover must receive the first
        # deterministic validation spawn and run toward the emitter at the
        # second. Concurrent joins made that assignment nondeterministic and
        # occasionally turned the intended contact test into players moving
        # away from one another.
        _auto_join(
            mover_spec.console_port,
            args.server,
            args.mover_team,
            0,
            args.wait,
            (),
        )
        _auto_join(
            emitter_spec.console_port,
            args.server,
            args.emitter_team,
            12,
            args.wait,
            (29,),
        )

        mover_console = GameConsole(port=mover_spec.console_port, timeout=15.0)
        emitter_console = GameConsole(port=emitter_spec.console_port, timeout=15.0)
        mover_console.connect(wait_seconds=args.wait)
        emitter_console.connect(wait_seconds=args.wait)

        emitter_console.run(
            "manager.scene.player.set_tool(29, True);"
            "manager.scene.player.character.pitch=-35.0;_='emitter-ready'"
        )
        emitter_before = _read_mapping(emitter_console)
        _start_primary_pulse(
            emitter_console,
            period=0.75,
            down_for=0.35,
            total=args.duration + 2.0,
        )

        template = next(
            segment
            for segment in DEFAULT_SEGMENTS
            if segment.name == "block_sprint_jump"
        )
        report = run_scenario(
            mover_console,
            segments=(replace(template, duration=args.duration),),
            repeats=1,
            interval=0.05,
        )
        emitter_after = _read_mapping(emitter_console)
        consumed = max(
            0,
            int(emitter_before.get("block_count", 0))
            - int(emitter_after.get("block_count", 0)),
        )
        report["scenario"] = "combined_replication_stress"
        report["emitter"] = {
            "before": emitter_before,
            "after": emitter_after,
            "resource_consumed": consumed,
            "tool_id": int(emitter_after.get("tool_id", -1)),
        }
        if consumed <= 0:
            failures = report["analysis"]["failure_reasons"]
            failures.append("projectile_emitter_action_not_exercised")
            report["analysis"]["passed"] = False

        path = write_report(report, args.artifact_dir)
        print(f"artifact: {path}")
        print(json.dumps(report["analysis"], indent=2, sort_keys=True))
        return 0 if report["analysis"]["passed"] else 1
    finally:
        if emitter_console is not None:
            try:
                _stop_primary_pulse(emitter_console)
            except Exception:
                pass
            emitter_console.close()
        if mover_console is not None:
            mover_console.close()
        for process in reversed(processes):
            stop_client(process)


if __name__ == "__main__":
    raise SystemExit(main())
