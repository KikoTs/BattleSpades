"""Run a matched retail block-transition versus no-block control.

Both phases start from the validation server's respawn point with yaw zero and
use the same per-rendered-frame scheduler.  The control waits exactly as many
frames as the real BlockLine needed to commit, then performs the same
sprint-next-frame and jump-next-frame edges without sending a block packet.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


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
    StressSegment,
    StressThresholds,
    _auto_join,
    _prepare_block_sequence,
    analyze_stress_samples,
    collect_segment,
)


CORRECTION_KINDS = {
    "snap",
    "adjust",
    "visible_teleport",
    "visible_rollback",
    "visible_vertical_snap",
}


def block_commit_frame(rows: Sequence[Mapping[str, object]]) -> int:
    """Return the rendered frame where the exact air-to-solid commit landed."""

    if not rows:
        raise RuntimeError("block A/B produced no block rows")
    events = rows[-1].get("sequence_events", [])
    for event in events:  # type: ignore[assignment]
        if event.get("name") == "block_committed":
            return int(event["frame"])
    raise RuntimeError("block A/B never observed block_committed")


def _vector_delta(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


def _correction_signature(
    samples: Sequence[Mapping[str, object]],
    events: Sequence[Mapping[str, object]],
    segment: str,
) -> list[dict]:
    rows = [row for row in samples if str(row.get("segment", "")) == segment]
    if not rows:
        return []
    first_loop = int(rows[0]["client_loop"])
    signature: list[dict] = []
    for event in events:
        if event.get("kind") not in CORRECTION_KINDS:
            continue
        if str(event.get("segment", "")) != segment:
            continue
        sample_index = int(event["sample_index"])
        row = samples[sample_index]
        signature.append({
            "kind": str(event["kind"]),
            "count": int(event.get("count", 1)),
            "relative_frame": int(row["client_loop"]) - first_loop + 1,
            "client_loop": int(row["client_loop"]),
            "network_loop": int(row["network_loop"]),
            "sequence_phase": str(row.get("sequence_phase", "")),
            "matched_loop_error": event.get(
                "matched_loop_error",
                row.get("matched_loop_error"),
            ),
            "matched_error_vector": row.get("matched_error_vector"),
            "airborne": bool(row.get("airborne")),
            "wade": bool(row.get("wade")),
        })
    return signature


def build_comparison(
    samples: Sequence[Mapping[str, object]],
    events: Sequence[Mapping[str, object]],
    *,
    block_commit_frame: int,
) -> dict:
    """Build aligned A/B evidence without hiding either phase's corrections."""

    block_rows = [
        row for row in samples
        if str(row.get("segment", "")) == "block_sprint_jump"
    ]
    control_rows = [
        row for row in samples
        if str(row.get("segment", "")) == "no_block_sprint_jump"
    ]
    if not block_rows or not control_rows:
        raise RuntimeError("matched block A/B requires both sample streams")
    return {
        "block_commit_frame": int(block_commit_frame),
        "control_delay_frames": int(block_commit_frame),
        "start_position_delta": _vector_delta(
            block_rows[0]["position"],  # type: ignore[arg-type]
            control_rows[0]["position"],  # type: ignore[arg-type]
        ),
        "start_orientation_delta": _vector_delta(
            block_rows[0]["orientation"],  # type: ignore[arg-type]
            control_rows[0]["orientation"],  # type: ignore[arg-type]
        ),
        "start_yaw_delta": abs(
            float(block_rows[0].get("yaw_degrees", 0.0))
            - float(control_rows[0].get("yaw_degrees", 0.0))
        ),
        "block_target": block_rows[0].get("block_target"),
        "block_target_forward_projection": block_rows[0].get(
            "block_target_forward_projection"
        ),
        "block_target_horizontal_distance": block_rows[0].get(
            "block_target_horizontal_distance"
        ),
        "block_target_outside_route_hull": bool(
            block_rows[0].get("block_target_outside_route_hull")
        ),
        "block_corrections": _correction_signature(
            samples,
            events,
            "block_sprint_jump",
        ),
        "control_corrections": _correction_signature(
            samples,
            events,
            "no_block_sprint_jump",
        ),
        "block_edges": list(block_rows[-1].get("sequence_events", [])),
        "control_edges": list(control_rows[-1].get("sequence_events", [])),
    }


