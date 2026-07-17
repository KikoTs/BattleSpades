# BattleSpades — Session Handoff (2026-07-09, evening)

## Current checkpoint — retail Map Creator (updated 2026-07-17)

- `run_map_creator.py` and `server/ugc_launcher.py` are the isolated third
  runtime. Normal `run_server.py` cannot register/select `ugc`, matching the
  tutorial isolation boundary.
- `server/ugc_project.py` recovers all nine terrain choices, the nine
  publishable target modes, all 19 Game Data object IDs, their exact per-mode
  requirements, and the retail `.ugc` sidecar fields. Projects are atomic
  `.vxl`/`.txt`/`.ugc` triplets; preview PNG, skybox, colors, objects, and
  validation state survive reconnect/restart.
- `modes/ugc.py` and `server/handlers/ugc.py` own the native editor handshake,
  one builder team/class, five combined backpack slots, raw-color construct
  build/erase, object placement/removal, mode validation, and autosave. The
  complete six-tab catalog is 138/90/47/47/26/25 = 373 entries.
- StateData prefab/entity counts are signed little-endian 16-bit values. The
  UGC source-map preamble is packet 54, zlib packet 56 chunks, then packet 58
  before ordinary MapDataValidation/MapSync. ForceTeamJoin(115) opens the stock
  selection screens after LoadingMenu Start.
- `PrefabActionService` snapshots packet 30/31 fields, prepares large retail
  KV6 models on one private worker, validates live contact in bounded slices,
  then uses the existing bounded commit queue. Worker code never touches world,
  player, or connection state.
- The prefab visibility/carve mismatch is closed. Retail VXL consumes
  `[from_block_index, to_block_index)`, so native echoes now use
  `0..model_block_count` instead of the zero-length `0..0` range. Packet 30
  uses raw voxel shorts, but packet 31 uses signed 1.6 fixed-point coordinates;
  `shared.packet` now preserves that retail asymmetry.
- UGC Super Spade primary is an exact type-29 cell and secondary is one native
  type-31 centered 3x3x3 action. Paintbrush accepts packet 7 or reconstructs
  held ClientData input; palette-open state no longer suppresses valid strokes.
- `[map_creator]` in `config.toml` selects the project/path, output directory,
  terrain, target validation mode, and retail asset root. The launcher prints
  the resolved `.ugc`/`.vxl`/`.txt` paths and reopen command. Metadata
  checkpoints asynchronously; the complete VXL is written only during clean
  shutdown.
- Clean-client evidence: all 373 catalog entries rendered; native selection of
  `UGC_Prefab_Desert_landscape_4` succeeded; all 115,080 raw-color voxels
  committed; packet-drain maximum fell from the old 410 ms stall to 0.90 ms,
  prefab commit peak was 4.29 ms, and the retail process remained alive.
- Focused UGC/combat/palette coverage is 82 passing after a native rebuild.
  A clean retail run built and erased `ugc_prefab_tree_stumpsmall` at
  `(112,269,223)` with 19/19 server commits; 18 newly visible client cells
  returned to air (the nineteenth authored cell overlapped terrain). A live
  primary spade packet removed exactly one client voxel, and a live secondary
  removed all 26 solid cells present in its 3x3x3 footprint. Paint with the
  palette active was previously verified as an authoritative packet-7 color
  change. Test terrain was not persisted.
- IDA evidence is retained in sessions `ugc-gamescene`, `ugc-vxl`, and
  `ugc-packet`: VXL range loop `sub_1002E7F0`; ErasePrefab read/write
  `sub_1005DE10`/`sub_1005E270`; fixed-short helpers
  `sub_10009520`/`sub_10041980`; BuildPrefab raw-short read
  `sub_1005D640`.
- The original menu Host branch assumes local Steam-lobby ownership. Forging
  that state on a direct connection crashes the client, so the dedicated
  launcher owns host authority/persistence while retaining the native in-game
  Construct and Game Data UI. Packet 101 is intentionally not injected into a
  live GameScene.

## Current checkpoint — 2026-07-16

- Match Lobby recovery is centralized in `server/lobby.py` and
  `server/game_rules.py`: ten public modes, selector presets, official map
  lists, and 102 visible/hidden rules.
- `config.toml` exposes every recovered rule. Unknown rules and illegal slider
  values fail validation. `InitialInfo`, class/loadout normalization, tool
  gates, combat, movement multipliers, block health/wallets, spawn protection,
  crates, CTF, VIP, Zombie, and voting consume the shared catalog.
- Multi-Hill, Territory Control, Diamond Mine, Demolition, and Occupation are
  registered scene-safe skeletons in `modes/lobby_skeletons.py`. Objective
  entities/scoring remain intentionally incomplete; do not call them playable.
- Plugin discovery now has an operator enable switch plus allow/deny lists.
- Project documentation is consolidated into eight maintained documents:
  README, CONTRIBUTING, ADMIN_GUIDE, ARCHITECTURE, GAMEPLAY, PROTOCOL, RUNBOOK,
  and HANDOFF. Vendored upstream notices are not project documentation.
- Focused Match Lobby/rule/mode/config tests are in `tests/test_game_rules.py`.

> Written for the next engineer/AI picking up mid-work. Read
> [CONTRIBUTING.md](../CONTRIBUTING.md) first (hard invariants + working agreements),
> then this file. The project is a Python 3.12 + Cython **1:1 recreation of the
> Ace of Spades 1.x (Battle Builders) dedicated server**, tested against the
> **compiled original client**. Ground truth = the live client, never the
> `aoslib-reversed` hand port (its physics/packet *layouts* are partly wrong;
> trust its *logic* only).

## Current-state addendum (2026-07-11)

This addendum supersedes the old performance, WorldUpdate-cadence, test-count,
and client-path claims below. The remainder of this handoff is retained because
its packet and map-sync investigation history is still valuable.

### Reconstructed retail tutorial checkpoint (updated 2026-07-17)

- The tutorial is intentionally absent from the normal `modes` registry.
  Source operators use `run_tutorial.py`; portable releases contain the second
  executable `BattleSpadesTutorial[.exe]`. `run_server.py` cannot select it.
- The dedicated launcher verifies the genuine retail `Training.vxl` SHA-256
  `aea9cc551f46d449324d24e6fbf0be0c11fc76286d1b00cff6cfe036e4e2114d`,
  registers mode `tut` only in that process, and locks all runtime settings in
  memory. It does not rewrite `config.toml`.
- Recovered client/map ground truth: mode ID 10; twelve repeated authored
  lanes; spawn `(140.5, 76.5, 230.75)` per lane; five 13-voxel red targets;
  staged pistol then block/spade grants; stock HelpPanel string IDs, target
  counter, tutorial music packet, completion sound 27, and countdown packet.
- The missing original server state machine is reconstructed in
  `modes/tutorial.py`: movement/jump/crouch geometry gates, authoritative
  target mutations, BlockLine completion, lane reset/reuse, invulnerability,
  and clean completion disconnect. The retail capsule reaches x=134.45 at the
  first obstacle, so the basic gate deliberately uses x<=135 rather than the
  unreachable raw voxel plane.
- Retail smoke reached Shooting through real W/Space/Ctrl input and displayed
  the translated stock prompt while packet 13 changed the loadout from `[]` to
  `[17]` and selected the pistol. The target-completion regression then grants
  `[17, 5, 2]` and selects the spade for Climb. The focused tutorial,
  packaging, and audio groups pass, and a staged Windows onedir bundle passed
  `--version`/`--check` for both executables.

### Map-owned skybox checkpoint (updated 2026-07-15)

- `Connection.send_skybox` no longer sends `Chicago.txt` for every map.
  Packet 51 now reads the active `WorldManager.map_metadata.skybox_name`.
- The recovered original server format uses a same-stem sidecar assignment
  named `skybox_texture`; retail UGC JSON uses `skybox_name`. The metadata
  loader accepts both plus JSON `skybox_texture`, parses legacy assignments
  with `ast` rather than executing them, and rejects paths/non-`.txt` names.
- The four shipped VXLs have same-stem JSON sidecars: Arctic Base uses
  `ArcticBase.txt`, Castle Wars uses `Classic.txt`, City of Chicago uses
  `Chicago.txt`, and 20th Century Town uses `WW1.txt`. All five relevant
  resources, including fallback `User_Grassland.txt`, exist in the stock
  retail client. Missing metadata uses `[world].default_skybox`.

### Map resources, fog, and authored flare lights (updated 2026-07-16)

- `MapResourceService` now owns map crates and static lights for every mode.
  TDM no longer has a private crate implementation, and CTF objective rebuilds
  remove only stale `base`/`intel` entities instead of clearing the registry.
- The original `.txtc` map modules prove that gameplay/environment data is a
  sidecar, not a VXL trailer. Dragon Island, Mayan Jungle, Spooky Mansion, and
  Trenches JSON sidecars now preserve their recovered fog, resource points,
  team spawn volumes, base volumes where present, and static-light colours.
- Physical ammo crates send `Restock.type=3`. Type 0 is reserved for the
  spawn/general restock and makes the retail Character refill health as well.
  Health, block, and jetpack crates call only their matching server path.
- IDA of `vxl.pyd` recovered the exposed blue/green chroma-marker deletion.
  Runtime VXL records the marker family while removing its collision voxel;
  the map service turns metadata-authorized markers into neutral coloured
  type-13 entities. This is the native `FlareBlockEntity` point-light path and
  automatically participates in late-join entity reveal.
- The shipped `ugc/maps/GrasslandBaseplate.txt` authoring template supplies
  the missing stock defaults exactly: family 0 `(255, 255, 82)` and family 1
  `(250, 250, 200)`. Recognized stock maps inherit those values, while an
  explicit sidecar overrides its own slot (Mayan Jungle remains amber).
  Unknown/community maps never inherit a guessed palette; missing families are
  skipped with one aggregate warning.
- `FlareBlockEntity.post_initialize` does two things in the native client: it
  creates the RGB user block and registers a radius/intensity `5.0` static
  point light. `MapResourceService` now mirrors the first operation in server
  collision without dirtying the map/reconnect mutation journals. This closes
  the former client-solid/server-air cell mismatch at recovered markers.
- A full audit loaded all 28 bundled VXLs: 703 exposed native markers across
  10 maps, zero unresolved families. The largest case, 20th Century Town,
  joined a clean retail `GameScene` with all 524 flare entities (IDs 9..532),
  533 total map entities, no traceback, and steady entity tick work around
  `0.07 ms`. Arctic Base delivered its exact 19/23 dual-family split; a live
  before/after removal capture visibly changed the recovered lit area from
  bright to dark.
- Retail CTF validation on Mayan Jungle joined cleanly with 7 ammo, 7 health,
  7 block crates, 4 `FlareBlockEntity` lights, both intel pickups, fog
  `(69, 76, 39)`, and `MayanJungle.txt`. The process remained in `GameScene`
  and crash dumps stayed 15 -> 15. Focused adjacent coverage: 188 tests plus
  the 31-test stock-map/resource group passed.

### Stock ambience and mesh-resource checkpoint (updated 2026-07-15)

- `mesh/<name>/<name>.txt` is a client-owned environment manifest. It places
  sky, cloud, mist, wave, sun, and similar render objects with translation,
  scale, rotation, and optional UV animation. Packet 51 selects that manifest;
  the mesh directory is not collision geometry and is not map-sync data.
- `MapMetadata.official_map` and `STOCK_MAP_SKYBOXES` identify shipped VXL
  basenames and their bundled presentation aliases. This never changes map
  transfer behavior. Retail checksum experiments proved that stock maps still
  require the full canonical VXL stream; empty or delta-only joins produce a
  hollow/desynchronized world.
- Original `ambient_sounds` rows are `[name, points, volume, attenuation]`.
  Empty points mean a global bed; non-empty points are local emitters such as
  Mayan Jungle's four `em_river` locations. The server accepts only the 21
  ambient assets present in the retail installation.
