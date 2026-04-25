# BattleSpades — Server Goal & Roadmap

> Target: a **1:1 functional re-implementation** of the original Ace of Spades 1.x (Battle Builders / Jagex era) dedicated server, in Python 3 + Cython, that any unmodified 1.x client can connect to and experience as the real thing — including every supported gamemode, weapon, tool, prefab, and edge case.

This is not a clean-room reimagining. The reversed `aoslib` (`../aoslib-reversed/`) and the live client (`../aceofspades_nonsteam/`) are the spec. The server's job is to satisfy them.

---

## 0. Where we are right now (honest assessment)

What works, roughly:

- ENet host with range-coder compression on a single channel ([server/main.py](../server/main.py)).
- A2S query intercept on the same UDP socket — server appears in browser/LAN ([server/a2s_query.py](../server/a2s_query.py)).
- The connect handshake gets the client to **spawn**: SteamSessionTicket → XOR-decrypt → InitialInfo → MapDataValidation CRC handshake → MapSync (chunked) → StateData → SkyboxData → ExistingPlayer → CreatePlayer → SetHP. ([server/connection.py](../server/connection.py)).
- 14 packet handlers wired ([protocol/packet_handler.py](../protocol/packet_handler.py)): `ClockSync(0)`, `ClientData(4)`, `ShootPacket(6)`, `UseOrientedItem(10)`, `SetColor(11)`, `BlockBuild(32)`, `BlockLiberate(35)`, `ChatMessage(49)`, `WeaponReload(76)`, `ChangeTeam(77)`, `ChangeClass(78)`, `ClientInMenu(110)`, `PositionData(116)`. The protocol defines **~127 packet IDs** — we handle ~11% of the receive surface.
- Skeleton CTF mode with hardcoded base/intel positions. TDM and Arena are stubs.
- Reversed `shared.packet`, `shared.bytes`, `aoslib.vxl`, `aoslib.world`, `aoslib.kv6` are present as `.pyx` (delegated to `../aoslib-reversed/` for the source of truth on layout/behavior).

What's broken / wrong / placeholder:

- **`InitialInfo` is mostly hardcoded** ([server/connection.py:459-518](../server/connection.py)) — `mode_name="CTF_TITLE"`, `filename="London"`, `checksum=592649088`, `prefabs=['supertower']`, magic Steam ID, hardcoded movement-speed table. Mode/map metadata isn't driven from playlists or actual maps.
- **`StateData` is also mostly hardcoded** ([server/connection.py:313-356](../server/connection.py)) — `mode_type=8` always (CTF), fog/light values are constants, lone `screenshot_camera` placeholder.
- **`fake LZF` framing** ([server/util.py](../server/util.py)) — chunking-only wrapper is intentional (ENet does the real compression), but fragile if a packet > 32 bytes path changes.
- **No real map handling** — `world_manager.load_map` falls back to a flat green slab if file missing; only one map name from `config.toml` is ever used. No playlist rotation, no `mapinfo` validation.
- **`WorldUpdate` is broadcast every tick to everyone** with no interest management or priority; only alive+spawned players included.
- **No movement validation worth the name** — `PositionData(116)` is recorded but never reconciled; no anti-cheat or anti-rubberbanding logic. Player physics in [server/player.py](../server/player.py) does soft-correction but the `aoslib.world` movement kernel is wrapped, not authoritative.
- **Combat is weapon-only** — [server/combat_runtime.py](../server/combat_runtime.py) handles `ShootPacket`, `BlockBuild`, `BlockLiberate`, melee, weapon reload. Nothing else: no grenade explosions, no C4, no MG turrets, no rocket turrets, no medpacks, no mines, no dynamite, no flare blocks, no block sucker, no painting, no UGC items.
- **Team management is two-team only** — `TEAM1`/`TEAM2` hardcoded; no spectator handling, no neutral, no zombie team logic.
- **Class system is pass-through** — `ChangeClass` updates an int, no per-class mechanics (movement multipliers, headshot multipliers, starting blocks, abilities, disguise, intel-tool, sniper2, etc.).
- **No persistence**: no bans, no admin records, no scoreboards across map changes.
- **Game flow is missing**: no warmup, no countdown, no map-end, no map vote, no map rotation, no MVP screen, no rank-ups.
- **CTF is the only mode with logic**, and intel positions are hardcoded XY coordinates — no parsing of map intel/tent metadata.
- **Tests under `tests/`** are split: `test_reversed_*.py` are the active suite (run by pytest), the older `test_*.py` are collect-ignored in [tests/conftest.py](../tests/conftest.py).

