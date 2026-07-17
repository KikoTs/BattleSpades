# RUNBOOK — Operating the Server, Game Client, and Control Tools

How to start everything, control the live game programmatically, and run
the measurement workflows. This is the operational handoff doc; the physics
ground truth and reverse-engineering workflow live in [PROTOCOL.md](PROTOCOL.md).

> **2026-07-11 operational note:** sections marked as historical later in this
> file describe the old always-on tracing rig. Production launches must keep
> parity capture and packet/movement tracing disabled. Current architecture and
> investigation context lives in [ARCHITECTURE.md](ARCHITECTURE.md) and
> [HANDOFF.md](HANDOFF.md).

## Components & ports

| Thing | Where | Port |
|---|---|---|
| BattleSpades server (py3) | `G:\AoSRevival\BattleSpades` | 27015 (ENet), config.toml |
| Game client (py2.7, 32-bit) | `G:\AoSRevival\AceOfSpades_no_steam_new` | connects to 27015 |
| Optional tracer console (TCP eval) | injected by `physics_tracer.py` for development only | 127.0.0.1:32896 |
| Debug parity UDP (client→server samples) | `server/debug_parity.py` | 127.0.0.1:32895 |

## Start / stop

```powershell
# Server (from BattleSpades; logs -> logs/log.txt, faulthandler.log)
py run_server.py

# Reconstructed retail tutorial (isolated; never use run_server.py for this)
py -3.12 run_tutorial.py --check
py -3.12 run_tutorial.py              # optional: --port 27016

# Hosted retail Map Creator (isolated; retail UGC assets are user-supplied)
py -3.12 run_map_creator.py --check `
  --retail-root G:\AoSRevival\AceOfSpades_no_steam_new
py -3.12 run_map_creator.py --project MyMap --terrain grassland `
  --target-mode ctf --retail-root G:\AoSRevival\AceOfSpades_no_steam_new

# Game client (from AceOfSpades_no_steam_new; MUST use the bundled py2!)
.\python\python.exe launcher.py +s

# Kill the server (it LOCKS the .pyd files — required before rebuilds)
Get-CimInstance Win32_Process | ? {$_.CommandLine -match 'run_server'} | % {Stop-Process -Id $_.ProcessId -Force}

# Rebuild Cython after editing aoslib/world.pyx etc. (server must be stopped)
py setup.py build_ext --inplace
```

The tutorial launcher verifies the exact `Training.vxl` SHA-256 before it
opens a socket. It then locks mode 10, allocates one of the map's twelve
authored lanes per player, restores that lane's five targets when reused, and
drives the stock HelpPanel, target counter, completion sound, and exit
countdown. The player begins with no selectable tools; packet 13 atomically
grants/equips the pistol at Shooting, then grants pistol/block/spade (with the
spade selected) at Climb. Bots, plugins, map votes/rotation, master
registration, normal map entities, and competitive damage are disabled only
for this process. The normal mode registry intentionally contains no `tut`
entry.

The Map Creator launcher creates or resumes a sibling `.vxl`/`.txt`/`.ugc`
project triplet. New projects default to `ugc-projects/`; pass an explicit
`.ugc` path or `--output-dir` to change that location. `--overwrite` replaces
the triplet from the selected retail baseplate. The normal server mode registry
intentionally contains no `ugc` entry, and editor startup disables bots,
plugins, voting, rotation, Steam registration, damage, and competitive round
rules in memory.

The persistent launcher defaults live in `config.toml`:

```toml
[map_creator]
project = "MyUGCMap"       # project name, or a path to an existing .ugc
output_dir = "ugc-projects"
terrain = ""               # blank/new = grassland; see --help for all nine
target_mode = ""           # blank = preserve existing; new projects use tdm
retail_root = ""           # AoS directory containing ugc/maps and ugc/kv6
```

Command-line values take precedence over this table. Relative project and
output paths are resolved from the application root, not the terminal's
current directory. For example, either of these reopens the same authored map:

```powershell
py -3.12 run_map_creator.py --project MyUGCMap
py -3.12 run_map_creator.py --project .\ugc-projects\MyUGCMap.ugc
```

At startup the launcher prints the resolved sidecar, editable VXL, metadata,
and exact reopen command. With the defaults above the files are:

```text
ugc-projects/MyUGCMap.ugc  # title, author, palette, Game Data objects, tags
ugc-projects/MyUGCMap.vxl  # authoritative edited voxel terrain
ugc-projects/MyUGCMap.txt  # baseplate presentation metadata
ugc-projects/MyUGCMap.png  # optional overhead preview when supplied
```

Small `.ugc` metadata changes checkpoint atomically at most once per second.
The full VXL is intentionally serialized only during a clean editor shutdown,
outside the 60 Hz gameplay loop. Stop with Ctrl+C and wait for shutdown to
finish; force-killing the process discards terrain edits made since startup.
`--terrain` selects a baseplate only when creating a project (or when explicitly
replacing one with `--overwrite`). `--target-mode` may reopen an existing
project under a different validation ruleset.

