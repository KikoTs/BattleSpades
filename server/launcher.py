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
from server.network_options import add_network_arguments, apply_network_config


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
    action.add_argument(
        "--fleet",
        type=Path,
        default=None,
        help="launch all enabled [[instances]] from a fleet TOML manifest",
    )
    action.add_argument(
        "--update",
        action="store_true",
        help=(
            "download, verify and stage the newest server from the update "
            "manifest; never replaces files of this installation"
        ),
    )
    action.add_argument(
        "--workshop-download", nargs="+", metavar="ID_OR_URL",
        help="download public Ace of Spades maps into world.maps_path without Steam, then exit",
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
    parser.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help=(
            "write a small JSON status snapshot (state, players, map, mode) "
            "about once per second; intended for a supervising launcher"
        ),
    )
    parser.add_argument('--steam-p2p', action='store_true', help='publish this server for patched retail clients over Steam relays')
    parser.add_argument('--steam-p2p-bridge', type=Path, help='path to the portable aos-retail-relay.exe helper')
    parser.add_argument('--steam-p2p-port', type=int, choices=range(1000), default=168, metavar='0..999', help='Steam virtual port (default 168; unique per hosting account)')
    parser.add_argument('--steam-p2p-private', action='store_true', help='validation only: keep the advertisement out of the public retail browser')
    add_network_arguments(parser)
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


def _apply_network_options(config: object, arguments) -> object:
    """CLI-only hosting settings; never rewrite the operator's configuration."""
    _apply_port_override(config, arguments.port)
    config.steam_p2p_enabled = arguments.steam_p2p
    config.steam_p2p_bridge = str(arguments.steam_p2p_bridge.resolve()) if arguments.steam_p2p_bridge else None
    config.steam_p2p_port = arguments.steam_p2p_port
    config.steam_p2p_private = arguments.steam_p2p_private
    if arguments.offline or arguments.master_url is not None:
        apply_network_config(config, offline=arguments.offline, master_url=arguments.master_url)
    return config


def _emit_check_report(report: CheckReport) -> int:
    """Print one complete health report to stdout on success or stderr on failure."""

    _configure_console_encoding()
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
    """Recognize an exact ASCII ``shutdown`` line from bounded chunks.

    Complete ``command <text>`` lines are collected in ``commands`` for the
    trusted operator console (server/control_channel.py); every other line
    is ignored, as before.
    """

    pending: bytearray = field(default_factory=bytearray)
    discarding_line: bool = False
    commands: list = field(default_factory=list)

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
                line = bytes(self.pending)
                if line in (b"shutdown\n", b"shutdown\r\n"):
                    return True
                self._collect_command(line.rstrip(b"\r\n"))
            self.pending.clear()
            self.discarding_line = False
        return False

    def finish(self) -> bool:
        """Recognize the sole legal unterminated line when the pipe closes."""

        return not self.discarding_line and bytes(self.pending) == b"shutdown"

    def _collect_command(self, line: bytes) -> None:
        if not line.startswith(b"command "):
            return
        from server.control_channel import MAX_PENDING_COMMANDS, parse_command_line

        text = parse_command_line(line)
        if text is not None and len(self.commands) < MAX_PENDING_COMMANDS:
            self.commands.append(text)

    def take_commands(self) -> list:
        """Return and clear the command lines received so far."""

        commands, self.commands = self.commands, []
        return commands


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
    on_command: Callable[[str], None] | None = None,
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
        stop = bool(data) and decoder.feed(data)
        # Commands that arrived before a shutdown line still run, in order.
        commands = decoder.take_commands()
        if on_command is not None:
            for text in commands:
                on_command(text)
        if stop:
            request_shutdown("parent requested shutdown on stdin")
            return
        await asyncio.sleep(_CONTROL_STDIN_POLL_SECONDS)


