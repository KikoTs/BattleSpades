"""Launch two independently instrumented development clients for parity tests."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLIENT_DIR = Path(r"G:\AoSRevival\AceOfSpades_no_steam_new")


@dataclass(frozen=True)
class ClientSpec:
    index: int
    client_dir: Path
    python_path: Path
    connect_target: str
    console_port: int
    tracer_port: int
    capture_dir: Path
    capture_enabled: bool = True
    stack_sampler_enabled: bool = False
    # Preserve the historical two-client parity behavior by default. Movement
    # stress overrides this because the retail game throttles a minimized
    # window heavily and no longer approximates foreground player pacing.
    minimized: bool = True


def build_client_specs(
    connect_target: str,
    *,
    client_dir: Path = DEFAULT_CLIENT_DIR,
    artifact_root: Path | None = None,
) -> tuple[ClientSpec, ClientSpec]:
    """Describe two clients whose tracer endpoints cannot collide."""
    client_dir = Path(client_dir)
    artifact_root = Path(artifact_root or ROOT / "logs" / "parity")
    python_path = client_dir / "python" / "python.exe"
    ports: Sequence[tuple[int, int]] = ((32896, 32895), (32897, 32898))
    specs = tuple(
        ClientSpec(
            index=index,
            client_dir=client_dir,
            python_path=python_path,
            connect_target=str(connect_target),
            console_port=console_port,
            tracer_port=tracer_port,
            capture_dir=artifact_root / f"client-{index}",
        )
        for index, (console_port, tracer_port) in enumerate(ports, start=1)
    )
    return specs  # type: ignore[return-value]


def launch_client(spec: ClientSpec) -> subprocess.Popen:
    """Start one retail client with explicitly enabled instrumentation.

    The retail tree loads ``physics_tracer`` only when
    ``PHYSICS_TRACER_ENABLED=1``.  Keeping that switch here is important:
    setting only the console/capture ports silently launches an ordinary
    client and leaves automation waiting for a console that will never open.

    Full frame capture is optional because synchronous NDJSON writes can
    perturb the timing that a movement stress test is trying to measure.  A
    stress scenario normally samples through the console and leaves capture
    disabled; parity/replay jobs can still opt in explicitly.
    """
    if not spec.python_path.is_file():
        raise FileNotFoundError(f"client Python not found: {spec.python_path}")
    if not (spec.client_dir / "run.py").is_file():
        raise FileNotFoundError(f"client run.py not found: {spec.client_dir}")

    spec.capture_dir.mkdir(parents=True, exist_ok=True)
    stdout_handle = (spec.capture_dir / "client_stdout.log").open(
        "a",
        encoding="utf-8",
        errors="replace",
    )
    env = os.environ.copy()
    env["PHYSICS_TRACER_ENABLED"] = "1"
    env["PHYSICS_TRACER_CONSOLE_PORT"] = str(spec.console_port)
    env["PHYSICS_TRACER_PORT"] = str(spec.tracer_port)
    env["PHYSICS_TRACER_CAPTURE"] = "1" if spec.capture_enabled else "0"
    env["PHYSICS_TRACER_STACK_SAMPLER"] = (
        "1" if spec.stack_sampler_enabled else "0"
    )

    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        if spec.minimized:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 6  # SW_SHOWMINIMIZED
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        process = subprocess.Popen(
            [
                str(spec.python_path),
                "run.py",
                "+debug",
                "+connect",
                spec.connect_target,
            ],
            cwd=spec.client_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=subprocess.STDOUT,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except Exception:
        stdout_handle.close()
        raise

    process._parity_stdout_handle = stdout_handle  # type: ignore[attr-defined]
    return process


def stop_client(process: subprocess.Popen, timeout: float = 10.0) -> None:
    """Stop only the supplied parity client process and close its log."""
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=timeout)
    finally:
        handle = getattr(process, "_parity_stdout_handle", None)
        if handle is not None:
            handle.close()