- The native sequence is `CreateAmbientSound(22)` followed by
  `PlayAmbientSound(24)` with the same loop ID. Packet 22 only registers the
  `AmbientSound` controller. Packet 24 allocates the streaming `GameSound`.
  Local loops bootstrap at the listener so the media manager cannot reject a
  distant initial source; the native controller then moves the loop to the
  closest authored point.
- Remote block-tool observers now receive a positioned `PlaySound(23)` cue.
  The actor is excluded because the retail client predicts its own impact.
  Spade, pickaxe, knife, Super Spade, zombie hands, crowbar, UGC mining tools,
  and the safe machete fallback use recovered `SOUND_MAP` IDs.
- Isolated Mayan Jungle retail validation created two live non-closed media
  players: a relative global `amb_jungle` bed and an attenuated positioned
  `em_river` stream. No playback failure, traceback, or new dump appeared;
  crash dumps remained 15 -> 15. The focused suite passed 92 tests.

### CastleWars water recovery and retail rollover checkpoint (updated 2026-07-15)

- CastleWars contains 72,306 water columns, and its farthest reachable water
  is 132 columns from shore. The worker's old local search defaulted to 24 and
  hard-capped at 64, so a genuinely stranded bot could never receive an exit
  route. `VoxelActionPlanner` now builds a full-map water-to-dry flow in the AI
  process, caches each next step, and selectively invalidates it only when a
  terrain delta touches a cached route. Dry bots still reject water entry;
  already-wading bots may follow the recovery route.
- `bot_city_soak.py --map CastleWars --mode tdm --bots 2 --sim-seconds 60
  --report-every 30 --strand-water-bots 2` injected both bots into the farthest
  water. Both began in `water_recovery`, reached dry terrain by 60 simulated
  seconds, and finished with `water_remaining=0`, zero action/jump loops,
  navigation stalls, invalid looks, or priority inversions.
- Retail packet 52 is the native full-scene boundary. IDA resolves
  `GameScene.process_packet_map_ended` near `0x101A07F0`, which calls
  `GameScene.on_map_ended` near `0x101A08C0`. That handler only freezes the
  scene; reason 18 is terminal and the retail build does not auto-reconnect.
  Map and mode replacement now preflight first, flush packet 52, retain the
  authenticated peer, and perform the fresh loader handshake after
  `client_patches/session_transition_patch.py` opens `LoadingMenu`. Invalid map
  names do not disturb the current GameScene.
- `GenericVoteMessage(47)` is also the stock map-vote path. The retail client
  binds the first three advertised records to F1/F2/F3; IDA places the receive
  path near `0x1017B780` and the cast sender near `0x1017C6C0`. The server now
  accepts exact candidate records instead of the former `starts with y`
  heuristic, stages the winning map, and consumes it only after the end screen.
  The map catalog is cached at startup, so the 60 Hz tick performs no glob I/O.
- Focused water/transition/vote/end-sequence checks and the adjacent bot/mode
  suite pass. A clean retail client live-validated CityOfChicago -> ArcticBase
  and TDM -> CTF on the same ENet peer, with no disconnect, traceback, crash
  dump, or map-transfer failure. A clean two-retail-client visual vote remains
  the final release gate.

### Remote player/bot desync checkpoint (updated 2026-07-15)

- Peerless bots previously left every WorldUpdate row `pong` at zero because
  they have no ClientData acknowledgement clock. The native client deduplicates
  Character movement by that row field, including remote players, so it
  accepted one bot snapshot and extrapolated stale velocity until respawn.
  `ReplicationService` now stamps bot rows with the monotonically increasing
  authoritative server loop. Human owner rows still use their consumed client
  loop and are unchanged.
- The repeated local five-block rollback in the three-client rig was a second,
  independent native-client identity bug. All retail installs used the same
  name (`KikoTs`); a later same-name CreatePlayer stole the first client's
  local association, and its no-id movement packets then moved the original
  server player to the new player's spawn. Human names are now made unique,
  case-insensitively and within the 15-byte retail field, before any roster
  packet (`KikoTs`, `KikoTs~2`, `KikoTs~3`).
- `scripts/scenarios/remote_replication_stress.py` launches three visible stock
  clients and samples one owner plus every bot from two independent observers.
  The broken capture under `logs/live-desync-20260715-botcombat2/` measured
  31.50-block bot observer disagreement and 6.77-9.89 blocks per loop. The
  corrected same-name capture under
  `logs/live-desync-20260715-botstamp-unique-names/` passed with zero missing
  Characters, stale moving snapshots, or teleports; maximum human disagreement
  was 1.38 blocks and bot disagreement 1.11 blocks.
- The bundled example plugin's documented awaitable `broadcast_message` API
  has been restored. Kill streaks no longer throw/log a traceback on the
  gameplay path. Final regression gate: `705 passed in 87.96s`.

### CTF objective/minimap checkpoint (updated 2026-07-14)

- CTF bases are native `MinimapZone(43)` zones with `ZONE_ICON_CTF` (6),
  not `BASE=1` entities. Authored UGC base extents are sent exactly after the
  map's source-Z shift; voxel-only maps use the recovered five-block classic
  capture bounds around each dry team anchor. Capture tests the same visible
  XY box, so the HUD marker and scoring volume cannot drift apart.
- Ground intel is a persistent `INTEL_PICKUP=16`, whose native object has
  `minimap=True`. Pickup destroys that entity and enables
  `ChangePlayer(17)` action 8 on the carrier. Drop, death, disconnect, capture,
  and restart clear that marker. `DropPickup(71)` only clears the carried tool;
  the server must follow it with a new type-16 `CreateEntity(21)` at the
  authoritative dry-ground position. Abandoned intel returns after the retail
  60-second timeout.
- `BattleSpadesServer.reveal_world_to` calls the mode-owned `reveal_to` hook
  after generic entity reveal. A late join therefore receives both base zones
  and the current carrier marker even when generic entity wire replication is
  disabled.
- Live isolated validation on UDP 27019 used an unpatched retail GameScene.
  A clean join held exactly two native `MinimapZone` objects with billboards
  and two `IntelPickup` entities with `minimap=True`. `/restart` kept the same
  GameScene, retained exactly two zones (no duplication), recreated both intel
  entities, and produced no client traceback. Logs are under
  `logs/ctf-minimap-live` and `logs/ctf-minimap-probe`.

### Gangster VIP checkpoint (updated 2026-07-14)

- `modes/vip.py` is a real sub-round state machine, not the unrelated generic
  high-visibility state. Both teams are class-locked to Gangster 1-4, then one
  random player per team is promoted to the team-specific boss class 10/11.
  `ChangePlayer(17)` action 8 owns the native crown/through-wall marker.
- A dead VIP immediately disables only that team's respawns. If the other VIP
  remains alive, its team keeps respawning; killing both VIPs locks both teams.
  Eliminating the last living member of a VIP-less team scores one of three
  sub-rounds. VIP disconnect uses the same death transition. Intermission
  demotes old bosses, respawns ordinary gangsters, and performs a fresh random
  selection without leaking the previous boss class into CreatePlayer.
- `InitialInfo.texture_skin` is a null-terminated string, not padding. The
  corrected Cython writer sends `mafia`; both the rebuilt Python 3 reader and
  the actual retail Python 2 packet module decoded the 274-byte packet as
  `u'mafia'`. StateData sets both native `locked_class` bits.
- Live validation used three retail clients on isolated UDP 27019. Both clients
  saw mode 7, skin `mafia`, boss classes 10/11, and both visibility markers.
  A third client requesting Miner was coerced to ordinary Gangster class 7.
  Blue VIP death stayed dead beyond the normal timer while its guard remained
  active and green retained its marker; guard elimination scored green. A new
  sub-round respawned/demoted everyone and selected fresh bosses. Terminating
  the green VIP client scored blue after ENet disconnect. Crash dump count
  remained 14 and the full suite is 640 passing.

### Zombie Infection and Glide Jetpack checkpoint (updated 2026-07-14)

- `modes/zombie.py` implements retail mode 2 as an authoritative phase machine:
  wait for two players, preparation countdown, Patient Zero selection,
  permanent conversion on every later survivor death, zero-delay zombie
  respawn, survivor timeout victory, periodic retail scores, last-survivor
  action-8 marker, and replacement when the sole zombie disconnects.
- Initial infection is capped at population minus one. The 600-second clock is
  reset at outbreak, so an idle server cannot instantly time out when players
  eventually join. Active-round joins are forced to team 3/class 4 even when
  the client requests the survivor team or another loadout.
- The stock picker receives only base Zombie. Fast/Jump Zombie constants are
  retained but hidden because their normal class-picker presentation is not
  stable in this client. Survivors receive the standard classes plus class 2
  Rocketeer.
- Three retail clients on isolated UDP 27019 validated the packet flow. After
  a two-second validation-only countdown, one client was team 2/class 2/tool
  67 with `high_minimap_visibility=1`; the other was team 3/class 4. A third
  late client requested team 2/class 2 and spawned as team 3/class 4. The
  survivor roster agreed on all roles and markers. Stable ticks were about
  0.27-0.34 ms with zero position-correction packets; no dump was created
  after the validation server started. Log: `logs/zombie-retail-validation/`.
- That run also exposed a one-time 268.45 ms first-TEAM3 spawn scan in the
  respawn subsystem. `WorldManager.load_map` now prewarms both spawn candidate
  caches during startup/background map preflight; Patient Zero no longer owns
  the terrain scan on a live 60 Hz tick. CityOfChicago measured 0.175 ms
  average / 0.270 ms maximum for 50 runtime spawn selections after prewarm.
- “Legacy Engineer” is the client's Rocketeer class. IDA confirmed Jetpack2
  tool 67 uses thrust 0.0125 at `world.pyd:0x10012C47`, versus Engineer tool
  68 at 0.020. The existing mover branch was correct and is now regression
  tested as a low-altitude, long-endurance glide. Live W+SPACE activated tool
  67 through the original input path and moved horizontally without climbing.
- Final regression gate: `652 passed in 86.32s` on 2026-07-14. The
  human-facing `zombie` alias is normalized to retail code `zom`, so both
  `/mode zombie` and `default_mode = "zombie"` still send native mode id 2.

### Verified landing, flare, and Snowball checkpoint (updated 2026-07-14)

- Packet-13 prefab loss was an inbound LZF decoder error. LZF stores
  `distance - 1`; `server/util.py` omitted the restoring `+ 1`, so real client
  selections arrived as strings such as
  `prefab_superpoleprefab_sfort_wall` plus an empty third slot. Validation then
  removed the damaged names and the client appeared to restore default prefabs.
  The strict decoder now preserves all three names and caps invalid/truncated
  streams. `tests/test_lzf_codec.py` contains a byte-for-byte vector generated
  by the retail 32-bit `shared.lzf.pyd`, and the spawn-handshake suite exercises
  the complete `0x31 -> LZF -> SetClassLoadout -> pending_selection` path.
  A clean retail run on isolated port 27019 selected the non-default Engineer
  set `superdome/superbridge/platform`; the receive trace, cached selection,
  outbound CreatePlayer, and spawned client's `current_class.prefabs` all held
  those same three ordered names. No new crash dump was created.
- The unwanted one-block tile at the start of the prefab selector was native
  `SelectClass.get_class_images` injecting `FLAREBLOCK_TOOL (22)`, not a prefab.
  Tool 22 is now advertised in `InitialInfo.disabled_tools` and omitted by the
  same default class normalizer, so it cannot reappear in CreatePlayer while the
  real Prefab tool 23 and its three selected names remain available.
- Settled-client terrain has bounded regular and collapse-confirmation lanes in
  `server/terrain_repair.py`. Rejected/cancelled predictions use the original
  two-second canonical replay. Successful builds, prefabs, and ordinary digs
  are not replayed because packet 33/37 would run native particles twice.
  Unsupported collapse is the evidence-backed exception: checked Damage asks
  each client to derive a component locally, so every server-removed collapse
  cell is confirmed later with exact type-6, `chunk_check=0` Damage. The queue
  drains visible shells first, drops rebuilt cells, and stays globally bounded.
  Joining clients remain gated out and use the full snapshot plus contiguous
  mutation journal. Packet 38 is not a terrain replacement mechanism; reverse
  engineering shows only damaged/occupied/user-block dictionaries.