Clean-client Map Creator acceptance is:

1. Run `--check` and require `9 terrain triplets` plus at least 400 installed
   KV6 files.
2. Join from a fresh retail process, finish LoadingMenu, and press Start.
3. Require the six native tab counts `138/90/47/47/26/25` and a 373-entry
   prefab catalog. Select up to five combined Constructs/Game Data items.
4. Place, rotate, and erase a small prefab; place an ammo point and the target
   mode's required spawn/base objects. Reconnect and require identical terrain,
   raw prefab colors, objects, palette, and validation rows.
5. Place a large catalog model while watching tick metrics. Packet drain must
   remain below 10 ms; preparation must not block the gameplay thread, and the
   client must remain in GameScene after the final commit.
6. Stop with Ctrl+C and verify that the VXL and sidecar timestamps advance and
   no `.tmp` checkpoint remains.

Do not introspect native Character/world fields during the immediate spawn
transition with the tracer console; the stock client can dereference an
incomplete scene and create a misleading crash dump. Use menu/state queries
until GameScene is settled.

## Controlling the live game (no human needed)

When explicitly enabled for a development session, the tracer
(`AceOfSpades_no_steam_new/physics_tracer.py`) gives full remote control. Its
frame capture is high volume and must not be part of a production or capacity
run.

```powershell
# Autonomous connect + team/class select + spawn (retries flaky first spawn)
py scripts/auto_join.py --wait 120

# Exact two-client build -> sprint -> jump gate while a second retail client
# emits real Snowblower projectiles. Use only the isolated 27016 validator.
py scripts/scenarios/combined_replication_stress.py `
  --server 127.0.0.1:27016 --duration 12 `
  --mover-team 2 --emitter-team 2 `
  --artifact-dir logs/combined-replication/manual

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

## Netcode architecture (current)

- Server-authoritative sim at fixed 60Hz (accumulator loop, 1ms Windows
  timers via timeBeginPeriod). `movement_authority = "server"` in config.
- Client clock runs 1 tick AHEAD (ClockSync): ClientData stamped N arrives
  at server tick N-1. Inputs are buffered by loop_count and applied at the
  matching delayed tick (`INPUT_DELAY_TICKS = 1` in server/player.py). A burst
  remains buffered; the server consumes at most one client-history frame per
  simulation tick.
- WorldUpdate is built after a completed simulation step and sent UNRELIABLE at
  30 Hz. Production includes the recipient's safe self row at the same cadence;
  the row is stamped with that recipient's consumed input loop, never a global
  player stamp. Without this fresh anchor, jump can visibly roll back toward the
  CreatePlayer spawn position even when native SNAP/ADJUST counters stay quiet.
- Observer snapshots remain 30 Hz during every movement state. Only the local
  airborne reconciliation row is reduced to 10 Hz (six simulation ticks) to
  avoid repeatedly re-arming the stock client's airborne history replay; do
  not raise it above six without a real flying-entity rollback gate.
- Held jump re-triggers every grounded frame (client mirror, no edge
  detection/queue). Spawns drop in 0.5 above standing height (exact
  boundary = degenerate bob equilibrium).
- Logging is bounded and queue-based. Never add a synchronous handler to the
  gameplay thread; saturation drops telemetry instead of stalling play. Watch
  `tick stats:` and the metrics snapshot for subsystem time and drop counts.
- Plugin callbacks, entity behavior ticks, incoming packet drains, and join
  mutation catch-up are bounded by `[network]` settings:
`plugin_event_budget_ms`, `entity_tick_batch_limit`,
  `packet_drain_budget`, `mode_event_queue_limit`,
  `mode_event_drain_budget`, and `max_map_mutation_journal`.
- A joining client snapshots the canonical per-cell terrain sequence when its
  MapSync stream is complete. Edits before that cursor are already in the VXL;
  later edits coalesce by coordinate and replay as explicit-RGB builds or exact
  removals before the client is admitted to gameplay.
- If the bounded cell journal overflows while a client is still joining, the
  server disconnects that join rather than replaying incomplete terrain. That
  client should reconnect for a fresh contiguous map snapshot.
- After changing VXL serialization or join orchestration, rebuild the native
  modules and run the convergence gates:

  ```powershell
  py -3 setup.py build_ext --inplace
  py -3 -m pytest tests/test_vxl_sync_stress.py tests/test_reversed_map_sync.py tests/test_join_mutation_catchup.py -q
  ```

  The stress decoder must match authoritative solidity and RGB, including
  isolated voxels, caves, deletion-only columns, collapses, repeated edits,
  simultaneous joiners, and a reliable-send retry inside a multi-cell batch.
