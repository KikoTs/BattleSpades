# Retail movement parity evidence

This server audit uses original Python source, Python 2 bytecode, and the x86
client binaries. Expected physics results come from executing the original
instructions, not from the BattleSpades server/client or gameplay captures.
That server-only audit did not change the native C++ client or original
binaries/databases. The subsequent [playtest polish](PLAYTEST_POLISH_2026-09-20.md)
aligns the native client with this core and removes the observed flight/jump
handoff failures. The original binaries/databases remain unchanged.

## Recovered server behavior

`aoslib/world.pyx` follows the original movement core's float32 stores and
x87 arithmetic order, including independent opposite-key operations, flight
acceleration, hover, water gravity, climbing and timer updates. Voxel movement
controls grounding; ordinary jump does not override that contact result.
Airborne uncrouch, the map-height clamp, peer clearance and push ordering also
follow the original branches. Auto-crouch adjusts the fall-distance origin
before committing movement, avoiding erroneous damage in low tunnels.

`server/class_data.py` indexes each of the ten original movement tables
independently for all 18 classes. This corrects 15 values previously inherited
from generic profiles, including Fast/Jump Zombie, Specialist, Medic and
Classic Soldier. The class fixture is extracted from original source and
checked against the original Python 2 bytecode assignments. Water-fall damage
is zeroed in the native class profile when its game rule is disabled, as in
the original `GameClass` constructor.

## Independent regression fixtures

| Fixture | Cases | Original execution covered |
| --- | ---: | --- |
| `retail_movement_core.json` | 1,600 | Velocity and jump flag; 20/60 Hz, five pack types, ability/control combinations, water and contact states |
| `retail_movebox.json` | 2,352 | Voxel mover; floors, steps, walls, corners, tunnels and boundary/contact positions |
| `retail_peer_collisions.json` | 2,000 | Peer resolver; multiple peers, crouch/hover, resolving and count-only probes |
| `retail_class_movement.json` | 180 values | Ten class-indexed source tables, independently bytecode-checked |

These 5,952 physics cases use exact numeric equality, rather than a visual
similarity threshold. Additional audit probes compared 240 arithmetic cases
with varied basis vectors and coefficients, 720 trajectories of 60 complete
native steps, and 27 seeded fall/auto-crouch trajectories. All matched. The
additional probes are archived in the local audit directory, not counted as
maintained pytest fixtures.

The original `aoslib/world.pyd` SHA-256 is
`ae45ec007e312c8d650620bc2779169f7b7461c74192b7a7480342c21237c1a0`.
The generators reject a different binary. Addresses use image base
`0x10000000`: movement core `0x10012B80`, peer resolver `0x10012710`, vector
normalization `0x10011D00`. The arithmetic fixture stops before collision at
`0x1001304A`; the other fixtures verify collision separately.

The emulators explicitly use x87 control word `0x037f` and replace the CRT
square-root import with `fsqrt`. The voxel harness supplies sparse voxel
lookup and CRT floor; peer tests supply external Python object access. This
does not verify the original Python 2 ABI, every deployed CRT/FPU setting,
lock-box clipping, or every possible map and input sequence.

Run the maintained checks with Python 3.12 after building the Cython modules:

```powershell
py -3.12 -m pytest -q tests/test_retail_binary_movement.py tests/test_retail_binary_movebox.py tests/test_retail_binary_peers.py tests/test_retail_world_branches.py tests/test_class_data.py tests/test_reversed_movement_engine.py tests/test_jetpack.py tests/test_parachute.py tests/test_movement_jitter.py tests/test_simulation_order.py
```

Normal tests need neither the proprietary binary nor the emulator. Regeneration
uses `scripts/reverse_movement_core.py`, `scripts/reverse_movebox.py`, and
`scripts/reverse_peer_collisions.py`, with `--binary <original-world.pyd>` and
`--output <fixture-path>`; install optional `pefile`/`unicorn` tooling or provide
`--dependency-dir`. `scripts/reverse_class_movement.py` accepts
`--source <original-constants.py> --output <fixture-path>` and uses optional
`xdis` for the adjacent original bytecode.

## Flight and synchronization boundaries

The original Character/Player/GameScene code was also checked in IDA against
the PE bytes. It proves input gates, immediate application of received flight
activity, pre-movement history recording, and owner snapshot handling. Hover
is allowed only for UGC pack 69; crouch after entering hover remains a valid
descent path. Identical repeated activity does not restart its timer.

The receiving client does not contain the original server's fuel resource
state machine. Existing two-frame ignition handoff, exhaustion tail, refill
ordering, release latch and long owner-snapshot suppression remain compatibility
policies. Known resource constants do not prove these transitions. Likewise,
a queued server snapshot does not establish which position the client has
received, so the server's jump-anchor heuristic remains unproven.

The source folder's `run.py` installs a custom jump-smoothing patch. Captures
from that runtime do not establish unpatched retail Character behavior. Its
Z-key parachute extension is also distinct from stock input. No speculative
replacement of these policies was described as a recovered original algorithm.

Consequently, the listed native paths have direct original-code evidence;
whole-server or end-to-end 1:1 parity is not established. Full addresses,
disassembly comparisons, extra probe results and validation logs are retained
locally under `tmp/retail-server-reversal-20260920/`.

## Regression and installed-build validation, 2026-09-20

The complete suite ran 7,635 cases: 7,632 passed and three fixture problems
were found. The waterbed fixture now requires sustained escape instead of
death when the path is open; the held-jump fixture settles initial floor
contact as verified against the original binary; the shutdown fixture uses
the real connection constructor while retaining its forbidden-native-call
guards. Their broader reruns passed 46, 88 and 11 tests respectively. A final
combined run of the four affected parameter cases also passed. These were
test-only corrections after the production build was fixed.

All 28 shipped-map navigation cases, five extended London water-crossing
scenarios and the class-specific movement routes passed in the complete run.
The log and correction evidence are retained separately so the original
full-suite failures remain visible.

The frozen package passed 24 embedded-source comparisons, startup checks,
and ten readiness/shutdown/port-release cycles. Its three server launchers
and native world module were installed into the native client's bundled
server; the other 351 installed files retained their hashes during staging.
The installed native hosting bridge became ready in 1,388 ms and completed
local discovery, full-map Protocol 168 bootstrap and clean shutdown. These
checks establish packaging and local hosting, not measured interactive
gameplay latency.

## Capacity result, 2026-09-20

The isolated 30-second AncientEgypt run spawned all 50 requested bots and
achieved 59.98 simulation ticks/second, with 6.5053 ms tick p99 and no dropped
in-game packets or skipped entity ticks. It nevertheless **failed** the
configured capacity gate: bot main-thread p99 was 1.916 ms versus a 0.75 ms
budget. These are observed results, not a claim of hitch-free gameplay.

The bot metric covers the whole director update separately from player physics.
Its perception work has a partial budget, while motor phases and some snapshot
preparation remain outside that budget. These wall-clock samples can also
include worker-thread contention. A matched baseline and paired per-tick
profiling are needed to attribute the cost; this audit did not relax the gate
or claim the physics changes caused or resolved it. Raw metrics are in
`tmp/retail-server-reversal-20260920/capacity-result.json`.
