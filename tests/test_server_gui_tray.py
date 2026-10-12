"""Tray actions must use the UI queue and never hide an unreachable window."""

from collections import deque

from server_gui.tray import ServerTray


class FakeIcon:
    visible = False
    title = ""

    def __init__(self):
        self.setup = None
        self.stopped = False

    def run_detached(self, setup):
        self.setup = setup

    def update_menu(self):
        pass

    def stop(self):
        self.stopped = True
        self.visible = False


def make_tray():
    calls, events = deque(), []
    icon = FakeIcon()
    tray = ServerTray(calls.append, {
        action: lambda name=action: events.append(name)
        for action in ("Start", "Stop", "Settings", "Exit")
    }, icon_factory=lambda owner: icon)
    return tray, icon, calls, events


def test_menu_dispatches_and_rechecks_server_state():
    tray, _, calls, events = make_tray()
    tray.invoke("Start")
    assert events == []
    tray.update("running")
    calls.popleft()()
    assert events == []  # Stale Start from a previously opened menu.
    assert not tray.enabled("Start") and tray.enabled("Stop")
    tray.invoke("Stop")
    calls.popleft()()
    assert events == ["Stop"]
    tray.update("stopping")
    assert not tray.enabled("Stop")
    for action in ("Settings", "Exit"):
        tray.invoke(action)
        calls.popleft()()
    assert events == ["Stop", "Settings", "Exit"]
    tray.update("stopped", closing=True)
    assert not any(tray.enabled(action) for action in tray.actions)


def test_ready_is_dispatched_after_the_icon_is_visible():
    tray, icon, calls, events = make_tray()
    tray.start(lambda: events.append("hide"), lambda message: events.append(message))
    assert not tray.ready and events == []
    icon.setup(icon)
    assert icon.visible and events == []
    calls.popleft()()
    assert tray.ready and events == ["hide"]
    tray.update("running")
    assert icon.title.endswith("Running")
    tray.stop()
    assert icon.stopped and not tray.ready


def test_exit_during_startup_does_not_leave_an_icon_or_run_queued_actions():
    tray, icon, calls, events = make_tray()
    tray.start(lambda: events.append("hide"), lambda message: events.append(message))
    tray.invoke("Start")
    tray.stop()
    icon.setup(icon)
    while calls:
        calls.popleft()()
    assert events == [] and not icon.visible and icon.stopped


def test_exit_after_setup_but_before_ready_does_not_hide_window():
    tray, icon, calls, events = make_tray()
    tray.start(lambda: events.append("hide"), lambda message: events.append(message))
    icon.setup(icon)
    tray.stop()
    calls.popleft()()
    assert events == [] and icon.stopped


def test_unavailable_backend_reports_error_without_hiding():
    tray, _, _, events = make_tray()

    def unavailable(owner):
        raise RuntimeError("No system tray")

    tray.icon_factory = unavailable
    tray.start(lambda: events.append("hide"), events.append)
    assert events == ["No system tray"]
    assert tray.icon is None and not tray.ready
