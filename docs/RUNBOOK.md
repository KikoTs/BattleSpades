# RUNBOOK — Operating the Server, Game Client, and Control Tools

How to start everything, control the live game programmatically, and run
the measurement workflows. This is the operational handoff doc; the physics
ground truth lives in [PHYSICS_CALIBRATION.md](PHYSICS_CALIBRATION.md).

## Components & ports

| Thing | Where | Port |
|---|---|---|
| BattleSpades server (py3) | `G:\AoSRevival\BattleSpades` | 27015 (ENet), config.toml |
| Game client (py2.7, 32-bit) | `G:\AoSRevival\aceofspades_nonsteam` | connects to 27015 |
| In-game tracer console (TCP eval) | injected by `physics_tracer.py` | 127.0.0.1:32896 |
| Debug parity UDP (client→server samples) | `server/debug_parity.py` | 127.0.0.1:32895 |

## Start / stop

```powershell
# Server (from BattleSpades; logs -> logs/log.txt, faulthandler.log)
py run_server.py

# Game client (from aceofspades_nonsteam; MUST use the bundled py2!)
.\python\python.exe run.py +debug +connect 127.0.0.1:27015

# Kill the server (it LOCKS the .pyd files — required before rebuilds)
Get-CimInstance Win32_Process | ? {$_.CommandLine -match 'run_server'} | % {Stop-Process -Id $_.ProcessId -Force}

# Rebuild Cython after editing aoslib/world.pyx etc. (server must be stopped)
py setup.py build_ext --inplace
```

## Controlling the live game (no human needed)

The tracer (`aceofspades_nonsteam/physics_tracer.py`, imported at the top of
`aoslib/run.py`) gives full remote control. It also captures EVERY frame of
the local player to `aceofspades_nonsteam/logs/physics_capture_<id>.ndjson`.

```powershell
# Autonomous connect + team/class select + spawn (retries flaky first spawn)
py scripts/auto_join.py --wait 120

# One-shot eval ON THE GAME THREAD (helpers: player, manager, scene, state,
# tag('name') to tag capture frames, attr_dump(obj), find_player())
py scripts/game_console.py "repr(manager.scene.player.get_world_object().position)"
py scripts/game_console.py --repl          # interactive
py scripts/game_console.py --file foo.py   # run a script in-game (py2 syntax!)

# Drive REAL inputs (full client pipeline incl. ClientData to server):
py scripts/game_console.py "from pyglet.window import key as K`nmanager.keyboard[K.W] = True`nmanager.window.dispatch_event('on_key_press', K.W, 0)`n_ = 'walking'"
# character-level setters also work: ch = manager.scene.player.character;
# ch.set_walk(f,b,l,r) / set_jump / set_sprint / set_sneak  (no ClientData? they DO flow)

# Oracle physics extraction (creates fresh aoslib.world.Player in-game,
# runs deterministic scenarios, saves fixtures to logs/oracle/*.json)
py scripts/oracle_experiments.py

