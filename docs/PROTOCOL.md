# BattleSpades Protocol Catalog

Living index of every packet in the Ace of Spades 1.x (Battle Builders) wire
protocol and its implementation status in this server. **Derived from the
source, not invented** — regenerate/re-verify against the files below when the
counts change.

Sources of truth:
- **`shared/packet.pyx`** — the full protocol surface. Every `cdef class X(Loader)`
  carries `id: int = N`. This is where the *definitions* live (~120 packets).
- **`protocol/packet_handler.py`** — every `@register_handler(N)` is a packet the
  server currently **handles on receive** (C→S).
- **`server/**` and `modes/**`** — every place a packet class is constructed and
  `.generate()`d is a packet the server currently **sends** (S→C).

## Transport

- **ENet**, `PROTOCOL_VERSION = 168`, a **single channel**, **range-coder**
  compression.
- **Wire framing:** each datagram is a **prefix byte** (`0x30` / `0x31` / `0x32`)
  followed by an **lzf-chunked** payload. The server always chunks on send; on
  receive it decodes real LZF only when the prefix is `0x31`. LZF encodes a
  back-reference as `distance - 1`; the decoder must restore the missing one or
  repeated strings are silently spliced together. Input references and expanded
  output are bounded before the packet decoder runs.
- **Every packet class is kept even if currently unused.** The unused ones are
  the *surface* for planned features (sounds, minimap, voting, deployables,
  territory control, UGC, entity management, legacy map-sync, network buffering).
  See the roadmap grouping at the end.

Statuses:
- **Handled** — has a `@register_handler` (server parses it on receive).
- **Sent** — server constructs it and calls `.generate()`.
- **Handled+Sent** — both.
- **Planned** — defined in `packet.pyx` but neither handled nor sent yet.

Direction: **C→S** (client→server, we handle), **S→C** (server→client, we send),
**both**, or **—** (neither yet).

---

## Master table (by packet id)

