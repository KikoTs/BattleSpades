# BattleSpades Protocol Catalog

Living index of every packet in the Ace of Spades 1.x (Battle Builders) wire
protocol and its implementation status in this server. **Derived from the
source, not invented** — regenerate/re-verify against the files below when the
counts change.

Sources of truth:
- **`shared/packet.pyx`** — the full protocol surface. Every `cdef class X(Loader)`
  carries `id: int = N`. This is where the *definitions* live (~120 packets).
- **`server/handlers/*.py`** — every `@register_handler(N)` (registry in
  `protocol/handler_registry.py`) is a packet the server currently **handles on
  receive** (C→S). `protocol/packet_handler.py` is only the decoder/dispatcher
  that imports those modules; handshake packets 15/60/105 are handled in
  `server/connection.py`.
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
- **Every packet class is kept even if currently unused.** A definition alone
  does not establish a receive/send path. Several historically planned domains
  now have live handlers; use the per-packet table and implementation references
  below rather than inferring feature status from the existence of a class.

- **ENet connect data** must be 168: the stock client connects with
  `host.connect(address, 1, shared.steam.game_version())` (168) and the native
  client with 168. Anything else is refused before any state is allocated
  (`[network] require_protocol_version`, default on): lower -> 10
  `ERROR_CLIENT_OUT_OF_DATE`, higher -> 3 `ERROR_SERVER_OUT_OF_DATE`.
- **ENet disconnect data** is the retail `DISCONNECT` enum
  (`shared/constants.py`). The ones this server sends: 1 banned (permanent),
  3/10 protocol mismatch (above), 4 `ERROR_FULL`
  (no free player slot after trying to swap a bot out; the client shows
  SERVERFULL_ERROR), 13 `ERROR_DATA`, 16 AFK, 18 match ended (map rollover
  without a `ClientInMenu(110)` ack), 19 `ERROR_TEMP_BANNED` (a timed `/ban`
  and a reconnect during it), 23-25 vote-kick reasons. Reason 3 is
  `ERROR_SERVER_OUT_OF_DATE` ("The server is out of date"); it is sent only to
  a client whose connect data is newer than 168. Beta.1 builds before f3518e2
  wrongly sent 3 when full.
- **ENet host**: 0/0 bandwidth and the library-default peer timeout, like the
  retail client and host; `[network] timeout_ms` / `bandwidth_limit` are
  ignored.

Statuses:
- **Handled** — has a `@register_handler` (server parses it on receive).
- **Sent** — server constructs it and calls `.generate()`.
- **Handled+Sent** — both.
- **Planned** — defined in `packet.pyx` but neither handled nor sent yet.
- **Reversed/unused** — behavior is understood but deliberately not emitted.
- **Reversed/blocked** — behavior is understood, but the retail wire/runtime
  cannot complete a safe lifecycle and the packet must not be emitted yet.

Direction: **C→S** (client→server, we handle), **S→C** (server→client, we send),
**both**, or **—** (neither yet).

---

## Master table (by packet id)

