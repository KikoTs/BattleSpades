"""Retail raw mouse input, based on AoS Revival's WM_INPUT design.

Relative device counts reach Pyglet directly. Menus retain legacy input. The
native view subclass supports the already-created retail window; it never
changes executable bytes or replaces a game/dependency file. Python 2.7.
"""
import ctypes
import time
from aosfix_runtime import method

default_timer = getattr(time, 'perf_counter', None) or time.clock

WM_INPUT = 0x00ff
WM_MOUSEMOVE = 0x0200
WM_NCDESTROY = 0x0082
RID_INPUT = 0x10000003
UINT_ERROR = 0xffffffff

# Windows ABI types are fixed-width, except HANDLE and WPARAM.
class InputHeader(ctypes.Structure):
    _fields_ = [('kind', ctypes.c_uint32), ('size', ctypes.c_uint32),
                ('device', ctypes.c_void_p), ('wparam', ctypes.c_size_t)]


class MousePacket(ctypes.Structure):
    _fields_ = [('flags', ctypes.c_uint16), ('buttons', ctypes.c_uint32),
                ('raw_buttons', ctypes.c_uint32), ('dx', ctypes.c_int32),
                ('dy', ctypes.c_int32), ('extra', ctypes.c_uint32)]


class MouseInput(ctypes.Structure):
    _fields_ = [('header', InputHeader), ('mouse', MousePacket)]


class DeviceRegistration(ctypes.Structure):
    _fields_ = [('page', ctypes.c_uint16), ('usage', ctypes.c_uint16),
                ('flags', ctypes.c_uint32), ('window', ctypes.c_void_p)]


class WindowsInput(object):
    def __init__(self):
        # Private ctypes function objects; do not mutate Pyglet's signatures.
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        pointer = ctypes.c_void_p
        uint = ctypes.c_uint
        signatures = [
            ('GetRawInputData', [pointer, uint, pointer, ctypes.POINTER(uint), uint], uint),
            ('GetRegisteredRawInputDevices', [ctypes.POINTER(DeviceRegistration),
                                            ctypes.POINTER(uint), uint], uint),
            ('RegisterRawInputDevices', [ctypes.POINTER(DeviceRegistration), uint, uint], ctypes.c_int),
            ('GetAsyncKeyState', [ctypes.c_int], ctypes.c_short),
            ('GetSystemMetrics', [ctypes.c_int], ctypes.c_int),
            ('CallWindowProcW', [pointer, pointer, uint, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_ssize_t)]
        self.set_proc = getattr(self.user, 'SetWindowLongW' if ctypes.sizeof(pointer) == 4
                                else 'SetWindowLongPtrW')
        self.set_proc.argtypes = [pointer, ctypes.c_int, pointer]
        self.set_proc.restype = pointer
        for name, args, result in signatures:
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = args, result
        self.callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, pointer, uint,
                                               ctypes.c_size_t, ctypes.c_ssize_t)

    def registrations(self):
        count = ctypes.c_uint(0)
        size = ctypes.sizeof(DeviceRegistration)
        query = self.user.GetRegisteredRawInputDevices
        if query(None, ctypes.byref(count), size) == UINT_ERROR or count.value > 256:
            raise RuntimeError('Cannot query raw-input registrations')
        if not count.value:
            return []
        records = (DeviceRegistration * count.value)()
        result = query(records, ctypes.byref(count), size)
        if result == UINT_ERROR or result > len(records):
            raise RuntimeError('Raw-input registration query changed')
        return [(row.window, row.flags) for row in records[:result]
                if row.page == 1 and row.usage == 2]

    def register(self, hwnd):
        # Flags 0 keeps legacy button/wheel messages and requires foreground
        # focus. RIDEV_REMOVE (1) requires a NULL target.
        record = DeviceRegistration(1, 2, 0 if hwnd else 1, hwnd)
        if not self.user.RegisterRawInputDevices(ctypes.byref(record), 1, ctypes.sizeof(record)):
            raise ctypes.WinError(ctypes.get_last_error())

    def read(self, handle):
        size = ctypes.c_uint(0)
        header_size = ctypes.sizeof(InputHeader)
        read = self.user.GetRawInputData
        if read(handle, RID_INPUT, None, ctypes.byref(size), header_size) == UINT_ERROR:
            raise RuntimeError('Raw-input size query failed')
        if not header_size <= size.value <= 4096:
            raise RuntimeError('Invalid raw-input buffer size')
        buffer = ctypes.create_string_buffer(size.value)
        received = read(handle, RID_INPUT, buffer, ctypes.byref(size), header_size)
        if received == UINT_ERROR or not header_size <= received <= len(buffer):
            raise RuntimeError('Raw-input read failed')
        header = ctypes.cast(buffer, ctypes.POINTER(InputHeader)).contents
        if header.kind != 0:
            return None
        if received < ctypes.sizeof(MouseInput):
            raise RuntimeError('Truncated raw mouse packet')
        packet = ctypes.cast(buffer, ctypes.POINTER(MouseInput)).contents
        return (packet.mouse.dx, packet.mouse.dy, packet.mouse.flags, packet.header.device)

    def subclass(self, hwnd, handler):
        callback = self.callback_type(handler)
        previous = self.set_proc(hwnd, -4, ctypes.cast(callback, ctypes.c_void_p))
        if not previous:
            raise ctypes.WinError(ctypes.get_last_error())
        return callback, previous

    def forward(self, previous, hwnd, message, wparam, lparam):
        return self.user.CallWindowProcW(previous, hwnd, message, wparam, lparam)

    def buttons(self):
        # Pyglet LEFT=1, MIDDLE=2, RIGHT=4. Respect swapped Windows buttons.
        left, right = (2, 1) if self.user.GetSystemMetrics(23) else (1, 2)
        return sum(mask for key, mask in [(left, 1), (4, 2), (right, 4)]
                   if self.user.GetAsyncKeyState(key) & 0x8000)

    def desktop_size(self, virtual):
        return (self.user.GetSystemMetrics(78 if virtual else 0),
                self.user.GetSystemMetrics(79 if virtual else 1))


