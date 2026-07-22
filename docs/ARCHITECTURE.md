# BattleSpades server architecture

This document describes the intended server boundaries and the compatibility
rules that constrain the ongoing refactor. It is a map for maintainers, not a
claim that every boundary has already been extracted into a separate module.
The stock Ace of Spades 1.x client and its observed behavior remain the protocol
oracle.

## Runtime data flow

```text
ENet peer
  -> Connection: framing, handshake, identity
  -> bounded incoming-event queue
  -> packet decoder and domain handler
  -> authoritative world/player/entity state
  -> fixed 60 Hz simulation
  -> replication snapshots at the retail cadence (30 Hz WorldUpdate)
  -> grouped serialization and ENet send
```

Only the simulation thread mutates gameplay state. File and console logging run
on a listener thread through a bounded queue. Diagnostic capture must follow the
same rule: it may observe gameplay, but must never make the simulation wait.
At process startup, after lazy gameplay imports but before server construction,
the launcher collects import-time cycles and moves the stable import graph to
CPython's permanent GC generation. Map, player, worker, and match objects are
created afterward and retain ordinary garbage-collection behavior.

## Service boundaries

The refactor is converging on explicit services. Until extraction is
complete, some responsibilities remain methods of `BattleSpadesServer`; new
code should respect these ownership boundaries instead of adding more unrelated
work to that class.

### Configuration, lobby, and rule catalog

`ServerConfig` is the validated composition input. `server/lobby.py` is the
descriptive retail Match Lobby catalog (ten modes, selectors, official map
sets). `server/game_rules.py` owns the 102 recovered `RULE_*` definitions,
legal values, class/tool mappings, and resolved values. Packet builders and
gameplay domains receive the same config object; a handler must not carry a
second hardcoded copy of a rule.

Legacy `[game]` and `[modes.*]` settings are compatibility adapters at config
load time. New work consumes `config.game_rules`, `config.mode_rule`, and
`config.configured_time_limit`. Rule validation is startup work and never runs
inside the 60 Hz tick.

### SimulationRuntime

Owns the fixed-step clock, ENet event budget, gameplay-packet drain budget,
per-subsystem timing, and overload policy. Its invariants are:

- Physics and gameplay advance at 60 Hz; network bursts do not cause extra
  simulation steps.
- Every queue and per-tick work list is bounded. Overload is measured and
  reported rather than allowed to monopolize the event loop.
- Authoritative mode events use a bounded FIFO. A per-tick drain budget defers
  bursts without reordering them; total saturation increments
  `dropped_mode_events` instead of allowing unbounded memory growth.
- Plugin callbacks run under a gameplay-thread time budget. If the budget is
  exhausted, remaining callbacks are skipped and counted in runtime metrics.
- Entity behavior `on_tick` work is batch-capped and round-robin deferred;
  proximity deployables still use the spatial index so mines and medpacks do
  not become an all-entities-by-all-players scan.
- No synchronous disk or console I/O runs on the tick thread.

### ReplicationService

Owns WorldUpdate construction, reconciliation stamps, entity create/change/
destroy messages, map mutation journaling, and late-join catch-up.

- WorldUpdates are emitted at 30 Hz and serialized once for recipients sharing
  the same reconciliation view.
- A joining player receives a stable base VXL snapshot plus every canonical
  voxel changed after its per-cell watermark. Repeated coordinates coalesce to
  final VXL state; collapse batches replay as exact cells, never as a stale
  topology-expanding effect.
- The cell journal is bounded and reliable-send retry retains an unsent cursor.
  If sequence continuity is lost before first `ClientData`, the server
  disconnects that join with data-error instead of replaying partial terrain.
- Entity IDs are server-owned. `CreateEntity`, state changes, and
  `DestroyEntity` must form one consistent lifetime; sending an unknown or
  already-destroyed ID can destabilize the native client.