| ID | Packet | Direction | Status | Notes |
|----|--------|-----------|--------|-------|
| 0 | ClockSync | both | Handled+Sent | Round-trip clock/loop_count sync; client 1 tick ahead. The client sends it unsequenced every 60 loops; the reply is sent ENet UNSEQUENCED too, so it is never held behind a lost reliable packet (a lost reply is replaced by the next one). |
| 1 | PlaceDynamite | C→S | Handled | Tool/loadout-gated Miner charge; server creates entity type 10 and owns its fuse/blast. |
| 2 | WorldUpdate | S→C | Sent | 30 Hz unreliable position/state feed. Header loop is the global snapshot clock; each human row pong is that player's consumed ClientData loop. Peerless bots have no client clock and use the authoritative server loop as a monotonic remote-row stamp; leaving bot pong at zero makes retail deduplicate every later bot position. Rocket-turret rows carry the turret entity id as the same signed short CreateEntity wrote (uint16 registry ids above 32767 become negative, e.g. 40000 -> -25536). |
| 3 | EntityUpdates | — | Planned | IDA (stock gameScene.pyd): `self.process_entity_updates(packet.updated_entities)`, a moving-entity delta stream. BattleSpades moves entities with ChangeEntityPosition(21); no mode needs the batch form yet. |
| 4 | ClientData | C→S | Handled | Buffered client input, applied at matching tick. The player byte uses bits 0–6 for `player_id`; bit 7 is `palette_enabled`. |
| 5 | SetHP | S→C | Sent | Sets a player's HP (spawn/heal/damage feedback). |
| 6 | ShootPacket | C→S | Handled | Client fire request. The server validates cadence/origin/orientation and resolves authoritative damage; retail has no incoming packet-6 action handler. |
| 7 | PaintBlockPacket | both | Handled+Sent | Validated (paint tool, reach/range, target solidity) before the authoritative color mutation, which is journaled for late joiners. Accepted paints are echoed to every in-game client, owner included (combat_runtime), so a Map Creator editor that is also a UGC map client converges. On a solid voxel it recolours only: `user_blocks` health and any `DamagedBlock` health are unchanged (live 2026-09-26), and `WorldManager.set_block` keeps the cell's recorded health and damage for the same solid→solid recolour. |
| 8 | ShootFeedbackPacket | S→C | Sent | Remote firearm shot only. Sent to observers (not the already-predicting human shooter); the native handler verifies the visible character's tool and calls `character.shoot(seed)`, producing gun audio/muzzle effects. Never send for spades/melee: those tools have no `shoot` method and use WorldUpdate action bit `0x01`. |
| 9 | ShootResponse | S→C | Sent | Authoritative player-hit response. Broadcast with `damage_by=shooter_id`; native clients show blood to observers but play the hit-confirm sound/crosshair only for the matching local shooter. |
| 10 | UseOrientedItem | both | Handled+Sent | Validated: active normalized tool, cadence, stock, and the reported origin's reach from the authoritative body. Legacy grenade-family objects are relayed to observers; entity-backed projectiles use CreateEntity instead. Never relay GL tool 55 into the retail client's stale `GLGrenade` packet constructor. |
| 11 | SetColor | both | Handled+Sent | Palette state for block, flare, and Block Cannon tools (5/22/29/48). Every new life starts from its configured team RGB and publishes that snapshot after CreatePlayer; later UI choices are broadcast only to observers because the sender already applied them locally. Relays are throttled per player (one per 0.1 s); the server state always updates and one trailing relay publishes the newest colour, so observers never keep a stale palette. |
| 12 | SetUGCEditMode | both | Handled+Sent | Map Creator only: the editor host's request changes the project target validation mode and the server rebroadcasts the committed mode (reliable, never journaled) to every editor; guests' requests and every non-UGC server ignore it. |
| 13 | SetClassLoadout | both | Handled+Sent | Normalized server-side; an unchanged selection is ignored, and a changed one is staged and applied at the next life (a live Character dies with KILL_CLASS_CHANGE). Only the Map Creator's live UGC backpack edit is committed in place and acknowledged with a reliable SetClassLoadout (instant=1). Retail may omit the trailing zero UGC-count byte; the bounded decoder accepts only that optional empty tail. UGC preserves one shared five-item prefab/Game Data backpack. |
| 14 | ExistingPlayer | — | Reversed/unused | Roster entry (id, demo, team, class, tool, pickup, dead, score, forced_team, language, colour, name, loadout, prefabs). Not sent: the roster goes out as CreatePlayer(28). Live 2026-09-26: the client reads `pickup` **signed**, so 0xFF arrives as -1 and the created player gets `pickup_id=None`; 0 gives `pickup_id=0` (the minimap `PICKUPS[0]` KeyError case). The earlier "no 0xFF sentinel" note was wrong; switching the roster to 14 would be a roster-lifecycle change, not needed for parity today. |
| 15 | NewPlayerConnection | C→S | Handled | Client's join announcement (name/team/class), parsed in handshake. Accepted only after this connection's InitialInfo → MapSync → StateData completed (every stock client, Steam or ticket-less legacy, sends SteamSessionTicket(105) first); earlier or duplicate in-flight packets are ignored. Before CreatePlayer, names are normalized to a case-insensitively unique 15-byte wire value; duplicate names can steal the native client's local-player association. |
| 16 | ChangeEntity | S→C | Sent | Server-owned turret/MG target, carrier, ammo, state, and map-pickup position. Action 1 (`SET_POSITION`) reliably settles a pickup after its supporting structure breaks. |
| 17 | ChangePlayer | S→C | Sent | Existing-player state changes. Action `SET_HIGH_MINIMAP_VISIBILITY` (8) exposes the CTF intel carrier, both VIP bosses, or ZOM's final survivor through terrain; each mode owns marker cleanup and late-join replay. |
| 18 | POIFocus | S→C | Sent | Handler reads only `target_x/y/z` and (with a local player) activates the GameScene `LookAtController` aimed at that point. No timeout and no release packet: live 2026-09-26 the lock held until the local player died (ChaseController) or was spawned (server `respawn_player` of a living player cleared it) or the map changed. Demolition sends it to every non-spectator team player when the objective airstrike launches, aimed at the base being struck (on a double destruction each team watches the base it destroyed); the round restart respawns them. `server.hud_packets.poi_focus`. |
| 19 | DestroyEntity | S→C | Sent | Removes an entity previously announced to that GameScene. Server-only objective markers must never receive a destroy packet. For Snowball it removes the visual/effect only; it does not apply blast impulse. |
| 20 | HitEntity | S→C | Sent | Visual impact callback for server-authoritative damageable-entity hits. |
| 21 | Entity / CreateEntity | S→C | Sent | Entity wire format + create; used for Map Creator markers (type 29, `ugc_mode` + `int_properties=[item]`), map crates, type-13 static/player flare lights, deployables, persistent ground intel 16, and moving projectile types including chemical 32, GL 33, sticky 34, and launched mine 37. The retail runtime table does **not** contain legacy FLAG=0 or BASE=1; sending BASE here freezes `GameScene.create_entity` with `KeyError: 1`. Both Entity and CreateEntity share id 21. |
| 22 | CreateAmbientSound | S→C | Sent | Registers a map-owned ambient controller. Empty points are a global bed; authored points define local emitters. Must precede packet 24 with the same loop ID. LIVE-VERIFIED. |
| 23 | PlaySound | S→C | Sent | One-shot positional/UI sound: pickups, round/kill cues, and observer-only block-tool impacts. The actor predicts its own mining sound and is excluded. LIVE-VERIFIED. |
| 24 | PlayAmbientSound | S→C | Sent | Allocates the streaming ambient `GameSound` registered by packet 22. Global beds are unpositioned; local loops bootstrap at the listener and are moved by the native point controller. LIVE-VERIFIED. |
| 25 | StopSound | S→C | Sent | Byte-sized loop-id teardown for server-owned PlaySound/PlayAmbientSound streams. The shared helper validates 0–255; the native receiver treats an unknown id as an idempotent no-op. |
| 26 | PlayMusic | S→C | Sent | Music track (server/audio.py — last-minute game_ending track at 61s remaining). The in-round `last_man_standing` bed at round start/join is a deliberate deviation behind `[audio] mode_start_music` (default on; off = retail silence, StopMusic only). |
| 27 | StopMusic | S→C | Sent | Stop the current music track (server/audio.py). |
| 28 | CreatePlayer | S→C | Sent | Spawns a player on clients; also carries the roster. Loadout and all three selected prefab names come from the same committed ClassSelection. Every live player name must be unique before this packet is emitted; the packet direction contains no movement-owner identity field. |
| 29 | PrefabComplete | S→C | Sent | Sent to the builder after its prefab commits (or is refused), always after the packet-30 echo. |
| 30 | BuildPrefabAction | both | Handled+Sent | Shared `PrefabActionService` validates selection, reach, line of sight, body overlap, reservations, per-player queue share, and stock (`blocks >= len(model points)`); a footprint with any voxel outside the buildable map is refused whole. Competitive prefabs commit every model voxel in one post-physics tick (`prefab_competitive_cell_budget` caps cells per tick; never split) with retail `replace_solids` semantics and `DEFAULT_PREFAB_HEALTH` (9) in the server damage model, charge exactly one block per model voxel, then broadcast ONE packet 30 (`add_to_user_blocks=1`, `color` = the blend base the server used, index range 0..0) to every in-game client **including the owner**, then 29 to the owner. The stock client expands the KV6 itself: `add_user_block(..., 9.0, replace_solids=True)` per voxel, 50/50 `blend_color(packet.color, voxel)` (client re-randomises the low 2 bits per channel), one smoke ring per top-layer voxel, and the owner's wallet drops once per model voxel even over existing solids; `send_build_prefab` does not predict. LIVE-VERIFIED 2026-09-26 (68-voxel superminibunker: 45 replaced solids, owner wallet 500→432 = server 432, observer wallet unchanged, all cells appear in one frame with 32 smoke rings, 9 SMG hits break a cell on owner, observer, late joiner and server). UGC keeps the off-thread KV6 preparation, raw model colours, bounded main-thread batches, and the native index-range echo. |
| 31 | ErasePrefabAction | both | Handled+Sent | UGC Map Creator only (ignored in competitive play). Native carve with verified yaw/pitch/roll fields. The action is echoed only after bounded live-world target validation, then the expanded set is removed through the authoritative block-destroy path. |
| 32 | BlockBuild | both | Handled+Sent | Single-block place; handled on receive, echoed to the builder only. Client material table `BLOCK_BUILD_TYPE_STATS`: `block_type` 0 adds a user block with health 9.0 (1 = snow, 3.0) and debits the local owner one block (live 2026-09-26). The server records every committed build at 9.0 (`USER_BLOCK_HEALTH`); observers get 33 + a 38 user row. No longer used for prefab cells. |
| 33 | BlockBuildColored | S→C | Sent | Per-block coloured placement for ordinary-build observers, terrain repair, persistent Block Cannon impacts, and MapSync join catch-up. The stock client adds it as a user block with health **3.0** and no wallet change, and ignores it on an already-solid voxel (live 2026-09-26). Block Cannon cells are recorded at 3.0 server-side; every other 33 is followed on the same reliable stream by a 38 user row carrying the server's health (9.0 built/prefab, 5.0 map voxel) and, for a damaged cell, a damaged row — live builds (`_send_observer_block_health`), terrain repair and join catch-up (`TerrainRepairService.canonical_health_packets`). |
| 34 | BlockOccupy | — | Reversed/unused | Handler reads `x, y, z, player_id` → `block_manager.occupy_block`, storing `occupied_blocks[(x,y,z)] = player_id`; live 2026-09-26 an occupied air cell fails `valid_to_add` for every client (build ghost refused) until BlockLiberate(35) or `clear_occupied_blocks`. It reserves cells only; BattleSpades commits builds immediately (BlockLine/BlockBuildColored), so there is no pending cell to reserve. |
| 35 | BlockLiberate | S→C (client-bound) | Rejected outside UGC | The stock client only *receives* 35 (`process_packet_block_liberate` → `block_manager.liberate_block`; IDA 2026-09-26 found no send site in gameScene.pyd, Steam or non-Steam); digging arrives as ShootPacket(6). A 35 from a client outside the Map Creator is a forged packet → `anticheat.protocol_violation` (kick when `[anticheat] kick_on_protocol_violation`). In UGC the legacy path stays with reach + line-of-sight checks. The server never sends 35. |
| 36 | ExplodeCorpse | S→C | Sent | Three bytes: player id and effect flag. Sent only to in-game clients whose roster knows that id (`known_player_lives`); loading clients are repaired by `send_catchup_state` on first ClientData. A normal death sends it after the retail corpse fuse (0s, or 1.0s with a selected jetpack) before creating the falling entity-11 grave; this activates the native controllable death camera. Classic CTF retains its client-owned `ClassicCorpse` until a hit (flag 1) or silent cleanup (flag 0). It is not entity type 12. |
| 37 | Damage | S→C | Sent | The only terrain-removal path. `BlockManager.handle_damage` expands the TYPE's footprint around `floor(position+0.5)` and calls `add_damage` on each solid cell with z ≤ 238; the `damage` byte is quarter units rounded to nearest. Footprints (live-fitted exactly for all 44 types, 2026-09-26; `server/block_damage_model.py`): single cell (0,1,4,6,25,26,28,29,34,42,43); z column z-1..z+1 (2,5,36); machete z,z+1 (35); 3×3×3 cube, x-major, `ceil4(amount + E·r)` with E = 5 (3), 8 (17), 0 (31); sphere `d² < R²`, z-major (z,x,y), `ceil4(amount·(1−d²/R²) + 2·r)` with R = 2 (22,23), 3 (10,11,12,13,14,15,21,33,38,40), 4 (7,8,24,30,37), 5 (39), 6 (9,18), 7 (19), 8 (16,41); none (20,27,32). `r` = successive `random()` of a Python-2 `random.Random(seed)` re-seeded per packet, one draw per footprint cell (solid or not); `ceil4` rounds up to 0.25. The server applies the identical per-cell damage (per-cell health) and sends ONE packet with its seed for digs, projectile/deployable blasts and drill bores; kills use 31.75. Snowball sends one reliable zero-damage type-20 event at impact before DestroyEntity(19), allowing the native explosion manager to predict impulse. |
| 38 | BlockManagerState | S→C | Sent | Per-cell block health. Wire (stock client `shared.packet` round trip, 2026-09-26, little-endian): `u8 38, i32 damaged_n, damaged_n × (i16 x, i16 y, i16 z, u8 remaining×4, u8 b, u8 g, u8 r), i32 user_n, user_n × (i16 x, i16 y, i16 z, u8 health×4), i32 occupied_n, occupied_n × (i16 x, i16 y, i16 z, u8 player_id)`. `receive_block_manager_state` MERGES both tables (live-verified): a user row sets `user_blocks[cell]` (initial health, unscaled); a damaged row sets `DamagedBlock(remaining, original_color)` (remaining is SCALED by `health_multiplier`) and darkens the voxel `0.125` per point of damage against the initial health held at that moment — so user rows are always sent in earlier packets (`block_state_packets`). Sent to observers after 33 builds, after every solid 33 in terrain repair/join catch-up, and by `PrefabActionService.reveal_to` (called from `reveal_world_to` after `replay_map_mutations`) for every recorded non-default health plus every partially damaged cell. Implicit-colour interior voxels get an equivalent user row (remaining/scale) instead of a damaged row so their client-owned colour is not repainted. `encode_block_manager_state` writes the bytes (the reconstructed server classes do not match). |
| 39 | ServerBlockAction | — | Reversed/unused | Live 2026-09-26: `process_packet_server_block_action` reads no packet attribute at all; it is a no-op stub in the stock client. Never worth sending. |
| 40 | BlockLine | C→S | Handled | How the 1.x client actually PLACES blocks (line of blocks). Validated (reach, face adjacency, stock) before commit; committed cells are recorded at 9.0. The builder gets the 40 echo (client health 9.0) + PaintBlock pins; observers get 33 per cell + one 38 user-row table. |
| 41 | MinimapBillboard | — | Reversed/unused | Handler reads `entity_id, color, x, y, z, icon_name, tracking` (**`key` is never read**) → `minimap.add_billboard(id, r/255, g/255, b/255, x, y, z, icon_name, tracking)`; the same id updates in place. Draws a minimap icon plus an in-world marker. `icon_name` loads `png/ui/<name>.png`; an unknown name raises IOError in the handler, so `server.hud_packets.BILLBOARD_ICONS` whitelists the stock minimap/marker art. `tracking=1` binds to `scene.entities[entity_id]` (a CreateEntity id; the icon follows it) and an unknown id is dropped with "invalid billboard tracking target id (KeyError)". Live-verified 2026-09-26 over the wire: static add, tracking on a crate, recolour/move, clear. No mode sends it: Demolition/CTF/TC/MH/Occupation bases already draw through 43, and crates, radar stations, C4, turrets and carried intel/bomb/diamond draw themselves. `add_billboard` / `reveal_billboards` / `clear_all_billboards`. |
| 42 | MinimapBillboardClear | — | Reversed/unused | Reads `entity_id` → `minimap.remove_billboard`; an unknown id is a silent no-op (live 2026-09-26). `server.hud_packets.remove_billboard`. |
| 43 | MinimapZone | S→C | Sent | CTF team-base zone and icon. Six signed-short fields are raw voxel min/max bounds for X/Y/Z; `key` is native `visible_team`, and icon 6 is `ZONE_ICON_CTF`. Sent at mode start and late join. |
| 44 | MinimapZoneClear | S→C | Sent | Clears a packet-43 zone by its exact six-coordinate identity; used by objective-mode phase/lifecycle cleanup. Retail Python 2 wire vector verified. |
| 45 | StateData | S→C | Sent | Per-spawn game/team/lighting snapshot (sent at join, prefix 0x31). Prefab and entity catalog lengths are signed little-endian 16-bit counts, not padded bytes; this carries all 373 native UGC items. VIP sends gangster locks and ZOM sends phase-aware team/class locks. `team_headcount_type` is 6 by default (draws like retail `TEAM_SCORE_VALUE`); Zombie sends 0 (`TEAM_PLAYERS_COUNT_VALUE`, team player counts) -- per-mode retail values unrecovered. |
| 46 | KillAction | S→C | Sent | Broadcast kill/death event. `kill_count` is the killer's retail multikill chain (kills no more than `MULTIKILLMAXTIMEGAP` 6 s apart, `server/kill_feed.py`), not the life streak or the scoreboard kill total. `/kill` sends `CLASS_CHANGE_KILL`: the stock client clears the domination pair on `TEAM_CHANGE_KILL`. |
| 47 | GenericVoteMessage | both | Handled+Sent | Kick and next-map vote overlay open/update/close plus client CAST. The server sends exact opaque candidate records; the retail client binds the first three to F1/F2/F3. Title, description, **and every candidate** are exactly `repr((string_id, arguments_tuple))`: native `GenericVotingHUD.decode_string` literal-evaluates and indexes both elements, so a raw map name or historical one-item tuple crashes or is misread as `KICK_PLAYER`. |
| 48 | InitiateKickMessage | C→S | Handled | Client starts a kick vote → VoteManager (server/voting.py). The stock client validates nothing; a refused kick is answered with the retail LocalisedMessage(50) to the starter (`KICK_DENIED_FOR_SPECTATOR`, `_REASON_SELF_KICK`, `_REASON_KICK_HOST`, `_REASON_VOTE_IN_PROGRESS`, `_REASON_VOTE_TOO_SOON` {seconds}, `KICK_NOT_ENOUGH_PLAYERS`), which also closes the open kick menu after 0.5 s. Cooldowns per starter: 300 s, 45 s after a cancel (retail `MIN_TIME_BETWEEN_[CANCELLED_]KICK_VOTES`). |
| 49 | ChatMessage | both | Handled+Sent | Player chat is relayed only as ALL(0) or TEAM(1): a client-supplied SYSTEM(2)/BIG(3) is coerced to ALL, TEAM lines reach only the sender's team, text is cut to the client's `MAX_CHAT_MESSAGE_LENGTH` (200), each player has a token-bucket limit (burst 5, then 1/s; excess dropped silently), and recipients must know the sender's id (`known_player_lives`), so a dead joiner's chat never names an unknown id. Slash commands and the private `/__local_ugc_title` bridge are never relayed. Private system replies use type 2; global server/mode announcements use `CHAT_BIG` type 3 and render at the top of every retail HUD. |
| 50 | LocalisedMessage | S→C | Sent | Top-screen string-table announcement. Resolves `string_id`, optionally resolves every positional parameter as another localization ID (for example `TEAM1_COLOR`), formats `{0}`/`{1}`/`{2}`, and supports replace-previous behavior. See Broadcast templates below. Server-sent retail ids added 2026-09-28: `PLAYER_LEFT` {name} (before PlayerLeft 64), `COUNTDOWN_FROM_TEN` 9..1 after the 10 s cue, private `TEAM_SWITCH_WAIT` / `TEAM_SWITCH_NOT_ALLOWED` / `TEAM_LOCKED` / `TEAM_FULL` team-change refusals (menu packet 77 and `/team`) and `TEAM_FULL` to a joiner the join balance moved. `PLAYER_JOINED` keeps `localise_parameters` for the team id, so an identifier-shaped player name gets one trailing space (never a string-table key) and is not translated. |
| 51 | SkyboxData | both | Handled+Sent | Null-terminated retail mesh-environment filename (sent at join, prefix 0x30). It comes from the active VXL's validated sidecar `skybox_texture`/`skybox_name`; `[world].default_skybox` is the missing-metadata fallback. C→S only in the Map Creator: the host's UGC Settings choice is validated to a safe skydome basename and relayed to the other editors; ignored elsewhere. |
| 52 | MapEnded | S→C | Sent | Native full-scene rollover trigger. It freezes the compiled `GameScene`; the compatibility hook opens `LoadingMenu` and acknowledges readiness with `ClientInMenu(110)`. Only then may the server send a fresh loader handshake over the same authenticated peer. Same-map score presentation deliberately omits it. |
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
| 64 | PlayerLeft | S→C | Sent | Announce a player disconnect, only to in-game clients that were told about that id (a dead joiner was never created on peers); those clients then forget the id. Loading clients get it from roster catch-up on first ClientData. |
| 65 | ProgressBar | — | Reversed/blocked | Wire: fixed16 progress, fixed16 rate, colour1, colour2. Every value the fixed16 writer can produce shows the bar (no hide sentinel), and the stock client crashes on the first draw: hud.pyd `ProgressBar.draw` calls draw.pyd `draw_progress_bar` with 7 arguments (it takes 5) and the TypeError ends the reactor (live 2026-09-26, byte-identical to Steam). Never sent; `set_progress`/`clear_progress` are no-ops. See "ProgressBar (65)" below. |
| 66 | RankUps | — | Planned | XP/rank changes at map end (match lifecycle/progression). |
| 67 | GameStats | S→C | Sent | End-of-round scoreboard widget (server/scoreboard.py, on_mode_end). Per team, up to `NOOF_GAME_STATS_TO_SHOW` (3, server-only) `(player_id, stat_type)` rows drawn at random from every one of the 30 retail `GAME_STAT_TYPES` someone on the team earned this round (selection policy inferred; values are counted round evidence, `server/combat_scores.py`), listed in stat-id order. |
| 68 | UGCObjectives | S→C | Sent | Exact current/min/max/priority rows for shared and target-mode Map Creator requirements. |
| 69 | Restock | S→C | Sent | Resource-specific refill. Type 0 is the full-life spawn/general restock; a physical ammo crate must send type 3. Health (4), block (5), and jetpack (6) crates use their own paths. Sending type 0 for an ammo crate also restores client health. |
| 70 | PickPickup | S→C | Sent | Authoritative objective pickup; initializes carried tool and burden state. CTF removes the type-16 ground entity and enables the carrier's high-visibility minimap marker. |
| 71 | DropPickup | both | Handled+Sent | Client drop request validated against sender/current pickup, then relayed with authoritative identity, type, position, and capped throw velocity. DropPickup clears the carried tool but does not persist ground intel, so CTF follows it with a type-16 CreateEntity at the settled dry-ground position. |
| 72 | ForceShowScores | S→C | Sent | IDA (stock gameScene.pyd, body 0x101A0300 -> GameScene.force_show_scores): forced=1 opens the ViewScores menu through manager.set_menu, stops movement and locks the menu to the scene; forced=0 (clear_force_show_scores) returns control. LIVE 2026-09-24 (tracer client): the client shows its scores scene (frontend MenuScene host) while staying connected, and the in-place restart's forced=0 drops it straight back into the same GameScene. Sent at every round end and released by the restart (`lobby.end_round_scoreboard`, default true). |
| 73 | ShowTextMessage | S→C | Sent | IDA (body 0x101A0490 -> GameScene.show_text_message(message_id, duration)): only when the active menu is ViewGameStats does it call set_message, i.e. it is the headline of the terminal statistics screen. The nine ids (NEXT_MAP_MESSAGE..TEAM_SCORES_DRAW) select HUD-local strings (END_OF_MAP, TEAM_DEFEAT, BASE_DESTROYED, ZOMBIE_WIN, SURVIVOR_WIN, GAME_DRAWN). Sent right after ShowGameStats(53) on a map rollover (`lobby.end_round_headline`, default true); inert and therefore not sent on same-map restarts. Not a free-text overlay. |
| 74 | FogColor | S→C | Sent | Live fog override from the admin command. Initial fog comes from the active map sidecar in StateData; the runtime override also persists into later spawn/rejoin snapshots. Map Creator: a host skydome choice that is a `FOG_COLORS` key also sends 74 to every editor, host included (retail `set_skybox_name` 0x1012d4c0). Wire: one int32 LE `(r<<24)|(g<<16)|(b<<8)`, i.e. bytes `00 B G R`. |
| 75 | TimeScale | — | Reversed/unused | Handler sets `scene.time_scale = packet.scale`. Live 2026-09-26 at 0.25 the client kept its 60 Hz `loop_count` but `scene.time` advanced at a quarter rate, so its prediction runs slow while the server simulates at full speed. No retail sender; not sent and no admin command. |
| 76 | WeaponReload | both | Handled+Sent | Reload request handled; also sent as reload confirmation. |
| 77 | ChangeTeam | C→S | Handled | Client team switch request. Refused (private CHAT_SYSTEM notice) within 5 s of the player's previous accepted switch, and, when `[teams] auto_balance` is on in a mode where players choose teams (no `prepare_join_team`), when the target side already leads by `balance_threshold` (same rule as joiners). Mode `allows_team_change` locks still apply; spectating is never balance-refused. |
| 78 | ChangeClass | C→S | Handled | Client class switch request. |
| 79 | LockTeam | S→C | Sent | IDA (body 0x1019EB70): sets `teams[team_id].locked` and refreshes an open SelectTeam/SelectClass/ChangeTeam menu. Zombie sends it at the countdown/outbreak boundary so players already in the scene see the same locks StateData gives joiners. |
| 80 | TeamLockClass | S→C | Sent | IDA (body 0x1019F880): sets `teams[team_id].locked_class`; the HUD then refuses the class menu (ZOMBIE_OUTBREAK_CLASS_SELECT). Zombie locks the survivor team at the outbreak and unlocks it on round start; the server rejects survivor class changes during the outbreak. |
| 81 | TeamLockScore | S→C | Sent | Sets `teams[team_id].locked_score`; live 2026-09-26 the client then ignores team SetScore(85) rows for that team (player rows still apply). Sent by `/lockscore <1|2|all> on|off` via `server.hud_packets.set_team_lock_score`, which also sets `Team.locked_score` server-side, re-sends the team score on unlock, and replays to joiners (`reveal_hud_state`). StateData does not carry it. |
| 82 | TeamInfiniteBlocks | S→C | Sent | Sets `teams[team_id].infinite_blocks`; the client block, prefab, flare and snow-blower tools then skip their block-count checks (client `blockTool`/`blockToolCommon`/`prefabTool`/`tool` bytecode). Sent by `/infiniteblocks <1|2|all> on|off` via `server.hud_packets.set_team_infinite_blocks` (sets `Team.infinite_blocks`, which the deployable and prefab wallets already honour; replayed to joiners). |
| 83 | TeamMapVisibility | S→C | Unused | Sets the client's `teams[team_id].can_see_other_team` (a whole-enemy-team minimap reveal, no range). Radar does NOT use it: the stock Minimap detects enemies itself from each team RadarStationEntity (`can_detect_player`, 250 blocks), verified live 2026-09-26. |
| 84 | DisplayCountdown | S→C | Sent | HUD round-timer countdown (server/scoreboard.py, seconds remaining; a mode's `countdown_seconds_remaining` may substitute a phase clock: Zombie pre-outbreak, Demolition build phase). LIVE-VERIFIED. |
| 85 | SetScore | S→C | Sent | Lightweight mid-game team/player score update (HUD). PLAYER rows go only to in-game clients that know that id (`known_player_lives`); a departed id (forgotten after PlayerLeft) or an id reused by a new joiner is never scored. |
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
| 96 | DisableEntity | — | Reversed/unused | Handler: `if entity_id in scene.entities: scene.entities[entity_id].disable()`. Only `C4Entity` has `disable()` (sets `enable_explode=False`, live 2026-09-26); any other entity type raises AttributeError in the handler. Not sent: server C4 has no inert-but-present state (charges leave with their owner-bound cleanup). |
| 97 | PlaceUGC | both | Handled+Sent | Host-only raw-voxel placement/removal for all 19 Game Data items; range, tool, bounds, duplicate, and project-cap checks precede authoritative echo. A placement must rest on a top face (solid cell at z+1). A removal matches the newest placement within distance² ≤ 1 (edited-mode-visible first, then same item) and the echo carries the REMOVED placement's stored position and item. Every placement is mirrored as CreateEntity(21) type 29 for stock clients (see UGC Map Creator). |
| 98 | InitialUGCBatch | S→C | Sent | Bounded initial/reconnect replay of persisted UGC objects. |
| 99 | ReqestUGCEntities | C→S | Handled | Retail refresh request; spelling is native. Replays packet 98 plus packet 68 validation. |
| 100 | UGCMessage | C→S | Handled | Recovered editor control channel (Map Creator only): MAPINFO requests get the preview reply; host MAP_VALIDATION requests resend objectives and request a checkpoint; host CONVERT_TO_GAME (0, no shipped sender) is the explicit save request: the VXL and sidecar are flushed and only the requester gets LocalisedMessage(50) `UGC_MAP_SAVE_SUCCESSFULLY` / `UGC_MAP_SAVE_ERROR`; late REQUEST_VXL/NO_VXL source-map requests are ignored (the launcher answers them before MapDataValidation). The server never sends 100. |
| 101 | UGCMapLoadingFromHost | — | Reversed/unused | Local Steam-lobby host progress packet. Unsafe and unnecessary on dedicated direct connect; packets 54/56/58 provide the source map before validation. |
| 102 | UGCMapInfo | both | Handled+Sent | Optional bounded PNG preview exchange; disk checkpointing stays off the gameplay thread. |
| 103 | VoiceData | — | Planned | Voice-chat audio frames (voice). Out of scope: no handler, never relayed. The compiled codec decodes the payload as UTF-8 (corrupting); `protocol/runtime_packets.decode_runtime_packet(103)` keeps it as raw bytes for any receive/trace path. |
| 104 | PlaceFlareBlock | C→S | Handled | Flare tool 22 only; raw voxel-short coordinates, ten-block cost, contact/range validation, and coloured entity type 13 with late-join persistence. A successful `FLARE BLOCK` log is this packet, not ordinary BlockLine(40). |
| 105 | SteamSessionTicket | C→S | — | Steam auth ticket; received in handshake (not via register_handler). |
| 106 | TerritoryBaseState | S→C | Sent | Territory owner, attacker, action, and fixed16 capture amount; replayed on join and updated during capture. Retail Python 2 wire vector verified. Actions in `TC_DETAIL_NOT_REQUIRED` (ENTERING 3, LEAVING 4, CONTENDED 6, UNCONTENDED 7) are only `base_index, action` (3 bytes): the retail reader stops there, so the long form leaves trailing bytes the client parses as packet 3 (NoDataLeft crash). |
| 107 | DebugDraw | — | Planned | Debug draw primitives (dev tooling). |
| 108 | LockToZone | S→C | Sent | Six-short native movement clamp used for Demolition's build phase. Retail Python 2 wire vector verified. |
| 109 | HelpMessage | S→C | Sent | Localized tutorial HelpPanel rows with the packet's exceptional big-endian float delay. Retail Python 2 wire vector verified. |
| 110 | ClientInMenu | C→S | Handled | Client reports it's in a menu (handshake/idle gating). |
| 111 | Password | — | Planned | Password packet (auth). |
| 112 | PasswordNeeded | — | Planned | Server requests a password (auth). |
| 113 | PasswordProvided | — | Planned | Client submits a password (auth). |
| 114 | InitialInfo | S→C | Sent | First join packet: map filename, checksum, direct per-class movement scales, and a null-terminated `texture_skin` string. Each movement value is the complete 1/64-rounded authority scale; clients must not divide it by the class baseline. VIP sends `mafia`; the empty string selects the normal skin. |
| 115 | ForceTeamJoin | S→C | Sent | Map Creator sends team 2/instant 0 after loading so Start opens the native prefab/Game Data selector. |
| 116 | PositionData | C→S | Handled | Records the client-reported position and its drift from the authoritative body (diagnostics / tick stats `pos=`); only the non-default `movement_authority = "client"` mode lets fresh reports pin the body. |
| 117 | TeamProgress | S→C | Sent | Flag-controlled numerator/denominator or fixed16-percent objective row; Demolition sends authoritative base health. Retail Python 2 wire vector verified. |
| 118 | SetGroundColors | both | Handled+Sent | Complete UGC terrain/water palette from the host, persisted and replayed to editor guests. |

### Continuous movement phase and acknowledgement

Retail sends `ClientData(4)` **unsequenced**. `GameScene.send_client_data`
(`gameScene.pyd:0x1016AAE0`) passes `True` to `send_packet`, but on the wire
the 60 Hz stream is ENet `SEND_UNSEQUENCED` (measured 2026-09-24 with
`scripts/udp_lag_proxy.py --sniff`, see `scripts/enet_sniff.py`); a dropped
datagram is never retransmitted and delivery may reorder. `GameScene.update`
advances `loop_count` by exactly one per update (one physics step, one
movement-history row, one ClientData), and `process_packet_clock_sync` only
rewrites the counter when it is more than `MAX_CLOCK_SYNC_DIFFERENCE` (10)
loops from `server_loop_count + latency`. A gap of up to
`[debug] input_gap_fill_limit` (default 8) missing labels is therefore lost
input: authority refills each such frame with the held (latched) input, one
per tick, and acknowledges it under the missing label, so its step count keeps
matching the client's frame count. Wider gaps are clock jumps and are not
refilled. Without the refill one lost packet left authority one frame behind
for good, and the client corrected on every self row for the rest of the
run (docs/RETAIL_INPUT_LOSS.md). The handshake sends loop zero; a promoted
gameplay connection must continue from the session's next loop and never
reuse zero.

With the production default `movement_input_latch_frames=1`, frame L applies
locomotion, jump, sneak, and sprint sampled at L-1 while crouch and orientation
apply from L. `WorldUpdate(2).pong` is the accepted ClientData loop represented
by that owner row. Prediction must compare the row with the journaled state for
that exact loop, not with the current render transform. InitialInfo movement
entries are direct scales after all rule multipliers are composed and rounded
once to 1/64; authority calls the same `speed_scale(class, rule_multiplier)`
function so custom speed rules cannot create server/client drift.

### Proven movement code and compatibility limits

The original `world.pyd` core at `0x10012B80` stores movement scalars and
velocity writes as float32. The server follows its ordering for pack thrust,
ordinary jump, hover, crouch/sprint acceleration, opposite movement keys,
gravity, and drag. Active Engineer/UGC airborne acceleration uses 0.1 only
outside hover; crouching in water does not add a separate buoyancy branch.
Climbing and voxel contact determine the final airborne state. The peer
collision pass uses one predicted position for the entire peer list.

`tests/test_retail_binary_movement.py` compares exact values from 1,600 direct
x86 executions across 60/20 Hz steps, all pack types, water/contact states,
and control combinations. Regenerate its fixture with
`scripts/reverse_movement_core.py --binary <original-world.pyd> --output
tests/fixtures/retail_movement_core.json` (optional tooling dependencies:
`pefile`, `unicorn`). The script checks the original PE's SHA-256. It executes
the arithmetic through `0x1001304A`, before collision, with the CRT square-root
call replaced by x87 `fsqrt` and the control word explicitly set to `0x037f`.
The basis and starting velocity are fixed; deployed CRT/FPU settings and
arbitrary-direction normalization need separate verification. These vectors
do not establish collision, network scheduling, or fuel-policy parity.

`tests/test_retail_binary_movebox.py` separately checks 2,352 original x86
voxel-mover cases: sparse floors, walls, steps, corners and tunnels with varied
movement/contact flags. Regenerate with `scripts/reverse_movebox.py` using the
same `--binary`, `--output`, and optional `--dependency-dir` arguments. This
harness executes the original clipping branches, with sparse voxel lookup and
CRT floor supplied externally. It does not cover player collisions or the
complete Character/network wrapper.

`tests/test_retail_binary_peers.py` adds 2,000 original-instruction peer
collision vectors, including multiple peers and count-only clearance probes.
The vertical push uses the same float normalization and intermediate stores as
the original; replacing that calculation with just the sign changes rounding.
Regenerate with `scripts/reverse_peer_collisions.py` using the same arguments.
The full evidence scope and remaining gaps are in
[Retail movement parity](RETAIL_MOVEMENT_PARITY.md).

The client schedules network polling before each scene step. Foreground uses
1/60 second; its explicit limited-update mode uses 1/20 second. ClockSync can
relabel the client loop, so label gaps cannot be converted into physics steps.
Character history is recorded before native movement. Owner WorldUpdate rows
refresh position, velocity, fuel, and ability state even when pong repeats;
the active-state setter ignores an unchanged value, without restarting its
activity timer. Owner orientation, movement input, tool, and health fields
are skipped by this WorldUpdate handler.

The original Character also restores its last *received* network position on
every native `jump_this_frame`. The server's chosen queued-row anchor,
once-per-hold guard, and 0.25-block guard are compatibility policies, not a
recovered authoritative-server algorithm. `character_jump_smoothing.py` in the
maintained Python client adds that distance guard; captures made with it
enabled cannot verify unpatched retail behavior. The two-frame flight defer,
exhaustion tail, owner-row suppression, and original fuel timer ordering remain
unproven by the inspected client binaries. Resource constants are recovered;
the receiving client is not the source of the original server's resource
state machine.

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

2026-09-27: the same applies to every blast whose terrain `Damage(37)` type
is a key of the stock `ExplosionDamageManager.damage_functions` table (7-13,
15, 16, 18-24, 30, 33, 37-41; the stock `handle_damage` dispatches on the
type alone, oracle-verified). That packet is also each stock client's push
prediction, so the server sends it even when the footprint holds no solid
cell (every client derives the same empty footprint) and queues the push
exactly like the Snowball's (`server/main.py` `STOCK_EXPLOSION_DAMAGE_TYPES`).

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

These ids were BattleSpades-specific debug packets, not part of the original
1.x surface (no class in `packet.pyx`, so never decodable on the game channel).
Parity capture runs over `DebugParityManager`'s separate opt-in UDP side
channel (`server/debug_parity.py`); the dead game-channel handlers were removed.

| ID | Handler | Direction | Status | Notes |
|----|---------|-----------|--------|-------|
| 241 | DebugParityToggle | — | Removed | Not handled on the game channel; dropped with a rate-limited debug log. Use the parity side channel. |
| 242 | DebugClientSample | — | Removed | As 241. |
| 243 | DebugClientEvent | — | Removed | As 241. |

---

## Summary

- **119** packets defined in `shared/packet.pyx` (ids 0–118; id 21 is shared by
  `Entity` and `CreateEntity`, plus the `id: -1` base `Loader`/`AddServer` which
  are not wire packets).
- Registered handlers live in `server/handlers/*.py` (imported by
  `protocol.packet_handler`); unknown or unhandled ids are dropped with a
  rate-limited debug log; NewPlayerConnection(15), MapDataValidation(60), and
  SteamSessionTicket(105) also have connection-layer paths outside the
  decorator registry. Do not preserve hand-counted totals: editor isolation
  makes the active set process-specific, and the master table is authoritative.

---

## Packet feature-area notes

The active and remaining packet families are grouped here with their recovered
ordering and native-client hazards.

### Sounds
CreateAmbientSound (22), PlaySound (23), PlayAmbientSound (24), StopSound (25),
PlayMusic (26), and StopMusic (27) are implemented. Packet 25 stops a
server-owned loop by byte-sized loop id; its native missing-id path is a safe
no-op, so cleanup may be idempotent.

### Minimap / POI

CTF base zones use MinimapZone (43). Radar needs no extra packet: the client
detects enemies within 250 blocks of its team's RadarStationEntity itself
(TeamMapVisibility 83 is unused).
Objective modes clear packet-43 bounds with MinimapZoneClear (44). POIFocus
(18), standalone MinimapBillboard (41), and MinimapBillboardClear (42) remain
unused until a mode needs their distinct point/icon semantics.

### Voting / kick

`GenericVoteMessage(47)` drives both majority kick ballots and the stock
next-map overlay. Candidate text is an identity field, not a yes/no string:
the server accepts only an exact advertised candidate and rejects forged or
missing records. Title, description, and candidate rows all use the retail
localized-literal shape `repr((string_id, arguments_tuple))`; candidate rows
must never contain a raw map/player name. The CAST reply is matched against
the exact advertised wire token, then resolved to the server-side candidate.
The map catalog is captured at startup, and the final-minute
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
current `GameClient`, and sends `ClientInMenu(110)` as the explicit scene-ready
acknowledgement. The server arms that acknowledgement before packet 52 and
does not infer readiness from a fixed delay. Only acknowledged peers receive
`InitialInfo`; non-acknowledging peers are retired with reason 18 before any
crash-sensitive loader packet. Only after receiving
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

- Before outbreak, `StateData(45)` exposes the stock survivor classes
  (`DEFAULT_TEAM_CLASSES`, stock order, no Rocketeer) on team 2 and locks
  team 3. HeadCount type 0 shows the two teams' player counts.
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

### Territory control / mode rules

TerritoryBaseState (106) is active in Territory Control. LockToZone (108) and
TeamProgress (117) are active in Demolition, while MinimapZoneClear (44) is
shared by the objective-mode lifecycle. TimeScale (75), LockTeam (79),
TeamLockClass (80), TeamLockScore (81), and TeamInfiniteBlocks (82) remain
unused until a mode needs a runtime rule transition; StateData remains the
correct join/spawn snapshot. ProgressBar (65) is reversed but blocked because
its 1.x fixed16 codec cannot encode the receiver's legacy NaN stop sentinel.
ForceTeamJoin (115) remains active in the isolated Map Creator.

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

The stock GameScene has no receive handler for 97 or 98; it sees markers only
as entities. Every placement is therefore also a CreateEntity(21) with
`type = UGC_ENTITY (29)`, the raw voxel position, `face = 4`, `state` = the
wire team from `UGC_ENTITY_TEAMS` (neutral otherwise), `ugc_mode` = the
placement's mode id and `int_properties = [item_id]` (UGCEntity reads
`ugc_item_id = packet.int_properties[0]`; `create_entity` 0x10178b80 sets
`mode_placed_in = packet.ugc_mode`). Removal sends DestroyEntity(19). Both use
the per-connection entity ledger, and joiners receive the live markers once
after GameScene. The native client keeps drawing from 97/98 and ignores type
29. A placement's mode follows retail `get_ugc_mode`: crate points `nor`, the
Occupation bomb `oc`, zones the edited target mode.

