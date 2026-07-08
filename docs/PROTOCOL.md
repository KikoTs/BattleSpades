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
  receive it un-chunks only when the prefix is `0x31`.
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
| 1 | PlaceDynamite | — | Planned | Deployable (dynamite placement). |
| 2 | WorldUpdate | S→C | Sent | 60Hz unreliable per-player position/state feed. |
| 3 | EntityUpdates | — | Planned | Moving-entity delta stream (entity mgmt). |
| 4 | ClientData | C→S | Handled | Buffered client input, applied at matching tick. |
| 5 | SetHP | S→C | Sent | Sets a player's HP (spawn/heal/damage feedback). |
| 6 | ShootPacket | both | Handled+Sent | Client fires; server validates & rebroadcasts (sanitized). |
| 7 | PaintBlockPacket | — | Planned | Recolor a placed block (building). |
| 8 | ShootFeedbackPacket | — | Planned | Per-shot feedback to shooter (combat). |
| 9 | ShootResponse | — | Planned | Server authoritative shot result (combat). |
| 10 | UseOrientedItem | both | Handled+Sent | Thrown grenades / RPG rockets; rebroadcast to others. |
| 11 | SetColor | both | Handled+Sent | Player build color; handled then broadcast. |
| 12 | SetUGCEditMode | — | Planned | Toggle UGC editor mode (UGC). |
| 13 | SetClassLoadout | C→S | Handled | Client's chosen class loadout (parsed at join). |
| 14 | ExistingPlayer | — | Planned | Roster entry format; imported but NOT sent — roster goes out as CreatePlayer(28) on purpose (client stores ExistingPlayer.pickup verbatim as pickup_id, no 0xFF sentinel). |
| 15 | NewPlayerConnection | C→S | Handled | Client's join announcement (name/team/class), parsed in handshake. |
| 16 | ChangeEntity | — | Planned | Mutate an existing entity's fields (entity mgmt). |
| 17 | ChangePlayer | — | Planned | Update an existing player's name/team/class. |
| 18 | POIFocus | — | Planned | Point-of-interest focus marker (minimap/UI). |
| 19 | DestroyEntity | S→C | Sent | Removes a map entity (crate/intel) from clients. |
| 20 | HitEntity | — | Planned | Report a hit on an entity (entity mgmt/combat). |
| 21 | Entity / CreateEntity | S→C | Sent | Entity wire format + create; CreateEntity sent for crates. Both share id 21. |
| 22 | CreateAmbientSound | — | Planned | Register a looping ambient sound source (sounds). |
| 23 | PlaySound | S→C | Sent | One-shot positional/UI sound (server/audio.py — crate pickups, kill/death stingers). LIVE-VERIFIED. |
| 24 | PlayAmbientSound | — | Planned | Start an ambient loop (sounds). |
| 25 | StopSound | — | Planned | Stop a playing sound (sounds). |
| 26 | PlayMusic | S→C | Sent | Music track (server/audio.py — last-minute game_ending track at 61s remaining). |
| 27 | StopMusic | S→C | Sent | Stop the current music track (server/audio.py). |
| 28 | CreatePlayer | S→C | Sent | Spawns a player on clients; also carries the roster. |
| 29 | PrefabComplete | S→C | Sent | Sent to the builder when a prefab finishes placing. |
| 30 | BuildPrefabAction | C→S | Handled | Prefab placement: server expands the KV6 model (server/prefabs.py), validates class list / block budget / world contact, places + broadcasts BlockBuildColored per block. |
| 31 | ErasePrefabAction | C→S | Handled | Prefab carve (UGC tool): destroys the expanded cell set via the verified Damage(37) block-destroy path. Wire layout carries no rotation fields — unverified vs live client. |
| 32 | BlockBuild | both | Handled+Sent | Single-block place; handled on receive, also sent by combat. |
| 33 | BlockBuildColored | S→C | Sent | Per-block colored placement — the broadcast stream for placed prefabs. |
| 34 | BlockOccupy | — | Planned | Mark a block occupied (building). |
| 35 | BlockLiberate | C→S | Handled | Block destroy request (spade dig). |
| 36 | ExplodeCorpse | — | Planned | Gib a corpse (combat/death FX). |
| 37 | Damage | S→C | Sent | Block/player damage broadcast (removal path uses this). |
| 38 | BlockManagerState | — | Planned | Bulk block-manager state sync (building). |
| 39 | ServerBlockAction | — | Planned | Server-authoritative block op; client-side no-op stub today. |
| 40 | BlockLine | C→S | Handled | How the 1.x client actually PLACES blocks (line of blocks). |
| 41 | MinimapBillboard | — | Planned | Place a minimap billboard/icon (minimap). |
| 42 | MinimapBillboardClear | — | Planned | Clear minimap billboards (minimap). |
| 43 | MinimapZone | — | Planned | Draw a minimap zone (minimap). |
| 44 | MinimapZoneClear | — | Planned | Clear minimap zones (minimap). |
| 45 | StateData | S→C | Sent | Per-spawn game/team/lighting snapshot (sent at join, prefix 0x31). |
| 46 | KillAction | S→C | Sent | Broadcast kill/death event. |
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
| 70 | PickPickup | — | Planned | Pick up a dropped item (pickups). |
| 71 | DropPickup | — | Planned | Drop an item (pickups). |
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
| 83 | TeamMapVisibility | — | Planned | Per-team map visibility (minimap/mode rules). |
| 84 | DisplayCountdown | S→C | Sent | HUD round-timer countdown (server/scoreboard.py, seconds remaining). LIVE-VERIFIED. |
| 85 | SetScore | S→C | Sent | Lightweight mid-game team/player score update (HUD). |
| 86 | UseCommand | — | Planned | Generic "use" action (deployables/interaction). |
| 87 | PlaceMG | — | Planned | Deploy a machine-gun turret (deployables). |
| 88 | PlaceRocketTurret | — | Planned | Deploy a rocket turret (deployables). |
| 89 | PlaceLandmine | — | Planned | Deploy a landmine (deployables). |
| 90 | PlaceMedPack | — | Planned | Deploy a med pack (deployables). |
| 91 | PlaceRadarStation | — | Planned | Deploy a radar station (deployables). |
| 92 | PlaceC4 | — | Planned | Place C4 (deployables). |
| 93 | DetonateC4 | — | Planned | Detonate placed C4 (deployables). |
| 94 | BlockSuckerPacket | — | Planned | Block-sucker tool action (building/tools). |
| 95 | DisguisePacket | — | Planned | Spy/disguise toggle (class ability). |
| 96 | DisableEntity | — | Planned | Disable an entity without destroying it (entity mgmt). |
| 97 | PlaceUGC | — | Planned | Place a UGC object (UGC). |
| 98 | InitialUGCBatch | — | Planned | Initial batch of UGC objects at join (UGC). |
| 99 | ReqestUGCEntities | — | Planned | Client requests UGC entities (UGC). |
| 100 | UGCMessage | — | Planned | UGC channel message (UGC). |
| 101 | UGCMapLoadingFromHost | — | Planned | UGC map loading from host (UGC). |
| 102 | UGCMapInfo | — | Planned | UGC map metadata (UGC). |
| 103 | VoiceData | — | Planned | Voice-chat audio frames (voice). |
| 104 | PlaceFlareBlock | — | Planned | Place a flare/light block (deployables/building). |
| 105 | SteamSessionTicket | C→S | — | Steam auth ticket; received in handshake (not via register_handler). |
| 106 | TerritoryBaseState | — | Planned | Territory/base capture state (territory control). |
| 107 | DebugDraw | — | Planned | Debug draw primitives (dev tooling). |
| 108 | LockToZone | — | Planned | Lock player to a zone (mode rules). |
| 109 | HelpMessage | — | Planned | Help/tutorial text (UI). |
| 110 | ClientInMenu | C→S | Handled | Client reports it's in a menu (handshake/idle gating). |
| 111 | Password | — | Planned | Password packet (auth). |
| 112 | PasswordNeeded | — | Planned | Server requests a password (auth). |
| 113 | PasswordProvided | — | Planned | Client submits a password (auth). |
| 114 | InitialInfo | S→C | Sent | First join packet: map filename, checksum, movement multipliers. |
| 115 | ForceTeamJoin | — | Planned | Force a player onto a team (mode rules/admin). |
| 116 | PositionData | C→S | Handled | Handler registered, but the 1.x client does NOT send it (no-op path). |
| 117 | TeamProgress | — | Planned | Team objective progress bar (territory/mode rules). |
| 118 | SetGroundColors | — | Planned | Set per-team ground color palette (visuals). |

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
- **14** standard ids handled via `@register_handler`: 0, 4, 6, 10, 11, 32, 35,
  40, 49, 76, 77, 78, 110, 116 (**17** including the 3 dev packets 241/242/243).
  Additionally SteamSessionTicket(105), NewPlayerConnection(15) and
  SetClassLoadout(13) are received/parsed through the handshake path (not a
  registered handler), so they are not counted as "Planned".