def run_ab(
    console: GameConsole,
    *,
    duration: float = 12.0,
    interval: float = 0.05,
) -> dict:
    """Execute the real mutation and matched no-packet control in one client."""

    if duration <= 0 or interval <= 0:
        raise ValueError("duration and interval must be positive")
    started_at = datetime.now(timezone.utc)
    template = next(
        segment for segment in DEFAULT_SEGMENTS
        if segment.name == "block_sprint_jump"
    )

    # A newly launched validation client is already at the fixed spawn.  If a
    # caller reuses a client at water, the preflight performs the normal dry
    # respawn before we freeze yaw for the measured phase.
    _prepare_block_sequence(console)
    console.run("manager.scene.player.character.yaw=0.0;_='ab-yaw-zero'")
    block_segment = replace(template, duration=duration)
    block_rows = collect_segment(
        console,
        block_segment,
        interval=interval,
        repeat=1,
    )
    commit_frame = block_commit_frame(block_rows)

    # The control must start from the same server spawn, not wherever the
    # twelve-second block route ended.  The placed voxel is behind the forward
    # route hull, so leaving it in the map cannot intersect control movement.
    _prepare_block_sequence(console, force_respawn=True)
    console.run("manager.scene.player.character.yaw=0.0;_='ab-yaw-zero'")
    control_segment = StressSegment(
        name="no_block_sprint_jump",
        duration=duration,
        tool_id=5,
        pitch_degrees=60.0,
        scripted_sequence="no_block_sprint_jump",
        control_delay_frames=commit_frame,
    )
    control_rows = collect_segment(
        console,
        control_segment,
        interval=interval,
        repeat=1,
    )

    samples = [*block_rows, *control_rows]
    thresholds = StressThresholds()
    analysis, segment_analysis, events = analyze_stress_samples(
        samples,
        interval=interval,
        thresholds=thresholds,
    )
    tracer = ast.literal_eval(
        console.run(
            "{'session_id': str(state.session_id), "
            "'capture_path': state.capture_path, "
            "'capture_on': bool(state.capture_on), "
            "'tick_count': int(state.tick_count)}"
        )
    )
    return {
        "schema_version": 1,
        "scenario": "block_transition_ab",
        "created_at": started_at.isoformat(),
        "configuration": {
            "duration_seconds": duration,
            "interval_seconds": interval,
            "block_segment": asdict(block_segment),
            "control_segment": asdict(control_segment),
            "thresholds": asdict(thresholds),
        },
        "tracer": tracer,
        "analysis": asdict(analysis),
        "segment_analysis": [asdict(row) for row in segment_analysis],
        "correction_events": events,
        "feature_evidence": {
            "block_mutations": [
                dict(event) for event in events
                if event.get("kind") == "block_mutation"
            ],
        },
        "comparison": build_comparison(
            samples,
            events,
            block_commit_frame=commit_frame,
        ),
        "samples": samples,
    }


def write_report(report: Mapping[str, object], artifact_dir: Path) -> Path:
    """Atomically publish one matched block-transition artifact."""

    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = artifact_dir / f"block-transition-ab-{stamp}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="127.0.0.1:27016")
    parser.add_argument("--console-port", type=int, default=33020)
    parser.add_argument("--tracer-port", type=int, default=33021)
    parser.add_argument("--duration", type=float, default=12.0)
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument("--artifact-dir", type=Path, default=ROOT / "logs" / "block-transition-ab")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--client-dir", type=Path, default=DEFAULT_CLIENT_DIR)
    parser.add_argument("--wait", type=float, default=120.0)
    parser.add_argument("--team", type=int, default=2)
    parser.add_argument("--class-id", type=int, default=0)
    parser.add_argument("--no-auto-join", action="store_true")
    parser.add_argument("--keep-client", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    process = None
    try:
        if args.launch:
            spec = ClientSpec(
                index=1,
                client_dir=args.client_dir,
                python_path=args.client_dir / "python" / "python.exe",
                connect_target=args.server,
                console_port=args.console_port,
                tracer_port=args.tracer_port,
                capture_dir=args.artifact_dir / "client-1",
                capture_enabled=False,
                stack_sampler_enabled=False,
                minimized=False,
            )
            process = launch_client(spec)
            if not args.no_auto_join:
                _auto_join(
                    args.console_port,
                    args.server,
                    args.team,
                    args.class_id,
                    args.wait,
                    (),
                )

        console = GameConsole(port=args.console_port, timeout=15.0)
        console.connect(wait_seconds=args.wait)
        try:
            report = run_ab(
                console,
                duration=args.duration,
                interval=args.interval,
            )
        finally:
            console.close()
        path = write_report(report, args.artifact_dir)
        print(f"artifact: {path}")
        print(json.dumps(report["comparison"], indent=2, sort_keys=True))
        print(json.dumps(report["analysis"], indent=2, sort_keys=True))
        return 0 if report["analysis"]["passed"] else 1  # type: ignore[index]
    finally:
        if process is not None and not args.keep_client:
            stop_client(process)


if __name__ == "__main__":
    raise SystemExit(main())