Choosing a `FOG_COLORS` skydome sends SkyboxData(51) to the guests and
FogColor(74) to every editor. An accepted single-cell paint plays PlaySound
47 (`PAINT_PRIMARY_SOUND`) at the cell centre (unreliable, at most one per
editor per 0.1 s). Voxel additions (32, 40, 30 and the snowblower's 10) are
refused while the retail capacity is reached (solids ≥ 2,800,000 AND
non-empty 16³ chunks ≥ 3,200, vxl.pyd `is_space_to_add_blocks`). The explicit
save request is UGCMessage(100) `UGC_CONVERT_TO_GAME`; it is answered with a
private LocalisedMessage(50) `UGC_MAP_SAVE_SUCCESSFULLY` or
`UGC_MAP_SAVE_ERROR` (chat type 3, no parameters). Every save rewrites the
sidecar `tags` as `["map", <publishable mode codes>]` (retail
`set_tags_from_supported_gamemodes`) and keeps the edited mode in
`ugc_target_mode`.

The stock Publish Map screen enumerates only `./hosted_ugc/maps/*.ugc`; it does
not inspect `ugc/maps` or the server's standalone `ugc-projects` directory.
Client-launched authoring therefore supplies `--publish-root` pointing to the
client's `hosted_ugc` directory. Same-stem VXL/sidecar/preview files remain in
that catalog across sessions and are consumed directly by `aoslib.ugc_data`.

Retail stores the editable project title in its Steam lobby and exposes no
dedicated title packet. The maintained local client therefore sends the private
`ChatMessage(49)` command `/__local_ugc_title <text>`. The server must consume
this command without broadcasting or passing it to public command dispatch,
and accept it only from the current isolated-UGC host (80 characters maximum).
This bridge keeps the final server-side project checkpoint from overwriting the
title selected in the stock settings screen.

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

Terrain Damage is type-dependent and deterministic given the packet seed (see
the packet 37 row and `server/block_damage_model.py`, whose fixture
`tests/fixtures/block_damage_footprints_live.json` is the live capture). The
server applies the same per-cell damage through
`WorldManager.apply_block_damage` (cell health × `RULE_BLOCK_HEALTH`) and sends
one area packet—never one expanding packet per removed cell. A spade swing
therefore deals 5 to each cell of its column: map voxels (5) break at once,
player-built/prefab cells (9) need two swings on every client. Peers that do
not know a deployable's causer entity get the exact outcome instead (type-6
kills plus type-6 partial damage). Clients emit one presentation impact per
received live Damage even if no cell reaches zero health; late-join catch-up
must suppress those historical effects.

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

