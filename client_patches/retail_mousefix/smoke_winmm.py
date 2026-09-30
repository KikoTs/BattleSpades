"""Run with 32-bit Python on Windows: python smoke_winmm.py path/to/winmm.dll."""
import ctypes
import os
import sys


if ctypes.sizeof(ctypes.c_void_p) != 4:
    raise SystemExit('This check needs 32-bit Python, matching the retail client.')
proxy = ctypes.WinDLL(os.path.abspath(sys.argv[1]))
directory = ctypes.create_unicode_buffer(260)
if not ctypes.windll.kernel32.GetSystemDirectoryW(directory, len(directory)):
    raise ctypes.WinError()
system = ctypes.WinDLL(os.path.join(directory.value, 'winmm.dll'))
proxy.timeGetTime.restype = system.timeGetTime.restype = ctypes.c_uint
for unused in range(10000):
    delta = (proxy.timeGetTime() - system.timeGetTime()) & 0xffffffff
    assert min(delta, 0x100000000 - delta) < 200
assert proxy.waveOutGetNumDevs() == system.waveOutGetNumDevs()
assert proxy.waveInGetNumDevs() == system.waveInGetNumDevs()
assert proxy.timeBeginPeriod(1) == 0
try:
    assert proxy.timeGetTime() >= 0
finally:
    assert proxy.timeEndPeriod(1) == 0
print('WinMM timer and audio-device forwarding passed (10000 timer calls).')
