# BattleSpades — Session Handoff (2026-07-09, evening)

> Written for the next engineer/AI picking up mid-work. Read
> [CLAUDE.md](../CLAUDE.md) first (hard invariants + working agreements),
> then this file. The project is a Python 3.12 + Cython **1:1 recreation of the
> Ace of Spades 1.x (Battle Builders) dedicated server**, tested against the
> **compiled original client**. Ground truth = the live client, never the
> `aoslib-reversed` hand port (its physics/packet *layouts* are partly wrong;
> trust its *logic* only).

---

## 0. TL;DR — where we are

A live multiplayer playtest (host `KikoTs`@127.0.0.1 + real remote friends
`DmitrySenpai`@185.242.x and others@5.142.x) surfaced **two real gameplay bugs**
plus one cosmetic one. The Steam-client **join crash is FIXED and shipped**
(commit `fd77e9d`, pushed to `origin/main`). Server tick health is perfect
(`avg=0.03ms max=0.22ms slow=0/600`) — nothing here is a server-performance
problem.

**Open, in priority order:**
1. **Block building does not replicate between clients** (Bug A, §2).
2. **Clients rubber-band / get stuck in an "invisible wall" while the server has
   them elsewhere, especially "when we run a lot"** (Bug B, §3). This is the big
   one and the user's main pain point.
3. Cosmetic: `UnicodeEncodeError` log spam on emoji player names — **fix already
   written, uncommitted** (§4).

**Do NOT restart the running server without coordinating** — the user + friends
may be mid-session, and a restart disconnects everyone. Batch all fixes → one
coordinated restart → verify.

---

## 1. What shipped this session (context)

Recent commits (all on `main`, pushed):
- `fd77e9d` **Fix Steam-client join crash: stream raw column spans, not the
  filled grid.** The full MapSync was re-serializing our in-memory grid, whose
  underground we fill solid for collision — writing every voxel *explicitly*
  = 36.5 MB for a 3.2 MB map, which the **stock Steam client rejects mid-build**
  (the patched nonsteam dev client tolerated it and masked the bug). Fix:
  `WorldManager.iter_full_sync_chunks()` walks the RAW `.vxl`'s column spans and
  wraps each in `struct.pack("<II", x, y)` + raw span bytes — native
  implicit-underground encoding, ~5 MB, client refills the underground itself.
  **Verified live on the actual stock Steam client** (connected, built, survived
  at team-select where it previously died). See
  [reference_vxl_map_format memory] and the commit body for the full story.
  NOTE: streaming the raw `.vxl` **bytes directly** (no x,y wrapper) crashed BOTH
  clients — the stream-builder needs the record framing; hence the walker.
- `9fee2af` Jetpack: emit the `0x04` jetpack-active WorldUpdate bit (server-
  authoritative flight). **Untested** — the join crash blocked the jetpack
  playtest. Press **Z** as Rocketeer to test once joins are stable. If jumps
  break, this bit is the first suspect (revert to 0).
- `84fb2aa` Per-tool melee (spade 3-tall column / pickaxe / knife).

---

## 2. OPEN BUG A — block building does not replicate

**Symptom:** a player places a block; the *builder* sees it (client-side
prediction) but *other* players never see it. Block **destroys replicate fine**
(they go out as `Damage(37)`).

**What the server does (confirmed from the live log):**
- Client PLACES blocks by sending `BlockLine(40)` (it never sends `BlockBuild`).
  Log: `RECV packet_id=40 (BlockLine) ... from <builder>`.
- Server receives it in
  [`server/combat_runtime.py`](../server/combat_runtime.py) `handle_block_line`
  (~L136) → for each cell calls `_broadcast_block_mutation` (~L133) which builds
  a **`BlockBuild(32)`** packet and `server.broadcast()`s it to ALL clients.
  Log confirms: `SEND packet_id=32 (BlockBuild) ... to 185.242.x / 5.142.x /
  127.0.0.1` (all three).

