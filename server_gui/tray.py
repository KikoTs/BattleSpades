"""Optional system tray integration; every app action is queued onto Tk."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol


class TrayIcon(Protocol):
    visible: bool
    title: str

    def run_detached(self, setup: Callable) -> None: ...
    def update_menu(self) -> None: ...
    def stop(self) -> None: ...


def _make_icon(owner: ServerTray) -> TrayIcon:
    # Import lazily: console/headless installs do not need a desktop backend.
    import pystray
    from PIL import Image

    if not pystray.Icon.HAS_MENU:
        raise RuntimeError("This desktop does not support tray menus")
    with Image.open(Path(__file__).parent / "assets" / "battlespades-server.png") as source:
        image = source.convert("RGBA")
    def item_for(action: str):
        return pystray.MenuItem(
            action, lambda icon, item: owner.invoke(action),
            enabled=lambda item: owner.enabled(action),
            default=action == "Settings",
        )
    items = [item_for(action) for action in ("Start", "Stop", "Settings", "Exit")]
    return pystray.Icon("battlespades-server", image, owner.title, pystray.Menu(*items))


class ServerTray:
    """One icon per host window, with guarded asynchronous startup/shutdown."""

    def __init__(self, dispatch: Callable[[Callable[[], None]], None],
                 actions: dict[str, Callable[[], None]], *,
                 icon_factory: Callable[[ServerTray], TrayIcon] = _make_icon) -> None:
        self.dispatch = dispatch
        self.actions = actions
        self.icon_factory = icon_factory
        self.icon: TrayIcon | None = None
        self.state = "stopped"
        self.closing = False
        self.ready = False
        self.closed = False

    @property
    def title(self) -> str:
        return f"BattleSpades Server — {self.state.capitalize()}"

    def enabled(self, action: str) -> bool:
        if self.closed or self.closing:
            return False
        if action == "Start":
            return self.state == "stopped"
        if action == "Stop":
            return self.state in ("running", "starting")
        return action in self.actions

    def invoke(self, action: str) -> None:
        def on_ui_thread() -> None:
            # Check again after queueing: state can change while a menu is open.
            if self.enabled(action):
                self.actions[action]()
        self.dispatch(on_ui_thread)

    def start(self, on_ready: Callable[[], None], on_error: Callable[[str], None]) -> None:
        if self.closed:
            return
        if self.ready:
            on_ready()
            return
        if self.icon is not None:
            return
        try:
            icon = self.icon_factory(self)
            self.icon = icon

            def setup(active: TrayIcon) -> None:
                try:
                    if self.closed:
                        active.stop()
                        return
                    active.visible = True
                    if self.closed:
                        active.stop()
                        return
                except Exception as exc:
                    self.dispatch(lambda message=str(exc): failed(message))
                    return

                def ready() -> None:
                    if self.closed or self.icon is not active:
                        active.stop()
                        return
                    self.ready = True
                    self.update(self.state, closing=self.closing)
                    on_ready()
                self.dispatch(ready)

            def failed(message: str) -> None:
                if self.closed:
                    return
                if self.icon is not None:
                    self.icon.stop()
                self.icon = None
                self.ready = False
                on_error(message)

            icon.run_detached(setup=setup)
        except Exception as exc:
            if self.icon is not None:
                self.icon.stop()
            self.icon = None
            on_error(str(exc))

    def update(self, state: str, *, closing: bool = False) -> None:
        self.state, self.closing = state, closing
        if self.ready and self.icon is not None:
            self.icon.title = self.title
            self.icon.update_menu()

    def stop(self) -> None:
        self.closed = True
        self.ready = False
        if self.icon is not None:
            self.icon.stop()
            self.icon = None