| ID | Packet | Direction | Status | Notes |
|----|--------|-----------|--------|-------|
| 0 | ClockSync | both | Handled+Sent | Round-trip clock/loop_count sync; client 1 tick ahead. |
| 1 | PlaceDynamite | C→S | Handled | Tool/loadout-gated Miner charge; server creates entity type 10 and owns its fuse/blast. |
| 2 | WorldUpdate | S→C | Sent | 30 Hz unreliable position/state feed. Header loop is the global snapshot clock; each human row pong is that player's consumed ClientData loop. Peerless bots have no client clock and use the authoritative server loop as a monotonic remote-row stamp; leaving bot pong at zero makes retail deduplicate every later bot position. |
| 3 | EntityUpdates | — | Planned | Moving-entity delta stream (entity mgmt). |
| 4 | ClientData | C→S | Handled | Buffered client input, applied at matching tick. The player byte uses bits 0–6 for `player_id`; bit 7 is `palette_enabled`. |
| 5 | SetHP | S→C | Sent | Sets a player's HP (spawn/heal/damage feedback). |
| 6 | ShootPacket | C→S | Handled | Client fire request. The server validates cadence/origin/orientation and resolves authoritative damage; retail has no incoming packet-6 action handler. |
| 7 | PaintBlockPacket | C→S | Handled | Tool/range/solidity validated; authoritative color mutation is journaled for late joiners. |
| 8 | ShootFeedbackPacket | S→C | Sent | Remote firearm shot only. Sent to observers (not the already-predicting human shooter); the native handler verifies the visible character's tool and calls `character.shoot(seed)`, producing gun audio/muzzle effects. Never send for spades/melee: those tools have no `shoot` method and use WorldUpdate action bit `0x01`. |
| 9 | ShootResponse | S→C | Sent | Authoritative player-hit response. Broadcast with `damage_by=shooter_id`; native clients show blood to observers but play the hit-confirm sound/crosshair only for the matching local shooter. |
| 10 | UseOrientedItem | both | Handled+Sent | Validates active normalized tool, cadence, and stock. Legacy grenade-family objects are relayed to observers; entity-backed projectiles use CreateEntity instead. Never relay GL tool 55 into the retail client's stale `GLGrenade` packet constructor. |
| 11 | SetColor | both | Handled+Sent | Palette state for block, flare, and Block Cannon tools (5/22/29/48); broadcast only to observers because the sender already applied the UI choice. |
| 12 | SetUGCEditMode | — | Planned | Toggle UGC editor mode (UGC). |
| 13 | SetClassLoadout | C→S | Handled | Normalized atomically at life boundaries. Retail may omit the trailing zero UGC-count byte; the bounded decoder accepts only that optional empty tail. Stock-LZF regression coverage proves three prefab strings survive compression in their selected order. |
| 14 | ExistingPlayer | — | Planned | Roster entry format; imported but NOT sent — roster goes out as CreatePlayer(28) on purpose (client stores ExistingPlayer.pickup verbatim as pickup_id, no 0xFF sentinel). |
| 15 | NewPlayerConnection | C→S | Handled | Client's join announcement (name/team/class), parsed in handshake. Before CreatePlayer, names are normalized to a case-insensitively unique 15-byte wire value; duplicate names can steal the native client's local-player association. |
| 16 | ChangeEntity | S→C | Sent | Server-owned turret/MG target, carrier, ammo, and state properties. |
| 17 | ChangePlayer | S→C | Sent | Existing-player state changes. Action `SET_HIGH_MINIMAP_VISIBILITY` (8) exposes the CTF intel carrier, both VIP bosses, or ZOM's final survivor through terrain; each mode owns marker cleanup and late-join replay. |
| 18 | POIFocus | — | Planned | Point-of-interest focus marker (minimap/UI). |
| 19 | DestroyEntity | S→C | Sent | Removes an entity previously announced to that GameScene. Server-only objective markers must never receive a destroy packet. For Snowball it removes the visual/effect only; it does not apply blast impulse. |
| 20 | HitEntity | S→C | Sent | Visual impact callback for server-authoritative damageable-entity hits. |
| 21 | Entity / CreateEntity | S→C | Sent | Entity wire format + create; used for crates, deployables, persistent ground intel 16, and moving projectile types including chemical 32, GL 33, sticky 34, and launched mine 37. The retail runtime table does **not** contain legacy FLAG=0 or BASE=1; sending BASE here freezes `GameScene.create_entity` with `KeyError: 1`. Both Entity and CreateEntity share id 21. |
| 22 | CreateAmbientSound | — | Planned | Register a looping ambient sound source (sounds). |
| 23 | PlaySound | S→C | Sent | One-shot positional/UI sound (server/audio.py — crate pickups, kill/death stingers). LIVE-VERIFIED. |
| 24 | PlayAmbientSound | — | Planned | Start an ambient loop (sounds). |
| 25 | StopSound | — | Planned | Stop a playing sound (sounds). |
| 26 | PlayMusic | S→C | Sent | Music track (server/audio.py — last-minute game_ending track at 61s remaining). |
| 27 | StopMusic | S→C | Sent | Stop the current music track (server/audio.py). |
| 28 | CreatePlayer | S→C | Sent | Spawns a player on clients; also carries the roster. Loadout and all three selected prefab names come from the same committed ClassSelection. Every live player name must be unique before this packet is emitted; the packet direction contains no movement-owner identity field. |
| 29 | PrefabComplete | S→C | Sent | Sent to the builder when a prefab finishes placing. |
| 30 | BuildPrefabAction | C→S | Handled | Shared `PrefabActionService` validates selected class prefab, stock, world contact, construction reservations, and queues bounded KV6 expansion. Stock is reserved up front; observers receive colored packet 33 cells, the owner packet 32 cells and packet 29 on completion. Bots use this same boundary. |
| 31 | ErasePrefabAction | C→S | Handled | Prefab carve (UGC tool): destroys the expanded cell set via the verified Damage(37) block-destroy path. Wire layout carries no rotation fields — unverified vs live client. |
| 32 | BlockBuild | both | Handled+Sent | Single-block place; handled on receive, also sent by combat. |
| 33 | BlockBuildColored | S→C | Sent | Per-block colored placement for prefabs, ordinary-build observers, terrain repair, and persistent Block Cannon impacts; recorded for MapSync catch-up when a join is active. |
| 34 | BlockOccupy | — | Planned | Mark a block occupied (building). |
| 35 | BlockLiberate | C→S | Handled | Block destroy request (spade dig). |
| 36 | ExplodeCorpse | — | Planned | Gib a corpse (combat/death FX). |
| 37 | Damage | S→C | Sent | Block/player damage broadcast. Snowball sends one reliable zero-damage type-20 event at impact before DestroyEntity(19), allowing the native explosion manager to predict impulse. |
| 38 | BlockManagerState | — | Planned | Bulk block-manager state sync (building). |
| 39 | ServerBlockAction | — | Planned | Server-authoritative block op; client-side no-op stub today. |
| 40 | BlockLine | C→S | Handled | How the 1.x client actually PLACES blocks (line of blocks). |
| 41 | MinimapBillboard | — | Planned | Place a minimap billboard/icon (minimap). |
| 42 | MinimapBillboardClear | — | Planned | Clear minimap billboards (minimap). |
| 43 | MinimapZone | S→C | Sent | CTF team-base zone and icon. Six signed-short fields are raw voxel min/max bounds for X/Y/Z; `key` is native `visible_team`, and icon 6 is `ZONE_ICON_CTF`. Sent at mode start and late join. |
| 44 | MinimapZoneClear | — | Planned | Clear minimap zones (minimap). |
| 45 | StateData | S→C | Sent | Per-spawn game/team/lighting snapshot (sent at join, prefix 0x31). VIP sends gangster classes with both `locked_class` bits. ZOM sends survivor classes on team 2, only base Zombie on team 3, and phase-aware team locks. |
| 46 | KillAction | S→C | Sent | Broadcast kill/death event. `kill_count` is the killer's current-life streak for the retail multikill HUD; it resets on death/round transition and is not the cumulative scoreboard kill total. |
| 47 | GenericVoteMessage | both | Handled+Sent | Vote overlay open/update/close (server/voting.py) + client CAST. Title/description is a repr'd localized-string tuple (client ast.literal_evals it). |
| 48 | InitiateKickMessage | C→S | Handled | Client starts a kick vote → VoteManager (server/voting.py). |
| 49 | ChatMessage | both | Handled+Sent | Chat; handled on receive, broadcast by server + commands. |
| 50 | LocalisedMessage | — | Planned | String-table localized message (UI/chat). |
| 51 | SkyboxData | S→C | Sent | Skybox visuals (sent at join, prefix 0x30). |
| 52 | MapEnded | S→C | Sent | End-of-round signal, sent with the stats screen (server/scoreboard.py). |
| 53 | ShowGameStats | S→C | Sent | Opens the full-screen end-of-round scores/credits screen (base_mode end sequence). LIVE-VERIFIED. |
| 54 | MapDataStart | — | Planned | Legacy map-data transfer start (map-sync-legacy). |
| 55 | MapSyncStart | S→C | Sent | Bare-id map sync start (prefix 0x32). |
| 56 | MapDataChunk | — | Planned | Legacy map-data chunk (map-sync-legacy). |
| 57 | MapSyncChunk | S→C | Sent | Map content chunk stream (prefix 0x31). |
| 58 | MapDataEnd | — | Planned | Legacy map-data transfer end (map-sync-legacy). |
| 59 | MapSyncEnd | S→C | Sent | Map sync stream terminator. |
| 60 | MapDataValidation | both | Handled+Sent | CRC handshake; server replies with OUR file CRC. |
| 61 | PackStart | — | Planned | Resource-pack transfer start (network buffering). |
| 62 | PackResponse | — | Planned | Client ack for pack transfer (network buffering). |
| 63 | PackChunk | — | Planned | Resource-pack chunk (network buffering). |
| 64 | PlayerLeft | S→C | Sent | Announce a player disconnect. |
| 65 | ProgressBar | — | Planned | UI progress bar (capture/build progress). |
| 66 | RankUps | — | Planned | XP/rank changes at map end (match lifecycle/progression). |
| 67 | GameStats | S→C | Sent | End-of-round scoreboard widget (server/scoreboard.py, on_mode_end). |
| 68 | UGCObjectives | — | Planned | UGC-defined objectives (UGC). |
| 69 | Restock | S→C | Sent | Refill ammo/health/blocks at a restock zone. |
| 70 | PickPickup | S→C | Sent | Authoritative objective pickup; initializes carried tool and burden state. CTF removes the type-16 ground entity and enables the carrier's high-visibility minimap marker. |
| 71 | DropPickup | both | Handled+Sent | Client drop request validated against sender/current pickup, then relayed with authoritative identity, type, position, and capped throw velocity. DropPickup clears the carried tool but does not persist ground intel, so CTF follows it with a type-16 CreateEntity at the settled dry-ground position. |
| 72 | ForceShowScores | — | Planned | Force the scoreboard open (match lifecycle). |
| 73 | ShowTextMessage | — | Planned | On-screen text message (UI). |
| 74 | FogColor | S→C | Sent | Set fog color (sent via admin/server command). |
| 75 | TimeScale | — | Planned | Game time-scale multiplier (mode rules). |
| 76 | WeaponReload | both | Handled+Sent | Reload request handled; also sent as reload confirmation. |
| 77 | ChangeTeam | C→S | Handled | Client team switch request. |
| 78 | ChangeClass | C→S | Handled | Client class switch request. |
| 79 | LockTeam | — | Planned | Lock a team from joining (mode rules). |
| 80 | TeamLockClass | — | Planned | Restrict classes per team (mode rules). |
| 81 | TeamLockScore | — | Planned | Lock team score (mode rules). |
| 82 | TeamInfiniteBlocks | — | Planned | Grant a team infinite blocks (mode rules). |
| 83 | TeamMapVisibility | S→C | Sent | Team radar visibility toggled while authoritative radar stations exist. |
| 84 | DisplayCountdown | S→C | Sent | HUD round-timer countdown (server/scoreboard.py, seconds remaining). LIVE-VERIFIED. |
| 85 | SetScore | S→C | Sent | Lightweight mid-game team/player score update (HUD). |
| 86 | UseCommand | C→S | Handled | Mount/dismount the nearest unoccupied machine-gun entity. |
| 87 | PlaceMG | C→S | Handled | Validated type-7 mounted-machine-gun placement; yaw/team/health and join persistence are server-owned. |
| 88 | PlaceRocketTurret | C→S | Handled | Validated Engineer/Rocketeer turret placement and server-owned targeting/rockets. |
| 89 | PlaceLandmine | C→S | Handled | Validated placement, four-second arm, buried proximity detection, and blast. |
| 90 | PlaceMedPack | C→S | Handled | Validated type-30 placement, three 25-HP team uses, health/destruction; two-client retail rendering verified. |
| 91 | PlaceRadarStation | C→S | Handled | Validated type-36 placement, 250-second life, team minimap reveal; two-client retail rendering verified. |
| 92 | PlaceC4 | C→S | Handled | Validated oriented type-38 placement with owner stock tracking; two-client retail rendering verified. |
| 93 | DetonateC4 | C→S | Handled | Detonates only the sender's live charges. |
| 94 | BlockSuckerPacket | both | Handled+Sent | Sanitized remote state relay plus authoritative timed voxel pull/grant. |
| 95 | DisguisePacket | C→S | Handled | Loadout/tool-gated disguise state, replicated through WorldUpdate bit 0x02. |
| 96 | DisableEntity | — | Planned | Disable an entity without destroying it (entity mgmt). |
| 97 | PlaceUGC | — | Planned | Place a UGC object (UGC). |
| 98 | InitialUGCBatch | — | Planned | Initial batch of UGC objects at join (UGC). |
| 99 | ReqestUGCEntities | — | Planned | Client requests UGC entities (UGC). |
| 100 | UGCMessage | — | Planned | UGC channel message (UGC). |
| 101 | UGCMapLoadingFromHost | — | Planned | UGC map loading from host (UGC). |
| 102 | UGCMapInfo | — | Planned | UGC map metadata (UGC). |
| 103 | VoiceData | — | Planned | Voice-chat audio frames (voice). |
| 104 | PlaceFlareBlock | C→S | Handled | Flare tool 22 only; raw voxel-short coordinates, ten-block cost, contact/range validation, and coloured entity type 13 with late-join persistence. A successful `FLARE BLOCK` log is this packet, not ordinary BlockLine(40). |
| 105 | SteamSessionTicket | C→S | — | Steam auth ticket; received in handshake (not via register_handler). |
| 106 | TerritoryBaseState | — | Planned | Territory/base capture state (territory control). |
| 107 | DebugDraw | — | Planned | Debug draw primitives (dev tooling). |
| 108 | LockToZone | — | Planned | Lock player to a zone (mode rules). |
| 109 | HelpMessage | — | Planned | Help/tutorial text (UI). |
| 110 | ClientInMenu | C→S | Handled | Client reports it's in a menu (handshake/idle gating). |
| 111 | Password | — | Planned | Password packet (auth). |
| 112 | PasswordNeeded | — | Planned | Server requests a password (auth). |
| 113 | PasswordProvided | — | Planned | Client submits a password (auth). |
| 114 | InitialInfo | S→C | Sent | First join packet: map filename, checksum, movement multipliers, and a null-terminated `texture_skin` string. VIP sends `mafia`; the empty string selects the normal skin. |
| 115 | ForceTeamJoin | — | Planned | Force a player onto a team (mode rules/admin). |
| 116 | PositionData | C→S | Handled | Handler registered, but the 1.x client does NOT send it (no-op path). |
| 117 | TeamProgress | — | Planned | Team objective progress bar (territory/mode rules). |
| 118 | SetGroundColors | — | Planned | Set per-team ground color palette (visuals). |

