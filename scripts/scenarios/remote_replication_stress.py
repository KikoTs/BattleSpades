"""Measure one moving retail player from two independent retail observers.

The local player and both remote Characters are sampled through their real
GameScene objects.  This catches recipient-specific WorldUpdate gaps and
rendered teleports which an owner-only reconciliation capture cannot see.
All processes are owned by this script and are stopped on every exit path.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from game_console import ConsoleError, GameConsole  # noqa: E402
from parity_clients import (  # noqa: E402
    DEFAULT_CLIENT_DIR,
    ClientSpec,
    launch_client,
    stop_client,
)
from scenarios.movement_stress import _auto_join  # noqa: E402


KEY_EVENT = """from pyglet.window import key as K
manager.keyboard[K.{key}] = {pressed}
manager.window.dispatch_event('{event}', K.{key}, 0)
_ = 'ok'"""

LOCAL_SAMPLE = """_p = manager.scene.player
_c = _p.character
_w = _c.world_object
_ = {'exists': True,
     'player_id': int(_p.id),
     'health': int(_p.health),
     'client_loop': int(manager.scene.loop_count),
     'network_loop': int(_c.network_position_loop_count),
     'position': tuple(round(float(_w.position[i]), 6) for i in range(3)),
     'network_position': tuple(round(float(_c.network_position[i]), 6) for i in range(3)),
     'velocity': tuple(round(float(_w.velocity[i]), 6) for i in range(3)),
     'airborne': bool(_w.airborne),
     'tool': int(_p.tool_id)}"""

REMOTE_SAMPLE = """_s = manager.scene
_p = _s.players.get({player_id})
_bots = {{}}
for _bot_id, _bot in _s.players.items():
    if int(_bot_id) < 12:
        _bc = _bot.character
        _bw = _bc.world_object
        _bots[int(_bot_id)] = {{
            'health': int(_bot.health),
            'just_killed': bool(getattr(_bot, 'just_killed', False)),
            'network_loop': int(_bc.network_position_loop_count),
            'position': tuple(round(float(_bw.position[i]), 6) for i in range(3)),
            'network_position': tuple(round(float(_bc.network_position[i]), 6) for i in range(3)),
            'velocity': tuple(round(float(_bw.velocity[i]), 6) for i in range(3)),
            'tool': int(_bot.tool_id)}}
if _p is None:
    _ = {{'exists': False, 'client_loop': int(_s.loop_count), 'bots': _bots}}