- Projectile collision advances before player physics. This mirrors the native
  frame where `process_packet_damage` (`gameScene.pyd:0x1018C270`) applies
  explosion prediction before the GameScene update core (`0x10149CF0`). Generic
  entities and turret behavior remain post-player work.
- Snowball detonation must send reliable, zero-damage `Damage(37)` type 20
  before `DestroyEntity(19)`. Entity id 0 is valid. The Damage event is
  transient: never add it to the late-join map journal. Queue authoritative
  knockback for the third ClientData frame accepted after impact and recompute
  it from the authoritative position/crouch state immediately before that
  frame's physics. This is a dense per-player input sequence, not
  `server.loop_count`, `loop_count + 2`, an ENet ACK, or the sparse client loop.
- Treat disconnect as an id-generation boundary. Purge that exact connection's
  queued packets, revalidate connection and Player object identities at packet
  drain, then retire pending world mutations, projectiles/turrets/fire, combat
  cadence, votes, replication cadence, and owner-bound deployables before
  releasing the id. Keep normal construction/objectives. Destroy an owned MG,
  but only unmount a foreign MG carried by the departing player. Remove radar
  through the station-count helper so team visibility remains reference-counted.
- A `FLARE BLOCK ... cost=10` log means the client selected flare tool 22 and
  sent packet 104. Ordinary block tool 5 uses `BlockLine(40)` and costs one;
  the tools merely share the same visible block model. Stock default carousel
  order keeps block first and flare last.
- Do not reintroduce the validation-only aggregate ENet
  `OwnerTransitionCoordinator`. `reliableDataInTransit` is a transport-batch
  signal, not a GameScene application ACK, and that experiment regressed the
  Engineer jetpack.

## Verifying smoothness after changes

The repeatable gate launches an explicitly instrumented retail client,
auto-joins it, drives the real input pipeline, and cleans up only that client:

```powershell
# Default two-cycle run (about three minutes).
py scripts\scenarios\movement_stress.py --launch --server 127.0.0.1:27015

# Fast development shakeout.
py scripts\scenarios\movement_stress.py --launch --repeats 1 `
  --duration-scale 0.12 --segments walk,sprint,crouch_walk,turn_left,slope_diagonal,jump_run

# Generic movement and block placement must use Soldier. Running this with
# Engineer turns the scripted SPACE hold into jetpack activation.
py scripts\scenarios\movement_stress.py --launch --class-id 0 --repeats 1 `
  --duration-scale 0.5 `
  --segments settle,jump_in_place,walk,sprint,block_sprint_jump

# Run Engineer flight separately so its owner-row handoff is measured rather
# than contaminating the grounded/block gate.
py scripts\scenarios\movement_stress.py --launch --class-id 12 --repeats 1 `
  --segments settle,engineer_jetpack_hold

# Rocketeer has two distinct equipment profiles. Validate the high-thrust Jump
# Pack (66) and low-thrust Glide Pack/Jetpack2 (67) independently.
py scripts\scenarios\movement_stress.py --launch --class-id 2 --repeats 1 `
  --segments settle,rocketeer_jump_pack_hold
py scripts\scenarios\movement_stress.py --launch --class-id 2 --repeats 1 `
  --segments settle,rocketeer_jetpack2_hold

Pack 66 release is deliberately asymmetric: observers receive the inactive
state immediately, while the owner row waits for key-up plus grounded input
settlement. Sending the inactive owner row at fuel exhaustion reproduced a
0.628-block correction; delaying activation instead caused a 4.898-block hard
snap. The clean retail reference artifact is
`logs/playtester-docx/rocketeer-pack66-final/`.

# Isolate the exact block -> sprint -> terrain-step landing regression.
py scripts\scenarios\block_transition_ab.py --launch --class-id 0 `
  --artifact-dir logs\block-transition\manual-soldier-ab

# Engineer activation -> full fuel exhaustion -> held-SPACE fall -> release.
# The scenario still gates native SNAP/ADJUST/visible rollback while treating
# the deliberately aged local-row loop as a handoff diagnostic.
py scripts\scenarios\movement_stress.py --launch --class-id 12 --repeats 1 `
  --duration-scale 2.4 --segments engineer_jetpack_hold,cooldown `
  --artifact-dir logs\jetpack-retail-stress\manual-release

# Two-client Snowblower/order gate after packet or scheduler changes.
py scripts\scenarios\combined_replication_stress.py `
  --server 127.0.0.1:27016 --duration 12 `
  --mover-team 2 --emitter-team 2 `
  --artifact-dir logs\combined-replication\snowball-order-validation
```

The JSON artifact under `logs/movement/` contains every sample, per-segment
analysis, explicit correction events, active tool IDs, palette state, and block
counts. Feature segments require real block/ammo consumption. A release
movement run requires no
hard SNAP, no soft ADJUST, no network-loop regression, matched-loop error at
or below 0.1 blocks, and actual slope/airborne coverage. The tracer console is
enabled explicitly, while synchronous frame capture stays off so it cannot
manufacture the jitter being measured.