### Snowball Damage/Destroy ordering

IDA shows `GameScene.process_packet_damage` at `0x1018C270` calling the native
explosion-damage manager. `DestroyEntity(19)` only removes the Snowball visual.
The server-to-client transition is therefore:

1. reliable `Damage(37)` with `player_id=thrower`, `type=20`, `damage=0`,
   `face=0`, `chunk_check=0`, `seed=0`, `causer_id=projectile entity id`, and
   the exact impact position;
2. `DestroyEntity(19)` for the same entity.

The Damage packet must precede destruction because the client resolves the
causer entity; id 0 is valid. This prediction event is not a map mutation and
must not be replayed from the late-join journal. A disconnect cancels all
projectiles owned by the departing id before that id is reusable. Native
packet processing applies Damage before the GameScene update core
(`0x10149CF0`), so authoritative projectile collision also runs before player
physics.

The authoritative impulse is deliberately delayed to the third **accepted
ClientData frame after impact**, using a per-player dense receive sequence. The
server queues origin/radius/falloff parameters, then recomputes direction and
crouch scaling from authoritative state immediately before that frame's physics
step. Do not use `server.loop_count`, a fixed `L+2`, the sparse ClientData loop
label, or a frozen impact-time vector: live A/Bs respectively remained
nondeterministic, produced 3 ADJUST/0.301891 maximum error, or produced
2 ADJUST/0.384826 maximum error. The accepted design passed 719 samples with
zero ADJUST/SNAP/rollback and 0.000076 maximum matched error in
`logs/combined-replication/snowball-sequence3-final-live/20260714T014849/scenario-run-1/movement-stress-20260713T224938.344225Z.json`.

