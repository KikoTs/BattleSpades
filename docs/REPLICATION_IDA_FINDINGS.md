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
Actual flight remains in `Player.update`: an active jetpack applies gravity
`* 0.05` and the high-friction movement branch. The broken Engineer path was a
spawn-handshake issue: a reordered or missing `SetClassLoadout` produced an
empty `CreatePlayer.loadout`, so the stock client constructed the class with no
jetpack. The server now supplies the stock concrete class default in that case.

The live client then exposed a second wire bug: `character.jetpack_fuel` was
overwritten with zero on every WorldUpdate. After the pickup byte, the player
row contains three consecutive 1.6 fixed shorts, not padding. Tuple element 26
is jetpack fuel and is assigned at `0x101852F1-0x1018530C`; element 27 is
`character.spawn_protection_timer` (`0x10185603-0x1018561E`), and element 28 is
`character.weapon_deployment_yaw` (`0x10185127-0x1018513E`). The server now
serializes its authoritative fuel in the first short. A live Engineer spawn
initializes at 100 fuel; holding jump activated both client and server jetpack
state, drained the meter to 82.8, and release cleared the active state.

Normal jetpacks do not use Z/hover for thrust. `GameScene.on_key_press`
(`0x10234610`/`0x1015C9D0`) routes the configured hover key through
`Character.toggle_hover` (`0x10079520`/`0x100231A0`). `Character.set_hover`
accepts that state only for A367 / UGC Builder jetpack 69 at
`0x10023438-0x10023569`, forcing it false for Engineer 68 at
`0x1002367A-0x100236B6`. Engineer, Rocketeer, and normal packs request thrust
with the ordinary jump input; the server now mirrors that split.

The Commando A370 parachute is selected by `world.set_parachute` at
`0x10018BC0` and toggled by `set_parachute_active` at `0x1000B030`. Its movement
branches are in `Player.update` at `0x10012EB9-0x10012F4C`: active descent uses
gravity `* 0.75`, high horizontal drag, and reduced landing severity. The stock
client does not autonomously open it in the observed live fall, so the server
now selects A370 from the loadout, opens it only while airborne and descending,
and replicates the existing parachute state flag.

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
