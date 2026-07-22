"""Frozen-safe BattleSpades command-line and server lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import faulthandler
import functools
import gc
import logging
import multiprocessing
import os
import select
import signal
import sys
from dataclasses import dataclass, field
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


_CONTROL_STDIN_POLL_SECONDS = 0.025
_CONTROL_STDIN_READ_BYTES = 4096
_CONTROL_STDIN_MAX_LINE_BYTES = 4096
_WINDOWS_PIPE_EOF_ERRORS = frozenset({109, 232, 233})


@dataclass(slots=True)
class _ControlLineDecoder:
    """Recognize only an exact ASCII ``shutdown`` line from bounded chunks."""

    pending: bytearray = field(default_factory=bytearray)
    discarding_line: bool = False

    def feed(self, data: bytes) -> bool:
        """Return true once one complete exact shutdown line is received."""

        remaining = bytes(data)
        while remaining:
            newline = remaining.find(b"\n")
            if newline < 0:
                if not self.discarding_line:
                    self.pending.extend(remaining)
                    if len(self.pending) > _CONTROL_STDIN_MAX_LINE_BYTES:
                        # A launcher control line has one legal eight-byte
                        # value. Bound malformed input until its next newline.
                        self.pending.clear()
                        self.discarding_line = True
                return False

            segment = remaining[: newline + 1]
            remaining = remaining[newline + 1 :]
            if not self.discarding_line:
                self.pending.extend(segment)
                if bytes(self.pending) in (b"shutdown\n", b"shutdown\r\n"):
                    return True
            self.pending.clear()
            self.discarding_line = False
        return False

    def finish(self) -> bool:
        """Recognize the sole legal unterminated line when the pipe closes."""

        return not self.discarding_line and bytes(self.pending) == b"shutdown"


def _control_stream_fd(stream) -> int:
    """Return the redirected control stream descriptor or raise clearly."""

    if stream is None:
        raise EOFError("parent stdin is unavailable")
    try:
        descriptor = int(stream.fileno())
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise OSError("parent stdin has no pollable file descriptor") from exc
    if descriptor < 0:
        raise OSError("parent stdin file descriptor is closed")
    if sys.platform == "win32":
        # CRT text translation may read ahead after a trailing carriage return,
        # defeating the byte count returned by PeekNamedPipe. This descriptor
        # is a dedicated ASCII control channel, so raw binary reads are exact.
        import msvcrt

        msvcrt.setmode(descriptor, os.O_BINARY)
    return descriptor


def _poll_windows_control_pipe(descriptor: int) -> tuple[bytes | None, bool]:
    """Poll one Windows anonymous/named pipe without starting a reader thread."""

    import msvcrt

    handle = msvcrt.get_osfhandle(descriptor)
    peek_named_pipe, ctypes, wintypes = _windows_peek_named_pipe()
    available = wintypes.DWORD()
    if not peek_named_pipe(
        handle,
        None,
        0,
        None,
        ctypes.byref(available),
        None,
    ):
        error = ctypes.get_last_error()
        if error in _WINDOWS_PIPE_EOF_ERRORS:
            return b"", True
        raise ctypes.WinError(error)
    if available.value <= 0:
        return None, False
    data = os.read(
        descriptor,
        min(int(available.value), _CONTROL_STDIN_READ_BYTES),
    )
    return data, not data


@functools.lru_cache(maxsize=1)
def _windows_peek_named_pipe():
    """Resolve ``PeekNamedPipe`` once instead of rebuilding ctypes per tick."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    peek_named_pipe = kernel32.PeekNamedPipe
    peek_named_pipe.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    peek_named_pipe.restype = wintypes.BOOL
    return peek_named_pipe, ctypes, wintypes