The goal of this document is to give us a path from "can spawn, can shoot, can build" to "the real server".

---

## 1. The full feature surface (what 1:1 actually means)

### 1.1 Gamemodes (13 total, IDs from [shared/constants_gamemode.py](../shared/constants_gamemode.py))

| ID | Code | Title | Status | Notes |
| --- | --- | --- | --- | --- |
| 0 | `nor` | NORMAL | not started | Generic deathmatch fallback |
| 1 | `dem` | DEMOLITION | not started | Plant/defuse, single-life rounds, BlockManagerState packet |
| 2 | `zom` | ZOMBIE | not started | One team Survivors, one team Zombies (3 zombie classes); zombies melee-only, infect on kill, crates spawn (`RULE_CRATES_SPAWN_TIME`) |
| 3 | `mh` | MULTIHILL | not started | Rotating capture zones (uses `LockToZone`, `MinimapZone`) |
| 4 | `oc` | OCCUPATION | not started | One contested zone, hold-to-win |
| 5 | `dia` | DIAMONDMINE | not started | Mine diamonds, deposit at base; uses `Restock`, `PickPickup`, `DropPickup` |
| 6 | `tdm` | TDM | stub | Pure team kill count to score limit |
| 7 | `vip` | VIP | not started | Mafia classes; protect VIP / kill VIP, single-life round |
| 8 | `ctf` | CTF | partial | Working pickup/capture, hardcoded positions only |
| 9 | `tc` | TERRITORY | not started | Multiple capturable bases; `TerritoryBaseState` packet (106) |
| 10 | `tut` | TUTORIAL | n/a | Single-player; ignore for server |
| 11 | `cctf` | CLASSIC CTF | not started | CTF + classic class restrictions, custom ground colors, no minimap, intel auto-return off |
| 12 | `ugc` | UGC | not started | User-generated maps; UGC packets 97–102 |

Each mode's playlist (`../aceofspades_nonsteam/playlists/*.txt`) defines: allowed maps, min/max players, classic/mafia flags, custom rule overrides. We must respect these.

### 1.2 Maps (29 release maps + 9 baseplates)

From [`mapinfo.py`](../../aceofspades_nonsteam/playlists/mapinfo.py):

- Standard: `AncientEgypt`, `ArcticBase`, `Atlantis`, `BlockNess`, `BranCastle`, `CastleWars`, `CityOfChicago`, `DoubleDragon`, `DragonIsland`, `Frontier`, `GreatWall`, `Invasion`, `London`, `LunarBase`, `MayanJungle`, `SpookyMansion`, `TheColosseum`, `TokyoNeon`
- Classic: `Crossroads`, `Hiesville`, `ToTheBridge`, `Trenches`, `WinterValley`, `WW1`, `Classic`
- Mafia: `Alcatraz`, `CityOfChicago`
- Baseplates (UGC): `Training`, `DesertBaseplate`, `LunarBaseplate`, `MountainBaseplate`, `GrasslandBaseplate`, `TempleBaseplate`, `UrbanBaseplate`, `MarshBaseplate`, `SnowyBaseplate`, `WaterBaseplate`

Each has a `max_players` cap and an `invalid_modes` blacklist that must be enforced server-side. Each `.vxl` ships with embedded metadata for **intel positions, tent positions, spawn zones, prefab anchors** that we currently ignore — we need a metadata parser.

### 1.3 Classes (per [shared/constants.py](../shared/constants.py))

`SOLDIER`, `SCOUT`, `ENGINEER`, `MINER`, `ROCKETEER`, `SPECIALIST`, `MEDIC`, `CLASSIC_SOLDIER`, `GANGSTER_1..4`, `GANGSTER_VIP_1`, `GANGSTER_VIP_2`, `UGCBUILDER`, `ZOMBIE`, `FAST_ZOMBIE`, `JUMP_ZOMBIE`.