else:
    _c = _p.character
    _w = _c.world_object
    _ = {{'exists': True,
         'client_loop': int(_s.loop_count),
         'network_loop': int(_c.network_position_loop_count),
         'position': tuple(round(float(_w.position[i]), 6) for i in range(3)),
         'network_position': tuple(round(float(_c.network_position[i]), 6) for i in range(3)),
         'velocity': tuple(round(float(_w.velocity[i]), 6) for i in range(3)),
         'lerp_timer': round(float(_c.position_lerp_timer), 6),
         'airborne': bool(_w.airborne),
         'tool': int(_p.tool_id),
         'bots': _bots}}"""


def _mapping(console: GameConsole, code: str) -> dict[str, Any]:
    """Execute one bounded client sample and validate its shape."""

    value = ast.literal_eval(console.run(code))
    if not isinstance(value, dict):
        raise TypeError(f"client sample is not a mapping: {value!r}")
    return value


def _set_key(console: GameConsole, key: str, pressed: bool) -> None:
    """Dispatch one real Pyglet key transition on the client game thread."""

    console.run(
        KEY_EVENT.format(
            key=key,
            pressed="True" if pressed else "False",
            event="on_key_press" if pressed else "on_key_release",
        )
    )


def _release_all(console: GameConsole) -> None:
    """Release every movement key used by this scenario."""

    for key in ("SPACE", "LSHIFT", "W", "A", "D"):
        try:
            _set_key(console, key, False)
        except ConsoleError:
            pass


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


def _client_spec(
    index: int,
    args: argparse.Namespace,
    console_port: int,
    tracer_port: int,
) -> ClientSpec:
    """Build one visible, independently instrumented retail client spec."""

    return ClientSpec(
        index=index,
        client_dir=args.client_dir,
        python_path=args.client_dir / "python" / "python.exe",
        connect_target=args.server,
        console_port=console_port,
        tracer_port=tracer_port,
        capture_dir=args.artifact_dir / f"client-{index}",
        capture_enabled=False,
        stack_sampler_enabled=False,
        # Minimized retail windows throttle their GameScene and manufacture
        # observer teleports. Keep all three visible for a representative run.
        minimized=False,
    )


def _phase_keys(
    console: GameConsole,
    previous: tuple[str, ...],
    current: tuple[str, ...],
) -> tuple[str, ...]:
    """Transition between movement phases without leaving sticky keys."""

    for key in reversed(previous):
        if key not in current:
            _set_key(console, key, False)
    for key in current:
        if key not in previous:
            _set_key(console, key, True)
    return current


def _collect(
    owner: GameConsole,
    observers: Sequence[GameConsole],
    owner_id: int,
    *,
    phase_seconds: float,
    interval: float,
) -> list[dict[str, Any]]:
    """Drive sustained movement and sample both observer render timelines."""

    phases = (
        ("stand", ()),
        ("sprint", ("W", "LSHIFT")),
        ("sprint_left", ("W", "LSHIFT", "A")),
        ("sprint_right", ("W", "LSHIFT", "D")),
        ("jump", ("W", "LSHIFT", "SPACE")),
        ("runout", ("W", "LSHIFT")),
    )
    samples: list[dict[str, Any]] = []
    held: tuple[str, ...] = ()
    try:
        for phase, keys in phases:
            held = _phase_keys(owner, held, keys)
            deadline = time.monotonic() + phase_seconds
            while time.monotonic() < deadline:
                started_ns = time.monotonic_ns()
                local = _mapping(owner, LOCAL_SAMPLE)
                remote = [
                    _mapping(
                        observer,
                        REMOTE_SAMPLE.format(player_id=int(owner_id)),
                    )
                    for observer in observers
                ]
                samples.append(
                    {
                        "phase": phase,
                        "monotonic_ns": started_ns,
                        "sample_span_ms": (
                            time.monotonic_ns() - started_ns
                        ) / 1_000_000.0,
                        "owner": local,
                        "observers": remote,
                    }
                )
                time.sleep(interval)
    finally:
        _release_all(owner)
    return samples


def _wait_for_remote_ready(
    observers: Sequence[GameConsole],
    owner_id: int,
    timeout: float,
) -> None:
    """Wait until both observers have consumed two live owner snapshots."""

    deadline = time.monotonic() + timeout
    previous: list[int | None] = [None for _ in observers]
    advanced = [False for _ in observers]
    while time.monotonic() < deadline:
        for index, observer in enumerate(observers):
            remote = _mapping(
                observer,
                REMOTE_SAMPLE.format(player_id=int(owner_id)),
            )
            if not remote.get("exists"):
                previous[index] = None
                advanced[index] = False
                continue
            stamp = int(remote["network_loop"])
            if previous[index] is not None and stamp != previous[index]:
                advanced[index] = True
            previous[index] = stamp
        if all(advanced):
            return
        time.sleep(0.025)
    raise TimeoutError("observers did not receive advancing owner rows")


def analyze(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize remote packet age, disagreement, and rendered discontinuity."""

    missing = [0, 0]
    max_owner_distance = [0.0, 0.0]
    max_packet_age = [0, 0]
    max_render_step = [0.0, 0.0]
    max_step_per_loop = [0.0, 0.0]
    max_observer_disagreement = 0.0
    teleports: list[dict[str, Any]] = []
    previous: list[Mapping[str, Any] | None] = [None, None]
    remote_stamp_state: list[Mapping[str, int] | None] = [None, None]
    bot_previous: list[dict[int, Mapping[str, Any]]] = [{}, {}]
    bot_stamp_state: list[dict[int, Mapping[str, Any]]] = [{}, {}]
    bot_max_step_per_loop = [0.0, 0.0]
    bot_max_packet_age = [0, 0]
    bot_max_disagreement = 0.0
    bot_teleports: list[dict[str, Any]] = []
    bot_respawns: list[dict[str, Any]] = []
    bot_respawn_skews: list[dict[str, Any]] = []
    bot_visible_counts: list[list[int]] = [[], []]
    owner_min_health = 100
    local_player_ids: set[int] = set()

    for sample_index, sample in enumerate(samples):
        owner = sample["owner"]
        local_player_ids.add(int(owner["player_id"]))
        owner_min_health = min(owner_min_health, int(owner.get("health", 100)))
        owner_position = owner["position"]
        remotes = sample["observers"]
        present_positions: list[Sequence[float]] = []
        for observer_index, remote in enumerate(remotes):
            if not remote.get("exists"):
                missing[observer_index] += 1
                previous[observer_index] = None
                remote_stamp_state[observer_index] = None
                continue
            present_positions.append(remote["position"])
            max_owner_distance[observer_index] = max(
                max_owner_distance[observer_index],
                _distance(owner_position, remote["position"]),
            )
            observer_loop = int(remote["client_loop"])
            network_loop = int(remote["network_loop"])
            stamp = remote_stamp_state[observer_index]
            if stamp is None or int(stamp["network_loop"]) != network_loop:
                stamp = {
                    "network_loop": network_loop,
                    "changed_at_client_loop": observer_loop,
                }
            remote_stamp_state[observer_index] = stamp
            max_packet_age[observer_index] = max(
                max_packet_age[observer_index],
                observer_loop - int(stamp["changed_at_client_loop"]),
            )
            old = previous[observer_index]
            if old is not None:
                step = _distance(old["position"], remote["position"])
                loop_delta = max(
                    1,
                    int(remote["client_loop"]) - int(old["client_loop"]),
                )
                step_per_loop = step / loop_delta
                max_render_step[observer_index] = max(
                    max_render_step[observer_index], step
                )
                max_step_per_loop[observer_index] = max(
                    max_step_per_loop[observer_index], step_per_loop
                )
                # One native 60 Hz movement frame cannot legitimately cover
                # a full block. Normalize by observer loop progress so a
                # temporarily stalled console sample is not called a teleport.
                if step_per_loop > 1.0:
                    teleports.append(
                        {
                            "sample_index": sample_index,
                            "phase": sample["phase"],
                            "observer": observer_index + 1,
                            "step": step,
                            "loop_delta": loop_delta,
                            "step_per_loop": step_per_loop,
                            "from": old["position"],
                            "to": remote["position"],
                        }
                    )
            previous[observer_index] = remote
        if len(present_positions) == 2:
            max_observer_disagreement = max(
                max_observer_disagreement,
                _distance(present_positions[0], present_positions[1]),
            )

        # Bot ids 0-11 are server-owned in the validation roster. Retired or
        # dead bots can disappear legitimately, so absence resets continuity;
        # only jumps within an uninterrupted visible life are classified.
        for observer_index, remote in enumerate(remotes):
            bots = {
                int(bot_id): row
                for bot_id, row in remote.get("bots", {}).items()
            }
            bot_visible_counts[observer_index].append(len(bots))
            for bot_id, row in bots.items():
                observer_loop = int(remote["client_loop"])
                network_loop = int(row["network_loop"])
                stamp = bot_stamp_state[observer_index].get(bot_id)
                if stamp is None or int(stamp["network_loop"]) != network_loop:
                    stamp = {
                        "network_loop": network_loop,
                        "changed_at_client_loop": observer_loop,
                        "changed_at_position": row["position"],
                    }
                bot_stamp_state[observer_index][bot_id] = stamp
                # A bot row is stamped with the authoritative server loop,
                # whose epoch is unrelated to the observer's retail loop.
                # Staleness is therefore time since the stamp last advanced,
                # not subtraction of the two clock values.
                stamp_age = (
                    observer_loop - int(stamp["changed_at_client_loop"])
                )
                if _distance(
                    stamp["changed_at_position"], row["position"]
                ) > 0.25:
                    # A corpse can legitimately retain its last Character and
                    # stamp until respawn. Only continued rendered movement
                    # from an old stamp represents stale extrapolation.
                    bot_max_packet_age[observer_index] = max(
                        bot_max_packet_age[observer_index], stamp_age
                    )
                old = bot_previous[observer_index].get(bot_id)
                if old is not None:
                    loop_delta = max(
                        1,
                        int(remote["client_loop"])
                        - int(old["observer_client_loop"]),
                    )
                    step = _distance(old["position"], row["position"])
                    step_per_loop = step / loop_delta
                    respawned = (
                        step > 2.0
                        and int(row.get("health", 0)) >= 100
                        and int(old.get("health", 0)) < 100
                    )
                    if respawned:
                        bot_respawns.append(
                            {
                                "sample_index": sample_index,
                                "phase": sample["phase"],
                                "observer": observer_index + 1,
                                "bot_id": bot_id,
                                "from_health": int(old.get("health", 0)),
                                "step": step,
                                "from": old["position"],
                                "to": row["position"],
                            }
                        )
                    else:
                        bot_max_step_per_loop[observer_index] = max(
                            bot_max_step_per_loop[observer_index],
                            step_per_loop,
                        )
                    if not respawned and step_per_loop > 2.0:
                        bot_teleports.append(
                            {
                                "sample_index": sample_index,
                                "phase": sample["phase"],
                                "observer": observer_index + 1,
                                "bot_id": bot_id,
                                "step": step,
                                "loop_delta": loop_delta,
                                "step_per_loop": step_per_loop,
                                "from": old["position"],
                                "to": row["position"],
                            }
                        )
                bot_previous[observer_index][bot_id] = {
                    **row,
                    "observer_client_loop": int(remote["client_loop"]),
                }
            bot_previous[observer_index] = {
                bot_id: bot_previous[observer_index][bot_id]
                for bot_id in bots
            }
            bot_stamp_state[observer_index] = {
                bot_id: bot_stamp_state[observer_index][bot_id]
                for bot_id in bots
            }
        first_bots = {
            int(bot_id): row
            for bot_id, row in remotes[0].get("bots", {}).items()
        }
        second_bots = {
            int(bot_id): row
            for bot_id, row in remotes[1].get("bots", {}).items()
        }
        for bot_id in first_bots.keys() & second_bots.keys():
            disagreement = _distance(
                first_bots[bot_id]["position"],
                second_bots[bot_id]["position"],
            )
            first_health = int(first_bots[bot_id].get("health", 0))
            second_health = int(second_bots[bot_id].get("health", 0))
            lifecycle_skew = (
                disagreement > 2.0
                and first_health != second_health
                and max(first_health, second_health) >= 100
            )
            if lifecycle_skew:
                bot_respawn_skews.append(
                    {
                        "sample_index": sample_index,
                        "phase": sample["phase"],
                        "bot_id": bot_id,
                        "health": [first_health, second_health],
                        "distance": disagreement,
                    }
                )
            else:
                bot_max_disagreement = max(
                    bot_max_disagreement, disagreement
                )

    failures: list[str] = []
    if any(missing):
        failures.append("remote_player_missing")
    if len(local_player_ids) != 1:
        failures.append("local_player_identity_changed")
    if teleports:
        failures.append("remote_render_teleport")
    if max_observer_disagreement > 2.0:
        failures.append("observers_disagree")
    if any(age > 12 for age in max_packet_age):
        failures.append("remote_worldupdate_stale")
    if bot_teleports:
        failures.append("bot_render_teleport")
    if bot_max_disagreement > 2.0:
        failures.append("bot_observers_disagree")
    if any(age > 12 for age in bot_max_packet_age):
        failures.append("bot_worldupdate_stale")
    return {
        "passed": not failures,
        "failure_reasons": failures,
        "sample_count": len(samples),
        "missing_samples": missing,
        "max_owner_remote_distance": max_owner_distance,
        "max_remote_packet_age_loops": max_packet_age,
        "max_remote_render_step": max_render_step,
        "max_remote_step_per_client_loop": max_step_per_loop,
        "max_observer_disagreement": max_observer_disagreement,
        "teleports": teleports,
        "owner_min_health": owner_min_health,
        "local_player_ids": sorted(local_player_ids),
        "bot_visible_count_range": [
            [min(values, default=0), max(values, default=0)]
            for values in bot_visible_counts
        ],
        "max_bot_packet_age_loops": bot_max_packet_age,
        "max_bot_step_per_client_loop": bot_max_step_per_loop,
        "max_bot_observer_disagreement": bot_max_disagreement,
        "bot_teleports": bot_teleports,
        "bot_respawns": bot_respawns,
        "bot_respawn_delivery_skews": bot_respawn_skews,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="127.0.0.1:27016")
    parser.add_argument("--client-dir", type=Path, default=DEFAULT_CLIENT_DIR)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=ROOT / "logs" / "remote-replication",
    )
    parser.add_argument("--phase-seconds", type=float, default=2.0)
    parser.add_argument("--interval", type=float, default=0.025)
    parser.add_argument("--wait", type=float, default=120.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Launch three clients, capture remote motion, and clean up safely."""

    args = parse_args(argv)
    if args.phase_seconds <= 0 or args.interval <= 0:
        raise ValueError("phase-seconds and interval must be positive")
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    specs = (
        _client_spec(1, args, 33100, 32895),
        _client_spec(2, args, 33101, 33111),
        _client_spec(3, args, 33102, 33112),
    )
    processes = []
    consoles: list[GameConsole] = []
    try:
        for spec in specs:
            processes.append(launch_client(spec))
        for spec, team in zip(specs, (2, 3, 3)):
            _auto_join(
                spec.console_port,
                args.server,
                team,
                0,
                args.wait,
                (),
            )
        for spec in specs:
            console = GameConsole(port=spec.console_port, timeout=15.0)
            console.connect(wait_seconds=args.wait)
            consoles.append(console)

        owner_id = int(ast.literal_eval(
            consoles[0].run("int(manager.scene.player.id)")
        ))
        _wait_for_remote_ready(
            consoles[1:],
            owner_id,
            timeout=min(float(args.wait), 15.0),
        )
        samples = _collect(
            consoles[0],
            consoles[1:],
            owner_id,
            phase_seconds=float(args.phase_seconds),
            interval=float(args.interval),
        )
        report = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "server": args.server,
            "owner_id": owner_id,
            "samples": samples,
            "analysis": analyze(samples),
        }
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = args.artifact_dir / f"remote-replication-{stamp}.json"
        path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"artifact: {path}")
        print(json.dumps(report["analysis"], indent=2, sort_keys=True))
        return 0 if report["analysis"]["passed"] else 1
    finally:
        if consoles:
            _release_all(consoles[0])
        for console in reversed(consoles):
            console.close()
        for process in reversed(processes):
            stop_client(process)


if __name__ == "__main__":
    raise SystemExit(main())
