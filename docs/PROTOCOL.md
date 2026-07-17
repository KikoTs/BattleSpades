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
| 12 | SetUGCEditMode | C→S | Handled | Isolated editor host changes the target validation mode; ordinary servers reject it. |
| 13 | SetClassLoadout | both | Handled+Sent | Normalized atomically at life boundaries and acknowledged with instant=1 when the class is unchanged. Retail may omit the trailing zero UGC-count byte; the bounded decoder accepts only that optional empty tail. UGC preserves one shared five-item prefab/Game Data backpack. |
| 14 | ExistingPlayer | — | Planned | Roster entry format; imported but NOT sent — roster goes out as CreatePlayer(28) on purpose (client stores ExistingPlayer.pickup verbatim as pickup_id, no 0xFF sentinel). |
| 15 | NewPlayerConnection | C→S | Handled | Client's join announcement (name/team/class), parsed in handshake. Before CreatePlayer, names are normalized to a case-insensitively unique 15-byte wire value; duplicate names can steal the native client's local-player association. |
| 16 | ChangeEntity | S→C | Sent | Server-owned turret/MG target, carrier, ammo, state, and map-pickup position. Action 1 (`SET_POSITION`) reliably settles a pickup after its supporting structure breaks. |
| 17 | ChangePlayer | S→C | Sent | Existing-player state changes. Action `SET_HIGH_MINIMAP_VISIBILITY` (8) exposes the CTF intel carrier, both VIP bosses, or ZOM's final survivor through terrain; each mode owns marker cleanup and late-join replay. |
| 18 | POIFocus | — | Planned | Point-of-interest focus marker (minimap/UI). |
| 19 | DestroyEntity | S→C | Sent | Removes an entity previously announced to that GameScene. Server-only objective markers must never receive a destroy packet. For Snowball it removes the visual/effect only; it does not apply blast impulse. |
| 20 | HitEntity | S→C | Sent | Visual impact callback for server-authoritative damageable-entity hits. |
| 21 | Entity / CreateEntity | S→C | Sent | Entity wire format + create; used for map crates, type-13 static/player flare lights, deployables, persistent ground intel 16, and moving projectile types including chemical 32, GL 33, sticky 34, and launched mine 37. The retail runtime table does **not** contain legacy FLAG=0 or BASE=1; sending BASE here freezes `GameScene.create_entity` with `KeyError: 1`. Both Entity and CreateEntity share id 21. |
| 22 | CreateAmbientSound | S→C | Sent | Registers a map-owned ambient controller. Empty points are a global bed; authored points define local emitters. Must precede packet 24 with the same loop ID. LIVE-VERIFIED. |
| 23 | PlaySound | S→C | Sent | One-shot positional/UI sound: pickups, round/kill cues, and observer-only block-tool impacts. The actor predicts its own mining sound and is excluded. LIVE-VERIFIED. |
| 24 | PlayAmbientSound | S→C | Sent | Allocates the streaming ambient `GameSound` registered by packet 22. Global beds are unpositioned; local loops bootstrap at the listener and are moved by the native point controller. LIVE-VERIFIED. |
| 25 | StopSound | — | Planned | Stop a playing sound (sounds). |
| 26 | PlayMusic | S→C | Sent | Music track (server/audio.py — last-minute game_ending track at 61s remaining). |
| 27 | StopMusic | S→C | Sent | Stop the current music track (server/audio.py). |
| 28 | CreatePlayer | S→C | Sent | Spawns a player on clients; also carries the roster. Loadout and all three selected prefab names come from the same committed ClassSelection. Every live player name must be unique before this packet is emitted; the packet direction contains no movement-owner identity field. |
| 29 | PrefabComplete | S→C | Sent | Sent to the builder when a prefab finishes placing. |
| 30 | BuildPrefabAction | both | Handled+Sent | Shared `PrefabActionService` validates selection, stock, contact, and reservations. In UGC it snapshots/echoes the native action, prepares retail KV6 rotation and raw colors off-thread, then validates and commits through bounded main-thread batches. Competitive owners still receive packet 32 cells and packet 29; observers receive packet 33. |
| 31 | ErasePrefabAction | both | Handled+Sent | Native UGC carve with verified yaw/pitch/roll fields. The action is echoed only after bounded live-world target validation, then the expanded set is removed through the authoritative block-destroy path. |
| 32 | BlockBuild | both | Handled+Sent | Single-block place; handled on receive, also sent by combat. |
| 33 | BlockBuildColored | S→C | Sent | Per-block colored placement for prefabs, ordinary-build observers, terrain repair, and persistent Block Cannon impacts; recorded for MapSync catch-up when a join is active. |
| 34 | BlockOccupy | — | Planned | Mark a block occupied (building). |
| 35 | BlockLiberate | C→S | Handled | Block destroy request (spade dig). |
| 36 | ExplodeCorpse | S→C | Sent | Three bytes: player id and effect flag. Classic CTF uses KillAction to create the client-owned `ClassicCorpse` Character, then packet 36 with flag 1 for an authoritative corpse hit or flag 0 for silent cleanup before respawn/late-join repair. It is not entity type 12. |
| 37 | Damage | S→C | Sent | Block/player damage broadcast. Snowball sends one reliable zero-damage type-20 event at impact before DestroyEntity(19), allowing the native explosion manager to predict impulse. |
| 38 | BlockManagerState | — | Planned | Three BlockManager dictionaries: damaged, occupied, and user-owned blocks. It is not a VXL topology/removed-voxel resync packet. Non-empty entry encoding remains unverified. |
| 39 | ServerBlockAction | — | Planned | Server-authoritative block op; client-side no-op stub today. |
| 40 | BlockLine | C→S | Handled | How the 1.x client actually PLACES blocks (line of blocks). |
| 41 | MinimapBillboard | — | Planned | Place a minimap billboard/icon (minimap). |
| 42 | MinimapBillboardClear | — | Planned | Clear minimap billboards (minimap). |
| 43 | MinimapZone | S→C | Sent | CTF team-base zone and icon. Six signed-short fields are raw voxel min/max bounds for X/Y/Z; `key` is native `visible_team`, and icon 6 is `ZONE_ICON_CTF`. Sent at mode start and late join. |
| 44 | MinimapZoneClear | — | Planned | Clear minimap zones (minimap). |
| 45 | StateData | S→C | Sent | Per-spawn game/team/lighting snapshot (sent at join, prefix 0x31). Prefab and entity catalog lengths are signed little-endian 16-bit counts, not padded bytes; this carries all 373 native UGC items. VIP sends gangster locks and ZOM sends phase-aware team/class locks. |
| 46 | KillAction | S→C | Sent | Broadcast kill/death event. `kill_count` is the killer's current-life streak for the retail multikill HUD; it resets on death/round transition and is not the cumulative scoreboard kill total. |
| 47 | GenericVoteMessage | both | Handled+Sent | Kick and next-map vote overlay open/update/close plus client CAST. The server sends exact candidate records; the retail client binds the first three to F1/F2/F3. Title/description is exactly `repr((string_id, arguments_tuple))`: native `GenericVotingHUD.decode_string` unconditionally indexes both elements and crashes on the historical one-item tuple. |
| 48 | InitiateKickMessage | C→S | Handled | Client starts a kick vote → VoteManager (server/voting.py). |
| 49 | ChatMessage | both | Handled+Sent | Player chat uses types 0/1; private system replies use type 2. Global server/mode announcements use `CHAT_BIG` type 3 and render at the top of every retail HUD. |
| 50 | LocalisedMessage | S→C | Sent | Top-screen string-table announcement. Resolves `string_id`, optionally resolves every positional parameter as another localization ID (for example `TEAM1_COLOR`), formats `{0}`/`{1}`/`{2}`, and supports replace-previous behavior. See Broadcast templates below. |
| 51 | SkyboxData | S→C | Sent | Null-terminated retail mesh-environment filename (sent at join, prefix 0x30). It comes from the active VXL's validated sidecar `skybox_texture`/`skybox_name`; `[world].default_skybox` is the missing-metadata fallback. |
| 52 | MapEnded | S→C | Sent | Native full-scene rollover trigger. It freezes the compiled `GameScene`; the compatibility hook opens `LoadingMenu`, then the server sends a fresh validated loader handshake over the same authenticated peer. Same-map score presentation deliberately omits it. |
| 53 | ShowGameStats | S→C | Sent | Opens `GameScene.show_game_statistics(False)`. Used only after voted-map preflight and only for maps with a bundled retail level screenshot; custom maps and same-map restarts omit it. |
| 54 | MapDataStart | S→C | Sent | Opens the native UGC source-map transfer before MapDataValidation. |
| 55 | MapSyncStart | S→C | Sent | Bare-id map sync start (prefix 0x32). |
| 56 | MapDataChunk | S→C | Sent | Persistent-zlib UGC source VXL chunks produced from 1048-byte input slices. |
| 57 | MapSyncChunk | S→C | Sent | Map content chunk stream (prefix 0x31). |
| 58 | MapDataEnd | S→C | Sent | Terminates the pre-validation UGC source-map stream. |
| 59 | MapSyncEnd | S→C | Sent | Map sync stream terminator. |
| 60 | MapDataValidation | both | Handled+Sent | CRC handshake; server replies with OUR file CRC. |
| 61 | PackStart | — | Planned | Resource-pack transfer start (network buffering). |
| 62 | PackResponse | — | Planned | Client ack for pack transfer (network buffering). |
| 63 | PackChunk | — | Planned | Resource-pack chunk (network buffering). |
| 64 | PlayerLeft | S→C | Sent | Announce a player disconnect. |
| 65 | ProgressBar | — | Planned | UI progress bar (capture/build progress). |
| 66 | RankUps | — | Planned | XP/rank changes at map end (match lifecycle/progression). |
| 67 | GameStats | S→C | Sent | End-of-round scoreboard widget (server/scoreboard.py, on_mode_end). |
| 68 | UGCObjectives | S→C | Sent | Exact current/min/max/priority rows for shared and target-mode Map Creator requirements. |
| 69 | Restock | S→C | Sent | Resource-specific refill. Type 0 is the full-life spawn/general restock; a physical ammo crate must send type 3. Health (4), block (5), and jetpack (6) crates use their own paths. Sending type 0 for an ammo crate also restores client health. |
| 70 | PickPickup | S→C | Sent | Authoritative objective pickup; initializes carried tool and burden state. CTF removes the type-16 ground entity and enables the carrier's high-visibility minimap marker. |
| 71 | DropPickup | both | Handled+Sent | Client drop request validated against sender/current pickup, then relayed with authoritative identity, type, position, and capped throw velocity. DropPickup clears the carried tool but does not persist ground intel, so CTF follows it with a type-16 CreateEntity at the settled dry-ground position. |
| 72 | ForceShowScores | — | Planned | Force the scoreboard open (match lifecycle). |
| 73 | ShowTextMessage | S→C | Reversed/unused | Selects one of nine hard-coded end/mode messages plus a duration; it is not an arbitrary text overlay. Free-form/localized broadcasts use packets 49/50 with `CHAT_BIG`. |
| 74 | FogColor | S→C | Sent | Live fog override from the admin command. Initial fog comes from the active map sidecar in StateData; the runtime override also persists into later spawn/rejoin snapshots. |
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
| 91 | PlaceRadarStation | C→S | Handled | Validated type-36 placement, native 35-second fuse/life, team minimap reveal, team-change cleanup, and damageable destruction. |
| 92 | PlaceC4 | C→S | Handled | Validated oriented type-38 placement with owner stock tracking; two-client retail rendering verified. |
| 93 | DetonateC4 | C→S | Handled | Detonates only the sender's live charges. |
| 94 | BlockSuckerPacket | both | Handled+Sent | Sanitized remote state relay plus authoritative timed voxel pull/grant. |
| 95 | DisguisePacket | C→S | Handled | Loadout/tool-gated disguise state, replicated through WorldUpdate bit 0x02. |
| 96 | DisableEntity | — | Planned | Disable an entity without destroying it (entity mgmt). |
| 97 | PlaceUGC | both | Handled+Sent | Host-only raw-voxel placement/removal for all 19 Game Data items; range, tool, bounds, duplicate, and project-cap checks precede authoritative echo. |
| 98 | InitialUGCBatch | S→C | Sent | Bounded initial/reconnect replay of persisted UGC objects. |
| 99 | ReqestUGCEntities | C→S | Handled | Retail refresh request; spelling is native. Replays packet 98 plus packet 68 validation. |
| 100 | UGCMessage | both | Handled+Sent | Recovered editor control channel for map info, validation, source-map, and conversion requests; late source-map requests are not replayed into GameScene. |
| 101 | UGCMapLoadingFromHost | — | Reversed/unused | Local Steam-lobby host progress packet. Unsafe and unnecessary on dedicated direct connect; packets 54/56/58 provide the source map before validation. |
| 102 | UGCMapInfo | both | Handled+Sent | Optional bounded PNG preview exchange; disk checkpointing stays off the gameplay thread. |
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
| 115 | ForceTeamJoin | S→C | Sent | Map Creator sends team 2/instant 0 after loading so Start opens the native prefab/Game Data selector. |
| 116 | PositionData | C→S | Handled | Handler registered, but the 1.x client does NOT send it (no-op path). |
| 117 | TeamProgress | — | Planned | Team objective progress bar (territory/mode rules). |
| 118 | SetGroundColors | both | Handled+Sent | Complete UGC terrain/water palette from the host, persisted and replayed to editor guests. |

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