- **25** distinct ids the server currently constructs and sends: 0, 2, 5, 6, 10,
  11, 19, 21, 28, 32, 37, 45, 46, 49, 51, 55, 57, 59, 60, 64, 69, 74, 76, 85, 114.
- **7** ids are **Handled+Sent** (both directions): 0, 6, 10, 11, 32, 49, 76.
- The remaining **84** wire ids are **Planned** — defined in `packet.pyx` but
  neither handled nor sent yet.

Quick counts: **119 defined · 17 handled · 25 sent · 84 planned.**

---

## Planned packets grouped by feature area

What lighting up each feature area unlocks (all ids below are already defined in
`packet.pyx`, just not yet wired):

### Sounds (planned)
CreateAmbientSound (22), PlaySound (23), PlayAmbientSound (24), StopSound (25),
PlayMusic (26), StopMusic (27).

### Minimap / POI (planned)
POIFocus (18), MinimapBillboard (41), MinimapBillboardClear (42),
MinimapZone (43), MinimapZoneClear (44), TeamMapVisibility (83).

### Voting / kick (planned)
GenericVoteMessage (47), InitiateKickMessage (48).

### Deployables / Place* (planned)
PlaceDynamite (1), UseCommand (86), PlaceMG (87), PlaceRocketTurret (88),
PlaceLandmine (89), PlaceMedPack (90), PlaceRadarStation (91), PlaceC4 (92),
DetonateC4 (93), PlaceFlareBlock (104).

