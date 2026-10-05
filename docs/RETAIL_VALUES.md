# Retail values audit

Audited 2026-09-26. This compares every gameplay tunable the server uses with
the retail Ace of Spades 1.x value, and records each place where BattleSpades
differs on purpose.

## Sources

- **GAME_RULES_LIST**: Match Lobby rule defaults and allowed values, from
  `shared/constants_matchmaking.py`. The original obfuscated copy is in
  `aceofspades_nonsteam/shared/`.
- **CG**: mode constants in `shared/constants_gamemode.py`.
- **C**: core constants in `shared/constants.py`.
- **Code default**: `ServerConfig()` or `GameRules.server_defaults()`, used
  when a server starts without TOML.
- **Shipped**: the sample `config.toml` (also `config.steam-local.toml`) and
  the ten fleet profiles `configs/official-*.toml`. The fleet profiles set
  only `RULE_SPAWN_PROTECTION_TIME`, `match_length_minutes` and non-gameplay
  keys. Every other rule comes from the code default.

`tests/test_retail_values.py` enforces this document. The rule catalog must
match GAME_RULES_LIST value for value. Server defaults and every shipped
config may differ from retail only by the documented deviations.

Status key:

- **retail**: matches retail.
- **fixed**: was wrong and was corrected in this audit.
- **deliberate**: differs on purpose, and the reason is written in the code.
- **server choice**: retail has no value for this setting.
- **not implemented**: a retail value exists, but the server has no matching
  behaviour.

## Changes made in this audit

| Setting | Before | After (retail) |
|---|---|---|
| `RULE_CTF_SCORE_TARGET` (code default, `config.toml`, fleet CTF) | 10 | **5** |
| `RULE_CRATES_SPAWN_TIME` (code default, `config.toml`, fleet) | 15 s | **25 s** |
| `[modes.zom] infection_delay` (`config.toml`) | 30 s | **60 s** |
| `mode_data` score fallbacks (StateData before a mode exists) | ctf 10, dia 10, oc 100, tc 100, zom 1, dem 5 | ctf 5, dia 15, oc 30, tc 5, zom 3, dem 1 |

None of these three rules had a written reason for differing from retail.
The fleet profiles do not override them, so after the next deploy official
CTF plays to 5 captures and crates respawn after 25 s. Zombie fleet servers
were already at 60 s because only the sample config overrode it.

## Match Lobby general rules

| Setting | Retail (source) | Code default | Shipped | Status |
|---|---|---|---|---|
| Respawn time `RULE_RESPAWN_TIMES` | 10 s (`C.DEFAULT_RESPAWN_TIME`; choices 0-60 step 5) | 5 | 5 | deliberate: historic BattleSpades pace, see `GameRules.server_defaults` |
| Classic Deuce weapons `RULE_ENABLE_WEAPON_CLASSIC_SMG` / `RULE_ENABLE_WEAPON_CLASSIC_SHOTGUN` | OFF (`classic.txt` playlist; `constants_matchmaking`) | OFF | ON in `official-cctf.toml` | deliberate: players asked for the Deuce rifle/SMG/shotgun choice on the official Classic CTF servers; other configs keep retail OFF |
| Spawn protection `RULE_SPAWN_PROTECTION_TIME` | 3 s (OFF/1/2/3) | 0 (bare/test servers) | 3 | retail when shipped; ends early when the player attacks or picks up an objective |
| Crate respawn `RULE_CRATES_SPAWN_TIME` | 25 s (`C.CRATE_SPAWN_DELAY`; 10-60 step 5) | 25 | 25 | fixed |
| Block health `RULE_BLOCK_HEALTH` | 100% (50/100/200) | 100% | 100% | retail |
| Weapon damage `RULE_WEAPON_DAMAGE` | 100% | 100% | 100% | retail |
| Block wallets (refill amount) `RULE_CHARACTER_BLOCK_WALLETS` | 100% | 100% | 100% | retail |
| Character speed `RULE_CHARACTER_SPEED` | 100% (50/100/150/200) | 100% | 100% | retail |
| Fall/water damage `RULE_ENABLE_FALL_ON_WATER_DAMAGE` | ON | ON | ON | retail. The rule only zeroes the WATER landing damage; `[game] fall_damage` is the separate operator switch for all fall damage (an explicit rule no longer rewrites it; it is mirrored into the rule only when the rule is absent) |
| One-hit kill, teabag points | OFF, OFF | OFF | OFF | retail |
| Gravestones, corpse explosion, sniper beam, death cam, minimap, spectators, colour picker | ON | ON | ON | retail |
| Blocks, prefabs | ON | ON | ON | retail |
| Flare blocks `RULE_ENABLE_FLARE_BLOCKS` | ON | ON | ON | retail since 2026-09-29. The Flare Block (tool 22, 10 blocks, radius-5 light) is the first tile of the Constructs page for every class except Zombie and Classic Soldier (`selectClass.py`, `flareBlockTool.py`); it had been hidden on the mistaken belief that the tile was fake |
| Vote-kick share `RULE_VOTES_REQUIRED_FOR_KICK` | 50% (25/50/75) | 50% | 50% | retail |
| Classes (7 `RULE_ENABLE_CLASS_*`) | all ON | ON | ON | retail |
| Equipment (20 `RULE_ENABLE_EQUIPMENT_*`, plus hidden parachute) | all ON | ON | ON | retail |
| Weapons (28 `RULE_ENABLE_WEAPON_*`) | ON except Classic SMG and Classic Shotgun (OFF) | same | same | retail |
| Friendly fire `[game] friendly_fire` | no lobby rule (InitialInfo flag only) | off | off | server choice (retail servers are believed to be off; unverified) |
| Max players | Lobby choices 2-24 (`NOOF_PLAYERS_LIST`) | 50 (dedicated, clamped 1-255) | 24 | retail when shipped; the code default applies only without TOML |

