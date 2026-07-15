# Block Replication / Client Protocol Reverse-Engineering Handoff

Date: 2026-07-10  
Target: stock Ace of Spades 1.x `gameScene.pyd`  
Purpose: preserve the current replication and IDA findings so another engineer/AI can continue without repeating the analysis.

## 0. CORRECTION (2026-07-10, live two-client verification)

**The central hypothesis in §1 below was WRONG, and is disproven by measurement.
Read this section before acting on the rest of the file.**

Measured on the real client (dev client, tracer console; see docs/HANDOFF.md §5):

1. **The client applies BOTH packets on receive.** Feeding a synthetic inbound
   `BlockBuild(32)` through `GameScene.process_packet_block_build` builds the
   cell; feeding an inbound `BlockLine(40)` through
   `GameScene.process_packet_block_line` builds every cell. Neither is a no-op.
2. **Our old id-32 bytes decoded correctly** on the client's own
   `shared.packet` reader: the live-captured
   `17 3B 00 00 | 02 | 6E 01 | F1 00 | B9 00 | 00` →
   `(loop_count=15127, player_id=2, x=366, y=241, z=185, block_type=0)`.
   So the packets were never malformed.
3. **A/B on the live server with two independently-driven clients** (builder on
   tracer console 32896, observer on 32897): with the OLD per-cell id-32 code
   the server emitted `3× id-32, 0× id-40` and **the observer still rendered all
   three cells**. Block placement replicated fine.