class WindowState(object):
    def __init__(self, window):
        self.window = window
        self.hwnd = None
        self.active = False
        self.owned = False
        self.disabled = False
        self.legacy_fallback = False
        self.capture_started = None
        self.failures = 0
        self.absolute = {}
        self.reported_motion = False
        self.last_motion = None
        self.legacy_started = None
        self.last_legacy = None
        self.legacy_count = 0


class MouseController(object):
    def __init__(self, api, log, clock=default_timer):
        self.api, self.log = api, log
        self.clock = clock
        self.states = {}
        self.views = {}
        # A native procedure can remain in another subclass's call chain.
        # Keep its ctypes thunk alive for the lifetime of the Python runtime.
        self.retired_callbacks = []

    @staticmethod
    def focused(window):
        return (bool(getattr(window, '_exclusive_mouse', False)) and
                bool(getattr(window, '_has_focus', False)))

    def release(self, state):
        state.active = False
        self.reset_capture(state)
        owned = state.owned
        state.owned = False
        if owned and state.hwnd and self.api.registrations() == [(state.hwnd, 0)]:
            self.api.register(None)

    def reset_capture(self, state):
        # Focus/capture transitions start a new input session. In particular,
        # cursor warps and read errors from before minimize are not evidence
        # that the restored window's device has stopped producing raw input.
        state.absolute.clear()
        state.failures = 0
        state.last_motion = None
        state.legacy_started = state.last_legacy = None
        state.legacy_count = 0
        state.legacy_fallback = False
        state.capture_started = None

    def fail(self, state, reason):
        state.disabled = True
        state.active = False
        try:
            self.release(state)
        except Exception:
            pass
        self.log('mouse: stock input restored for this window: %s' % reason)

    def sync(self, window):
        state = self.states.get(window)
        if state is None:
            state = self.states[window] = WindowState(window)
        try:
            hwnd = getattr(window, '_view_hwnd', None)
            if not hwnd or state.disabled or not self.focused(window):
                if state.owned:
                    self.release(state)
                return
            if state.hwnd != hwnd:
                if state.owned:
                    self.release(state)
                state.hwnd = hwnd
            if hwnd not in self.views:
                view = {'state': state}

                def procedure(h, message, wparam, lparam):
                    return self.message(view, h, message, wparam, lparam)

                callback, previous = self.api.subclass(hwnd, procedure)
                view.update(callback=callback, previous=previous)
                self.views[hwnd] = view
            registrations = self.api.registrations()
            if registrations and (not state.owned or registrations != [(hwnd, 0)]):
                # Another component owns raw input. Do not steal its target.
                state.active = False
                state.owned = False
                self.reset_capture(state)
                return
            if not registrations:
                self.api.register(hwnd)
                state.owned = True
            if not state.active:
                self.reset_capture(state)
                state.capture_started = self.clock()
                self.log('mouse: raw input active on view %s.' % hwnd)
            state.active = True
        except Exception as error:
            self.fail(state, repr(error))

    def message(self, view, hwnd, message, wparam, lparam):
        state = view['state']
        try:
            active = state.active and state.hwnd == hwnd and self.focused(state.window)
            if active and message == WM_INPUT:
                self.motion(state, lparam)
            elif active and message == WM_MOUSEMOVE:
                if not self.legacy_only(state):
                    return 0  # Do not count movement through both APIs.
        except Exception as error:
            self.fail(state, repr(error))
        # WM_INPUT must still reach the original/default procedure for Windows
        # cleanup, including when our decoder or a game handler raises.
        result = self.api.forward(view['previous'], hwnd, message, wparam, lparam)
        if message == WM_NCDESTROY:
            if state.hwnd == hwnd:
                try:
                    self.release(state)
                except Exception:
                    state.active = False
                state.hwnd = None
            self.views.pop(hwnd, None)
            self.retired_callbacks.append(view['callback'])
        return result

    def legacy_only(self, state):
        # Some remote/synthetic input providers move the Windows cursor without
        # supplying raw motion. Successful registration alone must not leave
        # those users with a frozen camera. Require sustained movement, so the
        # legacy message preceding the first raw packet after idle is safe.
        now = self.clock()
        if state.legacy_fallback:
            return True
        # Pyglet recenters the cursor while restoring exclusive capture. Let
        # its focus messages settle before evaluating a silent raw stream.
        if state.capture_started is not None and now - state.capture_started < 0.5:
            return False
        if state.last_legacy is None or now - state.last_legacy > 0.2:
            state.legacy_started, state.legacy_count = now, 0
        state.last_legacy = now
        state.legacy_count += 1
        silent = state.last_motion is None or now - state.last_motion > 0.3
        if silent and state.legacy_count >= 4 and now - state.legacy_started >= 0.3:
            # Keep the registration so real motion can restore raw input.
            # This is a transient provider condition, not a broken adapter.
            state.legacy_fallback = True
            self.log('mouse: temporarily using stock input; waiting for raw motion.')
            return True
        return False

    def motion(self, state, handle):
        try:
            packet = self.api.read(handle)
        except Exception:
            state.failures += 1
            if state.failures >= 3:
                self.fail(state, 'three consecutive raw-input read errors')
            return
        state.failures = 0
        if packet is None:
            return
        dx, dy, flags, device = packet
        if flags & 1:
            # Tablets/remote input report normalized absolute coordinates.
            # Seed each device on first use, rather than jumping the camera.
            previous = state.absolute.get(device)
            state.absolute[device] = (dx, dy, bool(flags & 2))
            if previous is None or previous[2] != bool(flags & 2):
                return
            width, height = self.api.desktop_size(bool(flags & 2))
            dx = (dx - previous[0]) * max(0, width - 1) / 65535.0
            dy = (dy - previous[1]) * max(0, height - 1) / 65535.0
        else:
            state.absolute.pop(device, None)
        if not dx and not dy:
            return
        state.last_motion = self.clock()
        if state.legacy_fallback:
            state.legacy_fallback = False
            state.legacy_started = state.last_legacy = None
            state.legacy_count = 0
            state.capture_started = state.last_motion
            self.log('mouse: raw motion resumed; stock movement suppressed again.')
            # The legacy half of this first packet may already have moved the
            # camera. Discard it once at the handoff to avoid double movement.
            return
        if not state.reported_motion:
            state.reported_motion = True
            self.log('mouse: first %s raw motion delivered.' %
                     ('absolute' if flags & 1 else 'relative'))
        window = state.window
        if not hasattr(window, '_exclusive_mouse_client'):
            window._reset_exclusive_mouse_screen()
        x, client_y = window._exclusive_mouse_client
        y = window._height - client_y
        window._mouse_x, window._mouse_y = x, y
        window._mouse_in_window = True
        buttons = self.api.buttons()
        if buttons:
            window.dispatch_event('on_mouse_drag', x, y, dx, -dy, buttons, window._get_modifiers())
        else:
            window.dispatch_event('on_mouse_motion', x, y, dx, -dy)

    def close(self, window):
        state = self.states.pop(window, None)
        if state is not None:
            try:
                self.release(state)
            except Exception as error:
                self.log('mouse: release during close: %r' % error)