Normal Battle Builder death is an ordered three-stage transition. `KillAction`
creates the dead Character; `ExplodeCorpse(36)` changes `Character.exploded`
and activates `DeathController`; then `CreateEntity(21)` introduces the
player-bound `GRAVE_ENTITY` at the corpse's current position and velocity. The
ordinary corpse fuse is zero. Any selected jetpack uses the recovered 1.0s
fuse while retail `world.Player.update` applies its dead-body upward corkscrew
branch. During that bounded fuse, the server retains the otherwise-dead player
row in `WorldUpdate`; the native client accepts position/velocity updates for
the dead Character and therefore displays the flight before packet 36. The row
is removed at the corpse-to-grave handoff. `GraveEntity` then performs gravity,
bouncing, and collision locally; the server keeps a separate dry-surface
center for its seven-second blast.

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

For this Classic representation, packet 36 is never sent at death because it
removes the Character corpse that `KillAction` just created. A surviving corpse is removed with effect flag 0
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
omit 53. RankUps (66) remains planned (the Revival client has its own XP path); ForceShowScores (72) and ShowTextMessage (73) now carry the retail end-of-round scoreboard headline and hold.

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

### Auth (not usable with the stock client)
Password (111), PasswordNeeded (112), PasswordProvided (113). The stock
client's `LoadingMenu.packet_received` has the PasswordNeeded branch
commented out and `send_password_callback` sends `False` rather than a
packet, so these can never complete a join. Retail server passwords went
through the Steam lobby (`network.pyd` `SteamSendPassword`). Resource packs
(61-63) have no client handler in any module either.

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
client then locally matches `mode=%04d` and optional `region=...`. **This tag
is a server category, not a gameplay mode ID.** The original native filter at
`shared.steam.pyd` RVA `0x2320` tests individual `SERVERMODE_*` bits;
`ServerMenu` requests `SERVERMODE_PUBLIC=1`. Public dedicated servers must
therefore advertise `mode=0001` for TDM, CTF, Classic CTF, VIP, etc. TDM's
session mode remains 6. Actual gameplay is identified by the map prefix and
session packets. Advertising `mode=0006` makes the public filter discard the
row even after successful registration and A2S queries. It uses a
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

