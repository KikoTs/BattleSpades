"""Validated multi-instance launcher for BattleSpades server fleets."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import toml


@dataclass(frozen=True, slots=True)
class FleetInstance:
    """One enabled server process described by a fleet manifest."""

    name: str
    config: Path
    port: int


@dataclass(frozen=True, slots=True)
class FleetManifest:
    """Validated fleet settings and enabled server instances."""

    path: Path
    instances: tuple[FleetInstance, ...]
    shutdown_timeout_seconds: float = 10.0


def _configured_port(document: dict, override: object) -> int:
    """Return one validated effective UDP port."""

    value = override
    if value is None:
        server = document.get("server", {})
        value = server.get("port", 32887) if isinstance(server, dict) else 32887
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid fleet server port: {value!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"fleet server port must be between 1 and 65535: {port}")
    return port


def load_fleet_manifest(path: str | Path) -> FleetManifest:
    """Load a fleet TOML and fail before starting any partial server set."""

    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_file():
        raise FileNotFoundError(f"fleet manifest does not exist: {manifest_path}")
    try:
        document = toml.load(manifest_path)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"cannot parse fleet manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise ValueError("fleet manifest root must be a TOML table")
    fleet_options = document.get("fleet", {})
    if not isinstance(fleet_options, dict):
        raise ValueError("fleet must be a TOML table")
    rows = document.get("instances", ())
    if not isinstance(rows, list):
        raise ValueError("fleet instances must use [[instances]] tables")

    instances: list[FleetInstance] = []
    names: set[str] = set()
    ports: set[int] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"fleet instance {index} must be a TOML table")
        if not bool(row.get("enabled", True)):
            continue
        name = str(row.get("name", "")).strip()
        if not name:
            raise ValueError(f"fleet instance {index} has no name")
        normalized_name = name.casefold()
        if normalized_name in names:
            raise ValueError(f"duplicate fleet instance name: {name}")
        names.add(normalized_name)

        configured_path = str(row.get("config", "")).strip()
        if not configured_path:
            raise ValueError(f"fleet instance {name!r} has no config")
        config = Path(configured_path).expanduser()
        if not config.is_absolute():
            config = manifest_path.parent / config
        config = config.resolve()
        if not config.is_file():
            raise FileNotFoundError(
                f"fleet instance {name!r} config does not exist: {config}"
            )
        try:
            config_document = toml.load(config)
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"cannot parse fleet instance {name!r} config {config}: {exc}"
            ) from exc
        if not isinstance(config_document, dict):
            raise ValueError(
                f"fleet instance {name!r} config root must be a TOML table"
            )
        port = _configured_port(config_document, row.get("port"))
        if port in ports:
            raise ValueError(f"duplicate fleet UDP port: {port}")
        ports.add(port)
        instances.append(FleetInstance(name=name, config=config, port=port))

    if not instances:
        raise ValueError("fleet manifest has no enabled instances")
    try:
        shutdown_timeout = float(
            fleet_options.get("shutdown_timeout_seconds", 10.0)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("fleet shutdown timeout must be numeric") from exc
    if not 1.0 <= shutdown_timeout <= 60.0:
        raise ValueError("fleet shutdown timeout must be between 1 and 60 seconds")
    return FleetManifest(
        path=manifest_path,
        instances=tuple(instances),
        shutdown_timeout_seconds=shutdown_timeout,
    )


def instance_command(
    instance: FleetInstance,
    *,
    source_entry: Path,
    executable: Path | None = None,
    frozen: bool | None = None,
) -> list[str]:
    """Build a shell-free Unicode-safe child command."""

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        command = [str(Path(executable or sys.executable).resolve())]
    else:
        command = [
            str(Path(executable or sys.executable).resolve()),
            str(Path(source_entry).resolve()),
        ]
    command.extend(
        (
            "--config",
            str(instance.config),
            "--port",
            str(instance.port),
            "--control-stdin",
        )
    )
    return command


def _request_shutdown(
    processes: Sequence[tuple[FleetInstance, subprocess.Popen]],
    timeout: float,
) -> None:
    """Ask every child to stop, then reap or terminate exact survivors."""

    for _instance, process in processes:
        if process.poll() is not None or process.stdin is None:
            continue
        try:
            process.stdin.write(b"shutdown\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            continue
    deadline = time.monotonic() + max(0.0, float(timeout))
    for _instance, process in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.terminate()
    for _instance, process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)


def run_fleet(
    manifest_path: str | Path,
    *,
    runtime_root: Path,
    source_entry: Path,
) -> int:
    """Start every validated instance and supervise until exit or Ctrl+C."""

    manifest = load_fleet_manifest(manifest_path)
    processes: list[tuple[FleetInstance, subprocess.Popen]] = []
    exit_code = 0
    try:
        for instance in manifest.instances:
            process = subprocess.Popen(
                instance_command(instance, source_entry=source_entry),
                cwd=Path(runtime_root).resolve(),
                stdin=subprocess.PIPE,
            )
            processes.append((instance, process))
            print(
                f"Started {instance.name} pid={process.pid} "
                f"port={instance.port} config={instance.config}"
            )
        while processes:
            active: list[tuple[FleetInstance, subprocess.Popen]] = []
            for instance, process in processes:
                status = process.poll()
                if status is None:
                    active.append((instance, process))
                    continue
                print(f"{instance.name} stopped with exit code {status}")
                if status != 0:
                    exit_code = 1
            processes = active
            if processes:
                time.sleep(0.2)
    except KeyboardInterrupt:
        print("Stopping BattleSpades fleet...")
    finally:
        _request_shutdown(processes, manifest.shutdown_timeout_seconds)
    return exit_code