So the broadcast IS happening and reaches every client. **The other clients
receive `BlockBuild(32)` and do nothing with it.**

**Hypothesis (needs client-side confirmation):** the compiled 1.x client does
**not apply `BlockBuild(32)` on receive** for a remote build. It probably applies
a different packet — most likely **`BlockBuildColored(33)`** (carries color; the
prefab path already broadcasts `33` — check whether *prefabs* replicate to other
clients, which would confirm `33` is the right one) or an **echoed
`BlockLine(40)`**.

**How to settle it (do this):**
1. IDA on the client engine `.pyd` — find the packet dispatch for ids `32`, `33`,
   `40` and see which one actually mutates the client's world / BlockManager
   (adds a block) when received from the network. That's the packet the server
   must broadcast. (IDA usage in §5.)
2. Cross-check `G:/AoSRevival/aoslib-reversed/aosdump/server/*.py` +
   `aosdump/shared/packet.py`: when a player builds, what packet does the
   original server broadcast to others? (grep `BlockBuild`, `BlockLine`,
   `BlockBuildColored`.)
3. In-game A/B (fast): with two dev clients joined (§5), build with one and grep
   the log; then temporarily change `_broadcast_block_mutation` to emit
   `BlockBuildColored(33)` (with `player.block_color`) instead of `BlockBuild(32)`
   and see if the *other* client renders it.

**Likely fix:** in `_broadcast_block_mutation`
([`server/combat_runtime.py`](../server/combat_runtime.py) ~L124), broadcast
`BlockBuildColored(33)` carrying `(loop_count, player_id, x, y, z, color)` — or
whatever id 1's IDA proves the client applies — instead of `BlockBuild(32)`.
Verify the exact field layout against `shared/packet.pyx`.

---

## 3. OPEN BUG B — rollback / "invisible wall" desync (the big one)