Registration and retrieval are separate checks. On 2026-09-20, the Windows
bridge logged on anonymously and Valve's public `ISteamApps/GetServersAtAddress`
registry returned BattleSpades with app `224540` and game dir `aceofspades`.
The old `hl2master.steampowered.com` hostname did not resolve, but that alone
does not establish failure of the Steam client API's list retrieval. The
retail wrapper calls `ISteamMatchmakingServers::RequestInternetServerList`;
its underlying transport depends on the loaded Steam runtime. Earlier notes
incorrectly treated the DNS observation as conclusive and overlooked the
server-category mismatch above. See [Steam discovery](STEAM_DISCOVERY.md)
for the corrected evidence and Windows setup.

### UI / messaging
ChatMessage (49) and LocalisedMessage (50) are active for retail top-screen
broadcasts. Because packet 49 never performs localization, its shared builder
resolves `TEAM1_COLOR`, `TEAM2_COLOR`, and `TEAM_NEUTRAL` to canonical display
text before serialization. ShowTextMessage (73) is fully reversed but intentionally unused for
free-form text because its byte is a fixed message enum. HelpMessage (109) is
active for the localized tutorial HelpPanel. The formatter contract and
variables are documented below. Which string ids the retail server sent (versus
ids the client renders locally) was settled by searching the client binaries;
the inventory and the per-mode mapping live in
docs/ANNOUNCEMENTS_RETAIL_2026-09-24.md.

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