- Engineer has exactly one numeric jetpack resource. Its CLASS_EQUIPMENT slot
  is `JETPACK_ENGINEER OR DISGUISE`, never both. Rocketeer's independent legacy
  slot is `JETPACK2 OR JETPACK_NORMAL`; Jetpack2 uses the existing per-pack
  fuel/SPACE-thrust properties. Spawn derives a pack only from the normalized
  active loadout and never appends a class fallback. HUD IDA found one fuel bar
  and one `jetpack_fuel`; the WorldUpdate row has one fuel short followed by
  spawn-protection and deployment-yaw shorts. The apparent second cylinder is
  static icon artwork. `jetpack_passive` is a separate boolean and remains off
  until its probable action-bit path is marker-replayed; adding another short
  would corrupt the stock packet tail.
  Retail validation is clean for both ownership branches: Engineer pack 68
  activated and drained with zero ADJUST/SNAP/visible rollback in
  `logs/jetpack-retail-stress/equipment-slot-fix-pack68/movement-stress-20260714T125101.609230Z.json`;
  Rocketeer Jetpack2 67 did the same in
  `logs/jetpack-retail-stress/jetpack2-rocketeer/movement-stress-20260714T125222.242837Z.json`.
  An Engineer selection containing Disguise 64 returned `NO_JETPACK (65)` to
  the live client, proving the two equipment choices no longer overlap.
- Specialist Machete is a three-strike, two-voxel dig. Native
  `BlockManager.handle_machete_damage` (`gameScene.pyd:0x1008AA60`) calls the
  single-block damage helper for `(x,y,z)` and `(x,y,z+1)` with damage 2. One
  type-35 Damage packet must be broadcast per accepted strike; per-cell packets
  would self-expand twice. Legacy BlockLiberate is rejected for this tool so a
  retail ShootPacket swing cannot be applied twice.
  The clean retail replay is
  `logs/machete-retail/two-voxel-fix-v3/movement-stress-20260714T130213.682489Z.json`:
  three real swings produced three reliable type-35 Damage packets, the
  client stayed in GameScene, and reconciliation recorded zero corrections.
- The known Win32 mouse-jitter fix was ported to both
  `G:/AoSRevival/aceofspades_decompiled` and the current retail validation
  client. It consumes `WM_INPUT` relative deltas instead of Pyglet 1.2's
  cursor-warp `WM_MOUSEMOVE` path. `+legacymouse` is the runtime fallback.
- Miner tool 3 is a Super Spade, not the normal three-high spade. Retail
  `handle_superspade_damage` (`0x10082C90`) starts at `(hit-1)` with extent 3;
  a direct packet-37 probe removed every existing voxel in the centered
  axis-aligned 3x3x3 cube. Both Shoot and legacy BlockLiberate now commit that
  canonical footprint and broadcast one type-3 Damage, while normal spades
  retain a z column and pickaxes remain single-cell. Per-cell type-3 packets
  are forbidden because every native handler would expand each one again.

- Historical logs such as `FLARE BLOCK ... cost=10` were not ordinary block
  packets being decoded incorrectly. They proved the retail client selected the
  injected flare tile and sent `PlaceFlareBlock(104)`. Tool 22 is now disabled
  by default to remove that misleading selector entry; ordinary block tool 5
  remains first and sends `BlockLine(40)` at a cost of one block.
- The remaining block -> sprint -> jump correction was an exact native landing
  mismatch, not ENet delay. Retail enters its shared landing branch whenever
  post-`boxclipmove` velocity Z is zero (`world.pyd` gate `0x100130F7`), including
  a terrain-step glide that the helper reports as a climb. The collision
  intermediates must remain float32, and a severe landing above `0.8 / gravity`
  halves X/Y exactly. The clean post-fix retail artifact recorded 1,200 samples,
  a real block mutation, zero ADJUST/SNAP/rollback, and 0.000031-block maximum
  matched error:
  `logs/movement/solo-block-after-landing-fix/run-1/movement-stress-20260713T030204.777214Z.json`.
  The pre-fix comparison is
  `logs/movement/solo-block-current/run-1/movement-stress-20260713T023430.198147Z.json`.
- `DestroyEntity(19)` only removes a Snowball visual/effect. Native
  `process_packet_damage` at `gameScene.pyd:0x1018C270` feeds the explosion
  manager, which owns the client-predicted blast impulse. The Snowball path
  sends one reliable, zero-damage `Damage(37)` with type 20 and the exact impact
  position **before** `DestroyEntity(19)`, while `causer_id` still names the
  projectile. Entity id 0 is valid. This transient event is never written to
  the late-join map-mutation journal.
- The same Snowball visual belongs to retail tool 29, whose final English name
  is **Block Cannon**. Its recovered weapon consumes `Character.block_count`,
  not a separate magazine. At admission the server snapshots palette RGB and
  the client loop, publishes a coloured type-24 entity, and on world contact
  commits the last free supported voxel before sending
  `BlockBuildColored(33)`, Damage(37), then DestroyEntity(19). The build is a
  real VXL mutation and participates in join catch-up; Damage stays transient.
  A clean-process reconnect reproduced exact RGB `0x2468AC` at `(274,256,231)`.
- `SetColor(11)` must be accepted while tools 29 and 48 are held. Both retail
  Snowblower classes activate the ordinary HUD palette and use shared
  `Character.block_color`; restricting the handler to block/flare tools left
  all Block Cannon impacts at stale `0x707070`.
- Native packet processing applies Damage before the frame's scene/Character
  update (`process_packet_damage` `0x1018C270`; GameScene update core
  `0x10149CF0`). Authoritative projectiles therefore advance before players;
  generic entities and turrets retain their post-player schedule. The timing
  witness is the target player's **dense accepted-ClientData sequence**, not
  the server loop or sparse client loop label. At impact, queue the Snowball
  origin/falloff parameters for the third subsequently accepted ClientData
  frame, then recompute direction, falloff, and crouch scaling from the
  authoritative state immediately before that frame's physics step. Do not
  freeze the impact-time impulse vector.
- The rejected timing experiments are preserved as evidence: server-loop
  labeling was nondeterministic (two ADJUST and 0.373261 maximum error in one
  1,077-sample run, while a shorter repetition happened to pass); the current
  server-loop repetition had two ADJUST and 0.400019 maximum error; fixed
  `L+2` had three ADJUST and 0.301891 maximum error; and two accepted
  ClientData frames plus application-time recomputation still had two ADJUST
  and 0.384826 maximum error. See the corresponding
  `logs/combined-replication/snowball-loop-*`, `snowball-loop-plus2-live`, and
  `snowball-sequence-recompute-live` artifacts. These approaches are
  superseded; do not tune them back in from a single lucky run.
- The accepted three-frame design passed the clean two-client retail gate:
  719 samples over 11.985602 seconds, zero ADJUST, SNAP, visible rollback,
  stall, or unmatched samples, 0.000076 maximum and p95 matched error, and
  0.008209 maximum backward step. Evidence:
  `logs/combined-replication/snowball-sequence3-final-live/20260714T014849/scenario-run-1/movement-stress-20260713T224938.344225Z.json`.
  The pinned source SHA-256 values, stable before and after the run, are
  `server/main.py=5FCE093AB5F18E45119B4D6C5F9E379A158AACD8ACB48B70DFA4F568774AC998`,
  `server/player.py=A255EBE576236CE2FCA656A65A51B625DAFB37CA06F3BF8A0D8B950818416469`,
  and
  `server/simulation_runtime.py=7AA38B827B60B7BACAB228692BD110CF8598DEC0AE113EA21873F95FCC1EA217`.
- Disconnect now retires owner-sensitive state synchronously before the small
  numeric player id is reusable. It purges that connection's queued gameplay
  packets and rechecks both peer-to-connection and id-to-Player object identity
  at drain time (the generation guard for a batch tail that survives an
  earlier await). It cancels pending world mutations, projectiles, rocket
  turrets, fire ownership, combat cadence, vote identity, and replication
  cadence; destroys owner-bound deployables/medpacks/graves; and retains
  ordinary construction/objectives. An owned machine gun is destroyed, while
  a foreign gun merely carried by the departing player is unmounted and kept.
  Radar removal decrements the owning team's station count through the normal
  visibility path. This distinction prevents stale credit or producers from
  attaching to a replacement player without erasing persistent world state.
- A validation-only aggregate ENet `OwnerTransitionCoordinator` was removed.
  `reliableDataInTransit` can acknowledge a coalesced transport batch, not a
  particular WorldUpdate's GameScene application, and the experiment regressed
  Engineer jetpack behavior. Keep the bounded causal owner-row history; do not
  recreate an application ACK from ENet counters.
- Current live movement/entity runs remain far below the 16.67 ms frame budget:
  representative total-tick maxima were below 2 ms, including projectile work.
  The reproduced corrections were protocol/physics ordering defects, so a broad
  Python-to-Cython rewrite is not the root fix. The Snowball/order retail gate
  is now closed by the pinned artifact above; keep the full regression and
  capacity gates separate rather than inferring them from this scenario.
- Late-weapon retail validation covers Auto Shotgun ammo/trigger, chemical type
  32, GL type 33, sticky type 34 and five-second removal, Mine Launcher type 37
  to armed type 9, and two-client disguise rendering. GL must stay on
  CreateEntity: relaying packet 10 enters a stale `GLGrenade.__init__` path and
  raises `Entity.initialize()`'s four-versus-five argument TypeError.
- Molotov fire is stable only when BlockFire's wire face is `FACE_TOP=4`.
  BlockFire is particle-only and has no `model`; faces 0/1/2/3/5 make the base
  Entity rotate that missing model and tear GameScene down. The server may use
  a side-adjacent anchor internally, but the serialized face must remain 4.

### Movement, palette, and Engineer checkpoint (2026-07-13)

- Owner-anchor history is now an ordered, duplicate-preserving queue. IDA proved
  that the retail local-player WorldUpdate path uses `force_update=True`, so two
  rows with the same pong stamp are two real cache writes rather than one
  replaceable dictionary entry. Each queued owner row records both its server
  tick and its monotonic send sequence; a jump sourced by ClientData J may only
  restore a row with `pong < J` that was queued before J reached the gameplay
  thread. This is a necessary causal exclusion rule, **not** an application ACK.
- The WorldUpdate header loop and player-row pong are independent clocks. In a
  deliberate `pong = header - 7` retail probe, 62/62 local cache transitions
  followed row pong and 0/62 followed the header. Production therefore sends
  the current global loop in the header and each player's last applied input
  loop in that player's row. Self recipients share one base serialization even
  when their row pong stamps differ.
- The exact launch trace recorded seven jump decisions. Five selected the exact
  row stamp later observed in the client; two selected a row two loops newer,
  but both positions were stationary/equal. The run had zero SNAPs, zero visible
  rollback, and zero backward steps. Its two soft ADJUSTs (maximum 0.600308)
  occurred earlier on the terrain route, not on the traced launch decisions.
  Evidence: `logs/causal-owner-live/anchor-trace/decisions.json` and the adjacent
  movement artifact.
- A separate fixed-spawn class-12 run completed an ordinary `jump_run` with zero
  corrections, placed a real block (inventory 2000 -> 1999), and had zero SNAP
  or visible rollback. Its 13 remaining soft corrections began only after
  Engineer jetpack activation; server tick maximum was 0.55 ms, excluding
  server saturation as the cause.
- Clean reliable/unreliable jetpack A/Bs do not justify changing production
  transport. All eight fresh Engineer lives started with fuel 100 and activated
  correctly. Reliable transition rows produced 2 soft corrections across four
  stationary activations; unreliable produced the same count with a worse peak.
  Four-second holds consumed fuel 100 -> 24.90625 in both variants, and modest
  forward flights were identical with zero ADJUST/SNAP. Keep the reliable
  transition plus immediate ENet flush; neither is a GameScene application ACK.