### Stock map resources and static flare markers

VXL contains voxel spans, not crate or atmosphere metadata. The original
feature server supplied those fields in a same-stem compiled sidecar. The safe
server representation accepts JSON or literal assignment syntax and imports
`fog_color`, `static_light_color0/1`, the three `*_crate_drop_points` arrays,
and team spawn/base volumes without executing map code.

The native VXL loader removes exposed chroma markers before gameplay. Green
markers select static-light colour slot 0 and blue markers select slot 1. The
server mirrors that collision removal and creates neutral type-13 entities at
the removed marker positions. The shipped editor baseplate defines stock slot
0 as `(255,255,82)` and slot 1 as `(250,250,200)`; recovered per-map sidecars
override those defaults. The fallback is restricted to recognized stock maps,
so a community VXL with missing palette metadata cannot turn accidental chroma
terrain into guessed lights.

Native `FlareBlockEntity.post_initialize` calls both
`BlockManager.add_user_block(x,y,z,RGB,5,0)` and
`LightManager.add_static_point_light(x,y,z,RGB,5.0)`. RGB bytes are normalized
by the client to floats; the server must not pre-normalize them on the wire.
Because the entity re-creates a solid coloured voxel after VXL cleanup, the
server restores that same voxel in authoritative collision without recording a
player mutation. Packet 21 is sent after the first ClientData/GameScene gate,
including for late joiners; sending hundreds inside the loading transition is
still forbidden. A retail 20th Century Town join accepted 524 flare entities
with uint16 IDs through this path.