1. Shipped binaries, verified IDA control flow, and original constant pools.
2. Direct execution of those binary paths with controlled inputs.
3. A clean retail client observed live, with active patches recorded.
4. Packet read/write layouts in `shared/packet.pyx`.
5. Maintained characterization tests and raw captures.
6. Reversed Python/Cython ports only as hypotheses.

For lobby data, import the constant module with the client's bundled 32-bit
Python 2 executable and print the obfuscated table directly. For native code,
record image base, function address, caller/callee, field offsets, packet
direction, binary hash, and exact reproduction. Keep decompiler interpretation,
direct binary verification, integration observations, and server compatibility
policy distinct. Agreement between our own client and server does not prove
retail parity.

Movement evidence must preserve the 60 Hz clock, input label, receipt tick,
owner send sequence, WorldUpdate stamp, and pre/post native state. Owner rows
are reconciliation events; observer rows are replication. Never tune both
sides simultaneously. Terrain evidence must record the originating input loop
and verify owner, observer, late join, and join-during-mutation views.

Crash-sensitive invariants include `InitialInfo` list shapes, compact player
IDs, entity create/destroy symmetry, map display names used for screenshots,
scene-terminal packets, localized-string tuple fields, and entity IDs used by
projectile effects. Change one only with a focused test and two clean clients.