## Per-mode values

Round clocks come from `CG.DEFAULT_MODE_GAME_LENGTH`. Every mode matches
retail in `mode_data`, in the `config.toml` `[modes.*]` overlays and in the
fleet `match_length_minutes`: TDM, Demolition, Diamond Mine, Occupation and
VIP 15 min; CTF 30 min; Classic CTF 90 min; Multi-Hill and TC 25 min; Zombie
10 min.

The retail HUD prints the countdown as `gmtime` minutes and seconds with no
hour field, so a clock above one hour (Classic CTF, the lobby's 90-minute
option) used to show 30:00, reach 00:00 an hour early and wrap to 59:59.
`scoreboard.send_round_timer` holds DisplayCountdown(84) at 59:59 until the
last hour; the round length is unchanged and 00:00 always means the round
has ended.

### Team Deathmatch

| Setting | Retail | Ours | Status |
|---|---|---|---|
| Score target `RULE_TDM_SCORE_TARGET` | 200 | 200 | retail |
| Team points per kill | 1 (`CG.TDM_TEAM_SCORE_FOR_KILL`) | 1 (`kill_points`) | retail |
| `headshot_bonus` | none | 0 | server extension, off |

### CTF and Classic CTF

| Setting | Retail | Ours | Status |
|---|---|---|---|
| Score target `RULE_CTF_SCORE_TARGET` | 5 (1-10) | 5 | fixed (was 10) |
| Intel auto-return | ON; `CG.CTF_INTEL_RETURN_TIME` = 60 s | ON, 60 s | retail |
| Return on touch, score with own intel home | OFF, OFF | OFF, OFF | retail |
| Shoot while carrying | OFF | OFF | retail |
| Classic CTF playlist | 5 captures, carrier may shoot, no auto-return | same (class defaults and `[modes.cctf]`) | retail |
| Re-pickup after drop | 2.5 s (`C.NO_PICKUP_AFTER_DROP_TIME`) | 2.5 s | retail |
| Classic base capture radius | 5 (`CG.CLASSIC_CTF_BASE_CAPTURE_DISTANCE`) | 5 | retail |
| Carrier minimap exposure | 30 s (`C.INTEL_MINIMAP_EXPOSURE_TIME`) | carrier shown for the whole carry | (superseded: implemented, see PARITY_CHANGES_2026-09) |
| CTF intel home | on the team's authored base point (no CTF intel offset constant; maps author only base boxes) | on the base anchor (nearest safe column to the `ctf_base_points` centre); 12 blocks toward midfield only on maps with no recovered base data | fixed (was 12 blocks toward the enemy, clamped into the box) |
| Classic CTF intel home | ≥ 3 from the base point (`CG.CLASSIC_CTF_INTEL_MIN_RADIUS_FROM_BASE`), inside the 5-block capture radius | exactly 3 toward the enemy base | retail radius; the direction is not recoverable (**inferred**) |

### Zombie