### Stock presentation assets and ambience

Packet 51 names a bundled mesh-environment manifest. Its render list contains
client-side sky/cloud/mist/wave/sun objects, transforms, and UV animation; it
does not contain voxel collision. `STOCK_MAP_SKYBOXES` maps shipped VXL names
to these presentation aliases, but stock and UGC maps both receive a full VXL
stream. The client's local CRC is validation, not permission to omit map data.

Map ambience uses paired packets. `CreateAmbientSound(22)` carries a validated
asset name, loop ID, and zero or more signed-short XYZ points. It only creates
the controller. `PlayAmbientSound(24)` with the same loop ID starts the stream
and supplies looping/positioned flags, volume, position, and attenuation.
Original metadata rows are `[name, points, volume, attenuation]`; empty points
are global, while non-empty point lists are local effects.

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
- Registered handlers are discovered from `protocol.packet_handler` and
  `server.handlers`; NewPlayerConnection(15), MapDataValidation(60), and
  SteamSessionTicket(105) also have connection-layer paths outside the
  decorator registry. Do not preserve hand-counted totals: editor isolation
  makes the active set process-specific, and the master table is authoritative.

---

## Packet feature-area notes

The active and remaining packet families are grouped here with their recovered
ordering and native-client hazards.

### Sounds
CreateAmbientSound (22), PlaySound (23), PlayAmbientSound (24), PlayMusic (26),
and StopMusic (27) are sent. StopSound (25) remains planned.