# Replay fixtures through OUR py3 engine, frame-diff (must stay ALL PASS)
py scripts/replay_parity.py
```

Gotchas:
- After a server restart the game drops to MenuScene; `auto_join.py` re-joins,
  but after several reconnect cycles the CLIENT's network state wedges —
  restart the game process when joins start timing out at "map transfer".
- Kill SERVERS with a CommandLine match on 'run_server' WITHOUT a Name
  filter: `py run_server.py` is a py.exe→python.exe chain and killing only
  python.exe leaves the parent; filtered kills during 2026-06-12 left THREE
  servers fighting over port 27015 (clients connect to a zombie → endless
  flakiness). Same for game instances: only ONE at a time (the tracer
  console port 32896 binds first-come; extra instances silently swallow
  console queries).
- Never call `player.set_jetpack(0)` in-game (corrupts HUD, hangs renderer).
- Never write config.toml with PowerShell `Set-Content -Encoding utf8`
  (BOM breaks toml.load → server silently runs DEFAULTS on port 32887!).
- py2 code sent to the console: no f-strings; coding-cookie lines stripped
  by game_console --file automatically.

## Netcode architecture (current, all measured/proven)

- Server-authoritative sim at fixed 60Hz (accumulator loop, 1ms Windows
  timers via timeBeginPeriod). `movement_authority = "server"` in config.
- Client clock runs 1 tick AHEAD (ClockSync): ClientData stamped N arrives
  at server tick N-1. Inputs are buffered by loop_count and applied at the
  matching tick (`INPUT_DELAY_TICKS = 0` in server/player.py).
- WorldUpdate: built+sent INSIDE the sim tick, UNRELIABLE (reliable 60Hz =
  ENet ACK head-of-line blocking = multi-second lag bursts), and
  **per-connection EXCLUDING the recipient's own row** — the 1.x client
  micro-corrects the local player on ANY self-echo (chunky walking); with
  no self-row the local player runs pure prediction (measured: 0 direction
  reversals) while other players still stream at 60Hz.
- Held jump re-triggers every grounded frame (client mirror, no edge
  detection/queue). Spawns drop in 0.5 above standing height (exact
  boundary = degenerate bob equilibrium).
- Logging is queue-based (QueueListener daemon thread) — never add a
  synchronous handler; it stalls the event loop. Watch `tick stats:` lines
  (every 10s) — slow ticks mean something is blocking the loop.

## Verifying smoothness after changes

```powershell
# Tag a capture window, drive a walk, then analyze direction reversals
# (0 = butter; the analysis snippet lives in git history / write inline):
py scripts/game_console.py "tag('mytest')`n..."   # start inputs
# ... let it run ...
py scripts/game_console.py "tag('')`n..."          # stop
# then: parse the newest physics_capture_*.ndjson, count frames where the
# horizontal movement vector reverses (>0.01) — see RUNBOOK history.
# Live client/server diff: py scripts/parity_summary.py --path logs/physics_parity_server_<id>.ndjson
```

## Current status (2026-06-12, evening — post map-sync fix)

DONE: physics parity (replay suite ALL PASS, see PHYSICS_CALIBRATION.md),
wade threshold (feet >= 239), unreliable WorldUpdates, non-blocking logs,
input buffering, jump mirror, spawn drop-in, InitialInfo speed-scale
alignment (class_data.speed_scale).

**FIXED today — the jump-rollback / "stuck" desync.** Root cause was the
map transfer, not physics: InitialInfo.checksum carried a chunker CRC
instead of the raw FILE crc32, so the client's local-map validation failed,
it discarded its map and played in an EMPTY world (wading at the waterline
at ~60% speed → 85-block divergence; every jump snapped it back to its only
network anchor, the CreatePlayer spawn). Now: checksum = file crc32,
MapDataValidation reply = our file CRC, map_sync_mode=full (the client's
world content comes ONLY from the sync stream — its local file is just for
validation). Verified live: CRC match at join, world columns match on real
terrain, mean client/server delta 0.13mm over 858 samples.

Tests: 75/75 pass (the old test_reversed_map_sync failure was a buggy raw
walker in the TEST — multi-span columns desynced its (x,y) attribution; the
loader itself is byte-faithful, 0/262144 mismatches on ArcticBase).
Harness scenarios all PASS (full_handshake, spawn_walk, walk_speed,
multi_bot, reconnect, block_build).

OPEN (task list):
1. **WorldUpdate self-row stamp calibration — one notch left.**
   worldupdate_include_self=true (original behavior; without it every jump
   snaps the player back to spawn, measured 6/6). Input timing was made
   DETERMINISTIC (INPUT_DELAY_TICKS=1 in server/player.py + in-game packets
   drained at tick start instead of create_task — previously input N was
   applied at tick N or N+1 per-packet randomly, which no stamp offset can
   compensate). Measured sweep of the WorldUpdate stamp (backward yanks
   per ~8s walk): stamp N-1: 38 (full-step 0.26 max); stamp N-2: 24
   (sub-step, 0.147 max) <- SHIPPED (worldupdate_loop_offset=-1);
   stamp N-3 (offset=-2) untested — client instances kept dying. NEXT: A/B
   offset -2 vs -1 by feel + smoothness analysis (tmp/smooth_analysis.py,
   change the tag). Note clock_sync_loop_bias=+1 is silently ignored by
   the client (under its resync threshold) — keep 0.
2. **auto_join map-build gate**: spawning before the client's async world
   build completes drops the player into water and entombs them when the
   terrain materialises. auto_join now polls world content stability
   before create_player. The REAL client UI gate should be confirmed.
3. **Native server crash after several connect/disconnect cycles** — dies
   silently (no Python traceback => native, likely enet peer lifecycle).
   faulthandler writes logs/faulthandler.log.
4. **Game client dies ~2-4 min after spawn during autonomous runs** (exit
   code 5, no traceback; instances left disconnected in the menu live
   indefinitely). Suspects: someone closing the popped-up window, or a
   periodic packet/timer. Track when it next happens with the window
   left alone.
5. **Climb micro-interplay**: slope transitions still produce small
   transient divergence (~0.4 max). Walking feel is fine; polish.
6. parity_summary.py: flag comparison uses mismatched schemas;
   latest_capture sorts by name not mtime.
7. Delta map sync ("auto" mode) parked: needs the client to actually use
   its local file as world base, which it does NOT (content comes only
   from the stream). Dirty-column tracking already implemented server-side.