- Engineer release had one real server phase bug: after a consumed SPACE
  release, `_jetpack_physics_active` retained one extra thrust/fuel recurrence.
  Native GameScene ordering and the exact retail hold/release capture both show
  physical key-up stops thrust immediately, even while the previous received
  WorldUpdate still advertises action bit `0x04`. The consumed-frame release
  now clears native thrust immediately. A tempting attempt to apply packet L's
  release ahead of the ordinary input latch was rejected live: it created a
  deterministic ballistic correction about 31 loops after activation.
- The post-fix 12-cycle retail artifact contains eight valid fuel-backed
  activations before the diagnostic client-side fuel reset diverges from the
  authoritative server meter. Six of those eight were exact; two had one soft
  correction (0.030167 and 0.037277 blocks), with zero SNAP or rollback. Do not
  count the later no-activation cycles as jetpack passes. Evidence:
  `logs/jetpack-release-validation/exact-hold-release-final-12.json`.
- Retail overloads ClientData byte bit 7 for `palette_enabled`; native
  `shared.packet` now masks that bit from the player id and exposes the flag.
  The runtime server decoder was already correct, so this was a parity/fallback
  repair rather than a proven flicker root. `SetColor` is now authorized for
  both normal blocks (tool 5) and flare blocks (tool 22). A red ghost preview
  while moving can still be stock invalid-placement feedback, not a palette
  mutation.
- Disconnect and every spawn clear per-player replication cadence/jetpack
  transition state. Reused player ids and new lives can no longer inherit a
  prior occupant's suppressed first self row.
- The full regression suite passed at this checkpoint. The later Snowball
  retail gate is recorded above; publish a new exact suite count only from the
  final current-tree run, not from this historical checkpoint.

### Jetpack, movement, and block-routing closeout (2026-07-14)

- IDA proved the retail client has no jetpack application ACK. WorldUpdate is
  the sole runtime writer of native `world.Player+0xB0`; ClientData has no
  active/fuel echo, and `ooo == (loop + 7) & 15` in 448/448 captured rows.
- Replication now keeps only the active owner's correction row suppressed for
  the finite fuel burn. A fixed 30-input expiry was unstable: one clean run
  resumed mid-flight with a 0.230469-block pre-correction error. Observer
  snapshots, hitboxes, fuel, and authoritative movement remain at 30 Hz. The
  release transition then uses the bounded held-key/settle/ground gate with a
  600-input cap.
- Retail artifact
  `logs/movement/engineer_exhaust_release_20260714/movement-stress-20260714T025324.993061Z.json`
  passed with 291 samples, 145 airborne, zero SNAP/ADJUST/rollback/stall, and
  0.024933 maximum matched error through full exhaustion, held-key fall,
  release, landing, and owner-row resumption. The shorter active-flight gate
  also passed 121 samples with zero correction in
  `logs/movement/engineer_active_handoff_20260714/`.
- Normal block packets and flare packets are now authorized by exact tool:
  tool 5 owns BlockBuild/BlockLine (32/40); tool 22 owns PlaceFlareBlock (104).
  `Player.is_block_tool()` remains a shared palette/UI predicate and is no
  longer used as packet-action authorization.
- Soldier block A/B artifact
  `logs/block-transition/soldier-post-flare/20260714T-current/` committed the
  block at frame 4, sprinted at frame 5, jumped at frame 6, and had zero
  corrections in block and control phases across 959 samples. Do not run this
  generic block gate as Engineer: its held-SPACE sequence activates jetpack.
- The current Soldier movement/block gate is
  `logs/movement/soldier_block_final_20260714/movement-stress-20260714T025501.282428Z.json`:
  680 samples over 22.659 seconds, a real placement while sprinting/jumping,
  and zero ADJUST, SNAP, visible rollback, stall, unmatched row, or matched
  error. The validator now normalizes vertical travel by native client-loop
  advance; ordinary jump ascent is no longer mislabeled as a one-frame snap.
- Final current-tree regression gate: `627 passed in 72.72s` with CPython
  3.12. The 50-player/30-second production gate held 59.995 Hz with 4.209 ms
  tick p99, zero gameplay drops, and 1.59 MiB memory growth.

The older statement below that recipients must share a reconciliation stamp,
the duplicate-stamp mapping language, and the 425-test count are historical and
superseded by this checkpoint.

### Movement/terrain chronology update (2026-07-12)

- Client-origin terrain edits no longer mutate the authoritative collision map
  during tick-start packet drain. `WorldMutationService` commits BlockLine,
  BlockBuild, and block-tool BlockLiberate only after the owner's physics has
  consumed the packet's retail loop label. Inventory is reserved and refunded
  on rejection/expiry. This fixes the build -> sprint -> jump mixed-map race.
- `InitialInfo.same_team_collision` and the server mover now share the same
  `config.same_team_collision` value. Previously the client was told allies
  were non-solid while authoritative physics still injected ally collision
  impulses, producing severe oscillation near a teammate.
- The deterministic two-client gate now joins in a fixed order. A Soldier
  crossed directly through a same-team Engineer while the Engineer emitted 33
  real Snowblower projectiles: zero hard SNAPs and zero visible backward
  rollbacks. Server tick max was 1.59 ms, so this was not Python saturation.
- Do not call the movement gate fully green yet. The latest normal-cadence
  12-second repeated-jump stress artifact has 14 soft ADJUSTs (maximum matched
  error 0.414 blocks) but zero SNAPs/visible rollbacks. Latch-0 and airborne
  interval-30 A/B variants were worse; production remains latch 1, grounded
  interval 2, airborne interval 6. Terrain-contact soft correction parity is
  the remaining movement item. **Superseded 2026-07-12:** retail restores the
  complete cached `network_position` on a launch frame, not only Z. The server
  now records a bounded history of owner rows only after enqueue and selects
  the newest row whose stamp is strictly older than the ClientData frame that
  supplied the latched jump. Three repeated block -> sprint -> jump cycles
  produced 2,159 samples with zero ADJUST, zero SNAP, zero rollback, and
  0.000015-block maximum error; see
  `logs/launch-climb-phase/stamp-aware-live/`.
- Crouch is the exception to the ordinary one-observed-frame button latch:
  `Character.set_crouch` changes eye Z before retail stores history row L.
  Authoritative movement therefore combines current packet L's crouch bit with
  the prior packet's locomotion buttons. The mixed walk/crouch gate recorded
  zero corrections and 0.000015-block maximum error in
  `logs/crouch-mixed-live/`.

- The representative 50-player baseline initially **failed** at 15.37 Hz and
  64.9 ms average ticks. The latest 30-second gate passes at 59.966 Hz with
  5.088 ms p99, zero gameplay-packet drops, and zero logging drops. See
  [RUNBOOK.md](RUNBOOK.md).
- The full 900-second release soak also passes: 59.999 Hz, 4.915 ms tick p99,
  zero gameplay/logging drops, no pending backlog, and 17.453 MiB memory growth.
- WorldUpdate now uses a 30 Hz retail cadence, grouped serialization for
  recipients sharing a reconciliation stamp, and unreliable delivery.
- Network event processing, the pending gameplay queue, and per-tick packet
  draining are bounded. Plugin callbacks, entity behavior ticks, and late-join
  mutation journals are also bounded; overflow is counted in metrics.
  Ordinary logging is queued and non-blocking.
- Production must disable parity capture, self-row capture, movement snapshots,
  packet tracing, and client physics tracing. The old always-on tracer workflow
  below is a development procedure, not a production launch configuration.
  If `debug_selfrow` is temporarily enabled, samples go through the bounded
  debug writer queue, not a synchronous WorldUpdate file flush.
- The maintained test client for this session is
  `G:\AoSRevival\AceOfSpades_no_steam_new`; start it with its bundled Python as
  `python\python.exe launcher.py +s`.
- Atomic class/loadout selection and centralized deployable authorization are
  implemented. Miner tool 21 creates entity 10 (dynamite); Medic tool 51
  creates entity 30 (medpack), and cross-class packet/tool combinations fail
  the shared authorization gate.
- The late Battle Builder CreateEntity wire table is live-verified against the
  retail client's `GameScene.ENTITIES`: MedPack=30, BlockGoo=31,
  ChemicalBomb=32, GLGrenade=33, Sticky=34, AttachedSticky=35, Radar=36,
  ProjectileMine=37, C4=38, RiotShield=39. Do not derive these IDs from class
  registration order. A two-client run on 2026-07-12 rendered type 38 as
  `C4Entity`, type 30 as `MedPackEntity`, type 36 as `RadarStationEntity`, and
  type 9 as `LandmineEntity` on the observer.
- Movement consumes only observed retail loop labels. Production includes each
  safe local self row at the 30 Hz WorldUpdate cadence; omitting it lets jump
  correct against the stale CreatePlayer spawn anchor. The current jump gate
  records 0 visible rollbacks and 0 hard SNAPs; a second client observed 30/30
  unique positions over 23.36 blocks.
- Block-tool self rows are required too. A live suppression A/B produced ten
  visible rollbacks and a 62.759-block discontinuity; restoring the ordinary
  self anchor kept palette/colour/placement correct with zero visible rollback.
- Grounded self rows remain 30 Hz. Airborne self rows are bounded at 10 Hz
  (`worldupdate_airborne_self_row_interval = 6`): on the same real
  Engineer/Snowblower run, 30 Hz airborne rows caused 263 soft corrections and
  0.342-block maximum matched error, while 10 Hz reduced that to 39 and 0.214
  with no SNAP or visible rollback. An interval of 10 ticks produced a visible
  rollback, so 6 is the measured safety boundary, not an arbitrary throttle.
- ClientData buttons retain the observed-frame latch, but orientation uses the
  current packet. The earlier 88 -> 41 orientation claim is invalid: its test
  controller wrote `world_object.orientation` directly and the native
  Character overwrote that write before movement. The corrected controller
  drives `Character.yaw`; one-step replay against its capture is within 0.0006
  blocks at p95, so the remaining soft corrections are scheduling/history
  phase work, not evidence that the Cython movement formula is wrong.
- Packet 13 uses a bounded Python runtime decoder because the retail client may
  omit the trailing zero UGC-count byte. This removed the Cython `NoDataLeft`
  traceback from join/class selection without relaxing any preceding length or
  count field.
- A current `/endround 1` retail run remained in GameScene for 24 seconds,
  continued advancing its loop, respawned alive as class 12, rebuilt nine map
  crates, peaked at 0.57 ms respawn work, and added no crash dump (9 -> 9).
- The full suite is 425 passing. The movement feature gate now
  requires actual block/ammo consumption and selects Snowblower as a normalized
  Engineer loadout rather than forcing an unauthorized local tool.
- A live Medic (17) -> Miner (3) transition committed only the normalized Miner
  loadout; native tool 21 remained `DynamiteWeapon` and placed server entity 10
  at the client's ghost voxel. ClientData received during the class-change
  death screen is now discarded instead of filling the old body's history and
  reporting false overflow before spawn re-anchors the new life.
- Architecture boundaries, native crash hazards, and rejected approaches are
  recorded in [ARCHITECTURE.md](ARCHITECTURE.md) and this handoff.

### Admin lifecycle and CTF rejoin checkpoint (2026-07-14)

- `/map`, `/mode`, and `/restart` now share `MatchTransitionService`. Invalid
  maps are preflighted off the simulation thread and cannot alter/disconnect the
  live match. Valid map/mode changes gate old gameplay, start a clean epoch,
  retain the authenticated connection, and run a fresh validated retail loader
  handshake on that same peer. Packet 52 requires the bundled client hook;
  reason 18 is only the per-peer failure path for a missing validation reply.
- Fresh CTF rejoin used to freeze in `GameScene.create_entity` with
  `KeyError: 1`. Live retail inspection proved `BASE=1` is absent from
  `GameScene.ENTITIES` while `INTEL_PICKUP=16` is present. Bases remain
  authoritative server-only anchors (`wire_visible=False`); only intel is sent
  through CreateEntity. Restart cleanup also skips destroy packets for markers
  the client never received.