Use `--client-frame-capture` only for offline physics replay. Use
`PHYSICS_TRACER_STACK_SAMPLER=1` only while hunting a native crash; it rewrites
and fsyncs a diagnostic file every 50 ms and invalidates timing results.
`debug_selfrow=true` writes through the bounded debug writer queue and is
rate-limited; still disable it for production and capacity gates.

The certified landing artifact is
`logs/movement/solo-block-after-landing-fix/run-1/movement-stress-20260713T030204.777214Z.json`:
1,200 samples, a real block mutation, zero ADJUST/SNAP/rollback, and 0.000031
maximum matched error.

The certified Snowball artifact is
`logs/combined-replication/snowball-sequence3-final-live/20260714T014849/scenario-run-1/movement-stress-20260713T224938.344225Z.json`:
719 samples over 11.985602 seconds, zero ADJUST/SNAP/visible rollback/stall/
unmatched samples, 0.000076 maximum and p95 matched error, and 0.008209 maximum
backward step. The pinned runner checked that these SHA-256 values were stable
before and after the retail run:

```text
server/main.py               5FCE093AB5F18E45119B4D6C5F9E379A158AACD8ACB48B70DFA4F568774AC998
server/player.py             A255EBE576236CE2FCA656A65A51B625DAFB37CA06F3BF8A0D8B950818416469
server/simulation_runtime.py 7AA38B827B60B7BACAB228692BD110CF8598DEC0AE113EA21873F95FCC1EA217
```

Before accepting a later Snowball change, hash the launched files with
`Get-FileHash -Algorithm SHA256`, run the two-client gate, and hash them again.
Do not accept one lucky server-loop run: that approach alternated between zero
corrections and 2 ADJUST/0.373261 error. Fixed `L+2` produced 3 ADJUST/0.301891
error, and two dense accepted frames with application-time recomputation still
produced 2 ADJUST/0.384826 error.

Profile before proposing Cython work. Current movement/entity reproductions
have total-tick maxima below 2 ms (combined baseline 1.86 ms), far below the
16.67 ms simulation budget. Those corrections were resolved as native
collision and packet-order mismatches, not Python saturation.

The commands below are the older manual workflow:

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

## Production capacity and release validation

Run the fast gate after server hot-path, replication, logging, bot, or entity
changes:

```powershell
py -m pytest -q
py scripts\server_capacity.py --players 50 --seconds 30 --port 27016
```

Root `pytest.ini` deliberately limits canonical discovery to `tests/`. Do not
remove that boundary: developer snapshots under `tmp/` and `.worktrees/` may
contain copied `test_*.py` modules and make an unconstrained recursive run look
like a collection stall. To verify the release inventory first:

```powershell
py -3.12 -m pytest --collect-only -q
```

The gate must spawn all 50 players, sustain at least 58 Hz, keep tick p99 at or
below 12 ms, and report zero dropped gameplay packets. A release candidate must
also pass a 15-minute soak:

```powershell
py scripts\server_capacity.py --players 50 --seconds 900 --port 27016
```

Before and after native-client validation, count crash dumps so a new dump
cannot be mistaken for an old one:

```powershell
$client = 'G:\AoSRevival\AceOfSpades_no_steam_new'
(Get-ChildItem $client -Filter 'aos_crash_*.dmp').Count
```

The validation and decompiled clients install the Pyglet 1.2 raw Win32 mouse
shim during `aoslib.run` startup. For an input-regression A/B, launch the same
binary with `+legacymouse` to restore the old cursor-warp path. Validate a full
360-degree turn, simultaneous fire/build drag, alt-tab/refocus, and
window/fullscreen transitions; automated movement scripts cannot synthesize a
real `WM_INPUT` device stream.

Terrain repair production defaults live under `[network]`: queue 8192, eight
cells per batch, every three ticks, after a 120-tick quiet delay. Capacity output
must show a bounded queue, zero failed repair sends, and tick p99 <= 12 ms. Do
not raise the batch size from a visual test alone; measure a 50-client backlog.

Validate at least one movement window, a Medic-to-Miner class change, one
dynamite placement observed by a second client, reconnect/map mutation catch-up,
and one complete end-round transition. The client must remain in `GameScene`
and the dump count must not increase.

## Map resources, fog, and static-light validation

Use a map with recovered stock metadata; Mayan Jungle exercises all three
crate families and green static-light markers in one join:

```powershell
py scripts/run_validation_server.py --config logs/nonexistent-validation.toml `
  --port 27023 --map MayanJungle --mode ctf
