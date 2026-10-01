# Validation, 2026-09-30

Target: the installed Steam client, 32-bit Python 2.7 and Pyglet 1.2.
Supported `aos.pkg` SHA256:
`c0d0cdc6f61f4b58172f74faf036c6f323b1cdbe59193f595fcce7d2a524e52c`.
Original `aos.exe` SHA256:
`4b5c63dc9b5641a2f5c37336e324af79be331ccae22747fcc71a178cafee6b73`.

## Automated checks

- **59 tests passed on Python 2.7 x86**, including real PEP 302 nested import
  dispatch, callback failure isolation, atomic replacement rollback, independent
  feature switches and the unsupported-bundle fallback.
- **18 movement tests passed on Python 2.7 x86**, covering the launch boundary,
  all-server behavior, remote/stale characters, replay steps, authoritative
  position changes, nested calls, exception cleanup and descriptor rollback.
- **480 frames against the original native `world.pyd` passed**, including
  walk, sprint, crouch, turns and eight jumps. Position and velocity matched
  an unwrapped native reference exactly; all eight stale launch resets were
  suppressed. This isolated test uses a small Character shell.
- **1,711 BattleSpades movement tests passed** across retail binary movement,
  reversed movement, jitter and input-loss recovery. See `MOVEMENT.md` in the
  source directory for the comparison and limits.
- Input tests cover native ABI packet decoding, truncation/oversize rejection,
  relative and absolute motion, drag buttons, no duplicate legacy movement,
  focus/menu release, raw registration conflicts, repeated read failures,
  native procedure chaining, view recreation and the legacy-only provider
  watchdog. The idle/movement tests guard against premature fallback.
- Equipment tests cover both DLC characters, the menu's weapon binding,
  unchanged shared predicates, non-DLC fallback and repeated installation.
- Scrolling/timer tests cover fractional deltas, direction changes, multi-notch
  motion, visibility/focus, inactive delayed calls and leaving during refresh.
- Seven integration checks use methods extracted read-only from the actual
  SHA256-verified retail bundle. Four prove the stock list layouts are already
  correct; two exercise the actual character predicate and refresh body.
  The seventh runs 100 loss/gain cycles through the actual retail focus
  handlers and the installed capture wrapper, without waiting for polling.
  No game module is imported by these tests.
- **40 native Win32 cycles passed**: hidden test-window creation, raw-input
  registration, actual ctypes procedure chaining, deregistration and destruction.
  These also include **120 capture loss/reacquisition cycles**, checking real
  raw-input registrations and recovery from temporary legacy fallback.
- **10,000 native WinMM timer forwards passed**, alongside audio-device counts
  and timer-period begin/end calls. The x86 DLL builds under MSVC `/W4 /WX`.

## Live retail checks

The new loader launched in the installed retail client. Its log confirmed the
mouse, equipment, scrolling and timer hooks installed as their modules loaded.
Specialist and Medic were visibly selectable; Medic spawned with its loadout
in a local BattleSpades match. Ordinary class selection and the Escape menu
also worked. The final scrolling scope was checked in the loading screen's
scores list: wheel input moved rows and they stayed inside the panel.

The trial layout wrappers were removed after the live check exposed a
decompiler error. The retail bytecode contains `continue` branches missing
from the local decompiled files. Those branches already implement the correct
row positioning; the final runtime does not replace any list-layout method.

The frozen runtime excludes `timeit`; the input watchdog therefore uses the
already-bundled `time.clock` on Python 2.7. No new standard-library dependency
is required by the final runtime.

The updated loader also installed the movement hook in the actual retail
client, joined the existing local BattleSpades server and spawned Commando.
The first-local-update log confirmed the real Character method reaches the
hook. Automated Space taps initially did not produce a suppression marker.
Later installed-client logs from the user's sessions (22:09 onward) contain
both `first relative raw motion delivered` and `first stale launch restore
suppressed`, confirming those paths were exercised in the full game. These
markers do not measure mouse feel or network reconciliation accuracy.

During synthetic cursor movement, the raw-input watchdog logged its fallback
to stock input. The original EXE, PKG, Character, World and GameScene files
retained their pre-test SHA256 values.

## Repeated minimize/restore repair

The user's later report was jitter/sensitivity changes after repeated
minimization. The installed-client log shows working raw motion followed by
the old permanent watchdog fallback at 22:13:40, 22:38:51 and 23:00:51. This
establishes that input mode changed; it does not establish why raw packets
were temporarily absent on that hardware.

The watchdog now leaves the registration in place during temporary legacy
fallback and returns to raw input when real motion arrives. It discards the
first returning motion packet, whose legacy counterpart may already have
moved the camera, to avoid double delivery at the handoff. Button-only
packets cannot trigger recovery. Focus/capture changes clear watchdog history,
absolute-device anchors and read-error counts, with a 0.5-second watchdog
grace period after reacquisition. Foreign registrations remain untouched;
actual adapter failures still use the existing fail-safe path.

Regressions cover repeated focus cycles, recovery without duplicate motion,
restore cursor warps, legacy-only input, button-only packets and foreign
registration ownership. A physical repeated-minimize playtest of this new
version remains necessary to confirm the reported symptom is gone.

## Limits

Automated cursor movement did not produce raw-motion events in the tested
desktop environment. Native registration and raw decoder/dispatch tests do
not establish physical-mouse feel or latency. A physical mouse check of the
minimize repair is still needed; the provider watchdog preserves control when raw
motion is unavailable. No claim of universal hardware, overlay or mod
compatibility is made. Other WinMM proxy mods cannot share the same filename
without an explicit chaining implementation.

Menu selection does not override server class/item restrictions. No remote
multiplayer compatibility guarantee or prolonged hardware soak was tested.
The movement patch is enabled on every server; testing on a local server
does not introduce an address whitelist. Extended movement accuracy and
physical mouse feel still need a manual playtest.
The native menu timer fix was checked against the real method using isolated
callbacks; it does not import the discontinued Revival lobby/hosting system.