Disconnect is also a protocol generation boundary. Queued gameplay packets are
purged for the exact departing Connection, and delivery revalidates both the
peer-to-Connection and numeric-id-to-Player object identities. Only after
pending mutations, projectiles/turrets/fire, combat cadence, votes, replication
state, and owner-bound deployables have been retired may the numeric id be
reused. Persistent construction/objectives remain. An owned MG is removed; a
foreign MG mounted by the departing player is merely unmounted; radar teardown
uses the normal per-team count/visibility transition.

### Normal block versus flare block

Normal block tool 5 sends `BlockLine(40)` and costs one block. Flare tool 22
sends `PlaceFlareBlock(104)` and costs ten. They share a visual block model, so
the selected tool must be established from the packet/tool state, not its hand
model. Normalized default loadouts preserve the stock carousel with block first
and flare last.

### Non-standard / server-internal packets

These are BattleSpades-specific debug packets, not part of the original 1.x
surface (no class in `packet.pyx`). Handlers exist for dev parity tooling:

| ID | Handler | Direction | Status | Notes |
|----|---------|-----------|--------|-------|
| 241 | DebugParityToggle | C→S | Handled | Dev: toggle parity capture. |
| 242 | DebugClientSample | C→S | Handled | Dev: client position sample for parity. |
| 243 | DebugClientEvent | C→S | Handled | Dev: client event marker for parity. |