- Live isolated validation passed TDM and CTF restart, invalid-map rejection,
  CityOfChicago -> ArcticBase rejoin, CTF -> TDM rejoin, and `/kick`. Client
  output contains no traceback/invalid entity warning and no new crash dump.
  Evidence: `logs/ctf-fixed-20260714-173357-*` and
  `logs/ctf-cleanup-20260714-174033-*`. The final background current-map reload
  for `/mode` is in `logs/mode-background-final-*` (3.22 ms max transition
  window; clean CTF rejoin).
- The already-running public server does not hot-reload these files. Coordinate
  one normal production restart before expecting the fixes on port 27015; do
  not kill or replace it during somebody else's session.

Current validation commands:

```powershell
py -m pytest -q
py scripts\server_capacity.py --players 50 --seconds 30 --port 27016
# Release soak after the short gate is green:
py scripts\server_capacity.py --players 50 --seconds 900 --port 27016
```

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

## 3. RESOLVED — rollback / "invisible wall" desync

Current production policy (validated 2026-07-11):

- consume and acknowledge only ClientData loop labels actually observed from
  the retail client; never fabricate skipped history labels;
- keep physics on the fixed 60 Hz step and use a bounded observed-label gap for
  render hitches; ENet arrival-time deltas are not physics time;
- broadcast authoritative WorldUpdates at 30 Hz, including each recipient's own
  safe self row (`worldupdate_include_self = true`) so jumps do not correct
  against the stale CreatePlayer spawn anchor.

