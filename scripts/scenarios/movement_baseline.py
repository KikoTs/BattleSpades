"""Drive and measure the client's real movement reconciliation pipeline."""

from __future__ import annotations

import argparse
import ast
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from game_console import GameConsole  # noqa: E402
from parity_artifact import ParityArtifact  # noqa: E402


PRESS_KEY = """from pyglet.window import key as K
manager.keyboard[K.{key}] = True
manager.window.dispatch_event('on_key_press', K.{key}, 0)
_ = 'down'"""

RELEASE_KEY = """from pyglet.window import key as K
manager.keyboard[K.{key}] = False
manager.window.dispatch_event('on_key_release', K.{key}, 0)
_ = 'up'"""

LOCAL_SAMPLE = """_c = manager.scene.player.character
_w = _c.world_object.position
_n = _c.network_position
_old = _c.get_old_movement_data(_c.network_position_loop_count)
_matched_error = None
if _old is not None:
    _hp = _old[0].position
    _matched_error = ((_n[0]-_hp[0])**2 + (_n[1]-_hp[1])**2 + (_n[2]-_hp[2])**2)**0.5
_ = {'history_length': int(len(_c.movement_history)),
     'lerp_timer': round(float(_c.position_lerp_timer), 6),
     'network_loop': int(_c.network_position_loop_count),
     'client_loop': int(manager.scene.loop_count),
     'matched_loop_error': None if _matched_error is None else round(float(_matched_error), 6),
     'position': tuple(round(float(_w[i]), 6) for i in range(3)),
     'network_position': tuple(round(float(_n[i]), 6) for i in range(3))}"""

OBSERVER_SAMPLE = """_s = manager.scene
_ = {'scene': _s.__class__.__name__,
     'client_loop': int(getattr(_s, 'loop_count', 0)),
     'player_count': int(len(getattr(_s, 'players', {})))}"""


@dataclass(frozen=True)
class MovementAnalysis:
    sample_count: int
    snap_count: int
    adjust_count: int
    unmatched_count: int
    max_matched_loop_error: float
    passed: bool


def analyze_movement_samples(
    samples: Iterable[Mapping[str, object]],
    *,
    error_limit: float = 0.1,
) -> MovementAnalysis:
    """Classify observable reconciliation side effects and prediction error."""
    snapshots = list(samples)
    snap_count = 0
    adjust_count = 0
    unmatched_count = 0
    max_error = 0.0
    previous_history: int | None = None
    previous_timer: float | None = None

    for sample in snapshots:
        history = int(sample["history_length"])
        timer = float(sample["lerp_timer"])
        error = sample.get("matched_loop_error")

        if previous_history is not None and previous_history > 1 and history <= 1:
            snap_count += 1
        if previous_timer is not None and timer > previous_timer + 1e-6:
            adjust_count += 1
        if error is None:
            unmatched_count += 1
        else:
            max_error = max(max_error, float(error))

        previous_history = history
        previous_timer = timer

    passed = bool(snapshots) and not snap_count and not adjust_count
    passed = passed and max_error <= float(error_limit)
    return MovementAnalysis(
        sample_count=len(snapshots),
        snap_count=snap_count,
        adjust_count=adjust_count,
        unmatched_count=unmatched_count,
        max_matched_loop_error=max_error,
        passed=passed,
    )


def _read_mapping(console: GameConsole, code: str) -> dict:
    value = ast.literal_eval(console.run(code))
    if not isinstance(value, dict):
        raise TypeError(f"console sample was not a mapping: {value!r}")
    return value


def _set_keys(console: GameConsole, keys: tuple[str, ...], pressed: bool) -> None:
    template = PRESS_KEY if pressed else RELEASE_KEY
    for key in keys:
        console.run(template.format(key=key))


def collect_segment(
    console_a: GameConsole,
    console_b: GameConsole,
    artifact: ParityArtifact,
    *,
    name: str,
    keys: tuple[str, ...],
    duration: float,
    interval: float,
) -> list[dict]:
    """Drive one input segment and capture local and observer state together."""
    samples: list[dict] = []
    console_a.run("repr(tag(%r))" % name)
    _set_keys(console_a, keys, True)
    deadline = time.monotonic() + duration
    try:
        while time.monotonic() < deadline:
            local = _read_mapping(console_a, LOCAL_SAMPLE)
            observer = _read_mapping(console_b, OBSERVER_SAMPLE)
            samples.append(local)
            artifact.record(name, client_a=local, client_b=observer)
            time.sleep(interval)
    finally:
        _set_keys(console_a, tuple(reversed(keys)), False)
        console_a.run("repr(tag(''))")
    return samples


def run_scenario(
    console_a: GameConsole,
    console_b: GameConsole,
    *,
    duration: float,
    interval: float,
) -> tuple[ParityArtifact, MovementAnalysis]:
    for console in (console_a, console_b):
        scene = ast.literal_eval(console.run("manager.scene.__class__.__name__"))
        if scene != "GameScene":
            raise RuntimeError(f"client is not in GameScene: {scene!r}")

    artifact = ParityArtifact("movement_baseline")
    all_samples: list[dict] = []
    segments = (
        ("walk", ("W",)),
        ("diagonal", ("W", "D")),
        ("sprint", ("W", "LSHIFT")),
        ("crouch_walk", ("W", "LCTRL")),
        ("crouch_release", ("W",)),
        ("jump", ("W", "SPACE")),
        ("wall_contact", ("W",)),
    )
    for name, keys in segments:
        all_samples.extend(
            collect_segment(
                console_a,
                console_b,
                artifact,
                name=name,
                keys=keys,
                duration=duration,
                interval=interval,
            )
        )
    analysis = analyze_movement_samples(all_samples)
    artifact.record("analysis", result=asdict(analysis))
    return artifact, analysis


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console-a", type=int, default=32896)
    parser.add_argument("--console-b", type=int, default=32897)
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument("--artifact-dir", type=Path, default=Path("logs/parity"))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    with GameConsole(port=args.console_a) as console_a, GameConsole(
        port=args.console_b
    ) as console_b:
        artifact, analysis = run_scenario(
            console_a,
            console_b,
            duration=args.duration,
            interval=args.interval,
        )
    path = artifact.write(args.artifact_dir)
    print(f"artifact: {path}")
    print(f"analysis: {asdict(analysis)}")
    return 0 if analysis.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