```

Join a clean client and query the instrumented scene. Expected Mayan values are
7 `AmmoCrate`, 7 `HealthCrate`, 7 `BlockCrate`, 4 `FlareBlockEntity`, fog
`(69, 76, 39)`, and skybox `MayanJungle.txt`; CTF additionally has two
`IntelPickup` entities. Reconnect once to prove the same static entities are
revealed after spawn. Touch an ammo crate while injured and verify ammo changes
but health does not; then touch the health crate and verify only health changes.

Repeat a mode start for TDM, CTF, Classic CTF, Arena, VIP, and Zombie. Resource
counts must be identical across modes. CTF must retain its map resources while
replacing only stale base/intel markers. Reject the build for a new dump,
`invalid entity on destroy`, missing flare lights, default fog, or a crate that
refills more than its own resource.

### Ambience and official presentation validation

Use Mayan Jungle because it has both a global bed and a localized river:

```powershell
py scripts/run_validation_server.py --config logs/nonexistent-validation.toml `
  --port 27023 --map MayanJungle --mode tdm

py scripts/auto_join.py --server 127.0.0.1:27023 --team 2 --class-id 0 `
  --console-port 32906

py scripts/game_console.py --port 32906 `
  "[(a.name, a.positions, a.loop_id) for a in manager.scene.ambient_sounds]"
py scripts/game_console.py --port 32906 `
  "[(type(p).__name__, p.relative, p.volume, p.closed) for p in manager.media.players]"
```

Expect controllers `amb_jungle` with no points and `em_river` with exactly
four authored points. Expect two non-closed `GameSound` players: the jungle
bed is relative/global, while the river is non-relative and attenuated. The
client log must not contain `Failed to play sound`.

Also confirm the log selects `mesh/MayanJungle/MayanJungle.txt`. That manifest
is presentation only. The server log must still say `mode=full` and send a
non-empty canonical VXL stream for official maps; checksum-only or delta-only
stock-map joins are a release blocker because they create hollow/desynchronized
worlds. Compare `aos_crash_*.dmp` counts before and after the join.

For static-light coverage, repeat with `--map ArcticBase` and inspect the live
scene after spawning:

```powershell
py scripts/game_console.py --port 32906 `
  "[(int(v.r),int(v.g),int(v.b)) for v in manager.scene.entities.values() if type(v).__name__=='FlareBlockEntity']"
```

Expect 42 entries: 19 `(250,250,200)` and 23 `(255,255,82)`. Then validate the
high-density boundary with `--map 20thCenturyTown`: expect 524 flare entities,
533 total map entities including the nine fallback crates, a responsive
`GameScene`, no new dump, and no `invalid entity`/traceback output. On the
server, probe marker `(362,153,229)`; it must be solid with canonical colour
`0x80FAFAC8`, while `dirty_columns` and the reconnect air-override journal stay
empty. This distinguishes map-derived collision restoration from a player
block mutation.

## CTF objective and minimap validation

Use an isolated CTF server and a clean retail client. Do not validate packet 43
by sending legacy `BASE=1`; that type is absent from the retail entity table.

1. Join CTF and inspect the minimap: both team base icons and both ground intel
   icons must be visible. The client scene should contain exactly two
   `MinimapZone` objects and two type-16 `IntelPickup` objects.
2. Pick up enemy intel. Its ground entity must disappear and the carrier must
   become high-visibility on the minimap for both teams.
3. Kill or explicitly drop the carrier. The carrier marker must clear and a
   new type-16 ground entity must appear at the settled drop position. Leave it
   untouched for 60 seconds and verify that it returns to its home marker.
4. Reconnect while intel is on the ground, then reconnect while another player
   carries it. Late join must reproduce the correct marker in both cases.
5. Carry enemy intel into the visible friendly base box. The score must change,
   carried state must clear, and the intel must reappear at home.
6. Run `/restart` while CTF is active. The client must stay in `GameScene`,
   retain exactly two base zones without duplicates, and receive two fresh
   ground-intel entities.

Reject the build for a new dump, traceback, `invalid entity on destroy`, a
missing marker, or a capture volume that does not match the visible base box.

## Classic CTF retail validation

Classic is an ordinary CTF scene with a feature bit, so validate both the wire
identity and a real objective cycle:

```powershell
py -3.12 scripts/run_validation_server.py --port 27019 --map Crossroads --mode cctf
py -3.12 scripts/auto_join.py --server 127.0.0.1:27019 --team 2 --class-id 0 --console-port 32901
py -3.12 scripts/game_console.py --port 32901 "_={'mode':manager.game_mode,'classic':manager.classic,'players':[(int(i),int(p.class_id)) for i,p in scene.players.items()],'intel':len([e for e in scene.entities.values() if e.__class__.__name__ == 'IntelPickup'])}"
```

Require mode 8, `classic=1`, local class 5 (Deuce), minimap disabled, and
exactly two IntelPickup entities. Pick up the enemy intel and return to the
visible friendly base zone. The carrier must clear, personal score must rise by
10, team score by one, and the intel must reappear three blocks from its home
anchor. Repeat once after reconnect. The default vote must contain only
Crossroads, Hiesville, ToTheBridge, Trenches, WinterValley, WW1, or Classic;
an explicitly configured map list may override that catalog.

