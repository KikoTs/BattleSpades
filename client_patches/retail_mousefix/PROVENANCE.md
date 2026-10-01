# Implementation provenance

This release replaces the earlier AGEX-derived mouse file and equipment
adaptation. No AGEX runtime module, installer, marker, artwork or bundled
replacement dependency is shipped or loaded.

The implementation uses the user's confirmed repository at
`G:/AoSRevival/aceofspades_revival` as a read-only reference:

| Patch | Source of the behavior | Implementation in this package |
| --- | --- | --- |
| Raw mouse | `aoslib/pyglet_win32_raw_mouse.py`, commit `1050067` (2026-03-29) | New native API adapter and view subclass around Revival's direct relative-motion design; preserves the three-read-error fallback. Adds late-window support, ownership checks, absolute-device handling, native callback lifetime protection and a Pyglet wake-mask correction. |
| Wheel | `revival_scroll.py`, GUI and match-settings changes in `f4bc259` (2026-07-28) | Small Python 2.7 wheel accumulator and two method replacements; rejects non-finite deltas as well. |
| Timer cleanup | `BaseSquadsMenu` lifecycle changes in `f4bc259` | New wrappers around stock start/stop/refresh. They cancel only active delayed calls and abort the remaining timer body when its refresh closes the menu. No Steam networking or lobby-creation code is copied into the wrapper. |
| Equipment and characters | Retail `selectClass` and `GameClass` call sites | Fresh scoped wrappers for DLC-listed tool/character IDs. No unconditional replacement of shared selection functions or Steam DLC APIs. |
| Jump launch | Revival `tools/patch_character_jump_restore.py` and BattleSpades `docs/RETAIL_JUMP_RESTORE.md`; AGEX `continuous_jump.py` reviewed for comparison | New scoped runtime implementation of the exact launch-restore omission. Keeps the post-physics position, rather than the old Revival wrapper's pre-frame position. Applies to the local character on every server. Uses GC dictionary discovery and CPython cache invalidation, without executable byte edits, guessed object-memory offsets, or a GameManager factory replacement. |
| Loader/coordinator | Original code written for this task | Existing WinMM forwarder, rewritten bootstrap, shared Python module observers and transactional attribute replacement. |
| Optional retail Steam relay | Installed retail bytecode and Valve Steamworks interfaces; BattleSpadesClient's transport inspected read-only as a design reference | New standalone native helper and Python 2 controller/browser adapter. No C++ client files or AGEX networking/lobby modules are copied or modified. Separate helper runtime; stock retail DLLs remain intact. |

Windows structure layouts, API signatures, message constants and export names
describe the platform ABI. Their presence is not evidence of copied custom
implementation. The source comparison report and supplied AGEX archive remain
separate historical audit inputs; neither is part of this distribution.

The user's original additions inform the ports. The Revival repository's
scoped license is reproduced in `LICENSE-Revival.txt`; it does not relicense
proprietary game code, assets or third-party libraries. No full decompiled game
module, native game library or proprietary asset is distributed here.

## Retail bytecode validation changed the port scope

The four Revival list-position fixes are not needed in this retail build.
Disassembly of the installed, SHA256-verified `aos.pkg` showed `continue`
branches absent from the local decompilation. The original compiled list
methods skip hidden rows and preserve header positions correctly. A trial
offset wrapper visibly displaced rows, so all four layout wrappers were
removed before packaging. Seven integration checks now use actual bundled
methods, including regression checks that the four stock layouts remain
correct. `retail_bytecode.py` is test tooling, not part of the game runtime.

The minimize/restore repair uses the existing capture wrapper and a new
recoverable watchdog state. Retail focus-handler bytecode is exercised by
tests; no copied replacement focus handler is shipped in the runtime.