### Territory control / mode rules (planned)
TimeScale (75), LockTeam (79), TeamLockClass (80), TeamLockScore (81),
TeamInfiniteBlocks (82), LockToZone (108), ForceTeamJoin (115),
TerritoryBaseState (106), TeamProgress (117), ProgressBar (65).

### UGC (planned)
SetUGCEditMode (12), UGCObjectives (68), PlaceUGC (97), InitialUGCBatch (98),
ReqestUGCEntities (99), UGCMessage (100), UGCMapLoadingFromHost (101),
UGCMapInfo (102).

### Entity management (planned)
EntityUpdates (3), ChangeEntity (16), HitEntity (20), DisableEntity (96).

### Building / blocks (planned)
PaintBlockPacket (7), BlockBuildColored (33), BlockOccupy (34),
BlockManagerState (38), ServerBlockAction (39), PrefabComplete (29),
BuildPrefabAction (30), ErasePrefabAction (31), BlockSuckerPacket (94).

### Pickups (planned)
PickPickup (70), DropPickup (71).

### Combat / death FX (planned)
ShootFeedbackPacket (8), ShootResponse (9), ExplodeCorpse (36),
DisguisePacket (95).

### Match lifecycle / stats / progression (planned)
MapEnded (52), ShowGameStats (53), GameStats (67), RankUps (66),
ForceShowScores (72), DisplayCountdown (84).

### Legacy map-data sync (planned)
MapDataStart (54), MapDataChunk (56), MapDataEnd (58).
(The active path is the MapSync* family: 55/57/59 + validation 60.)

### Network buffering / resource packs (planned)
PackStart (61), PackResponse (62), PackChunk (63).

### Player / roster management (planned)
ExistingPlayer (14) — defined but deliberately unused (roster ships as
CreatePlayer(28)), ChangePlayer (17).

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
