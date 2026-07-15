"""Validate held-block colour replication with two retail clients.

The probe uses the stock client's real palette key path, then samples the
owner and an observer while standing, walking, and jumping.  When this script
owns the clients (``--launch``), it also reconnects the observer and verifies
that the server's roster snapshot restores the owner's last selected colour.

Pass the validation server's ``--packet-trace`` log with ``--packet-trace-log``
to retain the raw SetColor and ClientData evidence beside the visual state.
The trace parser deliberately records the ClientData player-byte high bit:
that bit means "palette open" and must be masked before interpreting the
seven-bit player id.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


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


BLOCK_TOOL = 5
REQUIRED_PHASES = ("stand", "walk", "jump")

SELECT_BLOCK_TOOL = """manager.scene.select_player_tool_by_id(5)
manager.scene.hud.palette.show_selection()
_ = {'tool': int(manager.scene.player.tool_id),
     'palette_active': bool(manager.scene.hud.palette.active),
     'selector_active': bool(manager.scene.hud.palette.selector_active)}"""

OWNER_SAMPLE = """_p = manager.scene.player
_c = _p.character
_w = _c.world_object
_pal = manager.scene.hud.palette
_ = {'color': (int(_c.block_color[0]), int(_c.block_color[1]), int(_c.block_color[2])),
     'tool': int(_p.tool_id),
     'palette_active': bool(_pal.active),
     'selector_active': bool(_pal.selector_active),
     'position': (round(float(_w.position[0]), 6), round(float(_w.position[1]), 6), round(float(_w.position[2]), 6)),
     'airborne': bool(_w.airborne),
     'loop': int(manager.scene.loop_count)}"""

OBSERVER_SAMPLE = """_remote = manager.scene.players.get({owner_id})
_ = {{'exists': _remote is not None,
     'color': None if _remote is None else (int(_remote.character.block_color[0]), int(_remote.character.block_color[1]), int(_remote.character.block_color[2])),
     'tool': None if _remote is None else int(_remote.tool_id)}}"""

KEY_EVENT = """from pyglet.window import key as K
manager.keyboard[K.{key}] = {pressed}
manager.window.dispatch_event('{event}', K.{key}, 0)
_ = {pressed}"""


def _hex_bytes(value: str) -> tuple[int, ...] | None:
    """Return complete hexadecimal byte tokens, rejecting partial traces."""

    tokens = value.split()
    if not tokens or any(
        len(token) != 2 or any(char not in "0123456789abcdefABCDEF" for char in token)
        for token in tokens
    ):
        return None
    return tuple(int(token, 16) for token in tokens)


def parse_packet_trace_lines(lines: Iterable[str]) -> dict[str, list[dict]]:
    """Decode only palette-relevant records from a validation-server trace.

    SetColor serializes colour channels as BGR.  ClientData stores its tool in
    byte six and overloads bit 7 of byte five as the palette-open flag.  These
    indexes include the packet id byte printed by the logger.
    """

    set_colors: list[dict] = []
    client_data: list[dict] = []
    for line in lines:
        direction = None
        for candidate in ("RECV", "SEND"):
            if f" {candidate} packet_id=" in line:
                direction = candidate
                break
        if direction is None or " hex=" not in line:
            continue

        marker = f" {direction} packet_id="
        packet_text = line.split(marker, 1)[1].split(None, 1)[0]
        try:
            packet_id = int(packet_text)
        except ValueError:
            continue
        if packet_id not in (4, 11):
            continue

        wire_and_endpoint = line.split(" hex=", 1)[1].strip()
        endpoint_marker = " from " if direction == "RECV" else " to "
        if endpoint_marker in wire_and_endpoint:
            wire_text, endpoint = wire_and_endpoint.rsplit(endpoint_marker, 1)
            endpoint = endpoint.strip()
        else:
            wire_text, endpoint = wire_and_endpoint, ""
        wire_text = " ".join(wire_text.split())
        wire = _hex_bytes(wire_text)
        if wire is None or not wire or wire[0] != packet_id:
            continue

        if packet_id == 11 and len(wire) >= 5:
            set_colors.append(
                {
                    "direction": direction,
                    "endpoint": endpoint,
                    "player_id": wire[1],
                    # Native SetColor writes blue, green, red on the wire.
                    "rgb": (wire[4], wire[3], wire[2]),
                    "wire_hex": wire_text,
                }
            )
        elif packet_id == 4 and len(wire) >= 7:
            raw_player_id = wire[5]
            client_data.append(
                {
                    "direction": direction,
                    "endpoint": endpoint,
                    "raw_player_id": raw_player_id,
                    "player_id": raw_player_id & 0x7F,
                    "palette_enabled": bool(raw_player_id & 0x80),
                    "tool": wire[6],
                    "wire_hex": wire_text,
                }
            )
    return {"set_colors": set_colors, "client_data": client_data}


def _color(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    return tuple(int(channel) for channel in value)  # type: ignore[return-value]


def analyze_palette_report(report: Mapping[str, object]) -> dict[str, object]:
    """Evaluate visual, movement, raw-packet, and reconnect invariants."""

    failures: list[str] = []
    selections = [
        color
        for color in (_color(value) for value in report.get("selection_colors", []))
        if color is not None
    ]
    expected = selections[-1] if selections else None
    if expected is None:
        failures.append("no palette selection was captured")
    elif len(set(selections)) < 2:
        failures.append("palette did not produce two distinct selections")

    samples = [
        sample
        for sample in report.get("samples", [])
        if isinstance(sample, Mapping)
    ]
    phases = sorted({str(sample.get("phase")) for sample in samples})
    for phase in REQUIRED_PHASES:
        phase_samples = [sample for sample in samples if sample.get("phase") == phase]
        if not phase_samples:
            failures.append(f"missing {phase} samples")
            continue
        if expected is not None and any(
            _color(sample.get("owner", {}).get("color")) != expected
            for sample in phase_samples
            if isinstance(sample.get("owner"), Mapping)
        ):
            failures.append(f"owner color drift in {phase}")
        if expected is not None and any(
            _color(sample.get("observer", {}).get("color")) != expected
            for sample in phase_samples
            if isinstance(sample.get("observer"), Mapping)
        ):
            failures.append(f"observer color drift in {phase}")
        if any(
            sample.get("owner", {}).get("tool") != BLOCK_TOOL
            for sample in phase_samples
            if isinstance(sample.get("owner"), Mapping)
        ):
            failures.append(f"owner tool changed in {phase}")
        if any(
            sample.get("observer", {}).get("tool") != BLOCK_TOOL
            for sample in phase_samples
            if isinstance(sample.get("observer"), Mapping)
        ):
            failures.append(f"observer tool changed in {phase}")
        if any(
            not sample.get("owner", {}).get("palette_active")
            or not sample.get("owner", {}).get("selector_active")
            for sample in phase_samples
            if isinstance(sample.get("owner"), Mapping)
        ):
            failures.append(f"palette closed in {phase}")

    reconnect = report.get("reconnect", {})
    if not isinstance(reconnect, Mapping):
        reconnect = {}
    if expected is not None and _color(reconnect.get("color")) != expected:
        failures.append("reconnect color snapshot mismatch")
    if reconnect.get("tool") != BLOCK_TOOL:
        failures.append("reconnect tool snapshot mismatch")

    packet_trace = report.get("packet_trace", {})
    if not isinstance(packet_trace, Mapping):
        packet_trace = {}
    trace_colors = {
        color
        for row in packet_trace.get("set_colors", [])
        if isinstance(row, Mapping) and row.get("direction") == "RECV"
        for color in [_color(row.get("rgb"))]
        if color is not None
    }
    missing_wire_colors = [color for color in selections if color not in trace_colors]
    if missing_wire_colors:
        failures.append("selected colors missing from raw SetColor trace")
    client_data = [
        row
        for row in packet_trace.get("client_data", [])
        if isinstance(row, Mapping) and row.get("direction") == "RECV"
    ]
    if not any(
        row.get("palette_enabled") is True and row.get("tool") == BLOCK_TOOL
        for row in client_data
    ):
        failures.append("raw ClientData lacks palette high bit with block tool")

    # Keep each causal failure once even when many samples reproduce it.
    failures = list(dict.fromkeys(failures))
    return {
        "passed": not failures,
        "failure_reasons": failures,
        "expected_color": expected,
        "selection_colors": selections,
        "phases": phases,
        "sample_count": len(samples),
        "owner_colors": sorted(
            {
                color
                for sample in samples
                if isinstance(sample.get("owner"), Mapping)
                for color in [_color(sample["owner"].get("color"))]
                if color is not None
            }
        ),
        "observer_colors": sorted(
            {
                color
                for sample in samples
                if isinstance(sample.get("observer"), Mapping)
                for color in [_color(sample["observer"].get("color"))]
                if color is not None
            }
        ),
        "raw_palette_client_data_count": sum(
            1
            for row in client_data
            if row.get("palette_enabled") is True and row.get("tool") == BLOCK_TOOL
        ),
    }


def _read_mapping(console: GameConsole, code: str) -> dict:
    value = ast.literal_eval(console.run(code))
    if not isinstance(value, dict):
        raise TypeError(f"client result was not a mapping: {value!r}")
    return value


def _key(console: GameConsole, key: str, pressed: bool) -> None:
    console.run(
        KEY_EVENT.format(
            key=key,
            pressed="True" if pressed else "False",
            event="on_key_press" if pressed else "on_key_release",
        )
    )


def _tap(console: GameConsole, key: str, duration: float = 0.06) -> None:
    _key(console, key, True)
    time.sleep(duration)
    _key(console, key, False)


def _release_movement(console: GameConsole) -> None:
    for key in ("W", "LSHIFT", "SPACE"):
        try:
            _key(console, key, False)
        except ConsoleError:
            pass


def _observer_sample(console: GameConsole, owner_id: int) -> dict:
    return _read_mapping(console, OBSERVER_SAMPLE.format(owner_id=owner_id))


def _wait_for_observer_color(
    console: GameConsole,
    owner_id: int,
    expected: tuple[int, int, int],
    timeout: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = _observer_sample(console, owner_id)
        if _color(last.get("color")) == expected:
            return last
        time.sleep(0.05)
    raise TimeoutError(
        f"observer did not receive owner color {expected!r}; last={last!r}"
    )


def _sample_phase(
    owner: GameConsole,
    observer: GameConsole,
    owner_id: int,
    phase: str,
    duration: float,
    interval: float,
) -> list[dict]:
    rows: list[dict] = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        rows.append(
            {
                "phase": phase,
                "monotonic_ns": time.monotonic_ns(),
                "owner": _read_mapping(owner, OWNER_SAMPLE),
                "observer": _observer_sample(observer, owner_id),
            }
        )
        time.sleep(interval)
    return rows


def run_probe(
    owner: GameConsole,
    observer: GameConsole,
    *,
    interval: float = 0.08,
    phase_duration: float = 1.25,
) -> dict:
    """Run selection and movement phases on two already-spawned clients."""

    owner_id = int(ast.literal_eval(owner.run("int(manager.scene.player.id)")))
    prepared = _read_mapping(owner, SELECT_BLOCK_TOOL)
    if prepared.get("tool") != BLOCK_TOOL:
        raise RuntimeError(f"retail client did not select block tool: {prepared!r}")

    # Use the actual bound arrow-key events.  Two orthogonal moves ensure the
    # probe is not accidentally accepting an unchanged initial swatch.
    selection_colors: list[tuple[int, int, int]] = []
    for key in ("RIGHT", "DOWN"):
        _tap(owner, key)
        owner_state = _read_mapping(owner, OWNER_SAMPLE)
        selected = _color(owner_state.get("color"))
        if selected is None:
            raise RuntimeError(f"retail palette returned no colour: {owner_state!r}")
        _wait_for_observer_color(observer, owner_id, selected)
        selection_colors.append(selected)

    samples: list[dict] = []
    try:
        samples.extend(
            _sample_phase(
                owner,
                observer,
                owner_id,
                "stand",
                phase_duration,
                interval,
            )
        )

        _key(owner, "W", True)
        samples.extend(
            _sample_phase(
                owner,
                observer,
                owner_id,
                "walk",
                phase_duration,
                interval,
            )
        )

        _key(owner, "LSHIFT", True)
        _key(owner, "SPACE", True)
        time.sleep(0.12)
        _key(owner, "SPACE", False)
        samples.extend(
            _sample_phase(
                owner,
                observer,
                owner_id,
                "jump",
                phase_duration,
                interval,
            )
        )
    finally:
        _release_movement(owner)

    return {
        "owner_id": owner_id,
        "selection_colors": selection_colors,
        "samples": samples,
    }


def _auto_join(
    console_port: int,
    server: str,
    team: int,
    class_id: int,
    wait: float,
) -> None:
    command = [
        sys.executable,
        str(SCRIPTS_DIR / "auto_join.py"),
        "--server",
        server,
        "--team",
        str(team),
        "--class-id",
        str(class_id),
        "--console-port",
        str(console_port),
        "--wait",
        str(wait),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=wait + 240.0,
    )
    print(completed.stdout, end="")
    if completed.returncode:
        raise RuntimeError(
            f"auto_join on console {console_port} exited {completed.returncode}"
        )


def _client_spec(
    *,
    index: int,
    client_dir: Path,
    server: str,
    console_port: int,
    tracer_port: int,
    artifact_dir: Path,
) -> ClientSpec:
    return ClientSpec(
        index=index,
        client_dir=client_dir,
        python_path=client_dir / "python" / "python.exe",
        connect_target=server,
        console_port=console_port,
        tracer_port=tracer_port,
        capture_dir=artifact_dir / f"client-{index}",
        capture_enabled=False,
        stack_sampler_enabled=False,
        minimized=False,
    )


def _trace_tail(path: Path | None, offset: int) -> dict[str, list[dict]]:
    if path is None:
        return {"set_colors": [], "client_data": []}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        return parse_packet_trace_lines(handle)


def _write_report(report: Mapping[str, object], artifact_dir: Path) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = artifact_dir / f"palette-stability-{stamp}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="127.0.0.1:27015")
    parser.add_argument("--owner-console", type=int, default=33024)
    parser.add_argument("--owner-tracer", type=int, default=33025)
    parser.add_argument("--observer-console", type=int, default=33026)
    parser.add_argument("--observer-tracer", type=int, default=33027)
    parser.add_argument("--client-dir", type=Path, default=DEFAULT_CLIENT_DIR)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=ROOT / "logs" / "palette-stability",
    )
    parser.add_argument("--packet-trace-log", type=Path)
    parser.add_argument("--interval", type=float, default=0.08)
    parser.add_argument("--phase-duration", type=float, default=1.25)
    parser.add_argument("--wait", type=float, default=120.0)
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--keep-clients", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.interval <= 0 or args.phase_duration <= 0:
        raise ValueError("interval and phase-duration must be positive")
    if not args.launch:
        raise ValueError(
            "palette reconnect validation owns its clients; pass --launch"
        )

    trace_offset = 0
    if args.packet_trace_log is not None and args.packet_trace_log.exists():
        trace_offset = args.packet_trace_log.stat().st_size

    owner_spec = _client_spec(
        index=1,
        client_dir=args.client_dir,
        server=args.server,
        console_port=args.owner_console,
        tracer_port=args.owner_tracer,
        artifact_dir=args.artifact_dir,
    )
    observer_spec = _client_spec(
        index=2,
        client_dir=args.client_dir,
        server=args.server,
        console_port=args.observer_console,
        tracer_port=args.observer_tracer,
        artifact_dir=args.artifact_dir,
    )

    owner_process = None
    observer_process = None
    owner_console = None
    observer_console = None
    report: dict[str, object] = {}
    try:
        owner_process = launch_client(owner_spec)
        _auto_join(args.owner_console, args.server, 2, 0, args.wait)
        observer_process = launch_client(observer_spec)
        _auto_join(args.observer_console, args.server, 3, 0, args.wait)

        owner_console = GameConsole(port=args.owner_console, timeout=15.0)
        observer_console = GameConsole(port=args.observer_console, timeout=15.0)
        owner_console.connect(wait_seconds=args.wait)
        observer_console.connect(wait_seconds=args.wait)
        report.update(
            run_probe(
                owner_console,
                observer_console,
                interval=args.interval,
                phase_duration=args.phase_duration,
            )
        )

        # Disconnect only the observer process created above.  The owner stays
        # alive so this measures late-join state rather than a fresh round.
        observer_console.close()
        observer_console = None
        stop_client(observer_process)
        observer_process = None
        time.sleep(0.5)

        observer_process = launch_client(observer_spec)
        _auto_join(args.observer_console, args.server, 3, 0, args.wait)
        observer_console = GameConsole(port=args.observer_console, timeout=15.0)
        observer_console.connect(wait_seconds=args.wait)
        expected = _color(report["selection_colors"][-1])  # type: ignore[index]
        assert expected is not None
        report["reconnect"] = _wait_for_observer_color(
            observer_console,
            int(report["owner_id"]),
            expected,
            timeout=10.0,
        )

        # Give the line-buffered validation logger a frame to publish the last
        # reconnect snapshot before reading its tail.
        time.sleep(0.25)
        report["packet_trace"] = _trace_tail(
            args.packet_trace_log,
            trace_offset,
        )
        report["analysis"] = analyze_palette_report(report)
        report["metadata"] = {
            "server": args.server,
            "client_dir": str(args.client_dir),
            "packet_trace_log": (
                None if args.packet_trace_log is None else str(args.packet_trace_log)
            ),
            "trace_start_offset": trace_offset,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        path = _write_report(report, args.artifact_dir)
        print(f"artifact: {path}")
        print(json.dumps(report["analysis"], indent=2, sort_keys=True))
        return 0 if report["analysis"]["passed"] else 1  # type: ignore[index]
    finally:
        _release_movement(owner_console) if owner_console is not None else None
        if owner_console is not None:
            owner_console.close()
        if observer_console is not None:
            observer_console.close()
        if not args.keep_clients:
            if observer_process is not None:
                stop_client(observer_process)
            if owner_process is not None:
                stop_client(owner_process)


if __name__ == "__main__":
    raise SystemExit(main())