**Symptom (user's words):** "sometimes when we run a lot some clients get stuck
in an invisible wall client-side but on the server side they are in a different
location." Rubber-banding; players get wedged and don't resync.

**What "invisible wall client-side, elsewhere server-side" means:** the client's
*predicted* position and the server's *authoritative* position have diverged.
The client wedges against whatever terrain is nearest its (wrong) predicted spot;
the server has the player somewhere else. It's a **client↔server position
divergence that the WorldUpdate reconciliation isn't correcting.**

### Measurements taken (from the live `logs/log.txt`)
- **Server tick is perfect:** `tick stats: avg=0.03ms max=0.22ms slow(>10ms)=0/600`.
  Not a server-lag problem.
- **`client_loop` lags `server_loop` by ~3 ticks, jittery, spiking higher under
  load** (`ClientData stamp check: client_loop=.. server_loop=..` lines). Normal
  lag; the question is what we do with it.
- **My inline "map surface mismatch" check was BUGGED** (it advanced one span per
  column, not handling multi-span columns) — ignore its "8 mismatches" output.
  The valid earlier `game_console` probe showed the client's `col(256,128) =
  [188..239]` matching the server exactly. **Map mismatch is currently
  unconfirmed and probably NOT the cause**, but re-verify properly (§3 tasks).

### What's ALREADY correct (don't re-do)
The self-row is **already stamped per-recipient** with that client's own consumed
input loop — NOT the global server loop. See
[`server/main.py`](../server/main.py) L804–830:
```python
stamp = player.last_applied_input_loop + offset      # L817
data = self.build_world_update_data(loop_count_override=stamp)
```
`last_applied_input_loop` is set in
[`server/player.py`](../server/player.py) L1165. So "stamp with the client's own
loop" is done. Two things remain suspect:

### Suspect 1 — the input consumer drops inputs under bursts (MOST LIKELY)
[`server/player.py`](../server/player.py) ~L1150–1168, the per-tick input
consumer:
```python
if self.input_history:
    best = max(self.input_history)              # FRESHEST buffered input only
    flags, orientation = self.input_history[best]
    ... latch jump ...
    self.last_applied_input_loop = best
    self.update_input(*flags)
    self.input_history.clear()                  # DROPS all older buffered inputs
```
It applies **one input per tick (the freshest) and discards the rest**. This was
deliberate — consuming multiple inputs/tick made the server *outrun* the client
and reconciled it back to spawn on jumps (see the comment + tasks #23/#24). BUT
under **network jitter / bursts** (exactly "when we run a lot"), the client's
inputs arrive bunched: 3 inputs land in one tick, the server applies 1 and drops
2, so the **server takes 1 step while the client predicted 3** → the server
position falls *behind* the client's prediction → the self-row says "you're
behind where you think you are" → client SNAPs backward → wedges. This is the
prime suspect for the accumulate-under-load behavior.
- The correct netcode consumes inputs **in-order, matched to the client's
  loop_count progression** (1 client-frame per server-tick when rates match,
  buffering jitter) — never "newest-only, drop the rest." The challenge is doing
  that WITHOUT re-introducing the outrun-the-client bug. The safe framing: step
  exactly once per server tick, but consume the *next in-order* input
  (`next_input_loop` cursor already exists, `server/player.py` L270), not the
  freshest; let a bounded jitter buffer absorb bursts; if the buffer is empty,
  repeat the last input (coast) rather than skipping.

### Suspect 2 — `worldupdate_loop_offset` is mis-set
[`config.toml`](../config.toml) L112: `worldupdate_loop_offset = -1`. **But the
comment right above it (and the `reference_ida_netcode_re` memory) say the
calibrated value is `+2`** ("the measured structural phase between the consumed
input's loop_count and the client's movement_history index"). A **3-tick offset
error** is exactly the kind of thing that makes the client reconcile against the
wrong history slot and SNAP. Either -1 is a stale/accidental value or it was
re-calibrated and the comment is stale — **re-calibrate deterministically**:
1. Set `debug_selfrow = true` in `[debug]`, restart, do one ~12s straight walk on
   a dev client. Server writes `logs/selfrow_samples.ndjson` (stamp + position
   per self-row). The client writes its per-frame capture to
   `aceofspades_nonsteam/logs/physics_capture_*.ndjson`.
2. Run `py tmp/reconcile_sim.py` (the offline simulator that replays the client's
   exact reconciliation) to get the snap/adjust/no-op distribution per candidate
   offset; pick the one with all-no-op/adjust, zero snap.
3. Set `worldupdate_loop_offset` to that, `debug_selfrow = false`, retest.

### The user's "force resync over time" idea
It's the right instinct for a **bounded safety net**, wrong as the primary fix:
- As the main mechanism it rubber-bands visibly and does nothing against a map
  mismatch (client re-sticks on the invisible wall the instant after each snap).
- The original AoS handled real-ping multiplayer smoothly WITHOUT periodic
  resyncs → the correct fix lives in the reconciliation (suspects 1 & 2), not a
  hack on top.
- DO add a **bounded hard-correction escape hatch**: if server↔client position
  diverges past a threshold (~2–3 blocks) for more than a few ticks, send one
  authoritative correction. The client already has an internal SNAP threshold
  (`POSITION_RESET_TOLERANCE`) — this just guarantees it fires for the
  pathological case.

### Bug B task list
1. **Fix the input consumer** (suspect 1): step once/tick but consume the
   next-in-order input via the `next_input_loop` cursor with a small jitter
   buffer; coast (repeat last input) on underrun. Verify it does NOT reintroduce
   the outrun-on-jump bug (tasks #23/#24) — use `scripts/replay_movebox.py` +
   `scripts/replay_parity.py` (must stay ALL PASS on ArcticBase) and a live
   two-client run.
2. **Re-calibrate `worldupdate_loop_offset`** (suspect 2) via `debug_selfrow` +
   `tmp/reconcile_sim.py`. The `-1` vs `+2` discrepancy is a strong lead.
3. **Definitively confirm client==server map** at the collision surface: join a
   dev client (§5), and for ~10 columns compare
   `manager.scene.map.get_solid(x,y,z)` (client, via `game_console`) vs
   `ServerVXL(...).get_solid(x,y,z)` (server). If they disagree at the surface,
   THAT is the invisible wall and it's the `fd77e9d` walker's fault — fix the
   server loader to fill/stop underground by the SAME rule the client uses.
4. Add the bounded hard-correction safety net (last).

---

## 4. Uncommitted / in-flight changes

- [`run_server.py`](../run_server.py) — **UNCOMMITTED, ready to commit.** UTF-8
  logging fix: `sys.stdout/stderr.reconfigure(encoding="utf-8",
  errors="replace")` + `FileHandler(..., encoding="utf-8", errors="replace")`.
  Emoji player names (`beta keks🇷🇺`) were raising `UnicodeEncodeError` in the
  QueueListener thread on every logged packet (cp1252 default) — cosmetic spam +
  dropped log lines, NOT a gameplay bug. Applies on next server restart. Safe to
  commit now.
- [`config.toml`](../config.toml) — shows as `M` but it's a **phantom LF↔CRLF
  line-ending change only** (byte-identical to HEAD after CR-strip). Leave it;
  do NOT commit it (noise). ⚠️ NEVER write `config.toml` with PowerShell
  `Set-Content -Encoding utf8` — the BOM breaks `toml.load` silently and the
  server runs defaults on the wrong port. Edit it with the Edit tool.
- The interrupted investigation **workflow** `wf_9bcfe79b-de0` (script at
  `.../workflows/scripts/netcode-multiplayer-bugs-wf_9bcfe79b-de0.js`) was
  killed mid-run — its findings are the same three tracks documented above. You
  can resume it (`Workflow({scriptPath, resumeFromRunId: "wf_9bcfe79b-de0"})`)
  or just work from §2–§3.

---

## 5. HOW TO CONTROL THE GAME (navigation)

The whole point of the tooling: **run everything autonomously** and verify with
measurements before claiming a fix. The user launches the game to *play-test*,
but you can (and should) reproduce joins/movement yourself first. Canonical
reference: [docs/RUNBOOK.md](RUNBOOK.md).

### The three folders
- `G:\AoSRevival\BattleSpades` — **this repo** (the py3 server). Only place we
  write code.
- `G:\AoSRevival\aceofspades_nonsteam` — the **original game** (py2.7 32-bit,
  compiled `.pyd` engine + readable `.py` scenes). Runs as our **dev test client
  AND physics oracle**. `physics_tracer.py` lives in its root (auto-imported by
  `aoslib/run.py`).
- `G:\AoSRevival\aoslib-reversed` — a PREVIOUS AI's hand port. **Logic reference
  only; layouts partly WRONG. Never ground truth.**

### The server
```bash
# Start (prefer DIRECT python.exe over `py` — the py.exe launcher spawns a
# python.exe child and they zombie on port 27015 across restarts):
cd /g/AoSRevival/BattleSpades
nohup "/c/Users/todor/AppData/Local/Programs/Python/Python312/python.exe" run_server.py > logs/server_stdout.txt 2>&1 &
# Logs: logs/log.txt (DEBUG, per-packet) + logs/server_stdout.txt.
# Health: grep 'tick stats' logs/log.txt  (avg should be <1ms, slow=0).
```
Kill cleanly (the zombie problem is real — verify the port after):
```bash
# kill by matching python running run_server.py:
py -c "import psutil;[p.kill() for p in psutil.process_iter(['name','cmdline']) if (p.info['name'] or '').lower() in ('python.exe','py.exe') and 'run_server.py' in ' '.join(p.info['cmdline'] or [])]"
# then confirm exactly ONE owner (or none) of UDP 27015 (PowerShell):
#   Get-NetUDPEndpoint -LocalPort 27015 | Select OwningProcess
```
- Port **27015** (config `[server] port`). Fixed **60Hz** sim.
- ⚠️ **Stop the server before `py setup.py build_ext --inplace`** — a running
  server locks the `.pyd`s and the Cython rebuild fails silently.
- `logs/faulthandler.log` catches native (enet/Cython) segfaults that leave no
  Python traceback.

### The dev client (nonsteam) — your everyday test client + tracer
```powershell
# From the GAME folder, using its BUNDLED py2 (must!):
Set-Location 'G:\AoSRevival\aceofspades_nonsteam'
Start-Process -FilePath '.\python\python.exe' -ArgumentList 'run.py','+debug','+connect','127.0.0.1:27015' -PassThru -WindowStyle Minimized
```
- `physics_tracer.py` auto-loads and opens a **TCP console on port 32896** +
  captures every frame to `aceofspades_nonsteam/logs/physics_capture_<id>.ndjson`.
- ⚠️ **Only ONE game client at a time** — a 2nd instance can't bind 32896 and
  console queries silently hit the wrong process. Kill stale clients first:
  `Get-CimInstance Win32_Process | ? {$_.CommandLine -match 'aceofspades.*run.py'}`.
- The dev client is **patched/lenient** — it MASKS crashes the stock Steam client
  hits (e.g. the map-sync bloat). For any "does the real client accept this?"
  question, verify on the STOCK client (below).

### The stock Steam client — strict verification
`C:\Program Files (x86)\Steam\steamapps\common\aceofspades\aos.exe` (Steam must
be running). Launch `aos.exe +connect 127.0.0.1:27015`. **No tracer** (can't
console into it) — you observe it: is the process alive? did it write
`aos_crash_*.dmp` in that folder? did it reach team-select? Use this to confirm
anything wire-format-sensitive; the dev client is not authoritative for crashes.

### `game_console.py` — run code ON the game thread (port 32896)
```bash
cd /g/AoSRevival/BattleSpades
PYTHONPATH=scripts py -c "import sys;sys.path.insert(0,'scripts');from game_console import GameConsole;c=GameConsole(timeout=6);print(c.run('repr(manager.scene.__class__.__name__)'))"
```
- The tracer evaluates in **EVAL mode (expressions only)**. Single expressions
  work directly. For multi-line / statements you MUST assign the result to `_`
  (e.g. `_=[...]; repr(_)`) — a bare multi-line returns a default attr dump.
  `def` blocks do NOT work (eval, not exec) — inline it.
- Useful probes (client is in GameScene after spawning):
  - `manager.scene.player.get_world_object().position` — player xyz.
  - `manager.scene.map.get_solid(x,y,z)` / `.get_color(x,y,z)` — client's built
    world (compare vs server `ServerVXL`).
  - `manager.scene.player.get_world_object().airborne` etc.
- Sample the game thread via `pyglet.clock.schedule_interval`, **never
  `time.sleep`** (blocks the render loop).

### `auto_join.py` — drive the dev client into a spawned player
```bash
py scripts/auto_join.py --team 2 --class-id 0 --wait 80
# teams: 2=TEAM1, 3=TEAM2. Gates on map-transfer + async world-BUILD stability
# before spawning (spawning early drops you in water / entombs). Re-joins after
# a server restart (client drops to MenuScene).
```
Typical loop: start server → launch dev client → `auto_join.py` → probe with
`game_console` → observe.

### IDA Pro MCP — client ground truth (127.0.0.1:13337)
Wired up. Load tools with
`ToolSearch query "select:mcp__plugin_ida-pro_idalib__idb_open,...decompile,...list_funcs,...search_text,...xrefs_to"`.
- `character.pyd` = movement + the **position-reconciliation** the client runs on
  its own WorldUpdate row (ADJUST vs SNAP against movement history at the
  packet's loop_count). The engine `.pyd` has the **BlockManager** (block apply).
- Cython functions = a wrapper + a body; attribute access goes through interned-
  string `dword_XXXX` globals — trace those to resolve field names.
- See the `reference_ida_netcode_re` memory for the reconciliation contract we
  already RE'd, and `docs/PHYSICS_CALIBRATION.md` for the physics-oracle workflow
  (extract real constants from the live client).

### Verifying offline (before touching live)
- `py scripts/replay_parity.py` — MUST stay **ALL PASS**. Fixtures were recorded
  on **ArcticBase** only; temporarily set `[server] default_map = "ArcticBase"`
  to run it (other maps "diverge" purely from different terrain, not a
  regression). Set it back to `CityOfChicago` after.
- `py scripts/replay_movebox.py` — collision/climb/spawn gate.
- `py -m pytest tests/ -q` — full suite (was 75 pass; `test_reversed_map_sync`
  7/7 after `fd77e9d`).

---

## 6. Key invariants (do not regress) + files

Hard invariants live in [CLAUDE.md](../CLAUDE.md) — read them. Highlights that
touch the open bugs:
- WorldUpdate is 60Hz, **UNRELIABLE**, per-connection, INCLUDES the recipient's
  own self-row (stamped with that client's consumed input loop) — the client
  reconciles it. Without self-rows every jump snaps to spawn.
- ENet PROTOCOL_VERSION=168, single channel, range-coder; wire framing =
  prefix byte (0x30/0x31/0x32) + lzf chunking; block packets use RAW shorts for
  x/y/z (no /64 fixed-point).
- Map sync contract: `InitialInfo.checksum` = `zlib.crc32(raw .vxl bytes)`;
  `map_sync_mode` stays `"full"`; the client rebuilds the world from the stream
  (now the raw-span walker, `fd77e9d`).

**Files by concern:**
- Netcode / sim / self-rows: [`server/main.py`](../server/main.py) (world-update
  broadcast L780–900, sim loop), [`server/player.py`](../server/player.py)
  (input consume L1150–1168, `last_applied_input_loop`, INPUT_DELAY_TICKS=1).
- Blocks / combat: [`server/combat_runtime.py`](../server/combat_runtime.py)
  (`_broadcast_block_mutation`, `handle_block_line`, melee), packet routing in
  [`protocol/packet_handler.py`](../protocol/packet_handler.py).
- Map: [`server/world_manager.py`](../server/world_manager.py)
  (`iter_full_sync_chunks`), [`aoslib/vxl.pyx`](../aoslib/vxl.pyx) (Cython
  loader — rebuild needed for changes), [`server/connection.py`](../server/connection.py)
  (`send_map_data`).
- Config knobs: [`config.toml`](../config.toml) `[debug]`
  (`worldupdate_loop_offset`, `worldupdate_self_row_interval`, `debug_selfrow`,
  `worldupdate_include_self`, `broadcast_world_updates`).
- Offline reconciliation sim: `tmp/reconcile_sim.py`.

**Persistent memory** for this project lives in
`C:\Users\todor\.claude\projects\G--AoSRevival-BattleSpades\memory\` (index at
`MEMORY.md`). The relevant entries: `reference_ida_netcode_re` (reconciliation
contract), `reference_vxl_map_format` (map format + the `fd77e9d` fix),
`reference_gameplay_packets` (WorldUpdate byte layouts, block packets),
`project_physics_oracle`, `feedback_self_test_via_autojoin`,
`feedback_no_worktrees` (**never use git worktrees**).

---

## 7. Working agreements (Kiril / KikoTs)
- Work ONLY in this repo (+ the game folder for tracer/client tooling).
- **NEVER use git worktrees.**
- Don't delete his files — archive to `G:\AoSRevival\archive`.
- He launches the game to play-test; everything else must run autonomously.
- **Verify with measurements before claiming a fix.**
- Before pushing to `main`: fetch/rebase first. The whole project history commits
  directly to `main` (solo dev, his own repo `KikoTs/BattleSpades`).