**Therefore the "builds don't replicate" symptom is NOT caused by the packet id,
and switching 32 → 40 does not fix it.** The earlier reasoning ("logs prove the
32s reach all clients, yet remote clients did not render") drew a causal
conclusion from a correlation without ever testing the client's receive path.

What the packet-shape change IS still worth doing (and is now merged):
preserving the client's action shape matches the original server, collapses an
N-packet burst into one, and makes the line **atomic** — which removes the
genuinely dangerous `n = min(n, max_len)` bug, where the server built a *sparse*
subset of an overlong line while the client regenerates the *full* line from the
echoed endpoints. It is a correctness/parity fix, not the replication fix.

**The live replication failure remains UNEXPLAINED.** Leading candidates, in
order, for whoever picks this up:
- **World desync** (the other open bug, docs/HANDOFF.md §3): if the observer's
  map differs from the server's, the client's `add_block` support/adjacency rule
  silently drops cells. Measured: the server's `can_build` accepts a *floating*
  cell (e.g. 2 above the surface) that the client's `add_block` **refuses** —
  a real server↔client placement-rule divergence worth closing regardless.
- **Reliable-channel saturation.** A ChatMessage flood previously broke exactly
  this class of thing (block/pickup/death/score) — see the
  `reference_gameplay_packets` memory. Check for a flood during the failing session.
- **The prefab path** (`BuildPrefabAction(30)` → `BlockBuildColored(33)`), which
  the testers' loadouts use, is a different code path from BlockLine.

Reproduce the two-client rig with:
`PHYSICS_TRACER_CONSOLE_PORT=32897 PHYSICS_TRACER_PORT=32898` on the second
client, then `py scripts/auto_join.py --console-port 32897 ...`.

⚠️ **Tooling trap found while doing this:** never `tuple()`/`list()`/iterate a
`shared.glm.Vector3` in the game console. Its `__getitem__` has no bounds check
and never raises `IndexError` (`v[3]` → `0.0`), so Python's legacy iteration
protocol walks off the 3-float buffer and **hard-crashes the game**
(ACCESS_VIOLATION at `glm.pyd+0xA289`). Use `v.x/v.y/v.z`, `v[0..2]`, or the
console's `vec()` helper. Several "client crashes" chased here were self-inflicted
by exactly this.

---

## 1. Original conclusion (SUPERSEDED — see §0)

The stock client does **not** ignore all build packets:

- `BlockBuild` (packet 32) resolves a player color and calls the client's high-level `GameScene.build_block`.
- `BlockBuildColored` (packet 33) also calls `GameScene.build_block`, supplying an explicit color.
- `BlockLine` (packet 40) generates the line cells and calls `GameScene.add_block` for every generated point.

The original/reversed server logic preserves the incoming action shape:

- a single `BlockBuild` is broadcast as packet 32;
- a `BlockLine` is broadcast once as packet 40.

Our current server does something different. The stock client sends `BlockLine(40)` for ordinary player placement, but `server/combat_runtime.py::handle_block_line` converts every accepted line cell into a separate `BlockBuild(32)`. Live logs proved those packet-32 broadcasts reach all clients, while remote clients did not render the placement.

The highest-confidence parity fix is therefore:

1. validate the complete line before mutating anything;
2. consume the complete block cost atomically;
3. build the exact generated cells server-side;
4. broadcast one sanitized `BlockLine(40)` using the authenticated server-side player id and server loop count.

Packet 33 should not be selected merely because it contains color: its client path still enters `GameScene.build_block`, whereas packet 40 is the native remote line path. Packet 33 remains a possible exact-cell fallback only if partial-line acceptance is retained, but that has not been live-tested.

This diagnosis is strongly supported by the decompile and original-server comparison, but a two-client live test is still required before calling the gameplay bug closed.

## 2. Ground-truth binary and headless IDA state

Analyzed copy:

```text
G:\AoSRevival\BattleSpades\tmp\ida-replication\gameScene-replication.pyd
```

Original/copied SHA-256:

```text
3c4bae35f955eaa5c3f0cbfdfa5ce7e9bbc277293de849e3e6219c4cba67c1e7
```

IDA database/session:

```text
session/database: gamescene_replication
IDB: G:\AoSRevival\BattleSpades\tmp\ida-replication\gameScene-replication.pyd.i64
image base: 0x10000000
analysis: full auto-analysis complete
Hex-Rays: available
strings cached: 5154
```

The IDA MCP connection worked fully headless. If the old in-memory session has expired, reopen the `.i64` or the copied `.pyd`; do not reuse/kill the unrelated old smoke-test worker blindly.

## 3. Important client functions and addresses

| Client behavior | Function body | Qualified-name string | Important instruction/reference |
|---|---:|---:|---:|
| `GameScene.process_packet_block_build` | `sub_1018B310` | `0x10259D30` | `0x1018B62F` references `build_block` |
| `GameScene.process_packet_block_build_colored` | `sub_1018B830` | `0x10259D78` | `0x1018BA7B` references `build_block` |
| `GameScene.process_packet_block_line` | `sub_1018D690` | `0x10259EE0` | `0x1018E3E5` references `add_block` |
| `GameScene.build_block` | `sub_1018AD60` | `0x10259CF8` | source line metadata near `gameScene.pyx:3461` |
| `GameScene.add_block` | `sub_10136820` | `0x10257F6C` | source line metadata near `gameScene.pyx:1218` |

Function sizes recorded by IDA:

```text
sub_1018B310 size 0x513
sub_1018B830 size 0x43B
sub_1018D690 size 0x111D
sub_1018AD60 size 0x5AB
sub_10136820 size 0x22D
```

### Packet 32 flow (`sub_1018B310`)

Recovered behavior:

```text
player_id = packet.player_id
connection = scene.get_player_connection(player_id)

if connection and connection.character:
    color = connection.character.block_color
else:
    color = client default color

scene.build_block(packet.x, packet.y, packet.z, color, packet.block_type)
```

Important point: a missing connection does not make the handler a no-op; it selects a default color and continues to the build call.

### Packet 33 flow (`sub_1018B830`)

The colored handler reads the position and explicit color, then calls `scene.build_block`. It is not the native line-generation path.

### Packet 40 flow (`sub_1018D690`)

Recovered behavior:

1. read `packet.player_id`;
2. resolve the player connection and character block color, or use the default color;
3. read the six endpoint fields;
4. call the client's line generator with those endpoints;
5. iterate the returned cells;
6. call `scene.add_block` for each accepted/generated point.

Useful local landmarks:

```text
~0x1018DB1C   endpoint/line-generator call area
0x1018E3E5    load the interned `add_block` attribute
0x1018E3FD    PyTuple_New(5) for the per-point add call
0x1018E43C    PyObject_Call for the per-point add
```

This is the decisive difference between packet 40 and the current server's packet-32-per-cell conversion.

### `GameScene.build_block` (`sub_1018AD60`)

This function indexes the client's `BLOCK_BUILD_TYPE_STATS` using `block_type`, takes the selected material/stat entry, and calls `self.add_block(...)`. For normal/prefab build type 0, the original client constant table contains:

```text
BLOCK_TYPE_PREFAB = 0
BLOCK_TYPE_SNOW = 1
BLOCK_BUILD_TYPE_STATS = {0: [9], 1: [3]}
```

Therefore the server's packet-32 `block_type = 0` is not a malformed enum value. The parity problem is the conversion of a line action to a different packet path, not simply the numeric value zero.

## 4. Interned-name/global mappings recovered from Cython tables

| IDA global | Interned name |
|---:|---|
| `dword_1028BF48` | `player_id` |
| `dword_1028EA80` | `get_player_connection` |
| `dword_1028D888` | `character` |
| `dword_1028C7B0` | `block_color` |
| `dword_1028A4A0` | `build_block` |
| `dword_1028BE14` | `block_type` |
| `dword_1028F8B8` | `add_block` |
| `dword_1028F7A0` | `block_line` |
| `dword_1028EC48` | `block_manager` |
| `dword_1028EC38` | `players` |
| `dword_1028F49C` | `color` |
| `dword_1028FBC8` | raw interned string `A1011` (purpose not fully named) |

Single-build coordinate globals, identified by their ordered use in packet 32:

```text
dword_1028EA54 = x
dword_1028ECCC = y
dword_1028B2EC = z
```

Line endpoint globals, identified by ordered use but not independently renamed from the string table yet:

```text
dword_1028B36C = x1 (high confidence)
dword_1028C6C4 = y1 (high confidence)
dword_1028ADC0 = z1 (high confidence)
dword_1028AFBC = x2 (high confidence)
dword_1028FEB8 = y2 (high confidence)
dword_1028B65C = z2 (high confidence)
```

Helper:

```text
sub_10001000(name_object in EAX, object in EDX)
```

is the Cython/Python attribute lookup fast path used throughout these handlers.

## 5. Verified packet layouts and byte probes

The original client's compiled Python 2 packet encoders were invoked directly. With loop count `0x11223344`, player id `7`, and the test coordinates shown below, the exact generated packets were:

```text
BlockBuild(32):
20 44332211 07 7b00 ea00 2d00 00
hex: 2044332211077b00ea002d0000

BlockBuildColored(33):
21 44332211 07 7b00 ea00 2d00 563412
hex: 2144332211077b00ea002d00563412

BlockLine(40):
28 44332211 07 7b00 ea00 2d00 7c00 eb00 2e00
hex: 2844332211077b00ea002d007c00eb002e00
```

Decoded layouts:

```text
BlockBuild / id 32
u8  packet_id
i32 loop_count, little-endian
u8  player_id
i16 x
i16 y
i16 z
u8  block_type

BlockBuildColored / id 33
u8  packet_id
i32 loop_count, little-endian
u8  player_id
i16 x
i16 y
i16 z
u24 color, little-endian byte order on the wire

BlockLine / id 40
u8  packet_id
i32 loop_count, little-endian
u8  player_id
i16 x1
i16 y1
i16 z1
i16 x2
i16 y2
i16 z2
```

`shared/packet.pyx` matches these original encoder bytes.

## 6. Original-server/reference comparison

Reference inspected:

```text
G:\AoSRevival\aoslib-reversed\aosdump\server\aosserver\connection.py
```

Treat this port as logic corroboration, not binary ground truth. Its relevant behavior is:

```python
def build_block(self, x, y, z):
    # validate and build one point
    block_build.player_id = self.id
    block_build.xyz = (x, y, z)
    block_build.block_type = ACTION_BUILD
    self.protocol.broadcast_loader(block_build)

def build_line(self, x1, y1, z1, x2, y2, z2):
    points = self.protocol.map.block_line(x1, y1, z1, x2, y2, z2)
    if not self.block.build(len(points)):
        return False
    for point in points:
        self.protocol.map.build_point(*point, self.block.color.rgb)
    block_line.player_id = self.id
    block_line.xyz1 = (x1, y1, z1)
    block_line.xyz2 = (x2, y2, z2)
    self.protocol.broadcast_loader(block_line)
```

This independently corroborates packet-shape preservation: packet 40 is echoed once for a line.

## 7. Current BattleSpades code and identified hazards

Current path:

```text
server/combat_runtime.py
```

Current `handle_block_line`:

1. rasterizes cells;
2. skips any cell for which `can_build` is false;
3. consumes blocks one at a time until empty;
4. mutates every accepted cell;
5. broadcasts one packet 32 per accepted cell through `_broadcast_block_mutation`.

Problems:

- The broadcast packet shape differs from both the incoming client action and original server.
- Partial acceptance makes a single packet-40 echo unsafe unless the operation becomes atomic.
- The helper's nominal 64-cell cap is unsafe for echoing: it performs `n = min(n, max_len)` but still interpolates all the way to the original endpoint. An overlong line therefore becomes sparse server cells, while the stock client would regenerate the full line from the echoed endpoints.

Recommended handling for overlong lines: reject them before mutation rather than sparsely sampling them.

Recommended handling for invalid/occupied cells or insufficient inventory: reject the complete line before mutation. This guarantees that one packet 40 describes exactly the cells committed by the server.

## 8. Proposed minimal production change

In `server/combat_runtime.py`:

```python
from shared.packet import BlockBuild, BlockLine, ShootPacket
```

Then make `handle_block_line` transactional:

```text
cells = exact rasterized cells
reject empty/overlong line
reject if player.blocks < len(cells)
reject if any cell fails world_manager.can_build
consume len(cells) blocks
set every cell using player.block_color
broadcast one sanitized BlockLine:
    loop_count = server.loop_count
    player_id = authenticated player.id
    endpoints = validated packet endpoints
```

Keep the existing direct `handle_block_build` -> packet 32 behavior. Packet 32 is valid for a true single-build action; the bug is specifically converting incoming packet 40 into packet 32 announcements.

Do not combine this with movement/input-consumer changes. Replication should remain one isolated fix and one isolated live test.

## 9. Test-first work already present

`tests/test_reversed_combat.py` currently has an uncommitted test-only change:

- imports `BlockLine`;
- adds `test_block_line_replicates_as_one_native_block_line_packet`;
- adds `test_block_line_is_atomic_when_any_cell_cannot_be_built`;
- adds `test_block_line_is_atomic_when_inventory_cannot_cover_it`.

No production replication code has been changed yet.

The tests have **not been executed** in the Codex sandbox. The sandbox account cannot see the user's Python 3.12 installation, and `py -3.12` reports no suitable runtime. I stopped before creating or rebuilding any alternate Python runtime.

Run the red test outside the sandbox with the normal user Python:

```powershell
cd G:\AoSRevival\BattleSpades
py -3.12 -m pytest tests/test_reversed_combat.py -k block_line -q
```

Expected current failure: the successful-line test observes multiple packet-32 broadcasts instead of one packet 40; the two atomicity tests observe partial mutation/consumption.

After the production fix:

```powershell
py -3.12 -m pytest tests/test_reversed_combat.py -k "block_build or block_line" -q
py -3.12 -m pytest -q
py -3.12 scripts/replay_movebox.py
```

Do not claim completion without those checks and a real two-client build test.

## 10. Live-server and workspace safety state

- The running server was not stopped, rebuilt, or restarted during this investigation.
- Do not restart it until the user coordinates one batch restart.
- `config.toml` was already dirty due to a line-ending-only/phantom change; do not overwrite it.
- Two untracked Codex helper executables are present in the repository root. They were copied by the user to repair the tool protocol and are unrelated to BattleSpades source:

```text
codex-command-runner.exe
codex-windows-sandbox-setup.exe
```

The working-tree changes at handoff are expected to be:

```text
 M config.toml
 M tests/test_reversed_combat.py
?? codex-command-runner.exe
?? codex-windows-sandbox-setup.exe
?? docs/REPLICATION_IDA_FINDINGS.md
```

## 11. Suggested next-agent sequence

1. Read `CLAUDE.md`, `docs/HANDOFF.md`, and this file.
2. Review the existing test-only diff; do not delete it as unexplained work.
3. Run the focused test as the normal Windows user and record the expected red failure.
4. Implement only the transactional packet-40 echo.
5. Run focused and full tests.
6. Review the diff for accidental `config.toml` or helper-binary changes.
7. Coordinate one live-server restart.
8. Join two real clients, place both a single-tap line and a multi-cell line, and verify:
   - server log receives packet 40;
   - server sends exactly one sanitized packet 40;
   - both builder and remote client render identical cells/colors;
   - reconnect/full-map sync preserves the same blocks.

## 12. Replication parity implementation findings (2026-07-10)

The sequence above describes the original handoff state. The fixes have now
been implemented and exercised against an isolated server on UDP 27016 with
stock clients from `G:\AoSRevival\AceOfSpades_no_steam_new`. The public server
on 27015 was not restarted or modified.

### 12.1 WorldUpdate tool visibility

The two bytes after the action byte are not two disposable tool fields. The
first is state and the second is the equipped tool id:

```text
shared.packet WorldUpdate reader:      0x100429E0
action bit 0x10 test:                  0x10043688-0x1004368F
equipped-tool byte read/store:         0x100438CC / 0x100438DF
returned tuple item 24:                0x10043E2C-0x10043E33
gameScene WorldUpdate consumer:        0x10182900
set_can_display_weapon:                0x10184CF4-0x10184D3C
set_tool(tool, true):                  0x10184E3A-0x10184EA9
```

`Player.pack_action_flags()` now emits `0x10` for
`can_display_weapon`, and `WorldUpdate.write()` now writes the actual tool id.
In a two-client live run the observer followed changes `8 -> 5 -> 2`, with the
remote equipped tool equal to the sender's tool and display flag equal to one.

The adjacent state byte is packed independently: `0x01` parachute, `0x02`
disguise, and `0x08` touching-goo/water. The server currently advertises its
real disguise and wade state; parachute remains clear unless a runtime
`parachute_active` state exists. This prevents weapon ids from accidentally
toggling state while restoring observer-visible disguise/water activity.

### 12.2 Movement reconciliation phase

The stock movement core is `world.pyd Player.update` at `0x10012B80`:

```text
crouch/sneak/sprint acceleration select: 0x10012D65-0x10012D8A
airborne acceleration multiplier:        0x10012D90-0x10012DCE
diagonal 0.707106769 multiplier:          0x10012DD6-0x10012DF8
acceleration integration:                 0x10012DFC-0x10012E79
friction 1 + 4*dt:                        0x10012F25-0x10012F41
position scale dt*32:                     0x1001304A-0x10013058
```

The formulas match `aoslib/world.pyx`. The desync was scheduling, not the
constants: movement flags reported in client packet loop L are used by client
physics at L+1, while orientation is used in L. The server now advances at most
one history frame per tick, delays packet flags by one loop, applies orientation
in the current loop, predicts only a proven missing frame, rejects stale frames,
and freezes movement and acknowledgement together on starvation.

The matching WorldUpdate self-row offset is zero. A dry sprint baseline recorded
24 samples with zero snaps, zero adjustments, and maximum exact history error
`0.000153` blocks. Dry walk and diagonal motion likewise produced no
reconciliation corrections.

The pending input latch begins with an explicit idle frame after spawn, so the
first ClientData packet cannot bypass the recovered L-to-L+1 phase. Peerless
server bots take one ordinary physics step per tick and do not enter the human
client-history starvation freeze. After both corrections, another two-client
dry sprint recorded 18 samples, zero snaps, zero adjustments, and maximum error
`0.000061` blocks.

Stock terrain routines were also checked:

```text
_clip:                                   0x10001550
boxclipmove:                             0x10001710
```

Their probes and branches match `aoslib/world.pyx`. Stock uses float32 state
where the server's shared vector uses doubles, but captured terrain and airborne
paths still matched within approximately `1e-5` to `1e-4` blocks and no branch
divergence was observed. The larger water readings came from stale pre-correction
history in the tracer. Do not globally convert `shared.glm.Vector3` to float32
without a stock-oracle test proving an integer-face branch mismatch.

### 12.3 Hitboxes

The stock common collision wrapper/core are `0x1001F0F0` and `0x10012150`.
Character crouch wrapper/core are `0x1007B700` and `0x1002A8F0`. The server now
tests the recovered oriented KV6 bounds for every class in stock priority order:
torso, head, arms, left leg, right leg. It uses the stock yaw and AoS axis
mapping, including distinct standing/crouched legs and asymmetric Specialist and
Medic pivots. The former generic body box omitted much of the legs.

### 12.4 Shooting, spread, and cadence

The ShootPacket flag byte is split as bit zero `affect_shooter` and bit one
`secondary`; relays preserve both meanings. A shotgun trigger is transmitted as
one packet per already-spread pellet. The server now validates and traces each
client pellet once instead of generating a second random pellet cloud from the
seed. Cadence/ammunition are charged once per bounded pellet group.

The cadence gate now advances from a stable `next_shot_time`; one simulation
tick of network-arrival grace cannot compound into a faster sustained fire rate.
The recovered assault-rifle three-packet burst and minigun `0.30 -> 0.10` second
ramp are handled separately. Pistol values are restored to damage 20/50,
interval 0.3, reload 0.5, range 800, clip 6, reserve 30.

Assault burst packets must also be separated by the recovered six client loops;
increasing loop ids in one tick no longer bypass cadence. Shotgun groups retain
the client's distinct pellet rays but reject an exact duplicate direction, so a
replayed packet cannot apply one pellet repeatedly for a single shell.

### 12.5 Blocks and collapse

BlockLine now uses the same face-connected `world.cube_line` traversal as the
client and is validated atomically before mutation. Normal live placement and
spade removal were observed on both connected clients for block
`(224, 189, 221)`.

The stock collapse flood uses 18 neighbors (faces plus edges, excluding
three-axis corners), grounds only at `z > 238`, and has a roughly ten-million
operation budget rather than a structural block-count cap. The server mirrors
those rules and uses unchecked point removal. The triggering Damage retains
`chunk_check=1`, so clients run their native falling animation while the server
removes the identical component without flooding one Damage packet per voxel.

### 12.6 End-of-round transition

The restart coroutine now broadcasts a fresh per-connection StateData after
mode reset. Its `has_map_ended=0` clears the client's terminal score scene while
preserving the correct player id for each connection; team scores and respawns
then describe the new round.

### 12.7 Verification

The native `shared.packet` extension was rebuilt after the packet-layout change.
Focused movement, combat, collapse, packet, and end-sequence tests pass, and the
full repository run passes 220 tests. The live isolated run additionally proves
dry movement reconciliation, remote tool visibility, and normal block
placement/removal parity.

### 12.8 Class mobility and Engineer rocket turret

Engineer jetpack behavior was verified in `world.pyd` and the character
extension. `Character.update_jetpack` is wrapper/core
`0x100831C0`/`0x1003DFC0`; `set_hover` is
`0x10079530`/`0x100233D0`. The character routine only updates jetpack effects.
Actual flight remains in `Player.update` (`0x10012B80`). Held SPACE with active
Engineer pack 68 subtracts the pack's `0.020` thrust before ordinary gravity
and vertical damping; the first three stock-oracle velocities from rest are
`-0.0032786874`, `-0.0065036258`, and `-0.0096756974`. The `* 0.05` branch at
`0x10012EFB-0x10012F06` is `parachute_active`, not jetpack activity. The broken
Engineer path was originally a spawn-handshake issue: a reordered or missing
`SetClassLoadout` produced an empty `CreatePlayer.loadout`, so the stock client
constructed the class with no jetpack. The server now supplies the stock
concrete class default in that case.

Active-flight horizontal drag has a separate native branch. At
`0x10012F25-0x10012F7C`, an airborne active (`Player+176`) or passive
(`Player+180`) pack keeps the already-computed `1 + dt` vertical divisor for
X/Y. Only ordinary dry air uses `1 + 2*dt`; wade uses the class water-friction
divisor. The former port treated active Engineer flight as wading, applying
`1 + 8*dt`: a retail W+SPACE capture then showed seven periodic corrections
in one ascent. With the native branch restored, an exact capture matched all
thirteen active owner rows in X position and matched X velocity within
`4.5e-8`, with zero ADJUST and zero SNAP. Adjacent control flow at
`0x10012EAD` also proves hover (`Player+124`) skips the gravity-addition block;
the `0.75` gravity path belongs only to passive jetpack state. Parachute state
changes vertical gravity but retains ordinary airborne horizontal drag.

The live client then exposed a second wire bug: `character.jetpack_fuel` was
overwritten with zero on every WorldUpdate. After the pickup byte, the player
row contains three consecutive 1.6 fixed shorts, not padding. Tuple element 26
is the fuel value verified by parser round trips and the live meter; element 27
is `character.spawn_protection_timer` (`0x10185603-0x1018561E`), and element 28
is `character.weapon_deployment_yaw` (`0x10185127-0x1018513E`). The earlier
address attribution for fuel was wrong: `0x101852F1-0x10185305` applies the
parsed jetpack-active value to native `world.Player` (`Player+0xB0`), not to
`character.jetpack_fuel`. The server serializes authoritative fuel in the first
short. A live Engineer spawn initializes at 100 fuel; holding jump activated
both client and server state, drained the meter, and release cleared activity.

The same native audit proves there is no application acknowledgement to tune
this transition against. Incoming WorldUpdate is the sole runtime writer of
`world.Player+0xB0`: GameScene calls `Character.set_jetpack_active` around
`0x10185280-0x1018529C`, then sets the native Player attribute around
`0x101852F1-0x10185305`. `Character.update_jetpack` is effects-only, and
`send_client_data` contains no active/fuel acknowledgement. The ClientData
`ooo` nibble is exactly `(loop_count + 7) & 0x0F` in 448/448 captured rows, so
it is only a redundant clock-phase check. Reliable+flush reduces delivery
latency but cannot establish the GameScene frame where physics changed.

Normal jetpacks do not use Z/hover for thrust. `GameScene.on_key_press`
(`0x10234610`/`0x1015C9D0`) routes the configured hover key through
`Character.toggle_hover` (`0x10079520`/`0x100231A0`). `Character.set_hover`
accepts that state only for A367 / UGC Builder jetpack 69 at
`0x10023438-0x10023569`, forcing it false for Engineer 68 at
`0x1002367A-0x100236B6`. Engineer, Rocketeer, and normal packs request thrust
with the ordinary jump input; the server now mirrors that split.

Jump packet phase differs at the native state boundary, but does not require a
second server queue. `Player.update` clears the jump request at
`0x10012D3F-0x10012D48` only while airborne with neither a jetpack nor a
parachute equipped. Engineer therefore keeps held SPACE through the native
update, and `send_client_data` reads that state afterward at `0x1016B037`.
All buttons retain the ordinary one-observed-frame server latch. The separate
grounded launch reconciliation fix mirrors `Character.update_alive`: after
native launch it restores complete XYZ from the newest owner row strictly older
than the jump's source ClientData stamp. It retains launch velocity/airborne
state and must never restore an anchor on sustained airborne Engineer frames.

The Commando A370 parachute is selected by `world.set_parachute` at
`0x10018BC0` and toggled by `set_parachute_active` at `0x1000B030`. Its movement
branches are in `Player.update` at `0x10012EB9-0x10012F4C`: active descent uses
gravity `* 0.05` (`0x10012EFB-0x10012F06`), high horizontal drag, and reduced
landing severity. The separate `* 0.75` branch is passive jetpack/hover state.
The December 2015 client behavior opens A370 only on a second airborne SPACE
press after the launch press; the server mirrors that edge and keeps it open
until landing/water while replicating the existing parachute state flag.

Rocket-turret placement packet 88 is sent by wrapper/core
`0x102398D0`/`0x10171A30`. The most important corrected wire detail is the
WorldUpdate turret row: `shared.packet` reads a `uint16 entity_id` followed by
exactly two fixed-point shorts, `yaw` and `pitch`, at
`0x10044935` and `0x10044AAA-0x10044BF7`. The former four-short
`id/x/y/z` layout shifted the rest of the packet. `gameScene` consumes those
rows at `0x10185789`, resolves the entity at `0x101859D4`, verifies that it is a
RocketTurret at `0x10185A68`, then applies yaw and pitch at
`0x10185B0D` and `0x10185BE0`.

`ChangeEntity` packet 16 is action-based, not a flags-and-property-list packet.
Its common prefix is `entity_id:uint16, action:uint8`. RocketTurret
`SET_TARGET=5` carries one signed target byte (`0xFF` means no target), while
`SET_AMMO=7` carries one fixed-point short. The client consumes these paths at
`0x10198DA1-0x10198E87` and `0x10199146-0x1019916F`; target and ammo therefore
travel as two distinct packets.

The server now owns turret placement, stock/restock, target acquisition,
line-of-sight, 180-degree-per-second aiming, 1.5-second cadence, ammunition,
visible rocket entities, collision, and the recovered 50 damage / 10 block
damage / 3-block blast. Placement is constrained to the stock 10-block radius.
Both native extensions were rebuilt, and the repository suite passes 236 tests.

### 12.9 VXL map metadata, spawns, objectives, and water height

The retail `.vxl` payload is voxel data only: exactly 512 x 512 encoded column
streams. The bundled stock maps have no metadata trailer. Battle Builders UGC
maps keep authored gameplay data in a same-stem JSON sidecar (`.txt` or `.ugc`)
under `ugc_entities`; each row carries `position`, `mode`, and an item name such
as `ugc_spawnblue_small`, `ugc_basegreen_med`, or `ugc_health_drop`.
`UGC_ZONE_SIZES`, `UGC_ENTITY_TEAMS`, and `UGC_TOOL_IMAGES` in the recovered
client constants provide the exact zone extents, team mapping, and names.

The former server heuristic treated any pure-blue/pure-green surface voxel as
a spawn marker and removed it from the server map. That is not a VXL metadata
encoding and could mutate ordinary buildings while the client retained the raw
VXL geometry. The destructive scan is removed. Sidecars are authoritative when
present; voxel-only maps use cached dry, locally level spawn columns. Spawn
validation additionally requires solid support beneath the surface, rejecting
small platforms, broad roofs that defeat ring sampling, and cave ceilings.
Cached choices are revalidated against live terrain edits.

Player and entity z coordinates are distinct. A player ground anchor is the
surface minus the 2.25-block standing offset; a crate, flag, or base model is
placed at the voxel surface itself. Reusing the player anchor for crates was why
map entities appeared vertically displaced. TDM now prefers authored UGC drop
points and CTF creates the stock `BASE=1` tent and `FLAG=0` entity for each team,
including flag hide/recreate transitions for pickup, drop, and capture.

The client world is 240 blocks deep (`MAP_Z=240`) and the waterplane constant is
`Z_ABOVE_WATERPLANE=238`. The server's old `WATER_LEVEL=62` was a stale
64-height-world assumption and is now 238 in gameplay/config. Loaded bundled
Retail VXL loading normalizes short maps by
`max(0, 239 - max_referenced_z)`: 20thCenturyTown shifts +176,
CityOfChicago +39, and ArcticBase/CastleWars remain unshifted. This places dry
terrain at z=237/238 beside the fixed waterplane instead of visibly suspending
it far above water. Raw file bytes and CRC stay unchanged; full and delta
MapSync records use normalized client-world coordinates.

### 12.10 Battle Builder equipment, graves, and late entity types

The post-launch client's `gameScene.pyd.i64` supplies the late entity classes
that the older named `ENTITY_LIST` left as `UNKNOWN_ENTITY1..10`. The
`create_entity` core (`sub_10178B80`) indexes `GameScene.ENTITIES` directly by
the packet's numeric `type`. Class registration order is not the wire order;
using it previously made C4 render as a medpack and medpack render as block
goo. A live enumeration of that dispatch table plus two-client rendering gives:

| Entity type | Client class | Core function evidence |
|---:|---|---|
| 30 | `MedPackEntity` | live `GameScene.ENTITIES[30]`; observer rendered `MedPackEntity` |
| 31 | `BlockGooEntity` | live `GameScene.ENTITIES[31]` |
| 32 | `ChemicalBombEntity` | live `GameScene.ENTITIES[32]` |
| 33 | `GLGrenade` | live `GameScene.ENTITIES[33]` |
| 34 | `StickyGrenadeEntity` | live `GameScene.ENTITIES[34]` |
| 35 | `AttachedStickyGrenadeEntity` | live `GameScene.ENTITIES[35]` |
| 36 | `RadarStationEntity` | live table; observer rendered `RadarStationEntity` |
| 37 | `ProjectileMineEntity` | live `GameScene.ENTITIES[37]` |
| 38 | `C4Entity` | live table; observer rendered `C4Entity` |
| 39 | `RiotShieldEntity` | live `GameScene.ENTITIES[39]` |

The equipped Medic riot shield is not a placed server entity. Recovered
`riotShieldTool.py` renders it as the character's ordinary equipped tool;
remote clients already receive tool 52 plus `can_display_weapon` (action bit
0x10) and primary/bash (action bit 0x01) in WorldUpdate. No CreateEntity or new
packet is needed. Retail A1881-A1887 resolve to a 1.0-second bash interval, 2
damage, 50% frontal absorption, 0.5 knockback, 0.06 model size, and arm pitch
-80..0. The late `RiotShieldEntity` class remains part of generic entity
dispatch, but there is no placement/send path in the recovered shield tool.

`C4Entity` also exposes `disable`, minimap drawing, and a distinct delete path,
so C4 must be a real CreateEntity/DestroyEntity lifecycle rather than a hidden
server coordinate. Client send cores are `send_place_c4=0x10173F70` and
`send_detonate_c4=0x10174540`. Radar placement is `0x10173940`.
`process_packet_team_map_visibility=0x1019FEB0` indexes the packet-selected team
and applies its visible boolean, which is the per-recipient radar reveal lever.

The placement decoder had a cross-cutting wire bug. Live retail captures prove
that PlaceC4, PlaceDynamite, PlaceLandmine, PlaceMG, PlaceMedPack,
PlaceRadarStation, PlaceRocketTurret, PlaceUGC, and PlaceFlareBlock all send
literal voxel `uint16` coordinates. The generated readers divided those values
by 64 with `fromfixed()`, so a placement at voxel 339 became 5.296875 and failed
the distance check. Runtime compatibility decoders now read raw coordinates;
only the turret/MG yaw field remains a signed fixed-point short. Captured mine
`59 D1 50 00 00 00 53 01 A7 00 E3 00` and dynamite
`01 C6 57 00 00 B1 00 07 01 E2 00 04` fixtures protect the exact layouts.

Grave client functions are initialize `0x100CC420`, update `0x100CCD80`, and
on_delete `0x100CDC50`. The retail constants specify a 7-second fuse, radius 3,
25 player damage, and 3 block damage. `DeathController` has explicit
`on_mouse_move=0x1003ED20`, proving the intended post-death camera is
mouse-controlled rather than an unconditional auto-spin; its other anchors are
activate `0x1003E250`, update `0x1003F270`, and set_killer_info `0x10040B60`.

The later weapon classes are available as recovered Python under
`G:\AoSRevival\aceofspades_decompiled`, while exact values are readable from
the retail `shared/constants.pyc` with its bundled Python 2.7. Those sources
pin C4 (300 damage, radius 8, block 7), sticky grenade (200, radius 5, block 6,
5-second attached fuse), grenade launcher (100, radius 4, block 6, 3-second
lifespan), mine launcher (75 speed, deployed landmine), Blocksucker state
0/1/2 and 0.2-second cadence, radar lifetime 250, and disguise stock/state.

### 12.11 Grenade-launcher crash and gap-free join terrain sync

The retail crash at `gameScene.pyx:3588` is deterministic, not malformed
floating-point data. `process_packet_use_oriented_item` is wrapper
`0x102437E0`, with core `0x1018E930`. Its grenade-launcher branch at
`0x10190928-0x10190CC8` constructs position and velocity vectors, then calls
`GLGrenade(scene, position, velocity, value)` using a four-item Python tuple at
`0x10190C1B-0x10190C69`. `GLGrenade` now derives from `Entity`, whose recovered
initializer is `initialize(entity_id, team, player, spawned)`. The three values
after `scene` therefore reach an initializer requiring four and produce the
observed `initialize() takes exactly 5 arguments (4 given)`. No CreateEntity
dispatch references `GLGrenade`; packet 10 is a stale remote path and must not
be echoed for tool 55. Server projectile collision, lifetime, damage, and block
damage remain authoritative while the unsafe remote visual path is suppressed.

Map transfer previously serialized `dirty_columns`, sent MapSyncEnd, and then
gated every gameplay broadcast until the first ClientData. A terrain change in
that interval was absent from both the snapshot and the live stream. The server
now watermarks the exact immutable dirty-column set used for MapSync, journals
native PaintBlock/SetColor/BlockBuild/BlockBuildColored/Damage/BlockLine packets after that
watermark, and replays them before admitting the connection to live gameplay.
The replay watermark advances after every successful enqueue so a retry cannot
double-apply Damage. Mismatched-CRC full sync now substitutes each dirty column
in its original 512 x 512 position while walking the pristine raw VXL, keeping
exactly 262,144 unique records in the zlib stream; the former optimized full
path sent only the pristine file.

### 12.12 Player-build completeness and reconnect colour parity

Retail probing of `PaintBlock(7)` produced
`07443322117b00ea002d00563412`; coordinates are raw signed shorts, not fixed
point. The writer and authoritative handler now match that layout, recolour the
canonical VXL, dirty the column, relay the mutation, and include it in join
catch-up.

`BlockLine(40)` carries endpoints but no cells or colour. The client regenerates
the line, ignores already-solid cells, and charges only new cells. The server
now mirrors this instead of rejecting a whole line that crosses one existing
voxel. Late joiners receive every roster player's `SetColor` before replay. The
retail client also emits grey, red, and yellow `SetColor` defaults while
constructing its tools before `NewPlayerConnection`; the recovered server
ignores these packets until an alive player is holding `BLOCK_TOOL`, so they
must not be cached as palette input. Runtime RGB uses the
same opaque dynamic-block encoding as prefab tuples, preventing reconnect
MapSync from changing a placed block's colour/shading.

World-coordinate MapSync serialization is separate from raw/source VXL
serialization, so blocks built above a legacy map's original height range are
not clipped from dirty columns.

Follow-up retail IDA validation resolved the dynamic alpha ambiguity exactly.
`VXL.set_point` core `0x1002B0A0` calls `make_color` at `0x10019780`; module
initialization `0x100115B0` sets its scale constant to 128. Therefore RGBA
alpha 255 is serialized with high byte `0x80`, confirming the server's
`0x80RRGGBB` reconnect color word. `CreatePlayer` has no color field, so the
server emits `SetColor` immediately after join/respawn creation.

A follow-up live test exposed two palette regressions. `InitialInfo` had
`enable_colour_palette=0`, although the recovered retail server sets both
`enable_colour_palette` and `enable_colour_picker` to 1; `BlockTool.on_set`
therefore never activated the HUD palette. In addition, forcing SetColor before
every BlockLine overwrote the held-block selection with the server's cyan
fallback. The palette is enabled again and the retail default is neutral
`(112,112,112)`.

The builder cannot be excluded from placement replication: the local client
only displays ghost blocks and requires the native BlockLine(40) echo to commit
the drag and wallet change. The stable split is therefore BlockLine(40) to the
builder and explicit BlockBuildColored(33) cells to other clients.

A 2026-07-11 foreground A/B invalidated the earlier block-tool self-row
exception. Suppressing the row recreated a stale spawn anchor: ten visible
rollbacks and a 62.759-block maximum discontinuity. Sending the ordinary safe
self row produced zero visible rollbacks, retained the palette and selected
colour across two seconds of updates, and placed correctly coloured native
blocks. BlockLine owns the placement acknowledgement; WorldUpdate owns the
fresh reconciliation anchor. Both are required.

### 12.13 Mounted machine-gun placement and use

`PlaceMG(87)` is 14 bytes on the wire: id, `loop_count:int32`, claimed
`player_id:uint8`, raw voxel x/y/z, and fixed-point yaw. The claimed player
id is not trustworthy; placement ownership comes from the sending connection.
The recovered `ace-server` path creates entity type `MACHINE_GUN` (7), while
the retail constants pin range 5, health 100, destruction radius 3, player
damage 100, block damage 5, and knockback 0.2..1.0. The carried/deployed
`mgWeapon.py` switches from a 0.5-second cadence to 0.1 seconds when deployed.

The entity is durable and included in late-join StateData, with its placement
yaw preserved. `UseCommand(86)` mounts or releases the nearest unoccupied gun
through `ChangeEntity(16)` action `SET_PLAYER`; moving, jumping, crouching,
dying, or leaving its four-block leash releases the carrier server-side.
There is no recovered MG lifetime or placement-stock constant, so the server
does not invent either and suppresses retransmitted duplicates per owner.

`MG_AMMO=999` is a client-local infinite-ammo sentinel. It cannot be sent in
`ChangeEntity.SET_AMMO`, whose signed fixed-point short tops out at
511.984375; the client therefore retains its native 999 default instead of
receiving a clamped/corrupt property. Live validation remains required for the
compiled model's exact placement offset and mount camera transition.

### 12.14 Flare/light block placement

The recovered `flareBlockTool.py` fixes `block_cost=FLAREBLOCK_COST` (10), uses
the active palette colour, and calls `send_place_flare_block(x, y, z)` after
the ordinary support/range checks. A live packet capture,
`68 65 F7 01 00 D4 00 DD 00 EE 00`, decodes as loop `0x01F765` followed by raw
voxel coordinates `(212, 221, 238)`. Applying `fromfixed()` here would produce
`(3.3125, 3.453125, 3.71875)`, so packet 104 is deliberately decoded outside
the generated fixed-point Place* loader.

The client entity dispatch maps flare blocks to type 13. Native anchors in the
isolated `gameScene` image are `FlareBlockEntity.initialize=0x100DE610`,
`delete=0x100DF070`, and `GameScene.send_place_flare_block=0x10177C60`.
`delete` owns nontrivial render/light-manager cleanup, so every server removal
uses DestroyEntity rather than silently dropping registry state. The server
stores the placed entity's owner, wire team, and RGB palette colour, includes
it in late-join static-entity reveal, and removes it when damaged or when all
six-face support disappears.

Changelog evidence distinguishes flare blocks from ordinary building: normal
blocks cannot be built on water, but flare blocks were explicitly fixed to
display correctly there. Placement therefore permits z=238 when the retail
waterbed at z=239 supplies support, without passing through the ordinary VXL
`can_build` water restriction. Unsupported floating lights, duplicate/occupied
cells, bad tools/loadouts, insufficient block stock, and distant/out-of-bounds
coordinates are rejected before charging the ten-block cost.

### 12.15 Objective pickup and resource-crate lifecycle

Retail `PickPickup(70)` is a four-byte server-to-client packet: id,
`player_id:uint8`, `pickup_id:uint8`, and `burdensome:uint8`. The client lookup
at `GameScene.process_packet_pick_pickup` (`0x1019AAF0`) calls
`Player.pick_pickup(pickup_id, burdensome)`. `DropPickup(71)` is 19 bytes:
id, `loop_count:int32`, claimed player/type bytes, then fixed-point position
and velocity vectors. Its receive core is `0x1019AE10`; the send core is
`0x1016EED0`.

The client pickup table contains only objective types 14/15/16 (bomb, diamond,
intel), mapped to tools 25/26/30. Ammo, health, block, and jetpack crates are
entity types 3/4/5/6 and use `Restock(69)` instead. The server now supports the
missing authored jetpack-crate path without mixing these two namespaces.

CTF ground intel therefore uses entity type 16. On touch the server removes the
entity and emits reliable PickPickup; on explicit drop, death, disconnect, or
capture it emits authoritative DropPickup and updates the persistent entity
registry. The client-supplied player id is ignored, the claimed pickup type must
match server state, drop position must remain near the authoritative player,
and throw velocity is capped to the recovered 15-block/s intel speed. Pickup
radius and no-regrab delay use the recovered constants 3.0 and 2.5 seconds.

WorldUpdate's formerly hardcoded pickup byte now reflects each carrier, while
the existing reader retains its legacy tuple shape for compatibility. A
dedicated reliable PickPickup is also sent during late-join reveal, including
when generic static-entity replication is disabled, so burden/tool state cannot
be lost between roster creation and the first WorldUpdate.

### 12.16 CTF minimap zones and carrier visibility

`GameScene.process_packet_minimap_zone` is `gameScene.pyd:0x101A4A70`; the
native billboard construction path is `0x101A5B50`. A headless IDA session for
`hud.pyd` plus a live packet-43 probe recovered the field meanings that the
generated packet class leaves obfuscated. `key` becomes the zone's
`visible_team`; A2018/A2019 are X min/max, A2020/A2021 are Y min/max, and
A2022/A2023 are Z min/max. All six are raw voxel signed shorts. Icon id 6 is
`ZONE_ICON_CTF` and constructs a `MinimapBillboard` for the base.

Standalone billboard tracking was not used for the carrier: tracking id 1
resolves entity ids in the scene registry and does not follow player id 1.
Instead, `ChangePlayer(17)` action 8 is the native player path. Direct replay
with values 1 then 0 changed `Player.high_minimap_visibility` to 1 then 0.
Ground `IntelPickup` type 16 independently reports `minimap=True`, so the
complete objective representation is:

- packet 43 for both base/capture zones;
- entity type 16 while intel is on the ground;
- ChangePlayer action 8 while a player carries it.

`GameScene.process_packet_drop_pickup` remains `0x1019AE10`. Direct replay
cleared the carried tool but left no persistent ground entity, which is why a
drop/death must be followed by an authoritative type-16 CreateEntity. The
late-join mode snapshot replays both zones and any current carrier marker after
the generic entity reveal. In a clean isolated retail session, join and
same-scene `/restart` each produced exactly two native minimap zones and two
minimap-enabled intel entities, without duplicate zones or a traceback.

### 12.17 Explosion impulse, Molotov fire, and round cleanup

The native `shared/explosionDamageManager.pyd` applies knockback by adding an
impulse to the character's existing velocity; it does not use a dedicated
network packet. Distance uses squared falloff from the character body centre
(standing z offset 0.75, crouched 1.25), while direction uses the raw network
position. Per-warhead min/max values come from the recovered constants and the
result naturally replicates in the ordinary WorldUpdate velocity vector. LOS
blocks both health and impulse. With friendly fire disabled, teammate health is
suppressed while physical push remains; Rocket2 has its recovered self-boost
range. Partial-cover weighting still needs live calibration—the server uses a
conservative all-or-none terrain LOS gate.

Molotov is not merely a one-shot 50/3 impact. Constants pin entity type 28,
four-second block-fire life, five spread attempts at 0.5-second cadence, 0.7
block damage every 0.4 seconds, and character ignition at 2.5 damage every 0.3
seconds for ten seconds. The server creates durable BLOCKFIRE entities with
their fuse, owns damage/spread, clears fire in water, and drives the verified
WorldUpdate action bit 0x20 from authoritative state rather than trusting the
client's echo.

Finally, a same-map score-screen restart must clear controller indexes as well
as client models. The supplied dump followed GameStats(67), MapEnded(52), and
ShowGameStats(53): the latter two moved the retail client to `MenuScene`, then
late DestroyEntity packets produced the visible `invalid entity on destroy`
cascade before the native crash. Direct probes also proved
ForceShowScores(1) terminal, while GameStats alone leaves GameScene and the
local player intact.

The restart now sends final GameStats data without any terminal scene packet,
then removes projectiles, turret/MG state, mounts, C4/radar ownership,
block-fire/burning state, and old live entity ids before the mode rebuilds
crates/objectives, resets scores, and respawns players. A forced `/endround 1`
against the instrumented retail client stayed in GameScene, consumed nine
destroy/recreate pairs, respawned the player, remained responsive, and added no
new dump (9 before, 9 after).

A follow-up live transition exposed that the direct round respawn initially
bypassed the normal death-respawn application of `pending_class_id` and
`pending_loadout`. That could leave the server simulating the previous Medic
movement/loadout while the local UI had selected Miner, presenting the health
pack where dynamite was expected and causing reconciliation lag from mismatched
class multipliers. Pending selection is now applied inside the shared
`respawn_player` boundary. A retail Medic-to-Miner end-round transition returned
as class 3 with tool 21, created type-10 `DynamiteEntity`, and an eight-second
movement run recorded zero snaps/adjustments and a steady 600 applied, zero
dropped input frames.

### 12.18 Specialist/Engineer late tools and Block Cannon persistence

Retail `SnowBlowerWeapon.shoot` calls `send_snowball(position, forward * 50)`,
and `use_an_ammo` decrements `Character.block_count`. Final localisation
renames SNOWBLOWER to `Block Cannon` and says it builds blocks and uses blocks
for ammo. Damage type 20 is absent from the native BlockManager handler table;
its Damage packet supplies blast prediction, not persistent construction. The
server must commit the last free supported impact cell and publish packet 33
with a shot-time palette snapshot. A fresh retail reconnect verified the
canonical `0x2468AC` voxel from the rebuilt VXL stream.

`snowBlowerWeapon.on_set` and its UGC subclass activate the ordinary palette,
so SetColor is valid for tools 29/48 as well as 5/22. The source tool must have
reached authoritative ClientData before accepting the palette packet; an
automation script that selects and sends color in the same game-thread call can
intentionally hit the anti-forgery gate.

Native late-projectile validation used these exact entity transitions:
ChemicalBomb 32, GL 33, Sticky 34, ProjectileMine 37 -> armed Landmine 9. Do
not relay GL's packet 10 to remote clients: `process_packet_use_oriented_item`
enters the stale Python `GLGrenade` constructor and raises the known
`initialize() takes exactly 5 arguments (4 given)` exception. Molotov-spawned
BlockFire 28 has a separate native invariant: wire face must remain 4 because
every other face rotates a nonexistent model in base `Entity.set_face`.

### 12.19 Placement callback ownership, Machete, and jetpack equipment

Accepted voxel mutations must not automatically enter delayed terrain repair.
Native packet 32/33 processing reaches `GameScene.build_block` at
`0x1018AD60` and `GameScene.add_block` at `0x10136820`; a later canonical
packet 33 therefore invokes the placement callback/effect path a second time.
Normal BlockLine and prefab packets remain their primary reliable replication.
`TerrainRepairService.record_cells` is reserved for rejected/cancelled local
predictions.

`BlockManager.handle_machete_damage` is `gameScene.pyd:0x1008AA60`. The Cython
body sets debris display, constructs `range(z, z + 2)`, and calls
`handle_single_block_damage` once for each value while preserving the original
x/y, face, chunk-check, and causer. The integer global used as the upper offset
is initialized with `PyInt_FromLong(2)`. Combined with
`macheteTool.py` (`damage=2.0`, `shoot_interval=0.7`, damage type 35), the exact
canonical footprint is `(x,y,z)` plus `(x,y,z+1)`, accumulating 2 damage on
each. One base-position Damage(37,type=35,damage=2) makes every retail client
perform the same expansion.

Recovered `CLASS_ITEMS` makes jetpacks normal equipment choices: Rocketeer has
`JETPACK2(67), JETPACK_NORMAL(66)` and Engineer has
`JETPACK_ENGINEER(68), DISGUISE(64)`. Normalization and spawn must not append a
separate default jetpack after selecting the alternate equipment item.

Foreground packet replay confirmed the reconstructed boundaries. Three native
Machete swings sent three normalized ShootPacket(6) messages and received three
reliable Damage(37) messages whose damage-type byte was `0x23` (35). Engineer
pack 68 and Rocketeer Jetpack2 67 each activated and drained through the retail
client with zero ADJUST/SNAP/visible rollback; selecting Engineer Disguise 64
instead yielded `NO_JETPACK(65)` on the spawned player.

### 12.20 Classic CTF uses the CTF scene plus a feature bit

The shipped `playlists/classic.txt` declares `modes ['ctf']`, `classic True`,
shoot-with-intel ON, intel-auto-return OFF, and Classic SMG/shotgun OFF. The
apparently relevant `MODE_CCTF=11` constant is therefore not sufficient
evidence for a wire scene id.

Hex-Rays resolves `GameScene.is_in_classic_mode` to
`gameScene.pyd:0x10126C20`. Its Cython body gets `self.manager` and returns that
object's `classic` attribute; the method-name string is referenced at
`0x10126CD7`. HUD string tables separately expose `enable_minimap` at
`hud.pyd:0x10102AAC` and `can_shoot_holding_intel` at `0x100FD104`. Together
with the playlist, this establishes the server contract: send ordinary CTF id
8, set `InitialInfo.classic=1`, disable the minimap, and allow carrier fire.

Classic Soldier is client class 5 (Deuce) with Rifle 6, Classic Grenade 31,
and Classic Spade 4. Tools 37/38 are present in the recovered class table but
are excluded by the stock Classic playlist. `ClassicCTFMode` applies those
disabled tools during normalization as well as InitialInfo construction, so a
client packet cannot restore them after the menu hides them.

### 12.21 Shooter hit-confirm, kill-feed color, and Drill bore

`GameScene.process_packet_shoot_response` resolves to
`gameScene.pyd:sub_10193D60`. The native branch first emits blood when
`ShootResponse.damaged`/`blood` are set. It plays the hit sound and starts the
crosshair-change timer only when `packet.damage_by` equals the local player id.
Packet 9 therefore belongs on the broadcast path with the authoritative
shooter's id, not as an unaddressed owner-only effect. A live Miner-shotgun run
showed server packet 9 with `damage_by=12` and the retail client's
`change_crosshair` timer become positive after the bot lost health.

KillAction (46) has no wire color field. `HUD.add_kill` derives the names and
colors from its local Player objects. A live `KillLine` stored the remote killer
as green and the local victim `KikoTs` as `(255,255,255)`. The white local name
is the stock client's own-player highlight and must not be "fixed" server-side.

The Drill mismatch came from applying its collision trace as a one-cell server
mutation while asking the client to run native Damage type 10. Both
`BlockManager.handle_drill_damage` (`sub_1020B960` -> `sub_100864B0`) and the
destroyed variant accept `(by_player_id, position, type, damage, face,
chunk_check, seed=None)` and delegate to radius damage with hard-coded radius
2. Direct calls against the compiled retail client in a solid 9x9x9 fixture
removed exactly 81 cells for contact damage 20, stable for seeds 0, 1, 123,
and 255. Their integer offsets satisfy `dx*dx + dy*dy + dz*dz <= 6` within
`[-2,2]` on each axis.

Live replication keeps one type-10 packet so the retail sound/particles and
radius handler run once. Its `causer_id` must be the live Drill entity id;
entity id zero is valid and cannot be replaced through an `id or owner_id`
expression. Canonical terrain removes the same 81 cells, while reconnect
catch-up journals exact type-6 removals because the projectile may no longer
exist when replay begins.

### 12.22 Remote shots use ShootFeedback, not an echoed ShootPacket

`GameScene.process_packet_shoot_feedback` is
`gameScene.pyd:sub_101935C0` (Cython source line 3642). Its recovered attribute
accesses are `shooter_id`, `players`, `character`, `tool_id`, `shoot`, and
`seed`. The control flow requires the shooter in `self.players`, gets the
visible character, compares `packet.tool_id` with the character's current tool
id, and calls `character.shoot(packet.seed)` on a match. It does not consume
the packet-6 origin/direction/damage layout.

This establishes the firearm directional split: `ShootPacket(6)` is the client
fire request, while `ShootFeedbackPacket(8)` is the server's compact remote
firearm action. It drives the equipped weapon's gunshot/muzzle/tracer behavior.
Terrain and player damage remain separate authoritative packets/state. The
firing human is excluded because its local weapon already executed; a peerless
bot has no client and all real observers receive the event.

Packet 8 is not a generic melee feedback message. A retail bot replay with
tool 2 reached the recovered call and crashed at `Character.shoot` because
`SpadeTool` has no `shoot` method. Digging classes expose `use_primary`; their
remote action is WorldUpdate bit `0x01`, with `Damage(37)` carrying the terrain
result. A peerless bot must keep that bit high long enough to intersect the
30 Hz replication cadence; the server now holds each action pulse through two
future 60 Hz loops.

### 12.23 Zombie hand uses the native centered-cube damage handler

The headless IDA MCP session `bot_multikill_scene_20260715` resolves the native
Zombie terrain handler wrapper to `gameScene.pyd:0x10207ED0` and its Cython
implementation to `0x10081340`. The Super Spade wrapper is `0x10208E30` and its
implementation is `0x10082C90`. Hex-Rays shows both implementations subtract
one from each supplied x/y/z coordinate and call the same area routine with an
extent of three. The resulting client footprint is the centered 3x3x3 cube;
the handlers differ by the incoming Damage type and amount, not geometry.

`zombieHandTool.py` supplies `shoot_interval=0.4`, block damage 2, player
damage 70, and Damage type 17. Class damage still applies: ordinary Zombie has
multiplier 0.6 (42 effective player damage), while Fast/Jump Zombie use 0.5
(35 effective). The server therefore commits the complete cube atomically and
sends one type-17 area packet. Per-cell type-17 packets are invalid because
each retail client would expand every cell into another cube.

Zombie melee action remains a WorldUpdate primary-bit event; packet 8 must not
be used. The authoritative player hit follows normal LOS, HP, kill, feedback,
and mode-damage paths. Tool 28 is the distinct Zombie prefab tool and must be
preserved through the shared packet-30 prefab service instead of being coerced
to ordinary prefab tool 23.
