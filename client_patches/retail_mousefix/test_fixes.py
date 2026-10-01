"""Regression tests; run directly with Python 2.7 or Python 3.

Set AOS_RETAIL_BUNDLE to the installed aos.pkg for Python 2.7 bytecode checks.
These checks extract individual methods without importing the game.
"""
import ctypes
import hashlib
import os
import shutil
import sys
import tempfile
import textwrap
import types
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import aosfix_runtime as core
import aos_mousefix as mouse
import aos_equipmentfix as equipment
import aos_uifix as ui


class Box(object):
    def __init__(self, **values):
        self.__dict__.update(values)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.restore = []
        self.logs = []
        self.runtime = core.Runtime(self.logs.append)
        self.meta_path = list(sys.meta_path)

    def set(self, owner, name, value):
        old = getattr(owner, name, core.MISSING)
        self.restore.append((owner, name, old))
        setattr(owner, name, value)

    def module(self, name, **attrs):
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        previous = sys.modules.get(name, core.MISSING)
        self.restore.append((sys.modules, name, previous))
        sys.modules[name] = module
        return module

    def tearDown(self):
        sys.meta_path[:] = self.meta_path
        for owner, name, old in reversed(self.restore):
            if owner is sys.modules:
                if old is core.MISSING:
                    owner.pop(name, None)
                else:
                    owner[name] = old
            elif old is core.MISSING:
                delattr(owner, name)
            else:
                setattr(owner, name, old)


class RuntimeTests(Fixture):
    def test_loaded_patch_is_idempotent(self):
        module = self.module('fixture_loaded', value=1)
        def change(m):
            self.runtime.replace('once', [(m, 'value', 1, 2)])
        self.runtime.watch(module.__name__, 'once', change)
        self.runtime.watch(module.__name__, 'once', change)
        self.assertEqual(module.value, 2)
        self.assertEqual(self.runtime.status, {'once': 'active'})

    def test_callback_failure_isolated(self):
        module = self.module('fixture_failed')
        def fail(m):
            raise ValueError('bad patch')
        self.runtime.watch(module.__name__, 'bad', fail)
        self.runtime.watch(module.__name__, 'good', lambda m: None)
        self.assertEqual(self.runtime.status, {'bad': 'failed', 'good': 'active'})

    def test_transaction_checks_every_attribute_before_writing(self):
        obj = Box(a=1, b=2)
        with self.assertRaises(RuntimeError):
            self.runtime.replace('bad', [(obj, 'a', 1, 10), (obj, 'b', 9, 20)])
        self.assertEqual((obj.a, obj.b), (1, 2))

    def test_transaction_rolls_back_if_assignment_fails(self):
        class Guard(object):
            a = 1
            @property
            def b(self):
                return 2
        obj = Guard()
        with self.assertRaises(AttributeError):
            self.runtime.replace('bad', [(obj, 'a', 1, 10), (obj, 'b', 2, 20)])
        self.assertEqual(obj.a, 1)
        self.assertNotIn('a', obj.__dict__)

    @unittest.skipUnless(sys.version_info[0] == 2, 'Real PEP 302 importer requires Python 2')
    def test_nested_imports_are_both_observed(self):
        folder = tempfile.mkdtemp(prefix='aos-import-test-')
        try:
            for name, source in [('aos_test_outer', 'import aos_test_inner\n'),
                                 ('aos_test_inner', 'value = 5\n')]:
                with open(os.path.join(folder, name + '.py'), 'w') as stream:
                    stream.write(source)
            sys.path.insert(0, folder)
            seen = []
            for name in ['aos_test_outer', 'aos_test_inner']:
                self.runtime.watch(name, name, lambda m: seen.append(m.__name__))
            __import__('aos_test_outer')
            self.assertEqual(seen, ['aos_test_inner', 'aos_test_outer'])
            self.assertNotIn(self.runtime, sys.meta_path)
        finally:
            sys.path.remove(folder)
            for name in ['aos_test_outer', 'aos_test_inner']:
                sys.modules.pop(name, None)
            shutil.rmtree(folder)

    @unittest.skipUnless(sys.version_info[0] == 2, 'Real PEP 302 importer requires Python 2')
    def test_failed_import_restores_observer(self):
        self.runtime.watch('aos_missing_test_module', 'missing', lambda m: None)
        with self.assertRaises(ImportError):
            __import__('aos_missing_test_module')
        self.assertIn('aos_missing_test_module', self.runtime.pending)
        self.assertIn(self.runtime, sys.meta_path)