- Block coordinates on the wire are raw VXL coordinates. Physics coordinates
  and water-plane conversions stay at the boundary and are never guessed in a
  domain handler.

### RoundLifecycle

Owns join, death, respawn, class/loadout selection, and end-round reset.

- Class, tools, prefabs, and UGC tools form one selection. They must be
  normalized and applied atomically before movement profile setup, inventory
  reset, spawn, and `CreatePlayer` replication.
- `ChangeClass(78)` and `SetClassLoadout(13)` may arrive in either order. The
  active life must never combine one class with the previous class's equipment.
- A round reset destroys or resets runtime entities exactly once, clears stale
  ownership lists, applies pending selections, and only then respawns players.

### MatchTransitionService

Owns serialized admin and natural match lifecycle changes. A same-map restart
keeps the current `GameScene`; a map or mode replacement creates a new client
session because the retail client reads map/mode identity only while building
that scene.

- A requested map is path-normalized and loaded in a worker thread before any
  live state changes. Mode change also reloads the current map in that worker,
  resetting construction and applying target-mode metadata without blocking
  packet drain. A missing, malformed, or out-of-root map leaves players, mode,
  and world untouched.
- Map preparation, mode rollover, and restart are single-flight. Concurrent
  lifecycle commands fail without partially applying either request.
- A full rollover gates every old connection (`in_game = False`) before mode
  startup can emit objective or entity packets, clears old queues/journals,
  installs the new world/mode, then disconnects the old session with retail
  reason `ERROR_MATCH_ENDED` (18). Rejoin performs the ordinary InitialInfo ->
  MapSync -> StateData handshake.
- New-mode gameplay must never enter an old `GameScene`. If commit fails after
  the gate, clients remain retired and reconnect instead of being admitted to
  a partially rebuilt scene.
- Server-only mode markers may exist in `EntityRegistry` with
  `wire_visible=False`. Create and destroy paths must honor that flag
  symmetrically; the retail runtime entity table does not accept every legacy
  entity constant.

### TelemetryService

Owns structured runtime metrics, ordinary logging, and explicitly enabled
diagnostics. `BattleSpadesServer` receives it as an explicit constructor
dependency while retaining `server.metrics` as a compatibility alias.

- Ordinary logs use the bounded non-blocking queue in
  `server/logging_runtime.py`. Formatting and sink I/O happen on its listener
  thread. A full queue drops diagnostic records instead of gameplay frames.
- Packet parsing and hex dumps require both DEBUG logging and
  `logging.packet_trace=true`.
- The background file sink rotates at `logging.max_bytes` and retains at most
  `logging.backup_count` archives, so diagnostics remain disk-bounded.
- Physics parity, movement snapshots, stack sampling, and self-row capture are
  development tools. Production defaults must leave them disabled.
- Diagnostic producers are rate-limited to ten samples/second per session,
  batch writes at most once/second (or 128 records), and expose a drop counter.
  Self-row calibration samples use this bounded writer path; they must never
  reopen or flush files from `ReplicationService`.

### TerrainRepairService

Owns two delayed safety-net lanes for clients that are already in GameScene.
Reliable gameplay mutations and the join snapshot/journal remain the primary
terrain replication paths.

- Validators enroll rejected or cancelled client-predicted footprints in the
  regular lane. It starts after a two-second quiet period and drains at eight
  cells every three ticks (160 cells/second).
- Native unsupported collapse is the only successful mutation enrolled. A
  checked `Damage(37)` first preserves stock falling animation; the server then
  queues every actually removed component cell outside-in. After 18 ticks the
  collapse lane confirms eight exact air cells every three ticks. This repairs
  a client whose local BlockManager derived a different component without
  replaying collapse on clients that already removed it.
- Both lanes are de-duplicated under one bounded queue and read canonical VXL
  state at send time. A rebuilt collapse cell is discarded instead of replaying
  its placement callback. Solid regular repair uses `BlockBuildColored(33)`;
  air uses exact type-6 `Damage(37)` with `chunk_check=0`.