## Unused Packet Audit

This is the evidence-backed review of packet definitions which are not part of
BattleSpades' normal runtime path. It complements the master table in
`PROTOCOL.md`; it is not permission to register every packet as client input.

### Audit method

Each packet was classified on four independent axes before considering an
implementation:

1. **Direction** — client-to-server, server-to-client, handshake-only, or
   bidirectional.
2. **Phase** — handshake, map transfer, GameScene, round transition, or editor.
3. **Authority** — request, authoritative state, presentation-only, or local
   client state.
4. **Framing and bounds** — exact field order, signedness, fixed-point format,
   count limits, and legal lifecycle teardown.

Evidence came from `shared/packet.pyx`, the clean retail Python 2
`shared/packet.pyd`, native `gameScene.pyd` receiver decompilation, recovered
server mode code, and the current handler/sender call sites. Golden vectors in
`tests/test_recovered_objective_packets.py` are produced from the clean retail
module rather than this repository's own reader.

### Findings implemented or corrected

| ID | Packet | Finding |
|----|--------|---------|
| 25 | StopSound | Native `process_packet_stop_sound` (`gameScene.pyd:0x1019CCD0`) resolves `loop_id` through the media manager and catches the missing-id path. BattleSpades now exposes validated global and per-player teardown helpers. |
| 44 | MinimapZoneClear | Already active in objective-mode lifecycle cleanup. Its six shorts are the exact packet-43 zone identity; the old catalog entry was stale. |
| 106 | TerritoryBaseState | Already active in Territory Control for join replay and owner/attacker/capture updates. Retail bytes match. |
| 108 | LockToZone | Already active during Demolition's build phase. Retail bytes match. |
| 109 | HelpMessage | Already active in Tutorial. The delay is the protocol's unusual big-endian float, followed by bounded null-terminated localization ids. Retail bytes match. |
| 117 | TeamProgress | Already active for Demolition base health. Both its flag byte and fixed16-percent variant match retail. |