| Setting | Retail | Ours | Status |
|---|---|---|---|
| Rounds per map `RULE_ZOMBIE_NOOF_ROUNDS` | 3 (1-5) | 3 | retail |
| First infected `RULE_NOOF_FIRST_INFECTED_ZOMBIES` | 2 (1-5) | 2 | retail |
| Delay before first infection | 60 s (`CG.ZOM_TIME_BEFORE_FIRST_INFECTION`) | 60 s (code); sample config was 30 | fixed in `config.toml` |
| Zombie respawn | 0 s (`CG.ZOM_RESPAWN_AS_ZOMBIE_TIME`) | 0 s | retail |
| Round intermission | 5 s (`CG.ZOM_TIME_AFTER_ZOMBIE_WIN_BEFORE_SCORES`) | 5 s | retail |
| Class speed / zombie damage | 100% / 100% | 100% / 100% | retail |
| Minimum players to start | none | 2 | server choice |
| First-zombie spawn protection | 0.5 s (`C.FIRST_ZOMBIE_SPAWN_PROTECTION_TIME`) | none | (superseded: implemented, see PARITY_CHANGES_2026-09) |

### VIP

| Setting | Retail | Ours | Status |
|---|---|---|---|
| Rounds `RULE_VIP_NOOF_ROUNDS` | 3 (1-5), `VIP_NOOF_ROUNDS_BEFORE_NEXT_MAP` | 3 sub-rounds PLAYED, winner by rounds won, tie = draw | retail value; rounds-played reading **inferred** from the Zombie twin rule (was: first to 3 wins) |
| Dead VIP / sudden-death team KillAction | `NEVER_RESPAWN_TIME` 255 ("No respawns!") | 255 (0 during a sub-round reset) | retail (was 0) |
| VIP health `RULE_VIP_HEALTH` | 100% | 100% | retail |
| Sudden death `RULE_ENABLE_SUDDEN_DEATH` | ON | ON (the dead VIP's team stops respawning) | retail |
| Selection delay | 10 s (`CG.VIP_SELECTION_DELAY`, defined twice: 3.0, then 10.0) | 10 s | retail |
| Minimum team size to start | 1 | 1 | retail |
| Round intermission | none specific (generic `CG.TIME_AFTER_WIN_BEFORE_SCORES` = 5 s) | 7 s | server choice, unsourced |
| Sudden-death damage over time | 1 HP every 1 s after 60 s, starting 5 s after a VIP kill (`CG.VIP_SUDDEN_DEATH_*`) | none | (superseded: implemented, see PARITY_CHANGES_2026-09) |

### Multi-Hill

| Setting | Retail | Ours | Status |
|---|---|---|---|
| Active bases `RULE_MULTIHILL_MAX_ACTIVE_BASES` | 1 (1-5) | 1 | retail |
| Base active time `RULE_BASE_ACTIVE_TIME` | 240 s | 240 s | retail |
| Time between activations | 10 s | 10 s | retail |
| Team score tick | 1 point per 1 s | same | retail |
| Score limit | no retail rule | 100 (`mode_data`) | server choice |

### Territory Control

| Setting | Retail | Ours | Status |
|---|---|---|---|
| Active bases `RULE_TC_MAX_ACTIVE_BASES` | 5 (2-5) | 5 | retail |
| Capture rate `RULE_CAPTURE_RATE` | 100% | 100% | retail |
| Capture tick | 0.5 s (`CG.TC_CAPTURE_TICK_RATE`) | 0.5 s | retail |
| Capture speed | stock A2550 `TC_CAPTURE_RATE` = `[(0,0), (1,1), (5,4), (10,7), (15,9)]`: capture % per 0.5 s tick by capturing players (the decompile's [115..119] was corrupt) | the table, linear between points, x `RULE_CAPTURE_RATE`: 1 player 50 s, 5 players 12.5 s per ownership step | retail table; per-tick %, interpolation **inferred** (was 0.05/s per net occupant, 5-10x faster) |
| Contested base | `TC_BASE_CONTENDED` state, "Contend" score | frozen while both teams stand in it | **inferred** (was: moved by the head-count difference) |
| Claim / Control award | `TC_SCORE_CLAIM` / `TC_SCORE_CONTROL` | every capturing occupant (Multi-Hill: every claiming occupant) | **inferred** (was: lowest id only) |
| Win | own every active base | same (score limit = base count) | retail |

### Diamond Mine

| Setting | Retail | Ours | Status |
|---|---|---|---|
| Score target `RULE_DIA_SCORE_TARGET` | 15 (5-60 step 5) | 15 | retail (the `mode_data` fallback was 10, fixed) |
| Active bases | 1 | 1 | retail |
| Max active diamonds | 2 | 2 | retail |
| Diamond lifetime | 60 s | 60 s | retail |
| Time between diamond spawns | 15 s | 15 s | retail |

### Demolition

| Setting | Retail | Ours | Status |
|---|---|---|---|
| Build phase `RULE_BUILD_STATE_LENGTH` | 30 s (OFF or 10-120) | 30 s | retail |
| Airstrike wait | 5 s (`CG.DEM_TIME_TO_WAIT_FOR_AIRSTRIKE`) | 5 s | retail |
| Repair warning | 75% | 75% | retail |
| Destroy/repair scoring | 25 per 50 / 50 per 50; no repair score while building | same | retail |
| End-of-round hold after the airstrike | none | 3 s (`AIRSTRIKE_IMPACT_DELAY`) | deliberate: lets every client see the impacts (code comment) |

### Occupation

| Setting | Retail | Ours | Status |
|---|---|---|---|
| Score target `RULE_OCC_SCORE_TARGET` | 30 | 30 | retail (the `mode_data` fallback was 100, fixed) |
| Max active bombs | 1 (1-3) | 1 | retail |
| Bomb fuse `RULE_BOMB_FUSE_TIME` | 10 s (`C.BOMB_EXPLOSION_FUSE`) | 10 s | retail |
| Bomb respawn after explosion | 10 s | 10 s | retail |
| Team score for a bomb in base / carrier kill | 3 / 1 | 3 / 1 | retail |

## Other server-side values

| Setting | Retail | Ours | Status |
|---|---|---|---|
| Capture-point resupply | 10 s (`C.CAPTURE_POINT_REFILL_TIME`); server-only `CAPTURE_POINT_DISTANCE` 3.0 | owned TC territory / held MH hill, whole zone (superseded: implemented, see PARITY_CHANGES) | 3.0 distance unused: its point (TC flag? Classic tent?) is unrecovered |
| Map-vote window | `TIME_AFTER_MAP_VOTE_START_BEFORE_END` 10 s | ballot opens 10 s before the clock ends and runs 10 s | reading of the name (was 60 s lead, 15 s ballot) |
| Block build spacing | server-only `MIN_BLOCK_INTERVAL` 0.1 s | enforced by `[anticheat] enforce_block_interval` (default log-only); bots pace to it | retail value |
| Build reach | 10 (`MAX_BLOCK_DISTANCE`), Classic 5 (`CLASSIC_MAX_BLOCK_DISTANCE`) | 10 / 5 + 9 blocks drift slack | retail (Classic used 10) |
| Fall/world death credit | server-only `PLAYER_INTERACTION_EXPIRY_SECONDS` 5.0 | a fall death credits the enemy who hit the victim within 5 s; assists use the same 5 s window (was 10 s) | **inferred** use of the constant |
| Rocket-jump landing | server-only `ROCKET_JUMP_FALL_DAMAGE_MULTIPLIER` 0.2 | x0.2 on the landing after an own RPG/RPG2 self-push | **inferred** trigger |
| Short-air landing | server-only `ZERO/MAX_FALL_DAMAGE_AIR_TIME` 1/60 s, 4/60 s | fall damage scaled 0..1 over that air time | **inferred** semantics (normal falls unchanged) |
| One-hit-kill rule | `ONE_HIT_KILL_WEAPONS` (A2382) kill types | only those kill types are instakills | retail (was every sourced hit) |
| Radar station lifetime | 45 s (retail `A1901`, now `C.RADAR_STATION_LIFETIME`) | 45 s (`[game] radar_station_lifetime_seconds`), sent as the packet-21 fuse | fixed (was 35 s with no source) |
| Radar station detection range | 250 blocks (retail `A1900`, now `C.RADAR_STATION_RANGE`) | the client applies it itself from the radar entity | retail (see the radar note below) |
| Radar station replacement | a new station destroys the owner's previous one (AoS wiki) | same: a valid new placement tears the old one down (DestroyEntity 19) | fixed (the second placement used to be refused) |
| Generic `[game] score_limit` | none | 10 | legacy CTF-era field. Modes do not read it. StateData uses it only before a mode object exists. |

### CTF intel placement (evidence, 2026-09-26)

- No retail map format carries an intel point. The recovered `.txtc`
  (Trenches) has only `ctf_base_points` + `ctf_base_w_h_d` (10x10x10
  boxes). The retail UGC editor (`UGC_ITEM_*`, `UGC_ZONE_SIZES`) offers
  Blue/Green/Neutral base zones and spawn zones but no intel item, and the
  shipped UGC maps' `mode: ctf` entities are only `ugc_baseblue_*` /
  `ugc_basegreen_*`. So the retail server derived the intel from the base.
- The only intel-placement constant is Classic-only:
  `CLASSIC_CTF_INTEL_MIN_RADIUS_FROM_BASE = 3` next to
  `CLASSIC_CTF_BASE_CAPTURE_DISTANCE = 5`. The CTF block (`CTF_*`) has no
  intel offset or radius. Neither constant is read by any client binary
  (no `A2645`/`A2646` string anywhere in the stock pyds), so both are
  server rules.
- The client has no base/tent model or entity (`models.py` has only the
  intel models; `ENTITIES` exposes `INTEL_PICKUP`, and the base is a
  `ZONE_ICON_CTF` zone). Nothing client-side positions the intel relative
  to the base; it is a plain packet-21 entity.
- The older servers in the tree (`old_back/ace-server`, `aoslib-reversed`
  `aosmodes/ctf.py`) are community rewrites that place intel and command
  post at random team-area spots (0.75 style); they are not retail and do
  not use either constant.

Decision: CTF puts the intel on the base point (offset 0 inside the
authored box). Classic CTF keeps the retail 3-block minimum radius, which
the 5-block capture radius still covers, toward the enemy base (the
retail direction is unknown). Maps without recovered base data keep a
12-block server-choice offset from the inferred anchor so the intel is not
in the spawn. Tests: `tests/test_retail_intel_radar.py`,
`tests/test_retail_spawn_boxes.py`.

### Radar station (evidence, 2026-09-26)

- Retail constants block, in order: FAR_RADIUS 10, SHOOT_INTERVAL 1.5,
  MODEL_SIZE 0.03, `A1899` 45, `A1900` 250, `A1901` 45, MODEL_Z_OFFSET
  -0.55. Headless IDA on the stock `aoslib.scenes.main.gameScene.pyd`: no
  code reads `A1899` or `A1901`; `A1900` is read only by
  `RadarStationEntity.can_detect_player` (squared distance to the enemy's
  network position). So 250 is the **range**. The old labels
  (`LIFETIME = 250`, `RANGE = 45`) were wrong and are now
  `HEALTH = 45`, `RANGE = 250`, `LIFETIME = 45`. HEALTH follows MODEL_SIZE
  as in the C4 and medpack blocks; the two 45s are interchangeable by value.
- `RadarStationEntity.set_fuse` stores the packet fuse as `lifetime` and
  `update` counts it down (`lifetime -= dt` while > 0). The client has no
  lifetime of its own, so the server value is what players see.
- The Jagex-supported Ace of Spades wiki (Radar Station page) says the
  station "will self-destruct after 45 seconds, after another one by the
  same [player] is placed, or when shot enough times".
- Range: `hud.Minimap.draw` builds a list of the viewer team's
  `RADAR_STATION_ENTITY` (`A935`) entities and, for each enemy, calls
  `radar.can_detect_player(player)`. Detection is client-side and needs
  only the entity with its team. The server does no range test of its own.
  The server used to also send `TeamMapVisibility` (83); it no longer does
  (see the live check below).
- Live check (dev client, TDM ArcticBase with bots, 2026-09-26), with packet
  83 suppressed: after the client's own `send_place_radar_station`, each
  enemy was `can_detect_player` True at 36-152 blocks and False when the
  client radar was moved to 289-418 blocks away; teammates were always
  False; `teams[2]`/`teams[3].can_see_other_team` stayed 0. A per-frame hook
  showed the client calling `can_detect_player` for every player on its
  own. So the packet-21 entity (type 36, team state, position, fuse) is all
  the client needs.
- What 83 does: the client handler sets `teams[packet.team_id]
  .can_see_other_team`. Setting the VIEWER's team flag made
  `Player.get_map_icon` return an icon for every enemy (whole team, no
  range). Our old packet named the ENEMY team, which set a flag the owner's
  client never reads, so it was a no-op at best and a range-less reveal if
  ever sent the other way. Radar no longer sends it (`server/main.py`).
- Replacement: a valid new placement by the same player tears the old
  station down (team count, owner slot, DestroyEntity 19) before the new
  CreateEntity. Verified live: station 53 (29.9 s left) was destroyed when
  the owner placed station 58, and the client kept only 58. Stock rules are
  unchanged (1 carried, +1 per ammo crate), so replacing needs a restock or
  a new life. The live fuse is kept at the remaining lifetime so a late
  joiner's CreateEntity replay counts down from the right value.
- Live stations also die to enemy fire (45 health): in the same session
  bots shot two stations down within 20-30 s.


These need behaviour changes, not value changes, so they are left for a
separate task:

- (superseded) VIP sudden-death damage over time, the first-zombie 0.5 s
  spawn protection, the CTF carrier minimap exposure timer and capture-point
  resupply were all implemented later (docs/PARITY_CHANGES_2026-09.md).
- (superseded) The TC capture speed was recovered: stock A2550 (see the
  Territory Control table). The per-tick reading and the Multi-Hill score
  limit should still be confirmed live against a retail capture if one is
  ever available.