- Mid-join clients never receive repair packets. Their crash-sensitive world
  transition is handled exclusively by MapSync and the mutation journal.

### BotDirector and AIWorkerSupervisor

`BotDirector` owns server-created `Player` objects, population policy,
profiles, lifecycle hooks, staggered perception, expiring intent validation,
and the cheap 60 Hz look/locomotion motors. Peerless bot connections are
explicitly active server-owned players, so they pass through ordinary native
physics, death, respawn, mode, and replication boundaries.

`AIWorkerSupervisor` owns a Windows `spawn` child through a bridge thread.
Process creation, pickling, queue/pipe operations, Recast tile builds, LOS,
target search, behavior-tree traversal, and path queries never run on the
gameplay thread. The server-to-bridge frame queue is capped at 64, the result
queue at 128, and the director drains at most 12 intents per simulation tick.
Results carry bot generation, frame, map/mode epoch, topology version, and a
250 ms expiry; any mismatch is discarded.

Full map snapshots are serialized and level-1 compressed on the bridge, then
sent as versioned, Blake2-validated records no larger than 48 KiB. Frames and
terrain cannot overtake an incomplete transfer. Terrain deltas contain at most
1,024 cells and equal-version batches are idempotent in the worker. A transfer
lease begins with the first queued header and is renewed only by child
heartbeats, so a dead reader is restarted even when a large custom map fills
the 64-record queue before any perception frame can be sent.

The bridge retains a coalesced canonical terrain overlay in addition to the
immutable base VXL. Worker restart and the 65,536-cell overflow rebase compose
`base + overlay` on the bridge thread. This prevents a restarted navigator
from reverting to the original map and avoids calling `generate_vxl` from the
60 Hz thread. Terrain edits dirty the affected 32x32 Recast tile and its
neighbors; immediate movement still checks the live authoritative map.

Frames are coalesced by `(player_id, generation)` before crossing the process
boundary. A slow worker therefore receives the newest perception for every
concrete bot life instead of making decisions from an obsolete FIFO backlog;
the 64-entry bound applies to unique bot lives and overflow remains observable.

Each frame contains at most 32 prioritized players and 192 live entities. The
observer, server-owned bots, objective carriers, explosives, projectiles, and
nearby resources win deterministic priority. Dead respawning pickups are not
serialized; overflow is counted in runtime metrics. This prevents dense custom
maps and the protocol's optional 255-player configuration from producing one
unbounded Windows pipe write.

The worker uses `py_trees` at 8 Hz, perception at 10 Hz per staggered bot, and
a token bucket capped at 24 path requests/second for the default 12-bot roster.
Native Recast/Detour v1.6.0 is vendored under its Zlib license. A bounded
layered A* remains the source-only fallback. The native bridge owns a persistent
DetourCrowd instance (64-agent cap) and returns obstacle-avoiding desired
steering only; native `Player` physics still executes every movement input.
Live steering never calls Detour's synchronous `find_path` fallback. Expensive
native corridor warming is limited to one 32x32 tile per worker batch; bounded
voxel A* remains available while later batches warm the rest. Full-map water
escape is also resumable in 128-node slices rather than scanning a 512x512 sea
inside one decision. The worker emits a processed-frame heartbeat even when a
valid frame intentionally produces no intent. The supervisor clears a live
frame lease only for that frame or a newer one, so countdown/cadence states do
not cause false restarts and an old map-only acknowledgement cannot hide a real
wedge. The first snapshot/frame has an eight-second cold lease; after one
processed frame, five seconds without a result terminates and restarts only the
AI child. `/bots status` exposes stalls, current intent silence, awaited frame,
snapshot transfer, and heartbeat progress so an alive-but-wedged worker is
distinguishable from ordinary local route recovery.
Class-filtered jump, crouch, safe-drop, and fuel-gated jetpack transitions are
represented by an explicit affordance layer above the ground mesh. Immediate
waypoints are rechecked against the live VXL whenever the body voxel or
topology version changes, so an old worker path cannot authorize traversal
through a newly placed block.