### Minimap / POI

CTF base zones use MinimapZone (43), and radar uses TeamMapVisibility (83).
POIFocus (18), standalone MinimapBillboard (41), MinimapBillboardClear (42),
and MinimapZoneClear (44) remain planned.

### Voting / kick

`GenericVoteMessage(47)` drives both majority kick ballots and the stock
next-map overlay. Candidate text is an identity field, not a yes/no string:
the server accepts only an exact advertised candidate and rejects forged or
missing records. The map catalog is captured at startup, and the final-minute
vote offers at most three maps in deterministic rotation order. A vote merely
stages `VoteManager.next_map`; the round lifecycle consumes it at the safe
scene boundary. A sudden score-limit ending waits for an unresolved ballot's
bounded 15-second deadline instead of consuming `None`; zero-vote and tied
ballots select the earliest candidate in deterministic rotation order. A kick
ballot still active at the round boundary is closed before the map ballot.
Players finishing GameScene construction during voting receive the current
overlay after roster/terrain reveal. `InitiateKickMessage(48)` remains the kick
start/cancel path.

### Map and mode scene rollover

IDA confirms that the retail receiver dispatches packet 52 through
`GameScene.process_packet_map_ended` and then `GameScene.on_map_ended`.
`on_map_ended` only sets the three scene pause flags and stops movement; it does
not select `LoadingMenu`, disconnect, or reconnect. Disconnect reason 18 is
terminal in the tested retail build.

