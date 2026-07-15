# BattleSpades — Session Handoff (2026-07-09, evening)

> Written for the next engineer/AI picking up mid-work. Read
> [CLAUDE.md](../CLAUDE.md) first (hard invariants + working agreements),
> then this file. The project is a Python 3.12 + Cython **1:1 recreation of the
> Ace of Spades 1.x (Battle Builders) dedicated server**, tested against the
> **compiled original client**. Ground truth = the live client, never the
> `aoslib-reversed` hand port (its physics/packet *layouts* are partly wrong;
> trust its *logic* only).

## Current-state addendum (2026-07-11)

This addendum supersedes the old performance, WorldUpdate-cadence, test-count,
and client-path claims below. The remainder of this handoff is retained because
its packet and map-sync investigation history is still valuable.

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
- Settled-client terrain has a bounded prediction-repair layer in
  `server/terrain_repair.py`. It is enrolled only for rejected or cancelled
  client-predicted footprints. Successful builds, prefabs, digs, and collapse
  already have one reliable gameplay packet and must never be globally queued:
  replaying packet 33/37 after the quiet delay runs native placement/damage
  callbacks and particles twice. Joining clients remain gated out and use the
  full snapshot plus contiguous mutation journal.
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
  [SERVER_PERFORMANCE.md](SERVER_PERFORMANCE.md).
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
  recorded in [ARCHITECTURE.md](ARCHITECTURE.md) and
  [ENGINEERING_NOTES.md](ENGINEERING_NOTES.md).

### Admin lifecycle and CTF rejoin checkpoint (2026-07-14)

- `/map`, `/mode`, and `/restart` now share `MatchTransitionService`. Invalid
  maps are preflighted off the simulation thread and cannot alter/disconnect the
  live match. Valid map/mode changes gate old gameplay, start a clean epoch,
  disconnect with reason 18, and rely on a fresh retail handshake.
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
  already RE'd, and `docs/PHYSICS_CALIBRATION.md` for the physics-oracle workflow
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

Hard invariants live in [CLAUDE.md](../CLAUDE.md) — read them. Highlights that
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
- Server-owned Zombies receive deterministic native base/Fast/Jump variants
  before spawn and CreatePlayer. Humans remain base Zombie because the hidden
  variants have no safe class-picker icons.
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
