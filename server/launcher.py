"""Frozen-safe BattleSpades command-line and server lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import logging
import multiprocessing
import signal
import sys
import threading
from pathlib import Path
from typing import Callable, Sequence

import toml

from server.release_check import CheckReport, run_release_check
from server.runtime_paths import RuntimePaths, apply_runtime_paths, read_version


SOURCE_ENTRYPOINT = Path(__file__).resolve().parents[1] / "run_server.py"


def build_parser() -> argparse.ArgumentParser:
    """Create the side-effect-free server argument parser."""

    parser = argparse.ArgumentParser(
        prog="BattleSpades",
        description="Ace of Spades: Battle Builders dedicated server",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--version",
        action="store_true",
        help="print the packaged server version and exit",
    )
    action.add_argument(
        "--check",
        action="store_true",
        help="validate configuration, assets, native modules, and worker spawn",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="load a TOML file for this process without changing config.toml",
    )
    parser.add_argument(
        "--port",
        type=_parse_port,
        default=None,
        help="override the selected TOML file's UDP port for this process",
    )
    parser.add_argument(
        "--control-stdin",
        action="store_true",
        help=(
            "stop cleanly when redirected stdin receives 'shutdown' or EOF; "
            "intended for an embedding launcher"
        ),
    )
    return parser


def _parse_port(value: str) -> int:
    """Parse one usable UDP port for argparse-based launchers."""

    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _select_config(paths: RuntimePaths, value: Path | None) -> RuntimePaths:
    """Apply an explicit CLI config and fail closed when it is unusable."""

    if value is None:
        return paths
    selected = paths.with_config(value)
    if not selected.config.is_file():
        raise FileNotFoundError(f"configuration file does not exist: {selected.config}")
    try:
        document = toml.load(selected.config)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"cannot parse configuration file {selected.config}: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise ValueError(f"configuration root must be a TOML table: {selected.config}")
    return selected


def _apply_port_override(config: object, port: int | None) -> object:
    """Apply a validated, process-local listener override."""

    if port is not None:
        config.port = port
    return config


def _emit_check_report(report: CheckReport) -> int:
    """Print one complete health report to stdout on success or stderr on failure."""

    stream = sys.stdout if report.ok else sys.stderr
    for line in report.lines:
        print(line, file=stream)
    return report.exit_code


def _configure_console_encoding() -> None:
    """Keep arbitrary Unicode player names safe on Windows consoles."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue


def _control_stdin_worker(stream, loop, request_shutdown: Callable[[str], None]) -> None:
    """Block on a parent-owned pipe without touching the gameplay thread.

    Only an exact ``shutdown`` line (with the platform line ending removed by
    comparison, not arbitrary whitespace stripping) or EOF has meaning.  All
    other input is ignored so this opt-in channel cannot become an accidental
    command parser.
    """

    shutdown_lines = {
        "shutdown",
        "shutdown\n",
        "shutdown\r\n",
        b"shutdown",
        b"shutdown\n",
        b"shutdown\r\n",
    }
    while True:
        try:
            line = stream.readline()
        except (OSError, ValueError):
            logging.getLogger("BattleSpades").warning(
                "Parent stdin control channel failed",
                exc_info=True,
            )
            return
        if line in ("", b""):
            reason = "parent stdin reached EOF"
        elif line in shutdown_lines:
            reason = "parent requested shutdown on stdin"
        else:
            continue
        try:
            loop.call_soon_threadsafe(request_shutdown, reason)
        except RuntimeError:
            # The event loop can close between a parent pipe EOF and this
            # daemon reader waking. Shutdown has already completed in that case.
            pass
        return


def _start_control_stdin_monitor(
    loop,
    request_shutdown: Callable[[str], None],
    *,
    stream=None,
) -> threading.Thread:
    """Start the opt-in daemon that owns all blocking stdin reads."""

    monitor = threading.Thread(
        target=_control_stdin_worker,
        args=(sys.stdin if stream is None else stream, loop, request_shutdown),
        name="BattleSpades-stdin-control",
        daemon=True,
    )
    monitor.start()
    return monitor