Each class has its own:
- starting/max blocks (`CLASS_BLOCKS`)
- accel/sprint/jump/crouch-sneak multipliers
- headshot damage multiplier (`*_HEADSHOT_DAMAGE_MULTIPLIER`)
- damage taken multiplier
- water friction
- can-sprint-uphill flag
- fall damage thresholds
- starting loadout (which tools)
- ability (e.g. SPECIALIST disguise, MEDIC heal-on-spade, MINER drillgun, ROCKETEER RPG)

### 1.4 Tools / Weapons (66 listed in [`aceofspades_nonsteam/aoslib/weapons/`](../../aceofspades_nonsteam/aoslib/weapons))

Primary weapons: `rifle`, `smg`, `tommyGun`, `minigun`, `shotgun`, `shotgun2`, `autoShotgun`, `classicSmg`, `classicShotgun`, `classicRifle`, `lightMachineGun`, `mg`, `pistol`, `autoPistol`, `snubPistol`, `assaultRifle`, `sniper`, `sniper2`, `drillgun`, `ugcDrillgun`, `rpg`, `rpg2`, `ugcRPG2`, `grenadeLauncher`, `mineLauncher`, `chemicalbomb`, `c4`, `dynamite`, `landmine`, `stickyGrenade`, `molotov`, `medPack`, `radarStation`, `rocketTurret`, `mg`, `blockSucker`, `snowBlower`, `ugcSnowBlower`.

Tools: `block`, `flareBlock`, `spade`, `superSpade`, `ugcSuperSpade`, `classicSpade`, `pickAxe`, `ugcPickAxe`, `crowbar`, `digging`, `bomb`, `disguise`, `fakePistol`, `intel`, `knife`, `machete`, `paintbrush`, `prefab`, `ugcPrefab`, `riotShield`, `riotStick`, `diamond`, `ugcTool`, `zombieHand`, `zombiePrefab`, `laserAttachment`, `nullTool`.

Each weapon has: clip size, reserve ammo, fire interval, reload time, damage, headshot multiplier, block damage, range, spread, pellet count, projectile vs hitscan, recoil profile, deploy-yaw lock (for MG/rocket turret), and special placement rules (MG/rocket turret/medpack/landmine/c4 are **placed entities**).

### 1.5 Entities & Pickups

Server-tracked entities sent via `WorldUpdate.updated_entities` and lifecycle packets `CreateEntity(21)`, `DestroyEntity(19)`, `HitEntity(20)`, `ChangeEntity(16)`, `DisableEntity(96)`:

- **Intel** (CTF, CCTF) — pickable, drop on death, capture at tent
- **Tent / Base** — spawn anchor, intel return
- **Capture zones** (TC, MH, OC) — `MinimapZone(43)` + `LockToZone(108)`
- **Diamond crates** (DIA) — pickup objects
- **VIP target** (VIP, TC) — special player marker
- **Demolition objective** (DEM) — plantable bomb spot
- **Crates** (ZOM) — periodic spawn (`RULE_CRATES_SPAWN_TIME`)
- **Placed**: MG (87), Rocket Turret (88), Landmine (89), Medpack (90), Radar Station (91), C4 (92), UGC item (97), Flare Block (104), Dynamite (1)

### 1.6 Prefabs

`PrefabComplete(29)`, `BuildPrefabAction(30)`, `EprasePrefabAction(31)`, `PlaceUGC(97)`. Prefabs are pre-defined block clusters (`shared/constants_prefabs.py`). The "supertower" is the most famous one. UGC prefab sets are referenced by string id in `InitialInfo.ugc_prefab_sets`.

### 1.7 Voice, chat, scoring, ranking

- `VoiceData(103)` — server forwards Steam voice packets between teammates / global.
- `ChatMessage(49)` — global / team / squad channels (`chat_type` byte).
- `RankUps(66)` — XP / rank progression at end of match.
- `GameStats(67)`, `ShowGameStats(53)` — MVP screen + per-player KDA/captures.
- `LocalisedMessage(50)` — client-side translated strings (kill feed, "Intel captured!", etc.).

### 1.8 UGC (User-Generated Content)