For a voted official map, the end sequence is `GameStats(67)`, resolved vote,
`ShowGameStats(53)`, the configured `lobby.end_screen_seconds` dwell, then
`MapEnded(52)`. IDA shows packet 53 calls the live
`GameScene.show_game_statistics(False)` overlay; packet 52 remains the actual
loader boundary. A custom map may have no `png/ui/level_screenshots` asset, so
the server omits packet 53 rather than triggering the client's native
`ResourceNotFoundException`.

Replacing the VXL or mode then sends and flushes `MapEnded(52)`, detaches the
old server-side `Player`, commits the new runtime, and retains each settled
authenticated ENet peer. The client compatibility
hook selects `LoadingMenu(identifier=None)`, which deliberately reuses the
current `GameClient`. The server then sends `InitialInfo`; only after receiving
the matching `MapDataValidation` response does it stream the VXL and finish the
normal `MapSync`/`StateData`/roster sequence. A peer that does not enter the
loader is retired with reason 18 without affecting compatible peers. Invalid
targets fail before packets 53/52 and fall back to a same-map restart. A peer
still inside its original InitialInfo/MapSync when rollover begins never
receives gameplay-gated packet 52; it is retired with reason 18 instead of
starting an overlapping second VXL handshake.

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

Retail score metadata also defines two timed SetScore events. A living boss
receives 50 points every ten seconds with reason `VIP_SURVIVE` (12); each
living teammate within 15 blocks receives 10 points every five seconds with
reason `VIP_ESCORT` (13). BattleSpades re-arms these deadlines from the current
monotonic time, so a stalled tick can emit at most one event instead of a
reliable catch-up burst. Sub-round CreatePlayer/loadout/health publication is
likewise drained in bounded slices rather than respawning the whole roster in
one tick.

Molotov fire is server-owned. The native `BlockFireEntity` does not recursively
spawn children itself; constants permit five spread attempts for the whole
impact. Every child therefore shares one cluster budget instead of receiving a
fresh budget. A conservative 96-emitter global ceiling is an inferred native
client safety bound: a new impact replaces the oldest emitter at the ceiling,
while child spread stops. The shared-budget rule is recovered behavior; the
numeric global ceiling is BattleSpades operational hardening.

### Zombie Infection

Zombie uses the retail mode id 2 and existing role packets; it introduces no
custom wire format:

- Before outbreak, `StateData(45)` exposes the normal survivor classes plus
  Rocketeer on team 2 and locks team 3.
- At outbreak, `KillAction(19)` provides the native-safe model transition and
  the following `CreatePlayer(28)` respawns Patient Zero as class 4 on team 3.
- Team 3 is class-locked to base Zombie. Fast/Jump Zombie remain disabled
  because this client has no stable ordinary picker icons for those classes.
- `InitialInfo.exposed_teams_always_on_minimap` is set for Zombie mode. The
  native `Player.display_map_icon_out_of_bounds` routine uses this boolean for
  ordinary opposing-role map visibility; it does **not** apply the VIP icon.
- `ChangePlayer(17)` action 8 marks the sole remaining living survivor and is
  replayed to late joiners. This is a separate
  `high_minimap_visibility` path which does apply the special/VIP marker, so it
  must not be broadcast for every survivor.
- A client joining after outbreak is normalized to team 3/class 4 regardless
  of its requested team, class, or loadout. Zombie respawn delay is zero.

The 600-second survival clock starts when Patient Zero is selected. Time spent
waiting for enough players is not round time.

### Territory control / mode rules (planned)
TimeScale (75), LockTeam (79), TeamLockClass (80), TeamLockScore (81),
TeamInfiniteBlocks (82), LockToZone (108), TerritoryBaseState (106),
TeamProgress (117), ProgressBar (65). ForceTeamJoin(115) is already active in
the isolated Map Creator.

### UGC Map Creator

The editor is isolated behind `run_map_creator.py`; the normal mode registry
cannot select it. The recovered dedicated sequence is
`InitialInfo(114) -> MapDataStart(54) -> MapDataChunk(56)* -> MapDataEnd(58)
-> MapDataValidation(60) -> MapSync(55/57/59) -> StateData(45) ->
ForceTeamJoin(115)`. StateData's signed-16-bit catalog counts carry the six
native tabs (138/90/47/47/26/25, 373 total), and SetClassLoadout(13) commits a
shared five-item Construct/Game Data backpack.

Build/erase packets 30/31 preserve raw KV6 color and all three rotations.
Packets 97/98 own the 19 authored object types, packet 68 mirrors exact mode
requirements, packet 118 carries the ground/water palette, and packet 102
exchanges an optional preview PNG. Project state checkpoints as the retail
`.vxl`/`.txt`/`.ugc` triplet.

