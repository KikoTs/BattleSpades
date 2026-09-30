"""Exercise real x86 Windows registration/subclass/teardown on hidden windows."""
import ctypes
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aos_mousefix import WindowsInput, MouseController, InputHeader, MousePacket, MouseInput, DeviceRegistration

if ctypes.sizeof(ctypes.c_void_p) != 4:
    raise SystemExit('Use the retail-compatible 32-bit Python 2.7 runtime.')
assert [ctypes.sizeof(t) for t in (InputHeader, MousePacket, MouseInput, DeviceRegistration)] == [16, 24, 40, 12]
api = WindowsInput()
user = api.user
user.CreateWindowExW.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p,
                               ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.c_void_p]
user.CreateWindowExW.restype = ctypes.c_void_p
user.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user.SendMessageW.restype = ctypes.c_ssize_t
user.DestroyWindow.argtypes = [ctypes.c_void_p]
controller = MouseController(api, lambda message: None)

class Window(object):
    _exclusive_mouse = _has_focus = True

assert not api.registrations()
for unused in range(40):
    window = Window()
    window._view_hwnd = user.CreateWindowExW(0, u'STATIC', u'AoS test', 0, 0, 0, 0, 0,
                                           None, None, None, None)
    assert window._view_hwnd
    try:
        controller.sync(window)
        assert controller.states[window].active
        assert api.registrations() == [(window._view_hwnd, 0)]
        assert user.SendMessageW(window._view_hwnd, 0x8001, 0, 0) == 0
        controller.close(window)
        assert not api.registrations()
    finally:
        assert user.DestroyWindow(window._view_hwnd)
    assert not controller.views
    assert not controller.states
assert len(controller.retired_callbacks) == 40
try:
    api.read(None)
except RuntimeError:
    pass
else:
    raise AssertionError('Invalid raw-input handle was accepted')
print('x86 Win32 raw-input registration, callback chaining and 40 teardown cycles passed.')