UGC mode lets players load custom maps from a host. Packets `RequestUGCEntities(99)`, `UGCMessage(100)`, `UGCMapLoadingFromHost(101)`, `UGCMapInfo(102)`, `PlaceUGC(97)`, `SetUGCEditMode(12)`, `InitialUGCBatch(98)`. We probably ship this last.

### 1.9 Anti-cheat / validation

The original server validates:
- shot origin within `SHOT_ORIGIN_TOLERANCE` of player position
- shot orientation within `SHOT_ORIENTATION_DOT_TOLERANCE` of last-reported orientation
- block placement within reach + line-of-sight (`check_cube_placement`)
- shoot interval ≥ weapon `fire_interval` (`SERVER_SHOOT_INTERVAL_TOLERANCE`)
- packet age (`VALID_PACKET_AGE_ALLOWANCE_PAST=60`, `_FUTURE=30` ticks)
- max distance shoot discrepancy `MAX_DISTANCE_SHOOT_DISCREPANCY=4`
- max velocity discrepancy `MAX_VELOCITY_DISCREPANCY=4`

Some scaffolding exists in [server/combat_runtime.py](../server/combat_runtime.py); most isn't enforced.

### 1.10 Server browser & matchmaking

A2S currently advertises the server. We also need:
- Steam master server registration (so the official server browser sees us)
- the `ServerKeywords` tag list (`mafia`, `classic`, `ctf`, `tdm`, `dm`, `dm_ctf`, ...) so clients filter correctly
- correct `mode_key` in `InitialInfo` per current playlist

---

## 2. Roadmap — ordered for the current state of the codebase

The principle: **fix the foundations before adding modes**. Every mode shares the same packet bus, physics, combat, and game-flow plumbing. If those aren't solid, every mode will be buggy in the same ways.

### Phase 0 — Stabilise the handshake & framing (fix what we have)

Goal: a client can connect, spawn, shoot, build, chat, leave, and reconnect *cleanly*, repeatedly, on every supported map, without warning spam.

- [ ] **Audit `InitialInfo`** ([server/connection.py:459](../server/connection.py)) — every field driven from config or computed (real CRC, real map filename, real mode key, real mode strings via `MODE_MAP_TITLES`).
- [ ] **Audit `StateData`** — `mode_type` from current mode, fog/light from map metadata or config, real `screenshot_cameras_*` from VXL header, real prefab list from active mode, real entity list at startup.
- [ ] **Real map CRC** — replace hardcoded `592649088` with `crc32(vxl_bytes)`. `MapDataValidation` should accept the client's CRC, log mismatch, but proceed (live client tolerates this).
- [ ] **Map metadata parser** — read intel/tent/spawn zones embedded in `.vxl` (the `aoslib.vxl` loader exposes them; currently unused).
- [ ] **Disconnect reasons** — define + use proper enum (`SERVER_FULL`, `WRONG_VERSION`, `BANNED`, `KICKED`, `MAP_CHANGE`).
- [ ] **Reconnect sanity** — `_on_disconnect_sync` should free player ID slot and broadcast `PlayerLeft`. Verify the slot can be re-used by a new connect within the same tick.
- [ ] **Validate the `prefix` byte usage** — confirm with packet captures vs the live client which packets are 0x30 vs 0x31 vs 0x32. Currently it's eyeballed.
- [ ] **Drop the Cython `aoslib.world` dependency from server logic where we don't need it yet** — or rebuild it cleanly. The current `runtime_vxl.py` z-shifting kludge will keep biting us.

Acceptance: 4 clients can join London on default port, see each other, chat, shoot, build, disconnect, reconnect — no log warnings, no client desyncs visible.

### Phase 1 — Full receive-side packet coverage (the ~110 missing handlers)

Currently we register 14. The receive surface from a real client during a CTF match is at minimum:

- ✅ `0` ClockSync, `4` ClientData, `6` ShootPacket, `10` UseOrientedItem, `11` SetColor, `32` BlockBuild, `35` BlockLiberate, `49` ChatMessage, `76` WeaponReload, `77` ChangeTeam, `78` ChangeClass, `110` ClientInMenu, `116` PositionData
- ⏳ `1` PlaceDynamite, `7` PaintBlock, `8` ShootFeedback, `9` ShootResponse, `12` SetUGCEditMode, `13` SetClassLoadout (handled pre-join only), `15` NewPlayerConnection (pre-join), `33` BlockBuildColored, `34` BlockOccupy, `36` ExplodeCorpse, `40` BlockLine, `47` GenericVoteMessage, `48` InstantiateKickMessage, `60` MapDataValidation (handshake), `87` PlaceMG, `88` PlaceRocketTurret, `89` PlaceLandmine, `90` PlaceMedpack, `91` PlaceRadarStation, `92` PlaceC4, `93` DetonateC4, `94` BlockSucker, `95` Disguise, `97` PlaceUGC, `99` RequestUGCEntities, `103` VoiceData, `104` PlaceFlareBlock, `105` SteamSessionTicket (pre-join), `113` PasswordProvided

For each: write the handler, validate the data, broadcast (or respond), update authoritative state. Group by feature (placement, combat-secondary, chat/admin, voice, UGC) so each batch can ship as a working unit.

### Phase 2 — Authoritative physics & movement reconciliation

The server must own movement; the client predicts. Right now we trust client position blindly.

- [ ] Wire `aoslib.world.Player` into `Player.update()` so server simulates movement from input flags + orientation, ticks gravity/friction/water, and emits authoritative position in `WorldUpdate`.
- [ ] Reconcile against `PositionData(116)` reports — clamp/snap if drift > `POSITION_HARD_SNAP_THRESHOLD`.
- [ ] Per-class movement multipliers from [shared/constants.py](../shared/constants.py) (`CLASS_ACCEL_MULTIPLIER`, etc.) actually applied in the kernel.
- [ ] Crouch / sneak / sprint / jump buffer logic matching the reversed `aoslib.world.move_player`.
- [ ] Fall damage (`falling_damage_min/max_distance/_max_damage`) per-class.
- [ ] Water damage at z > water_level (`water_damage` config).
- [ ] Weapon-deployed yaw lock (MG, rocket turret).

Acceptance: a client with severe lag can't speedhack or teleport; movement looks identical to vanilla server side-by-side.

### Phase 3 — Combat completeness

- [ ] Per-weapon profile resolution with full table (currently 4 profiles, need ~30).
- [ ] Headshot detection using bone box from `aoslib.world.Player.eye` + class headshot multiplier.
- [ ] Hit registration server-side (we have hitscan stubs); broadcast `KillAction(46)`, `Damage(37)`, `SetHP(5)` correctly.
- [ ] Pellet shotguns (seeded RNG so client/server agree on pattern).
- [ ] Grenade physics — `aoslib.world.Grenade` already restored; spawn on `UseOrientedItem(10)` with timer, simulate, broadcast explosion, deal AOE damage + block damage.
- [ ] Explosive AOE damage with falloff (`ROCKET_FALLOFF`, `ExplosionDamageManager`).
- [ ] C4: place via `PlaceC4(92)` → entity → `DetonateC4(93)` triggers explosion.
- [ ] Landmines: place → invisible/visible per team → trigger on enemy proximity.
- [ ] MG / Rocket Turret: place → entity with health → enemies can damage it → it shoots (server-driven AI).
- [ ] Medpack: place → heals teammates within radius up to N times.
- [ ] Radar Station: place → reveals enemies on minimap within radius.
- [ ] Block sucker (`94`): suck blocks from world into player inventory.
- [ ] Paint block (`7`): change color of existing block (with cost?).
- [ ] Block line / flare block / dynamite.
- [ ] Corpse explosion if `enable_corpse_explosion=1`.

### Phase 4 — Class system & loadouts

- [ ] Loadout enforcement: server-side validation that the tools in `SetClassLoadout` are allowed for the selected class (`shared/constants.py` has the per-class allowed-tool tables).
- [ ] Specialist disguise (`Disguise(95)`): toggle, hide team/name from enemies during world updates.
- [ ] Mafia classes: VIP designation, melee-only gangsters.
- [ ] Zombie classes: melee-only loadout, sprint multiplier, jump multiplier (`JumpZombie`), team is auto-assigned, reinfect on death.
- [ ] UGC builder class for UGC mode.
- [ ] Class change kills the player (`KILL_CLASS_CHANGE`) — already partial.