The two prefab actions are intentionally asymmetric on the wire:

- `BuildPrefabAction(30)` writes its anchor as three raw signed voxel shorts.
- `ErasePrefabAction(31)` writes the same logical anchor as three signed
  1.6 fixed-point shorts (`coordinate * 64`).
- Both range fields are unsigned 32-bit indexes with native semantics
  `[from_block_index, to_block_index)`. A non-empty model echoed as `0, 0`
  processes zero client voxels. After authoritative commit the server echoes
  `0, model_block_count`, including the authored model count when clipping
  prevents some cells from changing.

This was recovered from retail `shared/packet.pyd` and `vxl.pyd`, not inferred
from the similarly named packet classes. For anchor `(112, 269, 223)`, packet
31's final six bytes are `00 1c 40 43 c0 37`; decoding those as raw shorts
produces the old 64-times-too-large erase position.

The UGC Paintbrush has two accepted input paths. A normal `PaintBlockPacket(7)`
is validated directly. Dedicated direct-connect clients can instead keep the
action only in held `ClientData(4)` primary/secondary bits, so the server
raycasts the authoritative eye/orientation, applies the single-cell or bounded
surface brush, and broadcasts packet 7 with the exact RGB. The packed
`palette_enabled` bit is not an action veto: the native Paintbrush deliberately
keeps its palette active while painting, while an actual palette click arrives
without the action bits.

The UGC Super Spade uses `ShootPacket(6).secondary` as its dual-use selector.
Primary sends UGC damage type 29 and affects one cell. Secondary sends UGC
secondary type 31 and affects one centered 3x3x3 footprint. The server commits
that footprint once and emits one matching native expanding `Damage(37)`;
emitting a damage packet for every removed cell would make the retail client
expand the cube repeatedly and create a larger client-only hole.

The stock menu Host path assumes a local Steam-lobby owner. Dedicated direct
connect therefore makes the server the editor host and retains the native
in-game Construct and Game Data screens. Packet 101 is reversed but unused;
injecting it or replaying source-map state after GameScene construction is a
native crash hazard.

### Entity management
ChangeEntity (16) is sent for turret/MG target, ammo, and carrier properties;
HitEntity (20) is sent as a visual impact callback after authoritative server
ray selection. EntityUpdates (3) and DisableEntity (96) remain unused pending
an evidence-backed gameplay path.

Create/destroy symmetry is tracked per connection. Moving projectiles are
deliberately omitted from a joining client's static snapshot; if one spawned
during MapSync and expires after first ClientData, that GameScene receives no
DestroyEntity because it never received the matching CreateEntity. Knowledge
is cleared on scene reload and on each successful destroy, so entity-id reuse
starts a fresh lifetime. This prevents the retail `invalid entity on destroy`
join race without replaying stale mid-flight projectiles.

### Building / blocks
PaintBlockPacket (7), BlockBuildColored (33), PrefabComplete (29),
BuildPrefabAction (30), ErasePrefabAction (31), and BlockSuckerPacket (94) have
active paths. Between MapSync and first ClientData, a bounded per-cell sequence
retains every committed canonical voxel coordinate. Replay coalesces repeated
edits, reads final solidity/RGB from the VXL, and sends only explicit-RGB
`BlockBuildColored(33)` or exact-cell `Damage(37)` with `chunk_check=0`. A
multi-cell collapse therefore cannot be re-expanded against newer topology,
and retry resumes at the first reliable packet ENet did not accept. If the
journal loses sequence continuity, the join is rejected rather than entering
with partial terrain. BlockOccupy (34), BlockManagerState (38), and
ServerBlockAction (39) remain planned.

Edits completed before a reconnect's MapSync boundary have a second exact-air
safety path. `WorldManager` retains current destroyed cells as one 240-bit mask
per changed `(x,y)` column. After the first ClientData proves `GameScene`
exists, the server reasserts those cells in bounded type-6,
`chunk_check=0` batches while keeping ordinary gameplay gated. Only after this
frozen pre-snapshot set drains does the newer per-cell journal replay, so a
block rebuilt while the client loads always wins. This ordering is required
for Drill tunnels: the native VXL worker can visually merge a dirty column yet
retain stale collision until an exact removal callback arrives.

Settled clients also receive a delayed, bounded canonical repair of recently
changed cells. This is not a new packet contract: solid cells use explicit-RGB
`BlockBuildColored(33)` and air uses exact-cell `Damage(37)` with type 6 and
`chunk_check=0`. The replay reads VXL state at send time and is deliberately
excluded from the late-join mutation journal.