async def _serve(config, logging_runtime, *, control_stdin: bool = False) -> None:
    """Own one asynchronous server instance until signal-driven shutdown."""

    from server.main import BattleSpadesServer
    from server.telemetry import TelemetryService

    logger = logging.getLogger("BattleSpades")
    server = BattleSpadesServer(
        config,
        telemetry=TelemetryService(logging_runtime),
    )
    loop = asyncio.get_running_loop()

    server_task = asyncio.create_task(server.start(), name="BattleSpades-server")
    shutdown_task: asyncio.Task | None = None

    async def stop_after_start_boundary() -> None:
        # A parent can close its pipe immediately after spawning us. Avoid
        # racing stop() through the server's partial-start cleanup while
        # start() is still about to enter its two long-running loops.
        while not server.running and not server_task.done():
            await asyncio.sleep(0.01)
        await server.stop()

    def request_shutdown(reason: str = "shutdown signal received") -> None:
        nonlocal shutdown_task
        if shutdown_task is not None:
            return
        logger.info("%s...", reason.capitalize())
        shutdown_task = asyncio.create_task(
            stop_after_start_boundary(),
            name="BattleSpades-graceful-shutdown",
        )

    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(shutdown_signal, request_shutdown)
        except NotImplementedError:
            signal.signal(
                shutdown_signal,
                lambda _signum, _frame: loop.call_soon_threadsafe(
                    request_shutdown,
                ),
            )

    if control_stdin:
        _start_control_stdin_monitor(loop, request_shutdown)

    try:
        await server_task
    finally:
        # Await the same stop task that lowered ``server.running``. Calling
        # stop() a second time would return early and let asyncio.run cancel
        # the first task midway through UGC's final VXL/sidecar checkpoint.
        if shutdown_task is not None:
            await shutdown_task
        else:
            await server.stop()


def _run_server(
    paths: RuntimePaths,
    *,
    config_transform: Callable[[object], object | None] | None = None,
    banner: str = "BattleSpades Server - Protocol 1.0 Battle Builders",
    control_stdin: bool = False,
) -> int:
    """Configure process resources, run one server variant, and close sinks.

    ``config_transform`` is intentionally applied after portable paths are
    resolved and before logging or networking starts.  Dedicated entrypoints
    such as the reconstructed tutorial can therefore lock their runtime
    identity without duplicating the normal lifecycle or modifying the
    operator's ``config.toml`` on disk.
    """

    from server.config import load_config
    from server.logging_runtime import configure_logging

    _configure_console_encoding()
    config = apply_runtime_paths(load_config(paths.config), paths)
    if config_transform is not None:
        transformed = config_transform(config)
        if transformed is not None:
            config = transformed
    paths.logs.mkdir(parents=True, exist_ok=True)
    logging_runtime = configure_logging(config, paths.logs)
    logger = logging.getLogger("BattleSpades")
    fault_file = paths.logs / "faulthandler.log"

    try:
        with fault_file.open("a", encoding="utf-8") as fault_stream:
            faulthandler.enable(fault_stream)
            logger.info("=" * 50)
            logger.info("%s", banner)
            logger.info("=" * 50)
            logger.info("Application root: %s", paths.root)
            logger.info("Log level set to: %s", config.log_level.upper())
            try:
                asyncio.run(
                    _serve(
                        config,
                        logging_runtime,
                        control_stdin=control_stdin,
                    )
                )
            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received")
            except Exception:
                logger.exception("Server startup/runtime failed")
                return 1
            finally:
                faulthandler.disable()
        logger.info("Server stopped.")
        return 0
    finally:
        logger.info(
            "Logging shutdown: dropped_records=%d",
            logging_runtime.dropped_records,
        )
        logging_runtime.stop()


def run(
    argv: Sequence[str] | None = None,
    *,
    paths: RuntimePaths | None = None,
) -> int:
    """Dispatch a normal start, version query, or bounded release check."""

    multiprocessing.freeze_support()
    try:
        arguments = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    runtime_paths = paths or RuntimePaths.discover(source_entry=SOURCE_ENTRYPOINT)
    if arguments.version:
        print(f"BattleSpades {read_version(runtime_paths.root)}")
        return 0
    try:
        runtime_paths = _select_config(runtime_paths, arguments.config)
    except (OSError, ValueError) as exc:
        print(f"Server startup failed: {exc}", file=sys.stderr)
        return 1
    if arguments.check:
        return _emit_check_report(run_release_check(runtime_paths))
    return _run_server(
        runtime_paths,
        config_transform=lambda config: _apply_port_override(
            config,
            arguments.port,
        ),
        control_stdin=arguments.control_stdin,
    )