---

## Summary

- **119** packets defined in `shared/packet.pyx` (ids 0–118; id 21 is shared by
  `Entity` and `CreateEntity`, plus the `id: -1` base `Loader`/`AddServer` which
  are not wire packets).
- **33** standard ids are registered in `protocol.packet_handler`, plus the
  three development ids 241/242/243. NewPlayerConnection(15),
  MapDataValidation(60), and SteamSessionTicket(105) also have connection-layer
  paths outside the decorator registry.
- The current table records **43** distinct server-sent ids and **51** ids still
  planned. These counts should be regenerated when a packet changes status;
  do not copy the older counts retained in historical handoffs.
- **11** ids currently operate in both directions: 0, 6, 10, 11, 32, 47, 49,
  60, 71, 76, and 94.

Quick counts: **119 defined · 33 registered standard handlers · 41 sent · 53
planned.**

---

## Planned packets grouped by feature area

What lighting up each feature area unlocks (all ids below are already defined in
`packet.pyx`, just not yet wired):

### Sounds (planned)
CreateAmbientSound (22), PlaySound (23), PlayAmbientSound (24), StopSound (25),
PlayMusic (26), StopMusic (27).

### Minimap / POI

CTF base zones use MinimapZone (43), and radar uses TeamMapVisibility (83).
POIFocus (18), standalone MinimapBillboard (41), MinimapBillboardClear (42),
and MinimapZoneClear (44) remain planned.