Unsupported collapse needs a distinct confirmation lane. The initiating
checked `Damage(37)` asks every retail BlockManager to derive and animate the
falling component from local topology. The server mirrors that component, then
queues every actually removed cell surface-first. After 18 ticks it sends a
bounded stream of exact type-6, `chunk_check=0` air confirmations. Correct
clients treat them as no-ops; a divergent client clears stale visible but
non-colliding geometry. Cells rebuilt before confirmation are dropped. Packet
38 cannot replace this path: IDA recovery of
`BlockManager.send_block_manager_state`/`receive_block_manager_state` shows
damage, occupancy, and user-block dictionaries rather than world topology.

Melee terrain Damage is type-dependent. Type 2 expands to the centered
three-cell z column; type 3 expands to a centered, axis-aligned 3x3x3 Super
Spade cube; pickaxe-family types are exact-cell. The server mirrors that
footprint and sends one area packet—never one expanding packet per removed
cell.

### Pickups
PickPickup (70) is server-to-client only. DropPickup (71) is handled and
relayed; objective pickup state is also carried by WorldUpdate and replayed to
late joiners.

Map crates remember their authored vertical offset from the first solid voxel
beneath them. At 10 Hz they verify only that remembered support cell. If it is
destroyed, the server finds the next support in AoS's +Z-down column, updates
the authoritative entity/home position, and reliably emits ChangeEntity (16)
action 1. A fall into a water-only column is redirected to the nearest dry
surface so a permanent map resource cannot become unreachable.

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

The shipped playlist contains Crossroads, Hiesville, ToTheBridge, Trenches,
WinterValley, WW1, and Classic. With no explicit operator rotation, voting is
limited to that catalog. Its stock capture target is five and its intel begins
three blocks from the authored base anchor. A capture awards 10 personal points
with reason `CTF_CAPTURE` (50) plus one team capture; a touch return awards one
personal point with reason `CTF_CLAIM` (53). Operator score/map overrides still
take precedence over the playlist defaults.

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
Classic CTF deliberately bypasses the normal entity-11 gravestone. `KillAction`
changes the existing native Character into `ClassicCorpse.kv6`; no
`CreateEntity(21)` packet is involved and the server allocates no entity id.
The server retains a generation-tagged static hit target using the recovered
48x50x14 KV6 bounds and compares its ray-entry distance with players,
deployables, and terrain. A hit emits `ExplodeCorpse(36)` once with
`show_explosion_effect=1`, then applies the recovered corpse blast constants:
radius 3, player damage 0, block damage 1, knockback 0.05–0.1, and kill reason
12. Disabling `RULE_ENABLE_CORPSE_EXPLOSION` leaves the corpse visible but not
hittable.

Packet 36 is never sent at death because it removes the Character corpse that
`KillAction` just created. A surviving corpse is removed with effect flag 0
before the same numeric player id receives its next `CreatePlayer`. Roster
catch-up records death separately from life creation: a joining GameScene sees
`CreatePlayer -> SetColor -> KillAction` exactly once, and if the corpse
exploded while gameplay was gated it receives only a silent packet-36 repair.
DisguisePacket (95) is handled and replicated through WorldUpdate.

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
are sent by the round lifecycle. The full-rollover order is 67, resolved vote,
53, configured dwell, then 52; same-map and screenshot-less custom-map paths
omit 53. RankUps (66) and ForceShowScores (72) remain planned.

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

### Steam Internet server discovery

The shipped `shared/steam.pyd` initializes `SteamGameServer011` as follows:

```text
SteamGameServer_Init(0, 8766, game_port, query_port, mode, "1.0.0.0")
SetProduct("aos")
SetGameDescription("Ace of Spades")
SetModDir("aceofspades")
SetDedicatedServer(true)
SetGameTags("v<protocol>;playlist=<id>[;region=...];mode=%04d[;classic][;skin=...]")
EnableHeartbeats(true)
LogOnAnonymous()
```

Mode `1` is LAN/no-list, `2` is public insecure, and `3` requests VAC. The
retail Internet list requests app `224540` and filters
`gamedir=aceofspades`. Its Official tab additionally requires `white=1`; its
User tab applies `nand(white=1)`. BattleSpades deliberately does not forge the
official-only key, so community hosts belong in the generic/User lists. The
client then locally matches `mode=%04d` and optional `region=...`. It uses a
separate game and query port. Its displayed map is `<MODE>_<MapName>` with
spaces removed and the following character capitalized, for example
`TDM_CityOfChicago`.