def install(runtime):
    def patch(module):
        import pyglet
        from pyglet.libs.win32 import constants
        if not getattr(pyglet, 'version', '').startswith('1.2'):
            raise RuntimeError('Unsupported Pyglet version')
        if ctypes.sizeof(ctypes.c_void_p) != 4:
            raise RuntimeError('The retail raw-input adapter requires 32-bit Python')
        cls = module.Win32Window
        original_exclusive = method(cls, 'set_exclusive_mouse')
        close = method(cls, 'close')
        controller = MouseController(WindowsInput(), runtime.log)

        def set_exclusive_mouse(self, exclusive=True):
            result = original_exclusive(self, exclusive)
            controller.sync(self)
            return result

        def close_window(self):
            controller.close(self)
            return close(self)

        def poll(unused_dt):
            for window in list(pyglet.app.windows):
                if isinstance(window, cls):
                    controller.sync(window)

        wake_mask = constants.QS_ALLINPUT
        # Retail Pyglet predates raw input in QS_ALLINPUT. Its existing message
        # pump must also wake for WM_INPUT when legacy movement is coalesced.
        runtime.replace('mouse.raw', [(cls, 'set_exclusive_mouse', original_exclusive, set_exclusive_mouse),
                                      (cls, 'close', close, close_window),
                                      (constants, 'QS_ALLINPUT', wake_mask, wake_mask | 0x0400)])
        # Detect lost registrations and fullscreen view recreation even when
        # no WM_MOUSEMOVE is arriving. No per-frame game-loop patch is needed.
        pyglet.clock.schedule_interval(poll, 0.5)
        poll(0)
        runtime.mouse = controller

    runtime.watch('pyglet.window.win32', 'mouse.raw', patch)