### Voting / kick (planned)
GenericVoteMessage (47), InitiateKickMessage (48).

### Deployables / Place*
PlaceDynamite (1), UseCommand (86), PlaceMG (87), PlaceRocketTurret (88),
PlaceLandmine (89), PlaceMedPack (90), PlaceRadarStation (91), PlaceC4 (92),
and DetonateC4 (93) are handled. Their packet layouts are stable. Two-client
retail validation now covers the native Landmine (type 9), MedPack (type 30),
RadarStation (type 36), and C4 (type 38) render lifecycles; exact damage feel
and the remaining deployables still need live calibration.

### Gangster VIP

VIP uses existing retail wire state rather than introducing a custom packet:

- `InitialInfo(114).texture_skin` is the null-terminated string `mafia`.
- `StateData(45)` advertises mode id 7, Gangster 1-4, and both native
  `locked_class` bits.
- `CreatePlayer(28)` carries ordinary gangster or team-specific boss class.
- `ChangePlayer(17)` action 8 toggles the boss crown/through-wall marker.

The server owns selection, respawn lockout, disconnect-as-death, sub-round
score, intermission, and late-join marker replay. Do not use `TeamLockClass(80)`
for this path; the stock SelectTeam flow reads the class lock from StateData.

### Zombie Infection

