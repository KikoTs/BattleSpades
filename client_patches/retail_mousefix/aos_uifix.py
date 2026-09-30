"""Ports of Revival's scrolling and timer lifecycle fixes.

No custom lobby creation, hosting, connection, browser or relay code is used.
See PROVENANCE.md for source commits. Compatible with Python 2.7.
"""
import math
from aosfix_runtime import MISSING, method


def wheel_steps(owner, delta):
    try:
        delta = float(delta)
    except (ValueError, TypeError, OverflowError):
        return 0
    if not delta or math.isnan(delta) or math.isinf(delta):
        return 0
    remainder = getattr(owner, '_retail_wheel_remainder', 0.0)
    if remainder * delta < 0:
        remainder = 0.0
    total = remainder + delta
    steps = int(abs(total) + 1e-9)
    if total < 0:
        steps = -steps
    owner._retail_wheel_remainder = total - steps
    return steps


def _scrollbar(runtime, module):
    cls = module.VerticalScrollBar
    original = method(cls, 'on_mouse_scroll')

    def scroll(self, x, y, scroll_x, scroll_y):
        if self.enabled and self.focus:
            steps = wheel_steps(self, scroll_y)
            if steps:
                self.set_scroll(int(self.scroll_pos) - steps)

    runtime.replace('ui.scrollbar', [(cls, 'on_mouse_scroll', original, scroll)])


def _settings(runtime, module):
    cls = module.MatchSettingsPanel
    original = getattr(cls, 'on_mouse_scroll', MISSING)

    def scroll(self, x, y, scroll_x, scroll_y):
        if not (self.enabled and self.visible and self.visible_content):
            return
        panel = self.list_panel
        if not panel.get_mouse_collides(x, y, include_scrollbar=True):
            return
        bar = panel.scrollbar
        if bar is not None:
            steps = wheel_steps(self, scroll_y)
            if steps:
                bar.set_scroll(int(bar.scroll_pos) - steps)

    runtime.replace('ui.settings_wheel', [(cls, 'on_mouse_scroll', original, scroll)])


class _MenuClosed(Exception):
    pass


def _cancel_refresh(menu):
    callback = getattr(menu, 'auto_data_refresh_callback', None)
    menu.auto_data_refresh_callback = None
    if callback is not None and callback.active():
        callback.cancel()


def _timers(runtime, module):
    cls = module.BaseSquadsMenu
    start = method(cls, 'on_start')
    stop = method(cls, 'on_stop')
    refresh = method(cls, 'on_auto_data_refresh')

    def on_start(self, *args, **kwargs):
        _cancel_refresh(self)
        self._retail_menu_closed = False
        try:
            return start(self, *args, **kwargs)
        except BaseException:
            self._retail_menu_closed = True
            _cancel_refresh(self)
            raise
        finally:
            if self._retail_menu_closed:
                _cancel_refresh(self)

    def on_stop(self, *args, **kwargs):
        self._retail_menu_closed = True
        _cancel_refresh(self)
        return stop(self, *args, **kwargs)

    def on_auto_data_refresh(self, *args, **kwargs):
        if getattr(self, '_retail_menu_closed', False):
            return
        # A firing Twisted call is already inactive. Clear it before a refresh
        # can navigate away and invoke on_stop.
        self.auto_data_refresh_callback = None
        saved = self.__dict__.get('on_refresh', MISSING)
        original_refresh = self.on_refresh

        def guarded_refresh(*a, **kw):
            result = original_refresh(*a, **kw)
            if getattr(self, '_retail_menu_closed', False):
                raise _MenuClosed()
            return result

        self.on_refresh = guarded_refresh
        try:
            return refresh(self, *args, **kwargs)
        except _MenuClosed:
            return
        finally:
            if saved is MISSING:
                del self.on_refresh
            else:
                self.on_refresh = saved
            if getattr(self, '_retail_menu_closed', False):
                _cancel_refresh(self)

    runtime.replace('ui.menu_timer', [
        (cls, 'on_start', start, on_start),
        (cls, 'on_stop', stop, on_stop),
        (cls, 'on_auto_data_refresh', refresh, on_auto_data_refresh)])


def install(runtime):
    targets = [
        ('aoslib.gui', 'ui.scrollbar', _scrollbar),
        ('aoslib.scenes.frontend.matchSettingsPanel', 'ui.settings_wheel', _settings),
        ('aoslib.scenes.frontend.baseSquadsMenu', 'ui.menu_timer', _timers)]
    for name, tag, patch in targets:
        runtime.watch(name, tag, lambda module, patch=patch: patch(runtime, module))