The retail `ServerInfo` code then hardcodes the connection port to `32887`
instead of honoring the game port returned by Steam. A stock-compatible host
must therefore bind its ENet server on UDP `32887`. The query port remains the
separate address advertised by Steam.

`steam_appid.txt` value `480` in a decompiled tree is Spacewar test identity,
not an AoS server identity. BattleSpades creates a private `224540` file for
the helper. The helper's Steam-owned query socket is separate from the ENet
port's direct A2S intercept.

As of the 2026 recovery, registration and retrieval are different systems.
Valve's public `ISteamApps/GetServersAtAddress` registry returns BattleSpades
with app `224540` and game dir `aceofspades`, while the old
`hl2master.steampowered.com` UDP endpoint no longer resolves. Consequently the
unmodified 2015 All/Community UI completes with `eServerFailedToRespond` even
for a correctly registered and publicly queryable server. This is a client
discovery outage, not permission to alter the recovered app/game-dir identity.

### UI / messaging
ChatMessage (49) and LocalisedMessage (50) are active for retail top-screen
broadcasts. Because packet 49 never performs localization, its shared builder
resolves `TEAM1_COLOR`, `TEAM2_COLOR`, and `TEAM_NEUTRAL` to canonical display
text before serialization. ShowTextMessage (73) is fully reversed but intentionally unused for
free-form text because its byte is a fixed message enum. HelpMessage (109)
remains planned. The formatter contract and variables are documented below.

### Voice (planned)
VoiceData (103).

### Visuals
SetGroundColors (118) is active in the isolated Map Creator; ordinary maps use
their StateData/map-metadata palette.

### Dev tooling (planned)
DebugDraw (107).

## Retail Match Lobby recovery

The lobby schema was recovered from the shipped Python 2 constant pool rather
than inferred from UI screenshots:

- `aoslib/scenes/frontend/matchSettingsPanel.pyc` supplies max-player and
  match-length selectors.
- `gameRulesPanel.pyc` consumes `shared.constants_matchmaking.A2667` (visible
  categories), `A2688` (defaults/legal values), `A2711` (rule-to-tool), and
  `A2712` (rule-to-class).
- `shared.constants_gamemode.A2448` contains the ten public rows and `A2662`
  contains their default clocks.
- `playlists/*.txt` contains official map compatibility and playlist defaults.

The public modes are `tdm`, `ctf`, `cctf`, `zom`, `vip`, `mh`, `tc`, `dia`,
`dem`, and `oc`. Tutorial and UGC creator entries are not public match rows.
Selectors and map sets are normalized in `server/lobby.py`; all 102 visible
and hidden rules live in `server/game_rules.py`. Hidden recovered controls are
vote threshold, own-intel-at-base scoring, riot shield, and normal parachute.
Do not duplicate these tables in handlers.

## Broadcast templates

Free-form text uses packet 49. Localized templates use packet 50 with a string
ID, positional parameters, a `localise_parameters` flag, and an
`override_previous` flag. Packet 73 is a fixed enum, not arbitrary text.

Before packet 49 serialization the server resolves `TEAM1_COLOR`,
`TEAM2_COLOR`, and `TEAM_NEUTRAL` into readable names. Packet 50 may pass those
identifiers as localized parameters. Construct both through
`server.announcements`; never interpolate untrusted tuple syntax into a native
client field.

## Reverse-engineering workflow and evidence navigation

Use evidence in this order:

1. A clean retail client observed live.
2. IDA/decompiler control flow and the shipped Python 2 `.pyc` constant pool.
3. Packet read/write layouts in `shared/packet.pyx`.
4. Maintained characterization tests and raw captures.
5. Reversed Python/Cython ports only as hypotheses.

For lobby data, import the constant module with the client's bundled 32-bit
Python 2 executable and print the obfuscated table directly. For native code,
record image base, function address, caller/callee, field offsets, packet
direction, and exact reproduction. A claim without a static path and a retail
observation remains provisional.

Movement evidence must preserve the 60 Hz clock, input label, receipt tick,
owner send sequence, WorldUpdate stamp, and pre/post native state. Owner rows
are reconciliation events; observer rows are replication. Never tune both
sides simultaneously. Terrain evidence must record the originating input loop
and verify owner, observer, late join, and join-during-mutation views.

Crash-sensitive invariants include `InitialInfo` list shapes, compact player
IDs, entity create/destroy symmetry, map display names used for screenshots,
scene-terminal packets, localized-string tuple fields, and entity IDs used by
projectile effects. Change one only with a focused test and two clean clients.
