# Retail bug fixes and menu QoL

The optional **AoS-Retail-Steam-Relay.zip** also contains a Steam server-browser
adapter and a portable native helper. Extract its entire contents beside
`aos.exe`, including the `relay` subfolder. Read **NETWORK.txt** in that ZIP
(`NETWORK.md` in source) for hosting, validation limits and removal. The
ordinary **AoS-Retail-Fixes.zip** retains the six-file package below.

Close the game, then drop these six files beside `aos.exe` and launch through
Steam normally:

```
winmm.dll
aosfix_runtime.py
aos_mousefix.py
aos_equipmentfix.py
aos_uifix.py
aos_movementfix.py
```

The ZIP contains these files plus their complete source. No installer, separate
Python installation, downloads, or administrator setup is needed. If another
mod already supplies `winmm.dll`, do not overwrite it; proxy chaining is not
implemented. Updating our previous package requires replacing its loader too.

## Included changes

- Raw mouse input based on Revival's design: direct relative device counts,
  normal menu input, focus handling, existing-window attachment, bounded packet
  decoding and stock-input fallback after repeated failures. No Windows pointer
  acceleration multiplier or movement batching. Absolute devices are handled
  separately and seeded on first use to avoid an initial camera jump.
  Sustained legacy cursor movement without raw motion temporarily falls back
  to stock input while monitoring for raw motion to resume. Minimize/restore
  resets transient input state and gives cursor recentering a short grace
  period, so a watchdog fallback cannot permanently change mouse behavior.
- DLC weapon choices and Specialist/Medic selection in the equipment menu.
  Team class lists, disabled classes/tools, locked classes and server validation
  still apply. This changes the local selection UI, not Steam ownership or
  server permissions, and supplies no game assets.
- Revival's fractional/multi-notch wheel handling for vertical scrollbars and
  the visible match-settings list.
- Revival's menu timer lifecycle fix: cancel active callbacks only, clear stale
  handles and stop a refresh from rescheduling after leaving the menu.
- Jump-launch prediction fix: keep the displacement computed by native physics
  instead of restoring an older server position on the launch frame. Applies
  to the local character on every server; no address whitelist. Walking,
  collisions, movement history and ordinary server corrections stay native.
  See `MOVEMENT.md` in the source directory for the BattleSpades comparison
  and test limits.

The six-file bug-fix package contains no custom lobby/hosting implementation, relay, browser, login marker, artwork,
first-launch display setup, map-transition protocol changes or
server changes are included. Existing stock menu functions still do their
normal work; the timer wrapper only controls their callback lifecycle.

## Disable or remove

Each feature can be disabled independently. Restart after making a change.

| Feature | Steam launch option | Or create an empty file beside aos.exe |
| --- | --- | --- |
| Mouse | `+legacymouse` | `aos_mousefix.disabled` |
| Equipment and characters | `+legacyequipment` | `aos_equipmentfix.disabled` |
| Scrolling/timers | `+legacyui` | `aos_uifix.disabled` |
| Jump prediction | `+legacymovement` | `aos_movementfix.disabled` |
| Optional Steam networking | `+legacynetwork` | `aos_networkfix.disabled` |

To uninstall, close the game and remove the six added files listed above.
For the relay edition also remove `aos_networkfix.py`, `aos_steam_bridge.py`
and its added `relay` folder. Server favourites/history are stored separately
in `%LOCALAPPDATA%/AoSRetailFixes`.
The diagnostic `aos_mousefix_loader.log` can also be removed. No original game
file needs restoring because this package does not rewrite any of them.

## Loader and compatibility

The added x86 WinMM DLL forwards exports to the absolute Windows system DLL.
It schedules Python work on the game's existing Python 2.7 main thread. It
uses no external injector, new process thread, dependency replacement or
on-disk EXE/PKG/PYD edits. In-memory Python attributes and the window procedure
are necessarily changed to apply the fixes.

Only this inspected `aos.pkg` SHA256 is supported:

`c0d0cdc6f61f4b58172f74faf036c6f323b1cdbe59193f595fcce7d2a524e52c`

Unknown bundles skip all Python patches. Named patches are logged as `waiting`,
`active` or `failed`; one failed target does not prevent unrelated targets from
loading. Import observers preserve the frozen importer's normal behavior and
also handle already-loaded modules. The source is compiled in memory without
writing `.pyc` files.

Logs go to `aos_mousefix_loader.log`. A registered mouse hook is distinct from
successful capture: `raw input active on view` confirms registration, and
`first ... raw motion delivered` confirms movement reached the dispatch path.
Raw-input ownership is checked periodically; a foreign registration is never
overwritten. Native callback thunks are retained for interpreter lifetime so
another window subclass cannot call freed callback memory.

The old Pyglet input wake mask is extended for raw input. Message cleanup and
subclass chaining follow Microsoft's documentation for
[WM_INPUT](https://learn.microsoft.com/en-us/windows/win32/inputdev/wm-input)
and [SetWindowLongW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowlongw).

## Source, tests and rebuilding

The previous AGEX-derived mouse adaptation has been removed from this release.
See `PROVENANCE.md` for the Revival source commits and new implementation scope.
The original-code license notice is in `LICENSE-Revival.txt`.

Rebuild the loader using Visual Studio's C++ x86 build tools:

```powershell
.\build.ps1
.\package.ps1
```

Run the regressions using Python 2.7 (the target runtime):

```powershell
python -B test_fixes.py
python -B test_movement.py
python -B smoke_movement.py path\to\unpacked-client
python -B smoke_mouse.py
python -B smoke_winmm.py path\to\winmm.dll
```

`smoke_mouse.py` and `smoke_winmm.py` require a 32-bit Windows Python process.
Set `AOS_RETAIL_BUNDLE` to the installed `aos.pkg` to enable the seven additional
Python 2.7 method-integration checks in `test_fixes.py`. They read the actual
bundled bytecode without importing game modules. No proprietary source or
bytecode is included in the package. See `VALIDATION.md` for observed results.

Revival's four row-positioning repairs are deliberately excluded: the actual
retail methods already skip hidden rows correctly. Those repairs compensate
for `continue` statements lost by decompilation; applying them to retail moves
rows outside their panels. The bundle tests verify the stock layouts instead.

## Future modding API

`aosfix_runtime.py` is a small internal foundation: named patches, module-ready
callbacks, atomic attribute replacement and per-patch status. It is not yet a
public modding API. A later version can add a documented compatibility contract,
mod manifests, dependency ordering, configuration and safe teardown. None of
that requires importing Revival's discontinued lobby system.
