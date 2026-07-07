# BattleSpades — Ace of Spades 1.x (Battle Builders) Server Recreation

Python 3.12 + Cython recreation of the original AoS 1.x dedicated server,
targeting 1:1 behavior with the original compiled game client.

## The three folders

- `G:\AoSRevival\BattleSpades` — THIS repo, the py3 server. Only place we work.
- `G:\AoSRevival\aceofspades_nonsteam` — the original game (py2.7 32-bit,
  compiled .pyd engine + partly readable .py scenes/menus). We run it as our
  test client AND as a physics oracle. Our `physics_tracer.py` lives in its
  root (imported at top of `aoslib/run.py`).
- `G:\AoSRevival\aoslib-reversed` — a PREVIOUS AI's hand-written
  reimplementation. **NOT a real decompile — its physics formulas are partly
  WRONG. Never treat it as ground truth.** Ground truth = the live game,
  extracted via the oracle workflow (docs/PHYSICS_CALIBRATION.md).
- `G:\AoSRevival\archive` — user's old junk (ace.py etc.), keep archived.

## Read these first

- **docs/RUNBOOK.md** — how to start/stop server+client, control the live
  game remotely (auto_join, game_console, oracle experiments), verify
  smoothness, current status and open bugs.
- **docs/PHYSICS_CALIBRATION.md** — every measured physics formula/constant
  and the extraction workflow. The replay suite
  (`py scripts/replay_parity.py`) must stay ALL PASS.
- **docs/GOAL.md** — long-term roadmap (modes, maps, classes, weapons).

## Key invariants (hard-won, do not regress)

- Server simulates at fixed 60Hz; WorldUpdate sent UNRELIABLE, per-connection,
  EXCLUDING the recipient's own row (self-echo = chunky walking).
- Client clock is 1 tick ahead; inputs buffered by ClientData.loop_count,
  applied at the matching tick (INPUT_DELAY_TICKS=0).
- InitialInfo movement_speed_multipliers: indexed by class id; it's a SPEED
  SCALE the client multiplies into its local class constants — the server
  sim must use identical wire-rounded scaled values (server/class_data.py).
- Logging must stay queue-based (QueueListener); synchronous handlers stall
  the event loop. Watch `tick stats:` log lines for loop health.
- Stop the server before `py setup.py build_ext --inplace` (it locks .pyds).
- Never write config.toml with PowerShell `Set-Content -Encoding utf8` (BOM
  silently breaks toml.load → server runs defaults on the wrong port).
- Tests: `py -m pytest tests/ -q` — ALL PASS (75) as of 2026-06-12. The old
  test_reversed_map_sync "VXL surface-z bug" was a bug in the TEST's raw
  walker (it consumed only the first span record per column); the loader is
  byte-faithful (verified 0/262144 column mismatches on ArcticBase).
- Only ONE game client instance at a time: a second instance can't bind the
  tracer console port (32896) and console queries silently hit the wrong
  process — kill old instances before launching
  (`Get-CimInstance Win32_Process | ? {$_.CommandLine -match 'aceofspades.*run.py'}`).
- game_console MULTI-LINE snippets must assign the result to `_`
  (otherwise the tracer returns a default player attr_dump).

## Protocol quirks

- ENet PROTOCOL_VERSION=168, single channel, range-coder compression.
- Wire framing: prefix byte (0x30/0x31/0x32) + lzf chunking; server sends
  always chunk, server receive un-chunks only on 0x31.
- Orientation wire format: sign-magnitude piecewise fixed-point
  (|v|<1 → v*8192; |v|≥1 → 16384+(v-1)*8192).
- Block packets (BlockBuild etc.) use RAW shorts for x/y/z (no /64 fixed).
- **Map sync contract (measured 2026-06-12, supersedes the old London hack):**
  - InitialInfo.checksum = `zlib.crc32(raw .vxl file bytes)` (London's
    592649088 was simply London.vxl's file CRC). The client compares it
    against the crc of its local copy of `filename`; a mismatch makes the
    client DISCARD its map and end up in an EMPTY world (wades at the
    waterline, "stuck", jumps snap to spawn).
  - MapDataValidation reply must carry OUR file CRC (never echo the
    client's own value back).
  - MapSyncStart (55) is the BARE id byte. aosprotocol.1x.md's `size: int`
    field is WRONG — extra bytes get parsed by the client's
    process_current_data as a truncated next packet (NoDataLeft crash).
  - The client uses its local file ONLY for CRC validation; world CONTENT
    comes exclusively from the MapSync chunk stream → `map_sync_mode`
    must stay "full". The client builds the world ASYNC after the
    transfer; spawning before the build finishes drops the player into
    water and then entombs them (auto_join now gates on content
    stability).
- WorldUpdate self-rows: the original server INCLUDED each client's own
  row; the client reconciles it against its movement history at the
  packet's loop_count (ADJUSTING/SNAPPING, compiled in character.pyd).
  Without self-rows the client's network anchor stays at the CreatePlayer
  spawn forever and EVERY JUMP snaps the player back to it (measured 6/6).
  With self-rows at stamp offset 0, walking shows one-step-back yanks
  (median 0.131 = exactly one tick of walk speed) — calibrate
  `worldupdate_loop_offset` (config.toml [debug]).
- Packet IDs of note: 0 ClockSync, 2 WorldUpdate, 4 ClientData,
  15 NewPlayerConnection, 28 CreatePlayer, 45 StateData, 55/59 MapSync,
  60 MapDataValidation, 105 SteamSessionTicket, 110 ClientInMenu,
  116 PositionData (the 1.x client does NOT send it).

## Working agreements with the user (Kiril)

- Work ONLY in this repo (+ the game folder for tracer/client tooling).
- NEVER use git worktrees.
- Don't delete his files — archive to G:\AoSRevival\archive.
- He launches the game himself to play-test; everything else must run
  autonomously (the RUNBOOK tooling exists exactly for that).
- Verify with measurements before claiming something is fixed.
