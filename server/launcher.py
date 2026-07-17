"""Frozen-safe BattleSpades command-line and server lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import logging
import multiprocessing
import signal
import sys
from pathlib import Path
from typing import Callable, Sequence

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
    return parser


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


async def _serve(config, logging_runtime) -> None:
    """Own one asynchronous server instance until signal-driven shutdown."""

    from server.main import BattleSpadesServer
    from server.telemetry import TelemetryService

    logger = logging.getLogger("BattleSpades")
    server = BattleSpadesServer(
        config,
        telemetry=TelemetryService(logging_runtime),
    )
    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        logger.info("Shutdown signal received...")
        asyncio.create_task(server.stop())

    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(shutdown_signal, request_shutdown)
        except NotImplementedError:
            signal.signal(shutdown_signal, lambda _signum, _frame: request_shutdown())

    try:
        await server.start()
    finally:
        await server.stop()


def _run_server(
    paths: RuntimePaths,
    *,
    config_transform: Callable[[object], object | None] | None = None,
    banner: str = "BattleSpades Server - Protocol 1.0 Battle Builders",
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
                asyncio.run(_serve(config, logging_runtime))
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
    if arguments.check:
        return _emit_check_report(run_release_check(runtime_paths))
    return _run_server(runtime_paths)