Validate the Classic-only death representation separately. Run `/kill` and,
before the respawn delay expires, require the native Character to report
`dead=True`, `exploded=False`, and a loaded `classic_corpse`; the scene entity
table must contain no `GraveEntity`. From a second client, shoot that corpse
once and require it to disappear without an entity-11 create/destroy pair or a
new dump. Repeated shots must do nothing. Also reconnect an observer while a
corpse is visible, then shoot it during that observer's MapSync: reveal must
produce one death transition followed by silent cleanup, never a duplicate
KillAction.

## VIP retail validation

Use a private port and at least one connected player per team. Two players are
enough to prove boss classes/markers; add a third ordinary gangster to the team
whose VIP will die so the round does not end immediately.

```powershell
py scripts/run_validation_server.py --port 27019 --map CityOfChicago --mode vip

# Launch instrumented clients with distinct PHYSICS_TRACER_CONSOLE_PORT values,
# then join both teams. The requested class is deliberately non-gangster; the
# server must normalize it.
py scripts/auto_join.py --server 127.0.0.1:27019 --team 2 --class-id 0 --console-port 32901
py scripts/auto_join.py --server 127.0.0.1:27019 --team 3 --class-id 1 --console-port 32903

py scripts/game_console.py --port 32901 "_={'mode':manager.game_mode,'skin':manager.skin,'players':[(int(i),int(p.class_id),int(p.get_team_id()),int(p.high_minimap_visibility)) for i,p in scene.players.items()]}"
```

Accept only if mode is 7, skin is `mafia`, each team has exactly one boss
(class 10 for team 2, class 11 for team 3), and both clients see both marker
bits. After adding an ordinary teammate, kill one VIP: that VIP must remain
dead beyond `respawn_time`, its marker must clear, its teammate must remain
alive, and the opposing VIP must retain its marker and respawns. Eliminate the
remaining teammate, confirm one team point and a clean new selection, then
terminate an active VIP client and confirm disconnect follows the same death
path. Compare crash-dump counts before/after and stop only the isolated clients
and server.

Keep both clients connected for at least 45 seconds and verify the native score
reasons: a live boss gains 50 every ten seconds and a living guard within 15
blocks gains 10 every five seconds. Move the guard outside the radius and
confirm escort score stops. Throw repeated Molotovs during the same interval;
fire entities must rise and decay instead of growing without bound.

Run the headless performance companion on a private port:

```powershell
py -3.12 scripts/server_capacity.py --players 12 --seconds 45 --port 27021 --mode vip --map CityOfChicago
```

Require 58+ Hz, overall tick p99 at most 12 ms, zero gameplay/mode/terrain
drops, bounded active fire, and VIP mode p99 comfortably below one millisecond.
The separate bot-main-thread 0.75 ms gate remains a bot-runtime requirement;
do not attribute that failure to VIP when the report identifies `bots` alone.

## Admin map, mode, and restart validation

Run lifecycle tests on an isolated port; do not restart the public server just
to validate a patch:

```powershell
py scripts/run_validation_server.py --port 27019 --map CityOfChicago --mode tdm

# Launch the development retail client with +connect 127.0.0.1:27019, then:
py scripts/auto_join.py --server 127.0.0.1:27019 --wait 120
```

After `/admin <password>`, exercise this sequence and inspect both logs after
each scene boundary:

1. `/restart`: the client stays responsive in `GameScene` with a live player.
2. `/map DefinitelyMissingMap_7391`: the current scene/world stays intact and
   the admin receives `Map not found`; no disconnect is allowed.
3. `/map ArcticBase`: the client processes native `MapEnded(52)`, enters
   `LoadingMenu`, receives `InitialInfo` and the validated VXL transfer on the
   same authenticated peer, then returns to `GameScene` on ArcticBase. The
   client must never report `disconnected=True` and the server log must not
   contain an ENet disconnect for that peer.
4. `/mode ctf`: the same retained-peer loader sequence produces a fresh CTF
   scene with visible intel and no `KeyError`, traceback, or
   `invalid entity on destroy` output.
5. Run `/restart` once in CTF, then `/mode tdm`, and finally `/kick` the test
   player. Only the explicit kick may disconnect the peer.

Before this test, install `client_patches/session_transition_patch.py` as
documented in `client_patches/INSTALL.txt` and restart the client once. The stock
packet-52 handler only freezes `GameScene`; disconnect reason 18 is terminal
and is not a reconnect mechanism. The server requires the client's
`MapDataValidation` reply before sending VXL bytes, so an unpatched client is
retired individually instead of receiving loader packets in an old scene.