def _poll_posix_control_pipe(descriptor: int) -> tuple[bytes | None, bool]:
    """Poll a POSIX pipe/file descriptor without blocking the event loop."""

    readable, _writable, _exceptional = select.select(
        (descriptor,),
        (),
        (),
        0.0,
    )
    if not readable:
        return None, False
    data = os.read(descriptor, _CONTROL_STDIN_READ_BYTES)
    return data, not data


async def _control_stdin_monitor(
    stream,
    request_shutdown: Callable[[str], None],
) -> None:
    """Poll the parent pipe without leaving a thread alive during AI spawns.

    PyInstaller's frozen Windows runtime can lose multiprocessing queue records
    when a different Python thread is already blocked in ``stdin.readline`` at
    ``spawn`` time. AI workers may restart throughout a match, so merely
    delaying that thread is insufficient. ``PeekNamedPipe`` keeps every poll
    non-blocking and entirely on the event-loop thread.
    """

    decoder = _ControlLineDecoder()
    try:
        descriptor = _control_stream_fd(stream)
    except EOFError:
        request_shutdown("parent stdin reached EOF")
        return
    except OSError:
        logging.getLogger("BattleSpades").warning(
            "Parent stdin control channel failed",
            exc_info=True,
        )
        return

    poll = (
        _poll_windows_control_pipe
        if sys.platform == "win32"
        else _poll_posix_control_pipe
    )
    while True:
        try:
            data, eof = poll(descriptor)
        except (OSError, ValueError):
            logging.getLogger("BattleSpades").warning(
                "Parent stdin control channel failed",
                exc_info=True,
            )
            return
        if eof:
            reason = (
                "parent requested shutdown on stdin"
                if decoder.finish()
                else "parent stdin reached EOF"
            )
            request_shutdown(reason)
            return
        if data and decoder.feed(data):
            request_shutdown("parent requested shutdown on stdin")
            return
        await asyncio.sleep(_CONTROL_STDIN_POLL_SECONDS)


def _start_control_stdin_monitor(
    loop,
    request_shutdown: Callable[[str], None],
    *,
    stream=None,
) -> asyncio.Task:
    """Schedule the opt-in, thread-free stdin control monitor."""

    return loop.create_task(
        _control_stdin_monitor(
            sys.stdin if stream is None else stream,
            request_shutdown,
        ),
        name="BattleSpades-stdin-control",
    )


def _freeze_import_graph_for_gc() -> bool:
    """Move the stable import graph out of later generation-2 scans.

    This must run after gameplay modules are imported and immediately before
    the server object is constructed.  Objects created for maps, players,
    workers, and matches therefore retain normal garbage-collection behavior.

    Returns:
        ``True`` when the runtime supports and completed ``gc.freeze``;
        otherwise ``False``.  Alternative Python runtimes may omit the API.
    """

    freeze = getattr(gc, "freeze", None)
    if not callable(freeze):
        return False

    # Retire import-time cycles before moving the remaining long-lived graph
    # to CPython's permanent generation.  This pause happens before gameplay.
    gc.collect()
    try:
        freeze()
    except (AttributeError, NotImplementedError):
        return False
    return True


async def _serve(config, logging_runtime, *, control_stdin: bool = False) -> None:
    """Own one asynchronous server instance until signal-driven shutdown."""

    from server.main import BattleSpadesServer
    from server.telemetry import TelemetryService

    logger = logging.getLogger("BattleSpades")
    loop = asyncio.get_running_loop()

    # Keep this directly beside construction: freezing later would retain
    # gameplay state forever, while freezing earlier would miss lazy imports.
    _freeze_import_graph_for_gc()
    server = BattleSpadesServer(
        config,
        telemetry=TelemetryService(logging_runtime),
    )

    server_task = asyncio.create_task(server.start(), name="BattleSpades-server")
    shutdown_task: asyncio.Task | None = None
    control_task: asyncio.Task | None = None

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
        control_task = _start_control_stdin_monitor(loop, request_shutdown)

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
        if control_task is not None:
            if not control_task.done():
                control_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await control_task


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