The foreground retail stress scenario now checks visible position rollbacks in
addition to native SNAP/ADJUST counters. A no-self-row A/B produced 36 visible
rollback events and a 25.15 block jump back toward spawn while the counters
stayed at zero. A separate clean two-client run moved Client A 23.36 blocks and
Client B observed 30/30 unique remote positions. The older investigation below
is retained as history.

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
   `AceOfSpades_no_steam_new/logs/physics_capture_*.ndjson`.
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
- `G:\AoSRevival\AceOfSpades_no_steam_new` — the maintained **original game**
  test client (py2.7 32-bit,
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
# Logs: configured INFO file/console sinks through the bounded listener queue.
# Packet details require both DEBUG level and logging.packet_trace=true.
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
Set-Location 'G:\AoSRevival\AceOfSpades_no_steam_new'
Start-Process -FilePath '.\python\python.exe' -ArgumentList 'launcher.py','+s' -PassThru -WindowStyle Minimized
```
- `physics_tracer.py` auto-loads and opens a **TCP console on port 32896** +
  captures every frame to `AceOfSpades_no_steam_new/logs/physics_capture_<id>.ndjson`.
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
  already RE'd, and `docs/PROTOCOL.md` for the physics-oracle workflow
  (extract real constants from the live client).

### Verifying offline (before touching live)
The parity runner now selects the fixture's ArcticBase terrain by default.
Do not edit the production `default_map`; the older instruction below to do so
is superseded.

- `py scripts/replay_parity.py` — MUST stay **ALL PASS**. Fixtures were recorded
  on **ArcticBase** only; temporarily set `[server] default_map = "ArcticBase"`
  to run it (other maps "diverge" purely from different terrain, not a
  regression). Set it back to `CityOfChicago` after.
- `py scripts/replay_movebox.py` — collision/climb/spawn gate.
- `py -m pytest tests/ -q` — full suite (was 75 pass; `test_reversed_map_sync`
  7/7 after `fd77e9d`).

---

## 6. Key invariants (do not regress) + files

Hard invariants live in [CONTRIBUTING.md](../CONTRIBUTING.md) — read them. Highlights that
touch the open bugs:
- WorldUpdate is 30 Hz and **UNRELIABLE**. Production includes every
  recipient's safe self row, including while holding the block tool, stamped
  with that client's consumed input loop. Omitting block-tool self rows caused
  ten live rollbacks and a 62.759-block stale-anchor discontinuity.
- ENet PROTOCOL_VERSION=168, single channel, range-coder; wire framing =
  prefix byte (0x30/0x31/0x32) + lzf chunking; block packets use RAW shorts for
  x/y/z (no /64 fixed-point). Real inbound LZF back-references encode
  `distance - 1`; changing that decoder requires the retail three-prefab vector.
- Map sync contract: `InitialInfo.checksum` = `zlib.crc32(raw .vxl bytes)`;
  `map_sync_mode` stays `"full"`; the client rebuilds the world from the stream
  (now the raw-span walker, `fd77e9d`).
- Late-join block edits are replayed only when the retained mutation journal is
  contiguous. If the bounded journal overflows, disconnect/retry beats letting
  a client enter with invisible or wrong-colour blocks.

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

## 8. Advanced bot runtime handoff (2026-07-14)

The monolithic prototype has been replaced by `server/bot_ai/`:

- `BotDirector` owns peerless active players, profiles, mode-aware lifecycle,
  backfill/admin population, staggered frames, stale-intent rejection, and the
  60 Hz aim/locomotion motor.
- `AIWorkerSupervisor` owns a Windows-spawn child through a bridge thread with
  64 inbound frames, 128 outbound intents, a 12-intent/tick drain, and
  1/2/5/30-second restart backoff.
- The bridge retains every canonical terrain edit as a coalesced overlay, so a
  worker restart or 65,536-cell overflow rebase receives current topology
  without `generate_vxl` on the gameplay thread.
- Recast/Detour v1.6.0 is pinned under `vendor/recastnavigation`; the Cython/C++
  bridge builds layered 32x32 tiles lazily, owns a bounded DetourCrowd instance,
  and falls back to bounded layered A* when the extension is unavailable.
- The affordance layer adds crouch, jump, safe drop, and fuel-gated jetpack
  transitions while native Player physics remains authoritative. Every
  immediate waypoint is invalidated by a new body voxel or topology version.
- `py_trees` selects visible combat, mode objective, frozen last-seen contact,
  approximate sound, or patrol. LOS fails closed; team reports are delayed;
  firing is impossible without fresh visibility and a final normal combat ray.
- `DeployableActionService` is shared by packet handlers and bots. The bot
  gateway currently supports fire, reload, melee mining, one-block bridge
  placement, dynamite, landmine, C4, radar, medpack, MG, rocket turret,
  disguise, and oriented grenades/rockets/drills/launchers. All class/loadout,
  held-tool, cadence, stock, range, and projectile checks remain authoritative.
- `PrefabActionService` is shared by packet 30 and bots. Production expands
  KV6 placements through a bounded queue (default 16 cells/tick, 32 pending),
  reserves/refunds stock transactionally, and preserves the retail owner/plain
  versus observer/colored replication split. `ConstructionSafetyService`
  protects spawns/objectives/bases and bounded team/friendly-path reservations.

Validated commands/results:

```powershell
py -3.12 -m pytest tests/test_bot_architecture.py tests/test_equipment_handlers.py -q
py -3.12 scripts/bot_worker_smoke.py --restart
py -3.12 scripts/bot_runtime_smoke.py --seconds 12 --bots 12 --restart-worker-at 2
py -3.12 scripts/server_capacity.py --players 12 --seconds 15 --port 32993
```

The multi-mode 12-bot headless smoke moved every bot in TDM, CTF, Zombie, VIP,
and Arena and produced ordinary replicated objective/deployable entities. The
strict 900-second capacity gate completed 54,000 ticks at 60.0 Hz with 0.846 ms
tick p99, 0.420 ms bot-subsystem p99, 0.150 worker CPU core, 58.758 MiB peak
worker memory, 23.348 MiB server growth, two committed bot terrain mutations,
and zero packet/mode/terrain/mutation drops or overflows. Forced worker
termination during a 12-bot match also passed one-second restart recovery and
all bots continued moving.

Open bot work: richer grenade/projectile tactics, statistical hit-rate
calibration, glider route tuning, a rendered operator debug overlay,
deterministic per-mode objective-contribution gates, and two clean retail
clients across TDM/CTF/Zombie/VIP/Arena. Do not claim retail animation or
complete objective acceptance from the headless smokes.

### Classic CTF and phase-aware bot policies (2026-07-14)

- `modes/classic_ctf.py` registers `cctf`, `classic_ctf`, and `classic-ctf` as
  a distinct rules object while deliberately sending retail `MODE_CTF` (8).
  `InitialInfo.classic=1` is the scene variant switch recovered from
  `GameScene.is_in_classic_mode`; the unused enum value 11 is not sent.
- Both teams are locked to Classic Soldier/Deuce. Selection normalization
  forces Rifle, Classic Grenade, and Classic Spade and rejects Classic SMG or
  Classic Shotgun, matching the shipped `playlists/classic.txt` switches.
- Classic uses the recovered 90-minute limit, five-block base capture box,
  shooting while carrying intel, no minimap, and no 60-second intel
  auto-return. Classic bot policy intentionally refuses exact hidden carrier
  or dropped-intel tracking because the same minimap is disabled for humans.
- Worker frames now carry the authoritative enum phase name and CTF intel
  home/dropped/carried state. Policies publish inspectable role names in bot
  debug snapshots. Reaching a role station yields to normal class actions so
  defenders can build/place legal equipment rather than pathing in place.
- A2S discovery now advertises the active native mode id and emits the
  `classic` keyword only for Classic CTF; the old response incorrectly claimed
  every TDM/VIP/Zombie server was Classic CTF.
- Headless four-bot runtime smokes passed for `cctf`, `vip`, and `zombie` with
  live child PIDs, movement from every roster, no mutation backlog, and native
  CTF intel entities. The automated suite is 692 passing. Clean retail-client
  observation of Classic HUD/Deuce animations and actual objective completion
  is still required before release acceptance.

---

## 7. Working agreements (Kiril / KikoTs)
- Work ONLY in this repo (+ the game folder for tracer/client tooling).
- **NEVER use git worktrees.**
- Don't delete his files — archive to `G:\AoSRevival\archive`.
- He launches the game to play-test; everything else must run autonomously.
- **Verify with measurements before claiming a fix.**
- Before pushing to `main`: fetch/rebase first. The whole project history commits
  directly to `main` (solo dev, his own repo `KikoTs/BattleSpades`).

---

## 9. Roster visibility and bot Player parity (2026-07-15)

- Fixed the simultaneous-handshake race that could leave two retail clients
  permanently invisible to each other. `server/roster.py` owns a per-connection,
  per-life CreatePlayer/death/departure catch-up at first ClientData.
- Reveal now finishes with one reliable remote-only WorldUpdate. It initializes
  the remote current tool/action/color state but cannot reconcile the joining
  owner's position because the local row is deliberately excluded.
- Bot WorldUpdates now carry weapon-display bit `0x10` across spawn and action
  transitions. Bot melee uses the public CombatSystem path, so Zombies/spades
  can damage, kill, replicate KillAction, and enter normal respawn lifecycle.
- TDM and fallback policies advance toward the enemy team anchor instead of
  wandering near spawn. Active Zombies globally pursue the nearest living
  survivor, then use fair LOS for attacks and real authorized breach/build
  actions when stuck.

Evidence:

```text
bot_combat_smoke.py: PASS; shots=3, one normal death, KillAction, respawn
bot_runtime_smoke.py --mode tdm: PASS; all four bots moved 51-107 blocks
bot_runtime_smoke.py --mode zombie: PASS; all four bots moved
retail palette/roster scenario: 22/22 remote-present samples; observer tool=5;
  observer color=(95,0,0); reconnect exists=true/tool=5/color=(95,0,0)
```

The retail scenario's direct native-client roster assertions all passed. Its
aggregate `passed=false` is not a roster failure: the trace was initially read
from empty stdout, and the corrected stderr trace contained only constructor
SetColor values plus palette-on ClientData for default tool 8, not the UI wire
events demanded by the separate palette gate. The exact simultaneous-join
ordering is also locked by an automated characterization test. Restart the
production server before play-testing; Python module edits do not hot-reload.

## 10. Hit-confirm and Drill terrain parity (2026-07-15)

- `server/combat_runtime.py` now emits ShootResponse (9) after authoritative
  melee/hitscan health loss. `damage_by` is the shooter id, so all observers
  receive blood while only that shooter receives the native sound/crosshair
  confirmation. Bot victims use the same path.
- `server/projectiles.py` defines the measured Drill contact footprint: 81
  offsets in a radius-2 volume satisfying squared distance at most 6.
- `server/main.py::_apply_drill_contact` removes that full footprint from the
  authoritative VXL and sends one native Damage type 10 while the Drill entity
  is alive. Entity id zero remains valid. Missing entities fall back to exact
  type-6 packets instead of entering the client's fatal invalid-entity path.
- Late-join mutation replay stores exact type-6 removals because a compact
  Drill packet cannot safely reference an entity after the projectile expires.
- A white local name in the kill list is intentional retail behavior. KillAction
  carries no color; the local HUD highlights its own player in white.

Validation:

```text
focused projectile/combat suite: 78 passed
full suite: 707 passed in 99.07s
retail hit run: ShootResponse damage_by=12 and positive crosshair timer
retail Drill run: repeated type-10 contacts with a live entity; no invalid-id
retail BlockManager fixture: exactly 81 contact cells for seeds 0/1/123/255
artifacts: logs/live-hitdrill-20260715-v2
```

## 11. Humanized bot combat and lifetime streak state (2026-07-15)

- Bot perception frames coalesce per `(player_id, generation)` at both the
  supervisor and worker-batch boundaries. One bot cannot fill the 64-frame
  bridge queue with obsolete snapshots, and a backed-up worker does not return
  seconds-old combat choices.
- Decisions run at 8 Hz (perception remains 10 Hz; movement remains 60 Hz).
  Combat holds a strafe direction for a human-sized interval, performs
  grounded cooldown-bounded evasive jumps, and honors profile reaction time
  before firearms or oriented equipment.
- Under serious pressure, a bot with the normal Block/Prefab tool requests
  real authorized cover through the public action gateway. Miner and Zombie
  bots proactively attack an immediate route obstruction with their selected
  melee tool. These paths do not mutate terrain or inventory in the worker.
- Reload is evaluated before grenades/deployables, and an in-progress reload
  cannot select another tool. A bot with no clip or reserve closes to melee
  rather than dry-firing forever. No free ammunition was added.
- `KillAction.kill_count` now carries a dedicated current-life streak. Death
  and round reset clear it; cumulative `Player.kills` still drives scoring.
  The native consumer was confirmed at `gameScene.pyd:0x10194940`.

Validation:

```text
focused bot/combat/round suite: 80 passed
combat worker smoke: both bots completed reload and fired afterward;
  8 shots, one kill, one RoundLifecycle respawn
12-bot TDM runtime (20 s): all bots moved 86.86-138.04 blocks
full suite: 716 passed in 89.21s
```

## 12. Remote firearm and mining audio replication (2026-07-15)

- The old combat relay sent accepted `ShootPacket(6)` messages back to every
  client. That direction is invalid: packet 6 is the retail fire request, and
  incoming clients reported it as unhandled. Damage still worked, which hid
  the missing remote weapon action and made bots appear silent.
- `CombatSystem` now emits `ShootFeedbackPacket(8)` for firearm tools with the
  authoritative shooter id, currently equipped tool id, server loop, shot
  marker, and seed. Human shooters are excluded because their client already
  predicted the same action; peerless bots still reach every real observer.
- IDA resolves `GameScene.process_packet_shoot_feedback` to
  `gameScene.pyd:sub_101935C0` (source line 3642). It looks up
  `players[shooter_id].character`, compares the character and packet tool ids,
  then calls `character.shoot(seed)`. That call owns remote firearm audio and
  muzzle/tracer effects.
- The first retail run proved packet 8 is crash-unsafe for spades: the native
  call reached `Character.shoot`, then raised because `SpadeTool` has no
  `shoot` method. Digging tools instead use WorldUpdate primary bit `0x01` to
  run `use_primary`. Bot action pulses now remain high through two future 60 Hz
  loops, guaranteeing at least one 30 Hz snapshot carries the mining
  animation/sound state. `Damage(37)` remains the canonical terrain result.
- The bot combat smoke now treats packet 8—not packet 6—as an observable shot,
  so the acceptance test cannot regress to damage-only/silent bots.

Validation:

```text
focused combat/bot suite: 79 passed
bot_combat_smoke.py --seconds 20: 12 packet-8 shots; both bots reloaded and
  fired afterward; one authoritative death and one RoundLifecycle respawn
retail client v2: joined 11 bots, completed 7.19 s GameScene run, no unhandled
  packet, traceback, crash dump, SNAP, ADJUST, or visible rollback
full suite: 719 passed in 93.84s
```

## 13. Zombie bot contact, breach, and tool parity (2026-07-15)

- Active Zombie bots no longer enter generic firearm spacing or ammo-crate
  behavior. They globally select the nearest living survivor, path at distance,
  directly close the final six blocks, sprint continuously, use variant-aware
  jumps, and require fresh LOS before the claw strike.
- IDA MCP recovered `handle_zombie_damage` wrapper/implementation at
  `0x10207ED0`/`0x10081340` and Super Spade at
  `0x10208E30`/`0x10082C90`. Both expand to the same centered 3x3x3 geometry.
  Combat authority commits one cube and sends one type-17 packet at the stock
  0.4-second claw cadence.
- Tool 28 now reaches the shared prefab service. Zombie hand/bone/head remain
  selected, charged, collision-checked, reserved, committed, and replicated by
  ordinary gameplay services; the worker has no direct VXL authority.
- Production server-owned and human Zombies currently use the native base
  Zombie. Fast/Jump remain reverse-engineering fixtures only: the hidden
  variants have no safe class-picker icons and are not enabled in rotation
  until a retail trace proves their exact selection rule and movement model.
- Bot spawn-tool selection no longer falls back to rifle 6 for melee-only
  classes. A Zombie's very first WorldUpdate exposes hand 24, matching its
  CreatePlayer loadout, and primary swings remain latched across the 30 Hz
  replication boundary.

Validation:

```text
py -3 -m pytest tests\test_bot_architecture.py tests\test_construction_actions.py `
  tests\test_zombie.py tests\test_reversed_combat.py -q
# 105 passed

py -3 scripts\bot_zombie_smoke.py --seconds 15
# real worker PID 52760; class 14, distance 10.00 -> 0.90,
# HP 100 -> 65, hand/swing visible

py -3 scripts\bot_worker_smoke.py --restart
# worker restarted once and returned a valid result

py -3 scripts\bot_runtime_smoke.py --seconds 12 --bots 12 --mode zombie
# real worker PID 66460; all 12 moved; 3 world mutations; 0 restarts

py -3 -m pytest -q
# 729 passed in 93.84s
```

The restricted Windows runner denies multiprocessing pipes, but scoped
unsandboxed execution now validates the real child-process path. Keep
`--inline-worker` only as an emergency smoke adapter; release evidence must
include a real child PID. Two clean retail observers in a full Zombie round
remain the final visual/feel confirmation.

## 14. City bot priority and navigation soak (2026-07-15)

- Combat and damage urgency now preempt construction/resource work. A visible
  point-blank enemy draws the normalized firearm immediately; a victim also
  receives a bounded reaction to the source position exposed by its normal HP
  packet. Reload precedes low-priority travel/building, and melee is selected
  only when both firearm clip and reserve are empty.
- Explosive entity snapshots now use current projectile coordinates and carry
  blast radius/fuse state. Worker and gameplay-thread gates reject friendly
  blast overlap, while active hazards preempt combat/building with escape.
- CityOfChicago exposed a zero-corridor softlock: Recast/fallback could return
  no direction, `_stuck_recovery` rejected that zero heading, and its saturated
  attempt counter permanently disabled later breach/build recovery. Every
  objective now falls through to the bounded voxel action planner. If no local
  edge exists, the bot remains safely stationary but retains a direct recovery
  heading; three exhausted attempts clear path state and reopen replanning.
- Visible Zombie charges use the same progress-gated recovery, so an infected
  bot separated by destructible terrain escalates to claw breach or a native
  Zombie prefab instead of returning `zombie_contact_charge` with zero motion
  forever.
- `scripts/bot_city_soak.py` is an accelerated real-VXL diagnostic. Its
  `BotSoakMonitor` detects point-blank construction, repeated actions, jump
  loops, travel-role zero-motion stalls, invalid looks, and authoritative wade
  stalls. It deliberately does not claim native movement/packet validation.

Validation:

```text
focused voxel/monitor suite: 23 passed
focused bot/voxel/Zombie/construction suite: 129 passed
CityOfChicago TDM, 12 bots, 60 simulated seconds:
  207 fire decisions, 70 melee, 44 block actions, 5 oriented actions;
  0 priority/action/jump/navigation/look/water failures; max stall 4.0 s
CityOfChicago Zombie, 12 bots, 60 simulated seconds:
  83 melee, 68 fast breaches, 26 Zombie prefab climbs;
  0 priority/action/jump/navigation/look/water failures
bot_worker_smoke.py --restart: real child restarted and returned an intent
bot_zombie_smoke.py --seconds 15: base class 4, 10.00 -> 2.35 blocks,
  survivor HP 100 -> 58, hand and swing replicated, real worker PID
12-bot real-worker runtime: TDM and Zombie moved every bot; zero restarts
full suite: 795 passed in 129.39s
```

The next acceptance step remains a real-time spawned-worker run followed by
two clean retail observers. The accelerated adapter approximates collision and
combat only to evolve policy state quickly; it cannot certify client sound,
animation, hitbox, terrain packet, or reconciliation behavior.

## 15. Bot combat interrupts, vertical escape, bridges, and marksman scope (2026-07-15)

- `BotIntent` now carries an explicit `ROUTINE < TRAVERSAL < COMBAT < SURVIVAL`
  priority. `BotDirector` cancels an older latched terrain swing when a newer
  combat/survival intent arrives. This closes the observed Miner/Engineer case
  where a pending dig kept aim priority and re-selected blocks or a spade after
  the worker had already decided to draw the gun.
- The action motor now preserves secondary-fire and zoom state. Scoped Scout
  engagements set both bits, `WorldUpdate` exposes `0x02|0x40`, and bot
  `ShootPacket.secondary` follows the same state. Long-range marksmen therefore
  use the stock scoped presentation/sniper-beam path instead of permanent hip
  fire.
- Native Recast steering is still the global route source, but its immediate
  voxel edge is classified before reaching Player input. Two-block climbs and
  short gaps retain `JUMP` instead of being flattened to `WALK`. The gameplay
  thread's final live gate validates raised landing clearance rather than
  rejecting the landing support as a current-height wall.
- Stalled builders can emit a bounded face-connected `BlockLine` across water
  or a deep gap. Hole recovery probes nearby higher dry ledges, jumps a legal
  two-block rise, breaks a low ceiling/side obstruction with the selected
  melee tool, or performs a two-phase self-build: jump until the authoritative
  grounded bit clears, then place the supported block below. All mutations use
  `BotActionGateway`; the worker never mutates VXL.
- Failed jump/build attempts are bounded and fall back to path reset. The
  accelerated adapter now models a short airborne lease; without it, the test
  harness falsely reported an endless launch state even though production
  native physics supplies a real grounded transition.

Validation:

```text
bot/navigation/policy/monitor suite: 107 passed
non-bot repository suite: 730 passed
CityOfChicago TDM, 12 bots, 60 simulated seconds:
  0 priority/action/jump/navigation/look/water failures;
  18 BlockLine actions, 2 jump-build launches and 2 matching placements,
  64 scoped marksman engagement samples
CastleWars Zombie, 12 bots, 3 fault-injected swimmers, 60 simulated seconds:
  water_remaining=0; 0 priority/action/jump/navigation/look failures;
  3 jump-build launches and 3 matching placements
real spawned-worker runtime, 12 bots x 12 seconds:
  TDM PID 61460 and Zombie PID 58292; every bot moved, zero restarts,
  4/2 canonical world mutations respectively
```

Two clean retail observers remain required to judge the beam visually and to
feel the native jump/build cadence. The regression tests do prove the exact
replicated action bits, shot secondary byte, preemption, VXL geometry, and
authoritative action path.

## Pickup settling, bot resource timeout, and HUD broadcasts (2026-07-15)

- Map pickups retain their official sidecar model offset, verify their support
  at 10 Hz, and reliably move with ChangeEntity action 1 when that support is
  destroyed. Water-only fall destinations relocate to a nearby dry surface.
- A bot spends at most three seconds on one `(entity_id, position)` resource.
  It then ignores that exact pickup location and chooses another; a pickup that
  later falls to a new position is eligible again.
- Global mode/plugin/admin announcements now use retail `CHAT_BIG` top-screen
  routing. Private command feedback stays in system chat. LocalisedMessage (50)
  is exposed through a bounded server API; `TEAM1_COLOR`/`TEAM2_COLOR` and all
  recovered positional variables are in `docs/PROTOCOL.md`.
- IDA evidence: `gameScene.pyd` session `broadcast_overlay_20260715`, handlers
  `0x1017CF90`, `0x1017DDA0`, `0x101A0490`, and `0x10166600`.
- Retail correction: packet 50 with `localise_parameters=1` resolves team IDs,
  but packet 49 never does. `server.announcements.build_overlay_message` now
  resolves `TEAM1_COLOR`, `TEAM2_COLOR`, and `TEAM_NEUTRAL` before serializing
  free-form mode/admin/plugin prose, preventing raw IDs on the HUD.

## Long-session bot liveness and per-life reset (2026-07-16)

- The reported six-minute MayanJungle TDM session was not a worker crash or
  server overload. The AI child stayed alive and simulation ticks remained
  healthy; several bots kept submitting non-zero movement while their
  authoritative positions remained at the same base obstruction.
- The deterministic 12-bot baseline reproduced the defect: maximum stationary
  time reached 363.4 seconds, one ceiling-recovery counter reached 400, and the
  old monitor incorrectly reported zero navigation stalls because it treated a
  non-zero movement command as proof of movement.
- Recovery pacing no longer writes `last_progress_at`. Only displacement from
  a later authoritative player snapshot counts as progress. Ceiling breach is
  bounded, an exhausted route resets its DetourCrowd proxy, and a two-second
  voxel-planned side route alternates shoulders instead of immediately asking
  for the same failed corridor.
- Immediate displacement and strategic progress are separate invariants. A
  travel bot must reduce its route-goal distance by three blocks within six
  seconds; short back-and-forth motion no longer clears recovery attempts.
- Mayan's TEAM1 exit demonstrates why a non-zero Detour vector is not enough:
  the native crowd points east into a missing floor, while the layered voxel
  route is east `(132,233)`, north `(132,234)`, then east `(133,234)`.
  Exhausted crowd routes now follow that recomputed voxel corridor toward the
  original goal for three seconds before trying arbitrary side detours.
- A bot isolated on a tall one-cell support first exhausts jump, breach,
  bridge, and side-route recovery. It may then take an adjacent clear ledge as
  an emergency `DROP`; normal server gravity and fall damage remain
  authoritative. The worker never teleports the bot.
- `PlayerSnapshot.life_id` carries the authoritative monotonic death count.
  Brain state is connection-scoped, so each changed life ID clears the old
  contacts, path, resource target, escape, and stuck counters even when death
  occurred beside the team's spawn. Large position discontinuities retain the
  same reset as an admin-teleport fallback.
- The accelerated soak now applies gravity after its synthetic terrain
  mutations. Without that adapter, a destroyed support left the harness actor
  hovering many blocks above the new floor and produced a false navigation
  stall that native server physics cannot produce.

Validation completed before retail handoff:

```text
focused bot architecture/policy/soak/voxel suite: 118 passed
focused traversal/soak subset after Mayan corridor repair: 41 passed
real spawned-worker runtime, TDM, 12 bots x 12 seconds:
  PID 39336; every bot moved 24.97-94.99 blocks; zero restarts
MayanJungle TDM accelerated replay, 12 bots x 420 simulated seconds:
  886 fire decisions; maximum same-life stationary time 15.2 seconds;
  no action loop, priority violation, invalid look, or water stall;
  final two active recoveries were 3.6/3.8 seconds
broken baseline with the same seed/map/roster:
  maximum stationary time 363.4 seconds; ceiling attempts reached 400;
  multiple bots retained the same base coordinates for the full replay
```

## Bot perception hitch and map-stable anchor cache (2026-07-16)

- A live 12-bot profile found an authoritative-thread regression rather than
  worker or reaction-timer latency. Every staggered perception batch called
  `team_base_anchor()` for both teams, and authored spawn zones were rescanned
  instead of reusing their map-stable result.
- In the broken 15-second profile, `BotDirector.update()` consumed 16.06
  seconds cumulatively, perception consumed 13.26 seconds, and 1,314 base
  lookups consumed 9.90 seconds. Live bot ticks spiked 18-42 ms at roughly the
  perception cadence, producing visible delayed motion/reaction.
- `WorldManager` now resolves each team anchor during map prewarm, caches it
  for the loaded map, and clears the cache only when a map is loaded or the
  generated flat map is rebuilt. Terrain edits do not move authored strategic
  base locations; spawn points still revalidate live terrain independently.
- The same profile now records 1.01 cumulative seconds in 900 director updates
  and 0.382 seconds in perception. A real 12-bot capacity run held 60.0 Hz,
  bot p99 0.711 ms, overall tick p99 1.023 ms, zero queue/gameplay drops, and
  worker usage 0.669 CPU core. Human aim noise and reaction bands were not
  weakened to conceal this scheduling bug.

## Alive AI worker deadlock and automatic recovery (2026-07-16)

- The freeze reproduced in the packaged `0.0.2-alpha.1` server with all humans
  disconnected. The server remained healthy, but its single AI child consumed
  one full core and stopped returning intentions. Bot leases expired after
  250 ms, so every bot correctly became inert even though the process still
  reported alive.
- A native `py-spy --native` capture identified the exact blocking path:
  `BotBrain.decide -> WorkerVoxelWorld._path_direction ->
  _native_path_direction -> recast.cp312-win_amd64.pyd`. The live fallback was
  inside Detour `find_path` on a bad multi-tile corridor. Process liveness alone
  could never detect this failure.
- Live worker navigation no longer calls synchronous native `find_path` after
  a zero DetourCrowd result. It falls through to the bounded voxel A* already
  owned by `next_path_direction`; the direct native API remains available for
  isolated bridge tests.
- `AIWorkerSupervisor` now starts a heartbeat lease when it sends a frame for
  an alive, spawned bot. After eight seconds of startup grace, five seconds
  without any returned intent causes only the AI child to be terminated and
  restarted through the existing 1/2/5/30-second backoff. `/bots status` shows
  `stalls` and current `silence` for operator diagnosis.
- Killing only the wedged packaged child proved the recovery boundary: the old
  supervisor restarted it one second later and unattended bots immediately
  resumed drill projectiles and combat while the authoritative server stayed
  online.
- Validation: startup/config check including a real spawned child passed; the
  full suite passed `868` tests; focused heartbeat/native-fallback coverage is
  included. A 900-second MayanJungle replay completed instead of hanging. A
  captured 120-second replay kept all bots producing actions with no action,
  jump, priority, look, or water violation, but still reported recoverable
  per-bot route stalls. Do not conflate those local terrain recoveries with the
  fixed worker-wide deadlock.

## Late-join terrain convergence and voted rollover (2026-07-16)

- The missing-texture/phantom-collision failure was a malformed VXL span, not a
  palette problem. Both generated-VXL and dirty-column serializers duplicated
  the final voxel of a fully explicit non-final run as overlapping top and
  bottom colors. The retail decoder rejects that whole column, so the client
  could render air while the authoritative server still collided with blocks.
  A one-voxel run must encode one color word, never two overlapping words.
- MapSync now records a canonical per-cell sequence watermark. Every committed
  voxel after that snapshot is retained only while a joiner is gated, repeated
  coordinates coalesce to final solidity/RGB in support-safe order, and replay
  uses `BlockBuildColored(33)` or exact `Damage(37, chunk_check=0)`. A retained
  send cursor resumes at the first packet ENet did not accept. Journal gaps or
  overflow fail closed and require a fresh join rather than admitting partial
  terrain.
- Match transitions rebind the mutation listener to the candidate world and
  restore it on rollback. A peer still inside the old InitialInfo/MapSync when
  rollover begins is retired with reason 18; beginning a second VXL handshake
  on that peer can splice map epochs and crash or corrupt the client.
- End-of-round map voting now resolves before transition. No-vote/tie results
  are deterministic, late joiners receive the active ballot, and invalid map
  choices fail preflight before the native terminal packets. Official maps use
  `GameStats(67) -> ShowGameStats(53) -> lobby.end_screen_seconds ->
  MapEnded(52)`; screenshot-less custom maps omit packet 53 but keep the dwell.
- Validation after a clean native rebuild: 101 high-risk terrain/vote/transition
  tests passed, the complete repository passed 903 tests, and
  `run_server.py --check` validated 28 maps, eight native imports, 40 prefabs,
  configuration, and a real spawned worker. The state-convergence fuzzer also
  matched exact solidity and RGB for 144 mutated columns across six official
  maps.

## Settled-client collapse ghost convergence (2026-07-16)

- Unsupported components were removed exactly on the server but represented to
  settled clients only by the initiating `Damage(37, chunk_check=1)`. Every
  retail BlockManager independently derived the falling component from local
  history; one differing voxel could therefore leave visible, non-authoritative
  geometry after the server had removed the whole component.
- `TerrainRepairService` now has a dedicated collapse-confirmation lane. Combat
  retains the original checked Damage/falling animation, records only cells
  actually removed by `WorldManager`, orders their visible shell before the
  interior, waits 18 simulation ticks, and drains eight exact type-6,
  `chunk_check=0` air confirmations every three ticks. Rebuilt cells are
  discarded before send. Both lanes share the existing 8192-cell hard bound;
  overflow preserves visible-shell confirmations before deep interior cells.
- IDA recovered packet 38 as three dictionaries (`damaged_blocks`,
  `occupied_blocks`, `user_blocks`), not a topology snapshot, so it must not be
  used as a removed-voxel resync. `BlockManager.handle_weapon_damage ->
  remove_block` reaches the native map removal/update call even for an exact
  confirmation, which also invalidates stale rendered geometry.
- Live retail validation on ArcticBase built an 18-cell fixture, removed its
  three-cell base, collapsed and confirmed the remaining 15 cells, and drained
  the repair queue to zero. The client remained in `GameScene`, reported zero
  solid fixture cells, and a framebuffer facing the fixture showed no orange
  ghost. No traceback, crash, invalid entity, or unhandled packet appeared.
  Artifacts are under `logs/terrain-collapse-confirmation/retail-fixture-v4/`.
- Final focused terrain/combat coverage passed 70 tests, including a
  50-recipient batch-bound test. `run_server.py --check` passed 28 maps, eight
  native imports, 40 prefabs, configuration, and a real spawned worker.
  The monolithic suite is not currently claimable as green: legacy
  `tests/test_packets.py` imports unexported `protocol.serialization`
  `tofixed/fromfixed`, and two telemetry tests patch `toml.load` while the
  Python 3.12 loader uses `tomllib`. These collection/harness defects are
  unrelated to collapse repair and were not folded into this change.

## Steam master registration recovery (2026-07-16)

- The inspected decompiled `steam_appid.txt` contains `480` (Spacewar); the
  retail AoS app and browser request use `224540`. The signed x86
  `steam_api.dll` SHA-256 is
  `abfedd473b3f4a9597bbdc90d20f4b6f696bb2ebb937a03177461df695430ad6`.
- The adjacent 300,456-byte `steamclient.dll` SHA-256 is
  `9c9baf7490598693c0f669ec79bec257e0f513fa443d5cc486ebca374fac9e32`;
  its Authenticode status is hash-mismatch. It previously blocked inside
  `SteamGameServer_Init`; a later desktop-Steam-assisted run did log on, but
  it remains excluded by default because that is not a headless guarantee.
- IDA recovered `SteamGameServer011`, product `aos`, game dir
  `aceofspades`, description `Ace of Spades`, updater port `8766`, version
  `1.0.0.0`, anonymous logon, heartbeats, and the exact tag/map transforms now
  implemented in `server/steam_master.py` and the Win32 helper.
- Browser filters are now distinguished: the generic list filters only
  `gamedir=aceofspades`, Official adds `white=1`, and User applies
  `nand(white=1)`. Do not set or spoof `white`; community servers correctly
  appear under generic/User results.
- With the old signed `steam_api.dll` plus Steam's current installed x86
  `steamclient.dll`, `tier0_s.dll`, and `vstdlib_s.dll`, the helper completed
  anonymous logon, received server SteamID `90289085333726212`, answered a
  local A2S_INFO on its separate query port, and shut down cleanly. That ID was
  ephemeral test evidence, not a configured constant.
- Proprietary Valve files are never committed or packaged. The service stages
  operator-owned copies in a private temporary directory with app ID 224540,
  kills blocked initialization, restarts later failures with bounded backoff,
  and does not touch the gameplay tick. Public registry/A2S visibility still
  requires externally reachable game/updater/query UDP ports.
- External proof is now complete. On `2026-07-16`, Valve's public
  `ISteamApps/GetServersAtAddress` returned `88.80.155.252:27016`, SteamID
  `90289091849869316`, app `224540`, game dir `aceofspades`, game port `27015`;
  a public challenged A2S_INFO returned the BattleSpades name/map/tags. This
  final run used the safe default (`use_supplied_steamclient = false`) and
  auto-discovered Steam's installed x86 runtime. Run
  `scripts/check_steam_registration.py` to reproduce both checks.
- The stock All/Community UI still returned zero. This is now explained:
  `hl2master.steampowered.com`, the legacy UDP list service, no longer resolves
  and its historical IPs time out. The retail callback finishes
  `eServerFailedToRespond`; this is independent of current Valve registration.
- Retail `serverInfo.py` hardcodes every selected row to game port `32887`.
  Deploy on/forward `32887` for unmodified row compatibility, or update the
  client to honor the returned game port.

## Generic vote native-crash fix (2026-07-16)

- Retail `GenericVotingHUD.decode_string` literal-evaluates title/description
  and unconditionally reads tuple indexes `0` and `1`. The prior
  `repr(("VOTE_MAP_TITLE",))` packet raised `IndexError` in native HUD code.
- All vote text now uses `repr((identifier, ()))`. The exact retail decoder was
  exercised live for map and kick identifiers; the focused vote/Steam suite
  passes 48 tests.

## VIP performance and Classic CTF retail completion (2026-07-17)

- VIP's progressive lag was not its state-machine scan. Historical tick traces
  showed `mode` near 0.01 ms while `fire` grew continuously. Each server-created
  BlockFire child had incorrectly received five new spread attempts, producing
  a supercritical tree the retail client never creates itself. Fire now shares
  one five-attempt budget per Molotov cluster, has one attempt per emitter, and
  obeys a 96-emitter operational ceiling with deterministic oldest replacement.
- VIP sub-round reset now publishes at most four respawns per gameplay tick,
  uses event-driven roster transitions with only a 4 Hz safety audit, and uses
  monotonic selection/intermission deadlines. Retail timed scoring is active:
  boss survival is 50/10 s, escort is 10/5 s inside 15 blocks, and missed
  intervals never replay as a reliable packet burst. Boss-kill and
  boss-as-killer bonuses retain distinct native score reasons. Default VIP
  voting uses the shipped Alcatraz/CityOfChicago playlist.
- A post-change 45-second CityOfChicago run with 12 real worker bots sustained
  59.999 Hz, tick p99 2.141 ms, VIP p99 0.019 ms, fire p99 0.132 ms, peak fire
  15/end fire 2, memory growth 2.25 MiB, and zero gameplay, mode, terrain, or
  mutation drops. The only red capacity gate was the separate bot main-thread
  p99 (1.310 ms against its 0.75 ms target).
- Classic CTF uses the ordinary CTF scene (`mode=8`) plus `classic=1`, locked
  Deuce class 5, no minimap, shooting with intel, no auto-return, a three-block
  intel offset, five-capture target, and the exact seven-map shipped rotation.
  Captures now award 10 personal points with native reason 50 and one team
  point; touch return awards one personal point with reason 53.
- Classic death now uses the native Character corpse rather than the Battle
  Builder entity-11 gravestone. `KillAction` creates `ClassicCorpse.kv6`;
  packet 36 removes it on a hit (effect byte 1) or silently before respawn
  (effect byte 0). The server owns a generation-tagged static KV6 hitbox and
  applies the recovered zero-player-damage/radius-3 corpse blast. Per-client
  death ledgers prevent a joining GameScene from receiving duplicate
  KillAction while still repairing an explosion missed during MapSync.
- Seven dedicated corpse lifecycle tests plus the high-risk Classic/combat/
  roster/entity/transition gate pass (136 tests). `run_server.py --check`
  validates configuration, 28 maps, eight native imports, 40 prefabs, and a
  real spawned worker. An isolated Crossroads retail run observed the native
  Character enter `dead=True`, `exploded=False` with its `classic_corpse`
  display list loaded, no Grave entity in the GameScene, and a clean respawn;
  no traceback, invalid entity warning, or new dump appeared. Evidence is in
  `logs/classic-corpse-retail-20260717/`. The monolithic pytest command emitted
  no progress before its bounded 300-second timeout, so no all-suite claim is
  made for this checkpoint.
- Clean retail Crossroads validation saw mode 8/classic 1, two IntelPickup
  entities, and no dump. An authoritative real pickup followed by return to
  base produced `intel_count=2`, personal score 10, and team score 1 while the
  client remained in GameScene. A second isolated VIP score soak stayed in
  GameScene/mode 7 with both boss markers, showed 100 survival points on both
  bosses plus 10-point escort increments on nearby guards, remained connected,
  and created no dump. That first soak exposed a projectile expiring just after
  first ClientData: the joiner had missed its CreateEntity during MapSync but
  received its DestroyEntity. Entity lifetimes are now tracked per connection;
  a second retail run had no invalid-entity, traceback, or unhandled-packet
  warning. Evidence is in
  `logs/classic-retail-20260717/`,
  `logs/classic-objective-retail-20260717-r2/`, and
  `logs/vip-retail-soak-20260717/` plus
  `logs/vip-score-retail-20260717-r2/`.
- Expanded entity/VIP/fire/Classic/CTF/score/vote coverage is 104 passing. The repository
  monolithic collection is slow enough on this Windows workspace to exceed a
  two-minute collection-only timeout; no all-suite claim was made for this
  checkpoint.

## Playtester DOCX closeout (2026-07-17)

The English playtester report and all 20 embedded screenshots were reviewed.
The reproducible protocol and gameplay defects were handled as one compatibility
pass:

- Knife, police baton, and machete damage now use the recovered retail values;
  the machete retains its one-hit zombie-head behavior. Snub pistol and
  semi-auto rifle are registered as real ranged weapons, including damage,
  tracer, and block-impact handling. The drill damages players as well as
  terrain, while the block sucker uses its own damage constant and grants the
  blocks it removes.
- Dynamite is placed against the selected face instead of inside the voxel,
  damages players, and uses one bounded native radius-destruction operation.
  C4 uses the same bounded terrain path, removing the client freeze caused by a
  burst of individual block packets.
- Rocket turrets are destructible, clean up on owner/team changes, retain a
  finite ammunition state, and accept the retail human-control/fire path. Radar
  uses the recovered 35-second fuse, is destructible, and is removed when its
  owner changes team. Team authorization is re-evaluated rather than inherited
  from a stale owner state.
- TDM rejects non-standard mode classes. Spectator selection no longer becomes
  a blue paratrooper. Zombie uses retail team semantics (Blue zombies, Green
  survivors), countdown/infection placement, scoring, sounds, role markers,
  and role colors. Zombie's `InitialInfo` now enables the native ordinary
  opposing-role minimap fallback, while only the final survivor receives the
  separate `ChangePlayer.high_minimap_visibility` VIP marker. VIP promotion
  and sub-round reset no longer retain invalid or duplicate state.
- Classic CTF uses the native corpse lifecycle rather than Battle Builder
  graves and now awards kill/capture/return points. Ordinary CTF carrier and
  dropped-intel state uses the native pickup/minimap flags. Remote spade
  destruction, block placement, and prefab placement now emit their native
  observer audio.
- Default team colors are the retail blue and green values, rather than the
  overly bright cyan palette. A distributable Python 2 client patch adds
  Ctrl+V support to every native `EditBoxControl`; it is bundled by the release
  packager and installed in the maintained non-Steam client.

The movement report was reproduced with a real retail client rather than
accepted as a generic interpolation complaint. Pack 66 produced one visible
0.628-block adjustment when fuel expired next to geometry. The client already
ends local thrust on fuel/key state; sending the owner an inactive
`WorldUpdate` at a fractional simulation phase forced the correction. The
replicator now defers only that owner transition until key release and grounded
input settlement. Observers still receive the authoritative inactive state.
Do not restore a general activation delay: a tested one-frame delay produced a
4.898-block error and a hard snap.

Live retail evidence:

- `logs/playtester-docx/rocketeer-pack66-final/`: Pack 66, zero SNAP,
  zero ADJUST, zero visible rollback, and zero matched position error.
- `logs/playtester-docx/rocketeer-pack67/`: Pack 67, zero SNAP/ADJUST and
  zero visible rollback.
- `logs/playtester-docx/engineer-pack68/`: Pack 68, zero SNAP/ADJUST and
  zero visible rollback.

The apparent all-suite collection stall was duplicate discovery, not a gameplay
or teardown leak: pytest recursively entered 104 copied test modules under
`tmp/` and 77 more under `.worktrees/`, in addition to the 95 canonical modules
under `tests/`. Root `pytest.ini` now constrains discovery to `tests/`. With a
real Windows worker permitted, the canonical monolithic command
`py -3.12 -m pytest -q` passes **1,075 tests** in 216.09 seconds. The launcher
check and `py -3.12 run_server.py --check` also pass their real spawned-worker
probe; no test-only inline worker was used for that acceptance.

Real multiprocessing gameplay smokes were also run after the permissions fix:

- CTF / CityOfChicago, eight bots, 20 simulated seconds: every bot moved
  12.24--188.78 blocks, no requested-motion stall reached five seconds, and
  all eight world mutations committed.
- Zombie / CastleWars, eight bots, 20 simulated seconds: every bot moved, 74
  world mutations committed, and a bot fault-injected into a real water column
  reached land in 0.87 seconds without an expired mutation.

Two report items are intentionally not represented as universal server fixes:

- The Classic CTF "tent" is map-authored VXL geometry, not a dynamic server
  entity in this retail build. All seven shipped Classic-map sidecars were
  inspected and contain no authored base zones, and the client assets contain
  no dedicated Classic tent KV6. Retail `GameScene` also omits legacy `BASE=1`
  from its active entity table, so sending type 1 can crash a clean client.
  BattleSpades therefore uses native packet-43 capture/minimap zones around
  stable dry map anchors and never exposes its internal base marker. A custom
  visual tent requires authored VXL geometry (or a coordinated custom-client
  asset), not a stock-server wire workaround.
- The report promises mouse videos but contains no video or deterministic
  reproduction. The maintained client still loads the existing raw
  `WM_INPUT`/pyglet mouse patch. A different mouse defect needs the promised
  capture plus sensitivity, display mode, and frame-rate details before another
  client patch is safe.

The report's broad statements about bot quality are playtest/tuning targets,
not a single protocol defect. The broken Zombie team semantics and mode state
were fixed here, but long-match navigation and human-like behavior remain an
ongoing bot-AI workstream and should not be marked complete from these focused
compatibility tests.