All six are **server-to-client only**. Adding receive handlers for them would
turn presentation or rule state into an untrusted-client authority path.

### Reversed but blocked

#### ProgressBar (65)

The 1.x wire packet encodes `progress` and `rate` as signed fixed16 values. The
native receiver (`gameScene.pyd:0x1019E3A0`) still contains a legacy
`is_stopped()` branch which hides the HUD when progress is NaN. That sentinel
belonged to the older float32 packet:
the clean 1.x writer's `stopped` setter does nothing, and attempting to encode
NaN cannot produce a valid fixed16 packet.

Sending an active bar is not even safe. Live check 2026-09-26 (tracer dev
client; its `aoslib.hud.hud.pyd`, `aoslib.draw.pyd`, `gameScene.pyd` and
`shared.packet.pyd` are byte-identical to the Steam install): feeding
fixed16 progress values 0x0000, 0x0020, 0x0040, 0x7FFF, 0x8000 (-0.0),
0x8001, 0xFFFF and 0xFFC0 through the client's own reader and
`process_packet_progress_bar` always calls `ProgressBar.set` and leaves
`visible=True` (`is_stopped()` is false for every one), and
`ProgressBar.update(dt)` never hides a full or empty bar. On the next frame
`ProgressBar.draw` (hud.pyd, `progressBar.py` line 21) calls
`aoslib.draw.draw_progress_bar` with 7 positional arguments; draw.pyd's
function takes exactly 5. The `TypeError` escapes `HUD.draw` →
`GameScene.draw` → `GameManager.draw` and ends the pyglet reactor: the client
exits (`TypeError: draw_progress_bar() takes exactly 5 positional arguments
(7 given)` in the client stdout). The stock
client can therefore never display packet 65 at all. BattleSpades does not
emit it; `server.hud_packets.set_progress` / `clear_progress` are documented
no-ops (`PROGRESS_BAR_SUPPORTED = False`). Objective progress stays on
TeamProgress (117) and the localized announcements.

### Deliberately unused or unsafe

| IDs | Reason |
|-----|--------|
| 3 | Entity delta path has no recovered lifecycle that is safer than the active Create/Change/Destroy entity path. Packet 3 is also rejected at the final outbound boundary: a truncated five-byte instance makes the retail reader consume a missing short count and crash with `NoDataLeft`. |
| 96 | Only `C4Entity` implements `disable()` (sets `enable_explode=False`); any other live entity id raises `AttributeError` inside the handler. Server C4 is removed with its owner-bound cleanup, so no state needs an inert-but-present charge. |
| 14 | Roster replay uses CreatePlayer (28). `ExistingPlayer.pickup` is read signed, so 0xFF is a safe "no pickup" (`pickup_id=None`, live 2026-09-26); the packet is usable if the roster is ever moved to it (dead players/scores for joiners), but that is a roster change, not a HUD one. |
| 34, 39 | Packet 34 only reserves cells in `BlockManager.occupied_blocks` (an occupied cell fails `valid_to_add` for every client); it does not repair VXL topology and BattleSpades never holds a cell in a pending-build state that other clients must be kept out of. Packet 39 is a native no-op (the handler reads no field). |
| 73 | Selects one of nine compiled messages; it is not free text. Packets 49/50 own broadcasts. |
| 101 | Steam-lobby host progress only. It is unnecessary and crash-prone during dedicated direct-connect map loading. |

### Valid candidates when a real feature needs them

| IDs | Conditions before implementation |
|-----|----------------------------------|
| 18 | Sent by Demolition at the airstrike (see the master table). The LookAt lock has no expiry: only send it where a death, respawn or map change follows, and never to spectators. |
| 41, 42 | API ready in `server.hud_packets` (id-keyed add/replace/clear with late-join replay). Do not duplicate packet-43 zones (their icons already draw a minimap marker and world billboard) or entities that draw themselves on the minimap (crates, radar stations, C4, turrets, intel/bomb/diamond carriers). |
| 72 | Implemented and live-checked 2026-09-24: forced=1 at the win, forced=0 on the in-place restart (`lobby.end_round_scoreboard`). The client enters its scores scene and returns to the same GameScene on release; packet 73 (headline) belongs to the ViewGameStats screen and follows ShowGameStats(53) on rollovers only. |
| 81, 82 | Sent by the admin commands `/lockscore` and `/infiniteblocks`; runtime rule mutations only, no client packet may set them. 79/80 are now sent by Zombie at its phase boundaries (StateData keeps the join truth). |
| 75 | TimeScale scales the client simulation clock (`scene.time` ran at 0.25x while `loop_count` stayed 60 Hz, live 2026-09-26). Server physics does not follow it, so any value other than 1.0 desynchronises prediction. No retail sender is known; not sent. |
| 61–63 | Resource-pack transfer requires a separate phase machine, byte/count caps, checksum validation, acknowledgement correlation, timeout, and cancellation. |
| 66 | Rank progression needs persistent authoritative progression; a display packet alone is not a progression system. |
| 103 | Voice requires bounded codec/frame validation, rate limiting, routing policy, mute/abuse controls, and no gameplay-thread decoding. |
| 107 | DebugDraw must remain authenticated development tooling and server-to-client only. |
| 111–113 | Password challenge/response requires pre-GameScene phase gating, attempt throttling, constant-time comparison, and secret-safe logging. |

The priority rule is simple: implement a packet only when its full state
transition is recovered. A known byte layout without direction, authority, and
cleanup behavior is not an implementation contract.

## Optional BattleSpades cosmetic replication

`server/cosmetics.py` synchronizes equipped catalog IDs obtained from the Revival
master. It is an optional presentation service, independent of the authoritative
weapon, class, combat and movement systems.

Only `battlespades-cosmetics-v1` in the identity returned by consuming a bound
game ticket enables transmission. A client name, ENet version or Steam identity
alone never enables it. `Connection.send` enforces the recipient gate as the final
outbound check, including during loading.

The reliable envelope is byte 240, ASCII `BSC1`, then JSON containing `player_id`
and an `items` slot-to-catalog-ID map. Maximum size is 8192 bytes. World weapon,
class body/hat and tombstone slots are accepted; paths, scripts, model data and
gameplay attributes are absent. Empty items clear a player's previous outfit.
Retail Protocol 168 packets and client-to-server input remain unchanged.

The background service batches up to 32 verified account IDs per HTTPS request,
with a five-second request timeout and five-second refresh interval. Requests run
off the simulation thread. It resolves `Player.id` from live connections after
HTTP completes, sends changed snapshots to capable peers, and clears departed
players without transferring old cosmetics to reused IDs. Failed lookups keep
the last verified data; publication failures are logged and retried.

Tests: `tests/test_cosmetics.py`, `tests/test_revival_master.py`, and
`tests/test_connection_skybox.py`. Master/client deployment policy is owned by
those projects; this repository defines only the server wire/service contract.