def _start_control_stdin_monitor(
    loop,
    request_shutdown: Callable[[str], None],
    *,
    stream=None,
    on_command: Callable[[str], None] | None = None,
) -> asyncio.Task:
    """Schedule the opt-in, thread-free stdin control monitor."""

    return loop.create_task(
        _control_stdin_monitor(
            sys.stdin if stream is None else stream,
            request_shutdown,
            on_command,
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


async def _serve(
    config,
    logging_runtime,
    *,
    control_stdin: bool = False,
    status_file: Path | None = None,
) -> None:
    """Own one asynchronous server instance until signal-driven shutdown."""

    from server.main import BattleSpadesServer
    from server.telemetry import TelemetryService
    from server.native_host import NativeHostStatus

    logger = logging.getLogger("BattleSpades")
    loop = asyncio.get_running_loop()
    native_status = (
        NativeHostStatus.from_environment(
            getattr(config, "port", 0), getattr(config, "game_mode", "")
        )
        if control_stdin else None
    )
    if native_status is not None:
        native_status.publish("starting")

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
    ready_task: asyncio.Task | None = None
    status_task: asyncio.Task | None = None
    status_state = {"state": "starting"}
    dispatcher = None
    if status_file is not None:
        from server.control_channel import publish_status_loop

        status_task = asyncio.create_task(
            publish_status_loop(server, Path(status_file), status_state),
            name="BattleSpades-status-file",
        )

    async def report_ready() -> None:
        while not server.running and not server_task.done():
            await asyncio.sleep(0.025)
        if server.running and shutdown_task is None and native_status is not None:
            native_status.publish("ready")

    if native_status is not None:
        ready_task = asyncio.create_task(report_ready(), name="native-host-readiness")

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
        status_state["state"] = "stopping"
        if native_status is not None:
            native_status.publish("stopping")
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
        from server.control_channel import CommandDispatcher

        dispatcher = CommandDispatcher(server, loop)
        control_task = _start_control_stdin_monitor(
            loop, request_shutdown, on_command=dispatcher.submit,
        )

    failed = False
    try:
        await server_task
    except Exception:
        failed = True
        if native_status is not None:
            native_status.publish("failed")
        raise
    finally:
        if ready_task is not None:
            ready_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready_task
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
        if dispatcher is not None:
            await dispatcher.close()
        if status_task is not None:
            status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await status_task
            from server.control_channel import build_status, write_status

            with contextlib.suppress(Exception):
                write_status(
                    Path(status_file),
                    build_status(server, state="failed" if failed else "stopped"),
                )
        if native_status is not None and not failed:
            native_status.publish("stopped")


def _start_update_notice(config, paths: RuntimePaths, logger: logging.Logger) -> None:
    """Log "new server version available" from a daemon thread (never blocks)."""

    from server.update_check import start_background_check

    try:
        current = read_version(paths.root)
    except (OSError, ValueError):
        return
    start_background_check(config, current, log=logger)


def _run_update_command(paths: RuntimePaths) -> int:
    """``--update``: stage the newest server component; replace nothing."""

    from server.config import load_config
    from server.update_check import run_update

    try:
        config = load_config(paths.config)
        current = read_version(paths.root)
    except (OSError, ValueError) as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1
    return run_update(
        paths.root,
        current,
        url=getattr(config, "update_manifest_url", ""),
    )


def _run_workshop_download(paths: RuntimePaths, values: Sequence[str]) -> int:
    """Install public maps without starting a server or changing its rotation."""
    from server.config import load_config
    from server_gui import workshop_public
    from server_gui.workshop_import import WorkshopError

    try:
        config = load_config(paths.config)
        maps_dir = paths.resolve_configured_path(config.maps_path)
    except (OSError, ValueError) as exc:
        print(f"Workshop failed: {exc}", file=sys.stderr)
        return 1
    failed = False
    for value in values:
        try:
            item = workshop_public.get_item(value)
            result = workshop_public.download_item(item, maps_dir)
            if not result.ok:
                raise WorkshopError(result.message)
            print(f"Installed {item.display_title} as {result.stem} in {maps_dir}")
            print(f'  Start map: [game] default_map = "{result.stem}"; '
                  f'custom rotation: [lobby] map_rotation = ["{result.stem}"]')
        except (OSError, WorkshopError) as exc:
            failed = True
            print(f"Workshop {value}: {exc}", file=sys.stderr)
    return 1 if failed else 0


def _run_server(
    paths: RuntimePaths,
    *,
    config_transform: Callable[[object], object | None] | None = None,
    banner: str = "BattleSpades Server - Protocol 1.0 Battle Builders",
    control_stdin: bool = False,
    status_file: Path | None = None,
    offline: bool = False,
    master_url: str | None = None,
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
    loaded = (
        load_config(paths.config, offline=offline, master_url=master_url)
        if offline or master_url is not None else load_config(paths.config)
    )
    config = apply_runtime_paths(loaded, paths)
    if config_transform is not None:
        transformed = config_transform(config)
        if transformed is not None:
            config = transformed
    if offline:
        apply_network_config(config, offline=True, master_url=master_url)
    paths.logs.mkdir(parents=True, exist_ok=True)
    logging_runtime = configure_logging(config, paths.logs)
    logger = logging.getLogger("BattleSpades")
    from server.config import admin_password_problem

    # load_config warned before file logging existed; repeat it into server.log.
    password_problem = admin_password_problem(getattr(config, "admin_password", ""))
    if password_problem is not None:
        rule = "!" * 72
        logger.warning(
            "%s\nIn-game /admin login is DISABLED: %s.\nSet [admin] password "
            "in config.toml to a unique secret of 12+ characters (the desktop "
            "host's Generate button makes one).\n%s",
            rule, password_problem, rule,
        )
    log_stem = Path(str(getattr(config, "log_file", "server.log"))).stem
    fault_file = paths.logs / f"{log_stem or 'server'}.fault.log"

    try:
        with fault_file.open("a", encoding="utf-8") as fault_stream:
            faulthandler.enable(fault_stream)
            logger.info("=" * 50)
            logger.info("%s", banner)
            logger.info("=" * 50)
            logger.info("Application root: %s", paths.root)
            logger.info("Log level set to: %s", config.log_level.upper())
            if not control_stdin:
                # Embedded (client-owned) servers are updated by the launcher.
                _start_update_notice(config, paths, logger)
            try:
                asyncio.run(
                    _serve(
                        config,
                        logging_runtime,
                        control_stdin=control_stdin,
                        status_file=status_file,
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
    _configure_console_encoding()
    try:
        arguments = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    runtime_paths = paths or RuntimePaths.discover(source_entry=SOURCE_ENTRYPOINT)
    if arguments.offline and (arguments.update or arguments.workshop_download is not None or arguments.steam_p2p):
        print("Server startup failed: --offline cannot be combined with downloads or --steam-p2p", file=sys.stderr)
        return 2
    if arguments.version:
        print(f"BattleSpades {read_version(runtime_paths.root)}")
        return 0
    if arguments.fleet is not None:
        if (
            arguments.config is not None
            or arguments.port is not None
            or arguments.control_stdin
            or arguments.status_file is not None
            or arguments.steam_p2p
            or arguments.steam_p2p_bridge is not None
            or arguments.steam_p2p_private
            or arguments.offline
            or arguments.master_url is not None
        ):
            print(
                "Server startup failed: --fleet cannot be combined with "
                "--config, --port, --control-stdin, --offline, --master-url, or Steam relay hosting options",
                file=sys.stderr,
            )
            return 2
        from server.fleet_launcher import run_fleet

        _configure_console_encoding()
        try:
            return run_fleet(
                arguments.fleet,
                runtime_root=runtime_paths.root,
                source_entry=SOURCE_ENTRYPOINT,
            )
        except (OSError, ValueError) as exc:
            print(f"Fleet startup failed: {exc}", file=sys.stderr)
            return 1
    try:
        runtime_paths = _select_config(runtime_paths, arguments.config)
    except (OSError, ValueError) as exc:
        print(f"Server startup failed: {exc}", file=sys.stderr)
        return 1
    if arguments.check:
        if arguments.offline or arguments.master_url is not None:
            return _emit_check_report(run_release_check(
                runtime_paths, offline=arguments.offline, master_url=arguments.master_url,
            ))
        return _emit_check_report(run_release_check(runtime_paths))
    if arguments.update:
        return _run_update_command(runtime_paths)
    if arguments.workshop_download is not None:
        return _run_workshop_download(runtime_paths, arguments.workshop_download)
    return _run_server(
        runtime_paths,
        config_transform=lambda config: _apply_network_options(config, arguments),
        offline=arguments.offline,
        master_url=arguments.master_url,
        control_stdin=arguments.control_stdin,
        status_file=(
            arguments.status_file.resolve()
            if arguments.status_file is not None
            else None
        ),
    )