Fairness is explicit: fresh firing requires worker LOS plus a final normal
`CombatSystem` trace; a hidden enemy becomes a frozen last-seen record; sound
locations are distance-fuzzed before entering the worker; teammate sightings
arrive after 0.4-1.0 seconds and never update while hidden. Aim uses a bounded
second-order yaw/pitch motor with correlated noise rather than direct snaps.
Combat movement holds human-sized strafes, adds grounded cooldown-bounded
evasive jumps, and closes with a selected melee tool when ammunition is truly
exhausted. Reload always preempts equipment selection because changing tools
would cancel the authoritative reload. Under serious pressure, capable classes
request one validated block or selected prefab as cover; Miner/Zombie route
obstructions request ordinary melee mining rather than editing VXL directly.

`BotActionGateway` accepts suggestions but owns no gameplay mutation.
Shooting/reload/mining/building route through public combat methods. Oriented
grenades, rockets, drills, block cannon, and specialist launchers route through
`OrientedActionService`, which owns cadence, stock, projectile registration,
and normal native replication.
Deployables route through `DeployableActionService`, which is also used by
retail packet handlers and repeats alive/spawned, committed class, normalized
loadout, held tool, range, stock, ownership, and entity-limit checks. Prefab
placement routes through the shared `PrefabActionService`. It reserves block
stock up front, validates the normalized selected prefab and construction
safety, then commits at most the configured number of cells per tick. The
proven owner/observer packet split is preserved: owner packet 32, observer
colored packet 33, then owner packet 29 on completion.

`ConstructionSafetyService` protects authored spawn/objective/base zones and
rejects overlap with friendly active paths or team reservations. Reservations
are bounded, expire automatically, and are cleared at round reset. Friendly
paths are read on demand rather than copied every motor tick, keeping this
policy out of the 60 Hz hot path.

## Isolated Map Creator service

`run_map_creator.py` is a third composition root, alongside `run_server.py`
and `run_tutorial.py`. It discovers a genuine retail installation, validates
all nine baseplate triplets and the KV6 catalog, then process-locally registers
`UGCMode`. Normal public servers cannot select mode `ugc`, so editor-only
packets and infinite-building rules cannot leak into a match.

`UGCProject` owns the portable authoring model: terrain identity, target mode,
title/description/author, skydome, ground and water colors, preview PNG, and
the 19 recovered Game Data object types. Its validation rows are generated
from the native per-mode minimum/maximum tables. Small sidecar and preview
writes use atomic background checkpoints; the full VXL is serialized only at
explicit shutdown, never from the 60 Hz tick.

Standalone authoring defaults to `ugc-projects/`. A client-integrated launcher
passes `--publish-root <client>/hosted_ugc`; this resolves strictly to the
`maps/` child enumerated by retail `aoslib.ugc_data`. The authored `.ugc`,
`.vxl`, `.txt`, and optional `.png` therefore survive temp-config deletion and
appear in Publish Map on its next refresh. The hidden child process must stop
gracefully so `deactivate()` can atomically serialize the final VXL.
Retail deletion does not remove the optional atmosphere `.txt`; project
creation recognizes that lone file as an orphan and refreshes it, while an
existing `.ugc` or `.vxl` remains protected from accidental overwrite.

The dedicated-client flow is:

1. `InitialInfo(114)` advertises the UGC client role.
2. The source VXL is delivered with the native pre-validation
   `MapDataStart(54) -> MapDataChunk(56)* -> MapDataEnd(58)` zlib stream.
3. The ordinary CRC/full MapSync handshake establishes the authoritative
   world.