Zombie uses the retail mode id 2 and existing role packets; it introduces no
custom wire format:

- Before outbreak, `StateData(45)` exposes the normal survivor classes plus
  Rocketeer on team 2 and locks team 3.
- At outbreak, `KillAction(19)` provides the native-safe model transition and
  the following `CreatePlayer(28)` respawns Patient Zero as class 4 on team 3.
- Team 3 is class-locked to base Zombie. Fast/Jump Zombie remain disabled
  because this client has no stable ordinary picker icons for those classes.
- `ChangePlayer(17)` action 8 marks the sole remaining living survivor and is
  replayed to late joiners.
- A client joining after outbreak is normalized to team 3/class 4 regardless
  of its requested team, class, or loadout. Zombie respawn delay is zero.

The 600-second survival clock starts when Patient Zero is selected. Time spent
waiting for enough players is not round time.

### Territory control / mode rules (planned)
TimeScale (75), LockTeam (79), TeamLockClass (80), TeamLockScore (81),
TeamInfiniteBlocks (82), LockToZone (108), ForceTeamJoin (115),
TerritoryBaseState (106), TeamProgress (117), ProgressBar (65).

### UGC (planned)
SetUGCEditMode (12), UGCObjectives (68), PlaceUGC (97), InitialUGCBatch (98),
ReqestUGCEntities (99), UGCMessage (100), UGCMapLoadingFromHost (101),
UGCMapInfo (102).

### Entity management
ChangeEntity (16) is sent for turret/MG target, ammo, and carrier properties;
HitEntity (20) is sent as a visual impact callback after authoritative server
ray selection. EntityUpdates (3) and DisableEntity (96) remain unused pending
an evidence-backed gameplay path.

### Building / blocks
PaintBlockPacket (7), BlockBuildColored (33), PrefabComplete (29),
BuildPrefabAction (30), ErasePrefabAction (31), and BlockSuckerPacket (94) have
active paths. Native block mutation packets are retained in a bounded late-join
journal between MapSync and first ClientData; if that journal cannot provide a
contiguous replay, the join is rejected so the client never enters with partial
terrain state. BlockOccupy (34), BlockManagerState (38), and ServerBlockAction
(39) remain planned.

Settled clients also receive a delayed, bounded canonical repair of recently
changed cells. This is not a new packet contract: solid cells use explicit-RGB
`BlockBuildColored(33)` and air uses exact-cell `Damage(37)` with type 6 and
`chunk_check=0`. The replay reads VXL state at send time and is deliberately
excluded from the late-join mutation journal.

Melee terrain Damage is type-dependent. Type 2 expands to the centered
three-cell z column; type 3 expands to a centered, axis-aligned 3x3x3 Super
Spade cube; pickaxe-family types are exact-cell. The server mirrors that
footprint and sends one area packet—never one expanding packet per removed
cell.

### Pickups
PickPickup (70) is server-to-client only. DropPickup (71) is handled and
relayed; objective pickup state is also carried by WorldUpdate and replayed to
late joiners.

### Classic CTF scene contract

Classic CTF is not sent as a separate retail scene. `StateData.mode_type` and
`InitialInfo.mode_key` remain `MODE_CTF` (8), while `InitialInfo.classic=1`
selects the Deuce/classic behavior inside `GameScene`. The same snapshot sends
`enable_minimap=0`, `allow_shooting_holding_intel=1`, one Classic Soldier class
for both teams, and disables tools 37/38 (Classic Shotgun/SMG). Sending enum 11
instead is unsupported by this retail scene table. The shipped Classic playlist
also disables CTF intel auto-return; that is a server rule and emits no new
packet. Ground intel remains entity type 16, and carried intel continues to use
the ordinary pickup/WorldUpdate representation.

