"""Graceful parent-process control without gameplay-thread blocking."""

from __future__ import annotations

import asyncio
from io import BytesIO, StringIO
import threading

from server import launcher, tutorial_launcher, ugc_launcher


class _ImmediateLoop:
    """Small call_soon_threadsafe recorder used by the daemon-reader tests."""

    def __init__(self) -> None:
        self.caller_threads: list[int] = []

    def call_soon_threadsafe(self, callback, *args) -> None:
        self.caller_threads.append(threading.get_ident())
        callback(*args)


def test_control_stdin_accepts_only_exact_shutdown_line() -> None:
    """Whitespace, suffixes, and case variants cannot become commands."""

    loop = _ImmediateLoop()
    reasons: list[str] = []
    main_thread = threading.get_ident()
    stream = StringIO(" shutdown\nshutdown now\nSHUTDOWN\nshutdown\n")

    monitor = launcher._start_control_stdin_monitor(
        loop,
        reasons.append,
        stream=stream,
    )
    monitor.join(timeout=1.0)

    assert not monitor.is_alive()
    assert reasons == ["parent requested shutdown on stdin"]
    assert all(thread_id != main_thread for thread_id in loop.caller_threads)


def test_control_stdin_eof_requests_shutdown_for_text_and_binary_pipes() -> None:
    """Closing either Popen pipe representation gracefully retires the child."""

    for stream in (StringIO(""), BytesIO(b"ignored\n")):
        loop = _ImmediateLoop()
        reasons: list[str] = []

        monitor = launcher._start_control_stdin_monitor(
            loop,
            reasons.append,
            stream=stream,
        )
        monitor.join(timeout=1.0)

        assert not monitor.is_alive()
        assert reasons == ["parent stdin reached EOF"]


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
    monkeypatch.setattr(launcher.signal, "signal", lambda *_args: None)
    start_monitor = launcher._start_control_stdin_monitor
    monkeypatch.setattr(
        launcher,
        "_start_control_stdin_monitor",
        lambda loop, callback: start_monitor(
            loop,
            callback,
            stream=StringIO("shutdown\n"),
        ),
    )

    asyncio.run(launcher._serve(object(), object(), control_stdin=True))

    assert len(instances) == 1
    assert events[-1] == "stop-complete"
    assert events.count("stop-started") == 1


def test_all_entrypoints_expose_opt_in_stdin_control() -> None:
    """Normal, tutorial, and Map Creator launchers share the parent contract."""

    for module in (launcher, tutorial_launcher, ugc_launcher):
        arguments = module.build_parser().parse_args(["--control-stdin"])
        assert arguments.control_stdin is True