4. StateData carries signed-16-bit prefab/entity catalog counts, allowing the
   complete 373-entry native catalog.
5. `ForceTeamJoin(115)` opens the native six-tab selection flow; packet 13
   commits one shared five-item prefab/Game Data backpack.
6. Packets 30/31 edit prefabs, while 97/98 edit and replay Game Data objects.
   Packets 68, 102, and 118 publish validation, preview, and palette state.

Large KV6 decode, rotation, raw-color expansion, and ordering run on a single
editor preparation worker. The gameplay thread polls the immutable result,
validates live-world contact in `prefab_validation_batch_limit` slices, and
commits at most `prefab_cell_batch_limit` cells per tick. No worker may touch
the VXL, player, connection, or packet queues. This split kept a live
115,080-voxel placement below 1 ms packet-drain time while preserving bounded
incremental commits.

The original main-menu Host path assumes a local Steam-lobby owner and crashes
when that role is forged over direct connect. The dedicated launcher therefore
owns host authority and persistence while leaving the stock in-game Construct
and Game Data screens unchanged. Packet 101 is not replayed into an existing
GameScene; late source-map requests are rejected as crash-sensitive.

## Steam discovery isolation

`SteamMasterService` is an optional asyncio-owned supervisor, never a tick
subsystem. The retail `steam_api.dll` and `SteamGameServer011` ABI are x86, so a
small Win32 helper owns DLL loading, anonymous logon, 20 Hz callback pumping,
the dedicated query socket, and heartbeats. The native 64-bit server sends only
coalesced immutable advertisements over stdin. A blocked init is killed by a
startup watchdog; later exits use 1/2/5/30-second backoff. Discovery failure is
non-fatal unless the operator explicitly requires registration.

The service stages app ID `224540` in a private working directory and never
edits an operator's `steam_appid.txt`. It stages only operator-owned Valve
files and does not redistribute them. Current compatible x86 Steam client
libraries are preferred over the recovered client-tree `steamclient.dll`.
After logon, the Valve-assigned server SteamID/public IP feed `InitialInfo` and
the direct A2S responder. Map/mode/player changes publish at most once per
second and force one heartbeat; no Steam callback or file copy runs in
`SimulationRuntime.tick`.

## Domain modules

`protocol/packet_handler.py` owns framing validation and registry dispatch.
Domain behavior lives under `server/handlers/`: `movement`, `blocks`, `combat`,
`equipment`, `deployables`, `team`, `social`, `world`, and `diagnostics`. Each
handler validates the sender and packet, invokes one domain operation, and
requests replication. Packet decoding itself does not mutate gameplay state.

`Player` remains the compatibility facade used by modes and existing handlers.
Its internal responsibilities should be extracted behind that facade in this
order: equipment selection, input buffering, movement simulation, and
replication snapshot construction. This order isolates the class/loadout bug
without forcing a risky movement rewrite at the same time.

## Compatibility rules

- Do not change packet IDs, byte layouts, reliable/unreliable flags, or retail
  coordinate conventions without compiled-client evidence.
- Do not use `aoslib-reversed` as byte-layout ground truth. It is useful for
  intent and algorithms; IDA/live captures and `shared/packet.pyx` determine
  the actual wire contract.
- Map full sync must stream native column-span records with the required `(x,
  y)` framing. Sending raw VXL bytes or explicitly expanding implicit
  underground voxels has crashed stock clients.
- Reconciliation stamps are per recipient. A global stamp can make one client
  correct against another client's input history.
- Prefer short invariant comments in code. Investigation history, rejected
  approaches, and reproduction evidence belong in
  [HANDOFF.md](HANDOFF.md).

## Release gates

An architecture change is incomplete until the full test suite passes, the
production-config 50-player gate passes, and a clean retail client survives a
movement plus round-reset scenario. Exact commands and acceptance thresholds
are in [RUNBOOK.md](RUNBOOK.md).