### Combat / death FX
ShootResponse (9) is sent only after authoritative player health decreases.
Its `damage_by` field is the shooter's player id: the native handler shows
blood to every recipient, while only the client whose local id matches that
field plays the hit-confirm sound and changes its crosshair. This also covers
server-owned bot victims because they pass through the same CombatSystem.
Every accepted ShootPacket (6) produces ShootFeedbackPacket (8) for observers,
excluding the firing human client because it already predicted its action.
`process_packet_shoot_feedback` resolves the shooter, requires the replicated
`tool_id` to match the visible character, and calls `character.shoot(seed)`;
this is the native remote firearm gunshot/muzzle path. Bots have no peer to
exclude, so every retail observer receives their firearm feedback. Packet 6
must not be broadcast back to clients (retail logs it as unhandled).

Spade, Super Spade, Machete, and the other digging tools are deliberately
excluded from packet 8: their classes implement `use_primary()` but no
`shoot()`, and a retail replay proved packet 8 crashes in
`Character.shoot`. Their remote animation/sound comes from WorldUpdate action
bit `0x01`; peerless bot pulses are held for three 60 Hz loops so the 30 Hz
replication stream cannot miss the state. `Damage(37)` remains the canonical
terrain hit/removal.
ExplodeCorpse (36) remains planned. DisguisePacket (95) is handled and
replicated through WorldUpdate.

Drill contact uses one reliable Damage (37) with type 10, damage 20,
`chunk_check=1`, and the still-live Drill entity id as `causer_id`. The retail
BlockManager expands this compact contact into a measured 81-cell radius-2
bore. The authoritative server removes that same footprint. Late-join replay
cannot depend on an expired projectile id, so the mutation journal stores 81
exact type-6 removals instead; a live contact whose entity has already vanished
also falls back to exact type-6 packets to avoid the native
`Drill entity ID not valid` abort.

### Match lifecycle / stats / progression
MapEnded (52), ShowGameStats (53), GameStats (67), and DisplayCountdown (84)
are sent by the round lifecycle. RankUps (66) and ForceShowScores (72) remain
planned.

### Legacy map-data sync (planned)
MapDataStart (54), MapDataChunk (56), MapDataEnd (58).
(The active path is the MapSync* family: 55/57/59 + validation 60.)

### Network buffering / resource packs (planned)
PackStart (61), PackResponse (62), PackChunk (63).

### Player / roster management
ExistingPlayer (14) is defined but deliberately unused: the retail client is
initialized with CreatePlayer (28), because ExistingPlayer stores its pickup
byte verbatim and has no safe `0xFF` sentinel.

The roster sent before MapSync is only a snapshot. Gameplay broadcasts remain
gated until the receiver's first ClientData (4), so two clients can both finish
their handshakes before either NewPlayerConnection is accepted. At first
ClientData, the server therefore runs a per-connection, per-life catch-up:

- missing alive lives receive CreatePlayer (28) once;
- a known life that died while the receiver was loading receives its retained
  KillAction;
- stale known IDs receive PlayerLeft before an ID can be reused;
- the receiver then receives one reliable remote-only WorldUpdate (2), which
  initializes the current tool, action flags, pickup, and position.

The reveal WorldUpdate must exclude the receiver's own row. Including it would
turn a roster repair into an owner reconciliation event and can cause a visible
join-time rollback. Ordinary 30 Hz WorldUpdates remain unreliable. ChangePlayer
(17) remains planned.

### Auth (planned)
Password (111), PasswordNeeded (112), PasswordProvided (113).

### UI / messaging (planned)
LocalisedMessage (50), ShowTextMessage (73), HelpMessage (109).

### Voice (planned)
VoiceData (103).

### Visuals (planned)
SetGroundColors (118).

### Dev tooling (planned)
DebugDraw (107).