Reject a lifecycle patch if a new `aos_crash_*.dmp` appears, the client log
contains a traceback/invalid entity warning, or a transition tick exceeds the
12 ms release threshold. `BASE=1` is a server-only CTF marker: never send it in
CreateEntity(21) or DestroyEntity(19). A deployed production process must be
restarted once in coordination with players to load changed Python modules;
editing files does not hot-reload the running server.

To validate retail map voting, lower the round time on the isolated server or
wait until its final minute. The localized next-map overlay must expose one to
three candidates on F1/F2/F3. Cast from two clients, confirm the chosen map is
announced on the HUD, and let the end sequence finish. On an official map the
client must receive final `GameStats(67)`, show the packet-53 scores/credits
screen for `lobby.end_screen_seconds`, and only then enter the packet-52 loader.
The winner must be consumed only at that boundary and use the same retained-peer
loader transition.
An unadvertised candidate packet must not affect the tally.

Also validate the boundary cases on an isolated server:

1. End a score-limited round before the final-minute vote and do not vote. The
   ballot must remain open for its bounded deadline, then choose the first
   rotated candidate and continue; the server must not restart the old map
   early.
2. Join a second client while the ballot is open. Once its first ClientData
   finishes world reveal it must receive the live vote records and current
   counts. Join another client but leave it inside MapSync when rollover starts;
   that peer must receive reason 18 and must never receive a second interleaved
   VXL stream.
3. Select an unavailable/stale target through a test/plugin ballot. No packet
   53 or 52 may be sent; the current map must restart safely.
4. Run the same end on `Training` or another map without a bundled level
   screenshot. Packet 53 must be absent, the configured dwell must still occur,
   and packet 52 must start the ordinary validated rollover.
5. Issue an admin `/map`, `/mode`, or `/restart` during the pre-transition end
   timer. The old end task must cancel and must never wake later to replace the
   administrator's selected epoch.

## Bot runtime validation

Build the pinned native navigator, then run the process and gameplay smokes:

```powershell
py setup.py build_ext --inplace
py scripts\bot_worker_smoke.py --restart
py scripts\bot_combat_smoke.py
py scripts\bot_zombie_smoke.py --seconds 15
py scripts\bot_runtime_smoke.py --seconds 12 --bots 12 --restart-worker-at 2
py scripts\bot_city_soak.py --mode tdm --bots 12 --sim-seconds 60 --report-every 10
py scripts\bot_city_soak.py --mode zom --bots 12 --sim-seconds 60 --report-every 10
py scripts\bot_city_soak.py --map CastleWars --mode zom --bots 2 --sim-seconds 60 --report-every 30 --strand-water-bots 2
py scripts\server_capacity.py --players 12 --seconds 30 --port 27016

# Exercise every implemented mode through the same real worker/native physics.
foreach ($mode in "tdm", "ctf", "cctf", "zombie", "vip", "arena") {
  py scripts\bot_runtime_smoke.py --seconds 4 --bots 12 --mode $mode
}
```

The runtime smoke must report a live child PID and movement for at least one
bot. A class-capable roster should eventually report a replicated deployable
entity; absence in one short seeded run is not itself a failure because
equipment use is intentionally probabilistic. The capacity gate must sustain
58+ Hz, keep overall tick p99 at or below 12 ms and `subsystem_bots_p99_ms` at
or below the configured 0.75 ms, keep worker memory below 256 MiB and CPU below
one core, and report zero gameplay/mode/world-mutation drops.

For a report that every bot froze at once while the server tick stayed healthy,
run `/bots status`. A running worker with growing `silence` and no new actions
is a worker-wide stall, not twelve simultaneous navigation failures. Production
supervision waits through the eight-second startup grace, then automatically
replaces a child that leaves a live-bot frame unanswered for five seconds;
`stalls` records those watchdog restarts. If profiling is available, capture
the child before restarting it; a stack inside `recast*.pyd` from
`WorkerVoxelWorld._native_path_direction` is the known synchronous Detour
signature. Do not stop the authoritative server to recover this condition.

`bot_city_soak.py` loads the real CityOfChicago VXL and advances worker policy
time without sleeping. It prints each bot's position, role, action, affordance,
movement direction, health/ammo/tool, stuck attempts, and stationary duration.
Its exit gate requires zero point-blank construction priority inversions,
repeated action loops, jump loops, travel-role navigation stalls, invalid look
targets, and water stalls. This is an accelerated decision/navigation
diagnostic, not native physics or replication proof; keep the real worker,
capacity, and two-retail-client gates above.

The CastleWars fault-injection command places bots in the two water columns
farthest from dry terrain. Both must report `water_recovery`, eventually reach
dry land, and finish with `water_remaining=0`. Dry bots must still refuse to
enter water. The test exercises the full-map cached voxel escape flow; it is
specifically intended to catch regressions to the old 24/64-column search cap.

