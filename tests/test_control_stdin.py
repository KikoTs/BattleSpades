"""Graceful parent-process control without gameplay-thread blocking."""

from __future__ import annotations

import asyncio
import os
import threading

from server import launcher, tutorial_launcher, ugc_launcher


async def _run_pipe_monitor(payload: bytes, *, close_writer: bool) -> list[str]:
    """Feed one real OS pipe through the production non-blocking poller."""

    read_fd, write_fd = os.pipe()
    reasons: list[str] = []
    try:
        with os.fdopen(read_fd, "rb", buffering=0) as stream:
            os.write(write_fd, payload)
            if close_writer:
                os.close(write_fd)
                write_fd = -1
            monitor = launcher._start_control_stdin_monitor(
                asyncio.get_running_loop(),
                reasons.append,
                stream=stream,
            )
            await asyncio.wait_for(monitor, timeout=1.0)
    finally:
        if write_fd >= 0:
            os.close(write_fd)
    return reasons


def test_control_stdin_accepts_only_exact_shutdown_line() -> None:
    """Whitespace, suffixes, and case variants cannot become commands."""

    main_thread = threading.get_ident()
    reasons = asyncio.run(
        _run_pipe_monitor(
            b" shutdown\nshutdown now\nSHUTDOWN\nshutdown\n",
            close_writer=False,
        )
    )

    assert reasons == ["parent requested shutdown on stdin"]
    assert threading.get_ident() == main_thread


def test_control_stdin_eof_requests_shutdown_after_ignored_input() -> None:
    """Closing the parent pipe gracefully retires the child."""

    reasons = asyncio.run(
        _run_pipe_monitor(b"ignored\n", close_writer=True)
    )

    assert reasons == ["parent stdin reached EOF"]


def test_control_stdin_accepts_unterminated_shutdown_at_eof() -> None:
    """EOF completes an exact final line just like buffered ``readline`` did."""

    reasons = asyncio.run(_run_pipe_monitor(b"shutdown", close_writer=True))

    assert reasons == ["parent requested shutdown on stdin"]


def test_control_stdin_decoder_bounds_unterminated_input() -> None:
    """A huge malformed line cannot grow memory or expose a suffix command."""

    decoder = launcher._ControlLineDecoder()

    assert not decoder.feed(b"x" * 5000)
    assert decoder.discarding_line
    assert not decoder.pending
    assert not decoder.feed(b"shutdown\n")
    assert not decoder.discarding_line
    assert decoder.feed(b"shutdown\r\n")


def test_serve_awaits_parent_requested_stop_to_completion(monkeypatch) -> None:
    """Event-loop teardown cannot cancel UGC's final persistence checkpoint."""

    events: list[str] = []
    instances = []

    class FakeServer:
        def __init__(self, _config, telemetry=None) -> None:
            self.running = False
            instances.append(self)

        async def start(self) -> None:
            events.append("started")
            self.running = True
            while self.running:
                await asyncio.sleep(0)
            events.append("start-returned")

        async def stop(self) -> None:
            events.append("stop-started")
            self.running = False
            # This represents UGCMode.deactivate writing the final VXL.
            await asyncio.sleep(0.01)
            events.append("stop-complete")

    monkeypatch.setattr("server.main.BattleSpadesServer", FakeServer)
    monkeypatch.setattr(
        "server.telemetry.TelemetryService",
        lambda _runtime: object(),
    )
    # Startup GC hardening is covered separately; never freeze pytest's graph.
    monkeypatch.setattr(launcher, "_freeze_import_graph_for_gc", lambda: True)
    monkeypatch.setattr(launcher.signal, "signal", lambda *_args: None)
    monitor_tasks = []

    def start_monitor(loop, callback):
        async def deliver_shutdown() -> None:
            await asyncio.sleep(0)
            callback("parent requested shutdown on stdin")

        task = loop.create_task(deliver_shutdown())
        monitor_tasks.append(task)
        return task

    monkeypatch.setattr(
        launcher,
        "_start_control_stdin_monitor",
        start_monitor,
    )

    asyncio.run(launcher._serve(object(), object(), control_stdin=True))

    assert len(instances) == 1
    assert len(monitor_tasks) == 1
    assert events[-1] == "stop-complete"
    assert events.count("stop-started") == 1


def test_all_entrypoints_expose_opt_in_stdin_control() -> None:
    """Normal, tutorial, and Map Creator launchers share the parent contract."""

    for module in (launcher, tutorial_launcher, ugc_launcher):
        arguments = module.build_parser().parse_args(["--control-stdin"])
        assert arguments.control_stdin is True