### Phase 5 — Game flow & match lifecycle

- [ ] Warmup state (no scoring, free join) → countdown → match → `MapEnded(52)` → `ShowGameStats(53)` → rotation.
- [ ] Score limit + time limit per mode.
- [ ] `DisplayCountdown(84)` for round timers.
- [ ] `ForceShowScores(72)` at end of map.
- [ ] `GameStats(67)` payload — per-player kills/deaths/captures/objective points/MVP.
- [ ] `RankUps(66)` — XP awarded, rank changes.
- [ ] Map rotation from playlist file (parse `playlists/*.txt`).
- [ ] `LockTeam(79)` / `TeamLockClass(80)` / `TeamLockScore(81)` mid-match controls.
- [ ] `ForceTeamJoin(115)` for auto-balance.

### Phase 6 — Per-mode logic

Implement each in its own [modes/](../modes/) file, each subclassing `BaseMode`. Order by complexity / shared scaffolding:

1. **TDM** — simplest; just count team kills, score limit. Validates flow plumbing.
2. **CTF (full)** — already partial; replace hardcoded positions with map metadata, fix drop/return/auto-return rules, intel-tool mechanics, `RULE_CTF_ENABLE_SHOOT_WITH_INTEL` etc.
3. **CCTF (Classic CTF)** — CTF + class restriction to `CLASSIC_SOLDIER` + classic-weapon-only + ground color override + minimap off.
4. **OC (Occupation)** — single zone, hold-to-progress, `TeamProgress(117)`, `MinimapZone(43)`, `LockToZone(108)`.
5. **MH (MultiHill)** — multiple zones rotating in sequence; reuse OC machinery.
6. **TC (Territory)** — multiple bases captureable in any order, `TerritoryBaseState(106)`, base-contested logic, mafia VIP.
7. **VIP** — single-life rounds, mafia classes, VIP target, kill-VIP-or-survive win condition.
8. **DEM (Demolition)** — single-life rounds, plantable bomb sites, `BlockManagerState(38)`, `ServerBlockAction(39)`.
9. **DIA (Diamond Mine)** — minable diamonds (drillgun/diamond tool), pickups, deposit at base, `Restock(69)`, `PickPickup(70)`, `DropPickup(71)`.
10. **ZOM (Zombie)** — auto-team-assignment, zombie classes, infect on melee kill, periodic crate spawns, last-survivor-wins.
11. **NORMAL** — generic deathmatch fallback.
12. **UGC** — last; needs UGC entity batches and host-supplied maps.

### Phase 7 — Admin, persistence, server browser

- [ ] Admin auth via in-game password, persistent admin list.
- [ ] Ban list (IP / Steam ID), persisted to disk; check on connect.
- [ ] Kick / mute / move / teleport commands (some scaffolded in [commands/](../commands/)).
- [ ] Vote-kick / vote-map (`GenericVoteMessage(47)`, `InstantiateKickMessage(48)`).
- [ ] Server password (`Password(111)`, `PasswordNeeded(112)`, `PasswordProvided(113)`).
- [ ] Steam master server heartbeat — appear in the official browser.
- [ ] `ServerKeywords` properly composed (mode-tag + map flags) so client filters work.

### Phase 8 — Performance, polish, hardening

- [ ] WorldUpdate interest management — per-player visibility filter (`CHARACTER_DRAW_RANGE`, `DEBRIS_DRAW_RANGE`).
- [ ] Bandwidth cap per peer (`config.toml` `bandwidth_limit` is parsed but unused).
- [ ] LZF: real implementation if we ever bypass ENet's compressor.
- [ ] Load test: 32 players, 60 Hz tick, simultaneous combat + builds.
- [ ] Crash isolation: per-handler exception walls so one bad packet doesn't kill the loop.
- [ ] Replay / demo recording (optional).
- [ ] Plugin / scripting API (optional, last).

---

## 3. Cross-cutting tracks (work in parallel with phases above)

### 3.1 Protocol fidelity