`bot_combat_smoke.py` is the stricter player-parity gate. It uses a real worker,
server-owned peerless Players, an authored VXL, and a settled packet observer.
It fails unless bot WorldUpdate rows expose the selected tool plus display bit
`0x10`, native ShootFeedback packets accompany firearm damage/kill against an opposing Player, KillAction is
replicated, and RoundLifecycle respawns the victim.

`bot_zombie_smoke.py` is the class-specific contact gate. It fails unless an
active Zombie closes a ten-block authored lane, damages an idle real Player,
starts with a Zombie class/loadout/tool-consistent CreatePlayer/WorldUpdate,
and exposes the primary claw swing to the packet observer. Normal validation
must report a real worker PID. If an automation sandbox denies Windows pipe
creation with WinError 5, `--inline-worker` may be used only for that restricted
smoke; production never uses the in-process adapter.

For the human roster race, run two clean clients against an isolated validation
port and keep packet trace output separate from production:

```powershell
py scripts\run_validation_server.py --port 27016 --packet-trace
py scripts\scenarios\palette_stability.py --server 127.0.0.1:27016 --launch `
  --packet-trace-log <validation-server-stderr-log>
```

Every observer sample must contain the remote player. After the scripted
observer reconnect, that player must still exist with the current tool and
block color. Python logging writes to stderr by default. The scenario also has
stricter palette-wire assertions: they require real UI-generated SetColor and
palette-on ClientData records and are separate from the roster acceptance gate.

For retail acceptance, observe 12 bots from two clean clients in TDM, CTF,
Zombie, VIP, and Arena. Confirm ordinary CreatePlayer/tool/WorldUpdate state,
natural turning, no fire through walls, visible deployable creation, objective
pickup/drop, and no new crash dump. Kill the `BattleSpadesAI` child only (never
an unrelated Python process), confirm players and 58+ Hz simulation survive,
then wait for the 1/2/5/30-second supervised restart and verify movement
recovers without stale traversal through edited terrain.

Bot administration is available after `/admin <password>`:

```text
/bots status
/bots fill 12
/bots add 2 team1
/bots remove 2
/bots difficulty mixed
/bots debug on
/bots debug BotName
```

Debug snapshots expose the bounded current goal, two-point path, action, and
movement affordance plus the current mode role. They remain off by default (`bots.debug_visualization =
false`) and do not render client packets. Prefab work is controlled by
`network.prefab_queue_limit` and `network.prefab_cell_batch_limit`; do not make
either unbounded to accelerate large models.

## Steam master-server listing

Windows releases include the x86 bridge but not Valve binaries. Put the
original signed x86 `steam_api.dll` in `steam-runtime/`, then set:

```toml
[steam]
enabled = true
runtime_dir = "steam-runtime"
steamclient_dir = ""       # auto-discover desktop Steam
steam_port = 8766
query_port = 0              # game port + 1
public = true
secure = false
require_registration = false
```

For a headless host without desktop Steam, set `steamclient_dir` to an
operator-owned directory containing compatible x86 `steamclient.dll`,
`tier0_s.dll`, and `vstdlib_s.dll`. Do not copy the 300 KB legacy
`steamclient.dll` from the decompiled client: it was observed blocking inside
`SteamGameServer_Init`. The helper watchdog contains that failure, but it
cannot register it.

Forward UDP for the ENet game port, `steam_port`, and the effective
`query_port`. A healthy startup reports `Steam GameServer011 initialized` and
then `Steam master logon complete: steam_id=...`. Verify both Valve
registration and the public A2S socket from another network:

```powershell
py scripts\check_steam_registration.py 203.0.113.10 `
  --game-port 27015 --query-port 27016
```

The checker uses Valve's public `ISteamApps/GetServersAtAddress` endpoint and
then performs the optional A2S challenge round-trip. A pass proves that the
server-owned registration path is healthy.

The original server-list UI has two independent 2026 limitations:

- Valve removed the `hl2master.steampowered.com` legacy list service used by
  the 2015 client. All/Community can therefore show zero and finish with
  `eServerFailedToRespond` even when the checker passes.
- The retail `ServerInfo` class ignores the returned game port and always
  joins UDP `32887`. Set `[server].port = 32887` and forward it when an
  unmodified browser row/direct default is a deployment requirement.

Do not change app `224540`, game dir `aceofspades`, or forge `white=1` to work
around the retired client endpoint. A modern directory/client fallback is a
separate client-distribution concern.

`require_registration=false` is the production-safe default: the supervisor
backs off while the game remains online. Set it true only when an orchestration
system should treat missing Steam discovery as a failed deployment.

## Historical status (2026-06-12, evening — post map-sync fix)

DONE: physics parity (replay suite ALL PASS, see PROTOCOL.md),
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
1. **Movement release gate.** Production self rows are on. The release gate must
   include visible-position rollback detection, not only native SNAP/ADJUST
   counters. Keep `clock_sync_loop_bias=0`; stale no-self-row experiments are
   known to pass counters while visibly rolling back on jump.
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