class EquipmentTests(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        self.module('shared', __path__=[])
        self.constants = self.module('shared.constants_DLC',
                                    get_tool_dlc_Name=lambda item: 'dlc' if item == 13 else None,
                                    is_tool_selectable=lambda item, manager: False)
        self.menu = self.module('aoslib.scenes.ingame_menus.selectClass',
                                is_tool_selectable=self.constants.is_tool_selectable,
                                unrelated=lambda: 'stock')
        class GameClass(object):
            def __init__(self, class_id):
                self.id = class_id
            def is_selectable(self):
                return self.id == 1
        self.classes = self.module('aoslib.scenes.main.gameClass', GameClass=GameClass,
                                 get_character_dlc_Name=lambda item: 'dlc' if item in (5, 6) else None)
        equipment.install(self.runtime)

    def test_dlc_weapons_only_change_menu_binding(self):
        self.assertTrue(self.menu.is_tool_selectable(13, None))
        self.assertFalse(self.constants.is_tool_selectable(13, None))
        self.assertFalse(self.menu.is_tool_selectable(99, None))
        self.assertEqual(self.menu.unrelated(), 'stock')

    def test_both_dlc_characters_enabled(self):
        for class_id in (1, 5, 6):
            self.assertTrue(self.classes.GameClass(class_id).is_selectable())
        self.assertFalse(self.classes.GameClass(99).is_selectable())

    def test_repeated_install_does_not_wrap_again(self):
        original = self.menu.is_tool_selectable
        equipment.install(self.runtime)
        self.assertIs(self.menu.is_tool_selectable, original)

    def test_equipment_waits_for_retail_modules(self):
        for name in (self.menu.__name__, self.classes.__name__):
            del sys.modules[name]
        runtime = core.Runtime(self.logs.append)
        equipment.install(runtime)
        self.assertEqual(set(runtime.status.values()), set(['waiting']))
        self.assertEqual(len(runtime.pending), 2)


class InputAPI(object):
    def __init__(self):
        self.registered = []
        self.registration_calls = []
        self.procedures = {}
        self.forwarded = []
        self.packet = (9, -3, 0, 1)
        self.pressed = 0
        self.error = False

    def registrations(self):
        return list(self.registered)

    def register(self, hwnd):
        self.registration_calls.append(hwnd)
        self.registered = [(hwnd, 0)] if hwnd else []

    def subclass(self, hwnd, handler):
        self.procedures[hwnd] = handler
        return handler, 123

    def forward(self, previous, hwnd, message, wparam, lparam):
        self.forwarded.append(message)
        return 77

    def read(self, handle):
        if self.error:
            raise RuntimeError('read failed')
        return self.packet

    def buttons(self):
        return self.pressed

    def desktop_size(self, virtual):
        return (65536, 65536)


class MouseTests(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        self.events = []
        self.api = InputAPI()
        self.window = Box(_view_hwnd=10, _exclusive_mouse=True, _has_focus=True,
                          _exclusive_mouse_client=(50, 40), _height=100,
                          _get_modifiers=lambda: 7,
                          dispatch_event=lambda *args: self.events.append(args))
        self.controller = mouse.MouseController(self.api, self.logs.append)
        self.now = [0.0]
        self.controller.clock = lambda: self.now[0]
        self.controller.sync(self.window)

    def send(self, message=mouse.WM_INPUT):
        return self.api.procedures[self.window._view_hwnd](self.window._view_hwnd, message, 0, 1)

    def test_relative_counts_and_vertical_direction(self):
        self.assertEqual(self.send(), 77)
        self.assertEqual(self.events, [('on_mouse_motion', 50, 60, 9, 3)])
        self.assertIn(mouse.WM_INPUT, self.api.forwarded)

    def test_drag_preserves_buttons_and_modifiers(self):
        self.api.pressed = 1
        self.send()
        self.assertEqual(self.events[0], ('on_mouse_drag', 50, 60, 9, 3, 1, 7))

    def test_no_duplicate_legacy_motion(self):
        self.assertEqual(self.send(mouse.WM_MOUSEMOVE), 0)
        self.assertFalse(self.api.forwarded)

    def test_legacy_only_provider_cannot_freeze_the_camera(self):
        for index in range(4):
            self.now[0] = 1.0 + index * 0.11
            result = self.send(mouse.WM_MOUSEMOVE)
        self.assertEqual(result, 77)
        state = self.controller.states[self.window]
        self.assertTrue(state.legacy_fallback)
        self.assertFalse(state.disabled)
        self.assertEqual(self.api.registered, [(10, 0)])

    def test_raw_motion_recovers_after_watchdog_without_double_delivery(self):
        self.test_legacy_only_provider_cannot_freeze_the_camera()
        self.send(mouse.WM_INPUT)
        self.assertFalse(self.events)  # Its legacy half may already be delivered.
        self.assertFalse(self.controller.states[self.window].legacy_fallback)
        self.assertEqual(self.send(mouse.WM_MOUSEMOVE), 0)
        self.send(mouse.WM_INPUT)
        self.assertEqual(self.events, [('on_mouse_motion', 50, 60, 9, 3)])
        self.assertEqual(self.api.registration_calls, [10])

    def test_button_only_raw_packets_do_not_end_legacy_fallback(self):
        self.test_legacy_only_provider_cannot_freeze_the_camera()
        self.api.packet = (0, 0, 0, 1)
        self.send(mouse.WM_INPUT)
        self.assertTrue(self.controller.states[self.window].legacy_fallback)
        self.assertEqual(self.send(mouse.WM_MOUSEMOVE), 77)

    def test_restore_warps_do_not_trigger_fallback(self):
        for index in range(5):
            self.now[0] = index * 0.1
            self.assertEqual(self.send(mouse.WM_MOUSEMOVE), 0)
        self.assertFalse(self.controller.states[self.window].legacy_fallback)

    def test_repeated_focus_cycles_clear_transient_state(self):
        state = self.controller.states[self.window]
        for index in range(100):
            state.legacy_fallback = True
            state.failures = 2
            state.absolute[7] = (999, 999, False)
            self.window._has_focus = False
            self.controller.sync(self.window)
            self.assertFalse(self.api.registered)
            self.assertEqual(self.send(mouse.WM_MOUSEMOVE), 77)
            self.now[0] += 1.0
            self.window._has_focus = True
            self.controller.sync(self.window)
            self.assertFalse(state.legacy_fallback)
            self.assertEqual(state.failures, 0)
            self.assertEqual(state.absolute, {})
            self.assertEqual(self.send(mouse.WM_MOUSEMOVE), 0)
            self.send(mouse.WM_INPUT)
        self.assertEqual(len(self.events), 100)
        self.assertFalse(state.disabled)
        self.assertEqual(len(self.controller.views), 1)

    def test_foreign_registration_during_fallback_is_respected(self):
        self.test_legacy_only_provider_cannot_freeze_the_camera()
        self.api.registered = [(999, 0)]
        self.controller.sync(self.window)
        self.assertEqual(self.send(mouse.WM_MOUSEMOVE), 77)
        self.controller.close(self.window)
        self.assertEqual(self.api.registered, [(999, 0)])
        self.assertEqual(self.api.registration_calls, [10])

    def test_raw_input_after_idle_does_not_trigger_watchdog(self):
        now = [100.0]
        self.controller.clock = lambda: now[0]
        for index in range(8):
            now[0] = 100.0 + index * 0.1
            self.assertEqual(self.send(mouse.WM_MOUSEMOVE), 0)
            self.send(mouse.WM_INPUT)
        self.assertTrue(self.controller.states[self.window].active)

    def test_isolated_cursor_warps_do_not_trigger_watchdog(self):
        now = [0.0]
        self.controller.clock = lambda: now[0]
        for index in range(10):
            now[0] = float(index)
            self.assertEqual(self.send(mouse.WM_MOUSEMOVE), 0)
        self.assertTrue(self.controller.states[self.window].active)

    def test_menu_and_focus_loss_restore_stock_messages(self):
        for attribute in ('_has_focus', '_exclusive_mouse'):
            setattr(self.window, attribute, False)
            self.controller.sync(self.window)
            self.assertFalse(self.api.registered)
            self.assertEqual(self.send(mouse.WM_MOUSEMOVE), 77)
            setattr(self.window, attribute, True)
            self.controller.sync(self.window)
            self.assertTrue(self.api.registered)

    def test_repeated_capture_does_not_repeat_registration(self):
        for unused in range(10):
            self.controller.sync(self.window)
        self.assertEqual(self.api.registration_calls, [10])

    def test_three_read_failures_fall_back_and_cleanup(self):
        self.api.error = True
        for unused in range(3):
            self.assertEqual(self.send(), 77)
        self.assertTrue(self.controller.states[self.window].disabled)
        self.assertFalse(self.api.registered)
        self.assertEqual(self.send(mouse.WM_MOUSEMOVE), 77)
        self.controller.sync(self.window)
        self.assertEqual(self.api.registration_calls, [10, None])

    def test_one_read_failure_does_not_disable_input(self):
        self.api.error = True
        self.send()
        self.api.error = False
        self.send()
        self.assertEqual(len(self.events), 1)
        self.assertTrue(self.controller.states[self.window].active)

    def test_foreign_registration_is_not_overwritten_or_removed(self):
        self.api.registered = [(999, 0)]
        self.controller.sync(self.window)
        self.controller.close(self.window)
        self.assertEqual(self.api.registration_calls, [10])
        self.assertEqual(self.api.registered, [(999, 0)])

    def test_initial_registration_on_same_view_is_respected(self):
        controller = mouse.MouseController(self.api, self.logs.append)
        controller.sync(self.window)
        self.assertFalse(controller.states[self.window].active)
        controller.close(self.window)
        self.assertEqual(self.api.registration_calls, [10])

    def test_absolute_input_seeds_each_device_without_camera_jump(self):
        self.api.packet = (1000, 3000, 1, 8)
        self.send()
        self.assertFalse(self.events)
        self.api.packet = (1010, 3004, 1, 8)
        self.send()
        self.assertEqual(self.events[-1], ('on_mouse_motion', 50, 60, 10.0, -4.0))
        self.api.packet = (9000, 9000, 1, 9)
        self.send()
        self.assertEqual(len(self.events), 1)

    def test_view_recreation_and_callback_lifetime(self):
        old = self.api.procedures[10]
        self.assertEqual(self.send(mouse.WM_NCDESTROY), 77)
        self.assertNotIn(10, self.controller.views)
        self.assertIn(old, self.controller.retired_callbacks)
        self.window._view_hwnd = 20
        self.controller.sync(self.window)
        self.send()
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.api.registered, [(20, 0)])

    def test_dispatch_failure_keeps_native_cleanup(self):
        def fail(*args):
            raise ValueError('game event failed')
        self.window.dispatch_event = fail
        self.assertEqual(self.send(), 77)
        self.assertIn(mouse.WM_INPUT, self.api.forwarded)
        self.assertFalse(self.api.registered)


class MousePacketTests(unittest.TestCase):
    def reader(self, kind=0, declared_size=None, result_size=None):
        packet = mouse.MouseInput()
        packet.header.kind = kind
        packet.header.size = ctypes.sizeof(packet)
        packet.header.device = 17
        packet.mouse.dx, packet.mouse.dy, packet.mouse.flags = -91, 37, 2
        raw = ctypes.string_at(ctypes.byref(packet), ctypes.sizeof(packet))
        def read(handle, command, buffer, size, header_size):
            if buffer is None:
                size._obj.value = len(raw) if declared_size is None else declared_size
                return 0
            ctypes.memmove(buffer, raw, min(len(buffer), len(raw)))
            return len(raw) if result_size is None else result_size
        api = mouse.WindowsInput.__new__(mouse.WindowsInput)
        api.user = Box(GetRawInputData=read)
        return api

    def test_decode_real_abi_buffer(self):
        self.assertEqual(self.reader().read(1), (-91, 37, 2, 17))

    def test_keyboard_packet_ignored(self):
        self.assertIsNone(self.reader(kind=1).read(1))

    def test_short_and_oversized_buffers_rejected(self):
        for size in (0, 4, 4097):
            with self.assertRaises(RuntimeError):
                self.reader(declared_size=size).read(1)
        with self.assertRaises(RuntimeError):
            self.reader(result_size=ctypes.sizeof(mouse.InputHeader)).read(1)


class MouseInstallTests(Fixture):
    def install_window(self):
        api = InputAPI()
        self.set(mouse, 'WindowsInput', lambda: api)
        self.set(ctypes, 'sizeof', lambda unused: 4)
        class Window(object):
            _view_hwnd = 10
            _exclusive_mouse = _has_focus = True
            def set_exclusive_mouse(self, exclusive=True):
                self._exclusive_mouse = exclusive
            def close(self):
                self.closed = True
        window = Window()
        scheduled = []
        constants = self.module('pyglet.libs.win32.constants', QS_ALLINPUT=255)
        self.module('pyglet', __path__=[], version='1.2alpha1',
                    app=Box(windows=[window]),
                    clock=Box(schedule_interval=lambda fn, dt: scheduled.append(fn)))
        self.module('pyglet.libs', __path__=[])
        self.module('pyglet.libs.win32', __path__=[], constants=constants)
        self.module('pyglet.window.win32', Win32Window=Window)
        mouse.install(self.runtime)
        return window, api, constants, scheduled

    def test_existing_window_future_capture_and_raw_input_wake_mask(self):
        window, api, constants, scheduled = self.install_window()
        self.assertEqual(self.runtime.status['mouse.raw'], 'active')
        self.assertEqual(constants.QS_ALLINPUT, 0x04ff)
        self.assertEqual(api.registered, [(10, 0)])
        window.set_exclusive_mouse(exclusive=False)
        self.assertFalse(api.registered)
        window.set_exclusive_mouse(exclusive=True)
        self.assertEqual(api.registered, [(10, 0)])
        self.assertEqual(len(scheduled), 1)
        window.close()
        self.assertTrue(window.closed)
        self.assertFalse(api.registered)

    @unittest.skipUnless(os.environ.get('AOS_RETAIL_BUNDLE') and sys.version_info[0] == 2,
                         'Use Python 2.7 and AOS_RETAIL_BUNDLE for bytecode integration')
    def test_retail_focus_handlers_reenter_capture_hook_without_polling(self):
        window, api, unused_constants, unused_scheduled = self.install_window()
        lose = retail_method('pyglet/window/win32.py', 'Win32Window', '_event_killfocus')
        gain = retail_method('pyglet/window/win32.py', 'Win32Window', '_event_setfocus')
        window.dispatch_event = lambda *args: None
        window._exclusive_keyboard = False
        window.set_exclusive_keyboard = lambda unused: None
        controller = self.runtime.mouse
        state = controller.states[window]
        for unused in range(100):
            state.legacy_fallback = True
            self.assertEqual(lose(window, 0, 0, 0), 0)
            self.assertFalse(state.active)
            self.assertFalse(api.registered)
            self.assertEqual(gain(window, 0, 0, 0), 0)
            self.assertTrue(state.active)
            self.assertFalse(state.legacy_fallback)
            self.assertEqual(api.registered, [(10, 0)])
        self.assertEqual(len(controller.views), 1)


class ScrollTests(Fixture):
    def test_fractional_wheel_and_large_delta(self):
        owner = Box()
        self.assertEqual([ui.wheel_steps(owner, 0.25) for unused in range(4)], [0, 0, 0, 1])
        self.assertEqual(ui.wheel_steps(owner, -3), -3)

    def test_reversal_discards_stale_fraction(self):
        owner = Box()
        self.assertEqual(ui.wheel_steps(owner, 0.75), 0)
        self.assertEqual(ui.wheel_steps(owner, -0.5), 0)
        self.assertEqual(ui.wheel_steps(owner, -0.5), -1)

    def test_invalid_and_zero_wheel_do_nothing(self):
        for delta in [0, None, 'oops', float('nan'), float('inf'), -float('inf')]:
            self.assertEqual(ui.wheel_steps(Box(), delta), 0)

    def test_scrollbar_honors_focus_and_disabled_state(self):
        class Bar(object):
            enabled = focus = True
            scroll_pos = 10
            def on_mouse_scroll(self, *args):
                raise AssertionError('stock scrolling should be replaced')
            def set_scroll(self, value):
                self.scroll_pos = value
        ui._scrollbar(self.runtime, Box(VerticalScrollBar=Bar))
        bar = Bar()
        bar.on_mouse_scroll(0, 0, 0, 3)
        self.assertEqual(bar.scroll_pos, 7)
        for attribute in ['focus', 'enabled']:
            setattr(bar, attribute, False)
            bar.on_mouse_scroll(0, 0, 0, -2)
            self.assertEqual(bar.scroll_pos, 7)
            setattr(bar, attribute, True)

    def test_settings_wheel_is_limited_to_visible_hovered_panel(self):
        class Panel(object):
            enabled = visible = visible_content = True
        ui._settings(self.runtime, Box(MatchSettingsPanel=Panel))
        panel = Panel()
        positions = []
        panel.list_panel = Box(get_mouse_collides=lambda *a, **kw: False,
                               scrollbar=Box(scroll_pos=5, set_scroll=positions.append))
        panel.on_mouse_scroll(0, 0, 0, 1)
        self.assertFalse(positions)
        panel.list_panel.get_mouse_collides = lambda *a, **kw: True
        panel.on_mouse_scroll(0, 0, 0, -2)
        self.assertEqual(positions, [7])
        panel.visible = False
        panel.on_mouse_scroll(0, 0, 0, -2)
        self.assertEqual(positions, [7])


class DelayedCall(object):
    def __init__(self, active=True):
        self.running = active
    def active(self):
        return self.running
    def cancel(self):
        if not self.running:
            raise RuntimeError('AlreadyCalled')
        self.running = False


class TimerTests(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        class Menu(object):
            def on_start(self):
                self.on_refresh()
                self.auto_data_refresh_callback = DelayedCall()
            def on_stop(self):
                if self.auto_data_refresh_callback:
                    self.auto_data_refresh_callback.cancel()
                self.stopped = True
            def on_refresh(self):
                if getattr(self, 'leave_during_refresh', False):
                    self.on_stop()
            def on_auto_data_refresh(self):
                self.on_refresh()
                self.auto_data_refresh_callback = DelayedCall()
                self.after_refresh = True
        ui._timers(self.runtime, Box(BaseSquadsMenu=Menu))
        self.menu = Menu()
        self.menu.on_start()

    def test_stop_cancels_pending_and_clears_handle(self):
        call = self.menu.auto_data_refresh_callback
        self.menu.on_stop()
        self.assertFalse(call.active())
        self.assertIsNone(self.menu.auto_data_refresh_callback)
        self.menu.on_stop()

    def test_expired_callback_can_be_stopped(self):
        self.menu.auto_data_refresh_callback = DelayedCall(False)
        self.menu.on_stop()
        self.assertTrue(self.menu.stopped)

    def test_leaving_during_refresh_cannot_reschedule(self):
        self.menu.leave_during_refresh = True
        self.menu.auto_data_refresh_callback = DelayedCall(False)
        self.menu.on_auto_data_refresh()
        self.assertIsNone(self.menu.auto_data_refresh_callback)
        self.assertNotIn('after_refresh', self.menu.__dict__)
        self.assertNotIn('on_refresh', self.menu.__dict__)
        self.menu.on_auto_data_refresh()
        self.assertNotIn('after_refresh', self.menu.__dict__)

    def test_restart_cancels_previous_timer(self):
        old = self.menu.auto_data_refresh_callback
        self.menu.on_start()
        self.assertFalse(old.active())
        self.assertTrue(self.menu.auto_data_refresh_callback.active())

    def test_normal_refresh_still_runs_stock_body(self):
        self.menu.on_auto_data_refresh()
        self.assertTrue(self.menu.after_refresh)
        self.assertTrue(self.menu.auto_data_refresh_callback.active())

    def test_leaving_during_start_cancels_late_timer(self):
        self.menu.leave_during_refresh = True
        self.menu.on_start()
        self.assertIsNone(self.menu.auto_data_refresh_callback)


class BootstrapTests(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        self.folder = tempfile.mkdtemp(prefix='aos-bootstrap-test-')
        self.set(sys, 'executable', os.path.join(self.folder, 'aos.exe'))
        self.set(sys, 'version_info', (2, 7, 18))
        self.set(sys, 'platform', 'win32')
        self.set(sys, 'argv', ['aos.exe'])
        self.set(sys, '_aos_retail_runtime', None)
        for name in ['aos_mousefix', 'aos_equipmentfix', 'aos_uifix', 'aos_movementfix']:
            self.set(sys, '_' + name + '_loaded', False)
            self.module(name)
            self.write(name + '.py', 'def install(runtime):\n    runtime.status[__name__] = "active"\n')
        self.module('aosfix_runtime')
        shutil.copyfile(os.path.join(ROOT, 'aosfix_runtime.py'), os.path.join(self.folder, 'aosfix_runtime.py'))
        self.write('aos.pkg', 'fixture')
        self.set(hashlib, 'sha256', lambda data: Box(hexdigest=lambda:
                 'c0d0cdc6f61f4b58172f74faf036c6f323b1cdbe59193f595fcce7d2a524e52c'))

    def write(self, name, text):
        with open(os.path.join(self.folder, name), 'w') as stream:
            stream.write(text)

    def run_loader(self):
        with open(os.path.join(ROOT, 'bootstrap.py')) as stream:
            exec(compile(stream.read(), 'bootstrap.py', 'exec'), {})

    def tearDown(self):
        Fixture.tearDown(self)
        shutil.rmtree(self.folder)

    def test_all_features_load_once_without_bytecode(self):
        self.run_loader()
        first = sys.modules['aos_mousefix']
        self.run_loader()
        self.assertIs(first, sys.modules['aos_mousefix'])
        self.assertEqual(len(sys._aos_retail_runtime.status), 4)
        self.assertFalse([name for name in os.listdir(self.folder) if name.endswith('.pyc')])

    def test_independent_disable_switches(self):
        sys.argv = ['aos.exe', '/LEGACYMOUSE', '+legacyui', '+legacymovement']
        self.run_loader()
        self.assertFalse(sys._aos_mousefix_loaded)
        self.assertFalse(sys._aos_uifix_loaded)
        self.assertFalse(sys._aos_movementfix_loaded)
        self.assertTrue(sys._aos_equipmentfix_loaded)

    def test_disable_file(self):
        self.write('aos_equipmentfix.disabled', '')
        self.run_loader()
        self.assertFalse(sys._aos_equipmentfix_loaded)
        self.assertTrue(sys._aos_mousefix_loaded)

    def test_broken_feature_does_not_prevent_other_features(self):
        self.write('aos_mousefix.py', 'raise RuntimeError("fixture")')
        self.run_loader()
        self.assertFalse(sys._aos_mousefix_loaded)
        self.assertTrue(sys._aos_equipmentfix_loaded)
        self.assertTrue(sys._aos_uifix_loaded)

    def test_unknown_bundle_skips_every_patch(self):
        hashlib.sha256 = lambda data: Box(hexdigest=lambda: 'unsupported')
        self.run_loader()
        self.assertIsNone(sys._aos_retail_runtime)

    def test_missing_file_does_not_prevent_others(self):
        os.remove(os.path.join(self.folder, 'aos_uifix.py'))
        self.run_loader()
        self.assertFalse(sys._aos_uifix_loaded)
        self.assertTrue(sys._aos_mousefix_loaded)

    def test_unsupported_python_skips_every_patch(self):
        sys.version_info = (3, 12)
        self.run_loader()
        self.assertIsNone(sys._aos_retail_runtime)


RETAIL = os.environ.get('AOS_RETAIL_BUNDLE')


def retail_method(relative, class_name, name, globals_=None):
    from retail_bytecode import RetailBundle
    return RetailBundle(RETAIL).method(relative[:-3].replace('/', '.'), class_name, name, globals_)


class Row(object):
    enable_on_scroll = True
    def set_enabled(self, value):
        self.enabled = value
    def update_position(self, x, y, width, height, highlight):
        self.y = y


class CategoryRow(Row):
    def enable_draw_spacing(self, *args):
        pass


@unittest.skipUnless(RETAIL and sys.version_info[0] == 2,
                     'Use Python 2.7 and AOS_RETAIL_BUNDLE for bytecode integration')
class RetailMethodsTests(Fixture):
    def layout(self, filename, class_name, rows):
        name = 'set_list_items_position_on_scroll'
        original = retail_method('aoslib/scenes/frontend/' + filename + '.py', class_name, name,
                                 {'CategoryListItem': CategoryRow})
        cls = type(class_name, (object,), {name: original})
        # Retail already skips hidden rows correctly. Revival's layout changes
        # repair a missing-continue decompilation artifact, so DO NOT port them.
        panel = cls()
        panel.__dict__.update(rows=rows, min_index=2, max_index=6, row_height=30,
                              line_spacing=4, top_padding=5, first_item_y_offset=10,
                              category_spacing_colour=(0, 0, 0, 0), category_row_spacing=2,
                              get_row_height=lambda row: 40 if type(row) is CategoryRow else 30)
        panel.set_list_items_position_on_scroll(100, 400, 250)
        return panel

    def test_generic_visible_row_stays_at_top(self):
        rows = [Row() for unused in range(7)]
        self.layout('listPanelBase', 'ListPanelBase', rows)
        self.assertEqual(rows[2].y, 400)
        self.assertFalse(rows[0].enabled)

    def test_leaderboard_preserves_header_space(self):
        rows = [Row() for unused in range(7)]
        self.layout('leaderboardListPanel', 'LeaderboardListPanel', rows)
        self.assertEqual([row.y for row in rows[2:5]], [365, 331, 297])
        self.assertFalse(rows[5].enabled)

    def test_expandable_rows_respect_variable_heights_and_visibility(self):
        rows = [CategoryRow(), Row(), Row(), CategoryRow(), Row()]
        self.layout('expandableListPanel', 'ExpandableListPanel', rows)
        self.assertEqual([row.y for row in rows[2:]], [400, 356, 322])
        self.assertEqual([row.visible for row in rows], [False, False, True, True, True])

    def test_profile_first_row_remains_fixed(self):
        rows = [Row() for unused in range(7)]
        self.layout('playerProfileMenu', 'PlayerProfileSummaryListPanel', rows)
        self.assertEqual([rows[i].y for i in [0, 3, 4, 5]], [410, 366, 332, 298])
        self.assertTrue(rows[0].enabled)
        self.assertFalse(rows[2].enabled)

    def test_real_character_predicate_uses_patched_class_method(self):
        lookup = lambda item: 'mafia' if item in (5, 6) else None
        original = retail_method('aoslib/scenes/main/gameClass.py', 'GameClass', 'is_selectable',
                                 {'get_character_dlc_Name': lookup})
        cls = type('GameClass', (object,), {'is_selectable': original})
        character = cls()
        character.id = 5
        character.manager = Box(dlc_manager=Box(is_installed_dlc=lambda name: False))
        self.assertFalse(character.is_selectable())
        self.module('aoslib.scenes.main.gameClass', GameClass=cls, get_character_dlc_Name=lookup)
        equipment.install(self.runtime)
        self.assertTrue(character.is_selectable())

    def test_real_menu_refresh_does_not_schedule_after_exit(self):
        scheduled, network = [], []
        original = retail_method('aoslib/scenes/frontend/baseSquadsMenu.py', 'BaseSquadsMenu',
                                 'on_auto_data_refresh',
                                 {'reactor': Box(callLater=lambda *a: scheduled.append(a)),
                                  'SteamRefreshLobbyData': network.append})
        cls = type('BaseSquadsMenu', (object,), {
            'on_start': lambda self: None, 'on_stop': lambda self: None,
            'on_auto_data_refresh': original,
            'on_refresh': lambda self: self.on_stop()})
        ui._timers(self.runtime, Box(BaseSquadsMenu=cls))
        menu = cls()
        menu.on_start()
        menu.on_auto_data_refresh()
        self.assertFalse(scheduled)
        self.assertFalse(network)


if __name__ == '__main__':
    unittest.main(verbosity=2)