`../aoslib-reversed/aosprotocol.1x.md` has known-incomplete entries: WorldUpdate(2), EntityUpdates(3), ChangeEntity(16), CreateEntity(21), ErasePrefabAction(31), BlockManagerState(38), ServerBlockAction(39), InitialUGCBatch(98), PositionData(116). Each gap closed unblocks the matching feature. Treat live-client packet captures as authoritative when the doc is silent — see `compare_packets.py` and `decode_hex.py` in `../aoslib-reversed/`.

### 3.2 Test strategy

- Unit tests against `shared.packet` round-trips (already exist in `tests/test_reversed_*.py`).
- Add **golden replay tests**: capture real client→server packet sequences, replay them, assert server state.
- Each phase delivers tests; nothing ships without coverage of the new packet handler.
- Dual-run any new packet against `aosdump` Python 2 binaries to confirm wire compatibility.

### 3.3 Anti-cheat / validation

Implement in lockstep with each combat feature, not as a final pass. Constants are already in [shared/constants.py](../shared/constants.py): `MAX_DISTANCE_SHOOT_DISCREPANCY`, `MAX_VELOCITY_DISCREPANCY`, `SERVER_SHOOT_INTERVAL_TOLERANCE`, `VALID_PACKET_AGE_ALLOWANCE_*`, `MAX_PACKET_DECOMPRESSION_SIZE`.

### 3.4 Configuration & playlists

Parse `playlists/*.txt` (Python literal dicts) into a `Playlist` dataclass. Drive map rotation, mode selection, custom rules, and class restrictions from playlists, not `config.toml` per-server hardcodes. `config.toml` should select a *playlist*, not a single map+mode.

---

## 4. Suggested next concrete steps (this week, not this year)

In order, smallest first:

1. **Replace the hardcoded `InitialInfo` map filename + CRC** with the real values from the loaded VXL. Easiest visible fix; unblocks proper map validation.
2. **Drive `mode_name`/`mode_description`/`mode_key` from `MODE_MAP_TITLES`/`MODE_DESCRIPTIONS`/`MODE_MODE_IDS`** in [shared/constants_gamemode.py](../shared/constants_gamemode.py).
3. **Register the missing high-frequency handlers** (`PlaceDynamite(1)`, `PaintBlock(7)`, `BlockBuildColored(33)`, `BlockOccupy(34)`, `BlockLine(40)`) — even as stubs that broadcast — to stop the "Unhandled packet ID" log spam and let basic building feel right.
4. **Real map CRC + map metadata parsing** so intel/tent positions stop being hardcoded XY.
5. **Grenade lifecycle end-to-end** — first feature that exercises `aoslib.world.Grenade` + AOE damage + `WorldUpdate` entities. Touching this proves out the patterns we'll need for C4, dynamite, rockets, mines.
6. **TDM mode** — simplest mode, validates the match-flow plumbing without intel/zone complexity.
7. **Playlist parser + map rotation** — once one full match can end, we can cycle.
8. From there, walk Phase 2 (movement) and Phase 3 (combat completeness) before adding more modes.

---

## 5. References

- Protocol spec: [`../aoslib-reversed/aosprotocol.1x.md`](../../aoslib-reversed/aosprotocol.1x.md) (1647 lines, all packet IDs)
- Reversed packet definitions: [`../aoslib-reversed/shared/packet.pyx`](../../aoslib-reversed/shared/packet.pyx)
- Reversed physics: [`../aoslib-reversed/aoslib/world.pyx`](../../aoslib-reversed/aoslib/world.pyx)
- Restoration notes: [`../aoslib-reversed/docs/world-restoration.md`](../../aoslib-reversed/docs/world-restoration.md), `vxl-restoration.md`, `kv6-restoration.md`
- Live client code: [`../aceofspades_nonsteam/aoslib/`](../../aceofspades_nonsteam/aoslib) and [`../aceofspades_nonsteam/aoslib/weapons/`](../../aceofspades_nonsteam/aoslib/weapons)
- Original server reference (compiled): `../aoslib-reversed/aosdump/server.py`, `../aoslib-reversed/aosdump/aoslib/network.pyd`
- Playlists: [`../aceofspades_nonsteam/playlists/`](../../aceofspades_nonsteam/playlists)
- Map metadata: [`../aceofspades_nonsteam/playlists/mapinfo.py`](../../aceofspades_nonsteam/playlists/mapinfo.py)
