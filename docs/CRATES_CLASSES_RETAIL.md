# Supply crates and class deployables: retail vs BattleSpades (2026-09-26)

Sources: stock client `aoslib/scenes/main/gameScene.pyd` (identical to the
Steam `aoslib.scenes.main.gameScene.pyd`), headless IDA on a private copy
(`scratchpad/crates_ida/`: `dec_classes.py` decompiles every function whose
Cython traceback string names a class, `asm_names.py` dumps a function with
its interned names resolved, `who_uses.py` lists the functions that read an
obfuscated `A####` constant), the shipped `shared.constants` from the Steam
`aos.pkg` (unmarshalled with the client's own Python 2.7; it matches the
non-Steam `shared/constants.py` except `A159`), and live measurements on the
dev client (validation server port 27046, tracer console 32916).

The retail *server* source is not available. Anything below marked
**inferred** is our reading, not a recovered value.

## 1. Supply crates

### What the client does (recovered + live-measured)

`AmmoCrate`/`HealthCrate`/`BlockCrate`/`JetpackCrate` (entity types 3/4/5/6)
all derive from `Crate(SpinningEntity)` (`crate.py`). `Crate.initialize`
builds a `GenericMovement` world object from the packet-21 position and
velocity (`set_bouncing(True)`, `set_stop_on_collision(False)`), so **any
crate created above the ground falls on the client by itself**.
`Crate.update` then, every frame:

- starts the `cratedrop_freefall` loop (11.5 s long) while falling and closes
  it on landing; plays `cratedrop_land` (`CRATEDROP_LAND_SOUND`) when the
  impact speed passes `landing_sound_threshold`;
- hit-scans straight down; below `CRATE_PARACHUTE_DEPLOYMENT_HEIGHT` (10)
  it sets `parachute_deployed`, draws `CRATE_PARACHUTE_MODEL`
  (`Crate_Parachute.kv6`) and plays `cratedrop_chuteopen`; below
  `CRATE_PARACHUTE_REMOVAL_HEIGHT` (2) it sets `parachute_removed`;
- while deployed and not removed: `world_object.set_velocity(velocity *
  CRATE_PARACHUTE_SLOWDOWN)` (0.75), after the move.

Live trace (AmmoCrate created at z=100 over a column whose top solid is 213,
samples every 0.05 s): free fall at exactly 30 blocks/s² (1.5 per sample),
chute opens at z≈205 (8 above ground), stored speed converges to 1.500
(=0.75·(v+0.5) at 60 Hz) while the crate moves 2.0 blocks/s, chute released
at z≈211.1, crate lands, bounces twice and rests at **z = support voxel**
(213.0). A crate created on the ground reports `parachute_deployed` and
`parachute_removed` already set, so a ground crate never shows a chute.
Nothing is sent by the server during the fall.

The three `CRATEDROP_FLYBY_*_SOUND_ID`s (24 `_pos_ww`, 25 `_pos`,
26 `_space_pos`, 5.9-7.2 s) are network sound ids: only the server can play
them, i.e. the retail server announced each drop with a positioned aircraft
fly-by.

`AmmoDropPointEntity`/`HealthDropPointEntity`/`BlockCrateDropPointEntity`
(18/19/20, `Crate_Target` model with a 3D name label) are the UGC editor's
drop-point markers. They are ordinary visible entities (verified by creating
one on the live client), not something a normal match shows, so the server
does not create them.

No crate string id exists in `aoslib/strings`: there is no chat/HUD
announcement for crates.

### Retail vs ours

| Item | Retail | Before | Now |
|---|---|---|---|
| Positions | map `*_crate_drop_points` (4 stock maps) / UGC drop points | same; fallback spots near bases on maps without them | unchanged |
| Types | ammo 3, health 4, block 5 per drop-point array; jetpack 6 | same | unchanged |
| Respawn delay | `CRATE_SPAWN_DELAY` 25 = `RULE_CRATES_SPAWN_TIME` default | 25 via rule (table default 15) | 25; behaviour table now also defaults to `CRATE_SPAWN_DELAY` |
| Respawn | air drop: client falls/parachutes any airborne crate | reappeared on the ground | **re-created at the drop altitude and parachuted down** (live-verified) |
| Drop altitude | not in any table | - | **inferred**: top of the world (`CRATE_DROP_START_Z` = 1.0); a drop point under a roof starts two cells below the roof |
| Fly-by cue | server-only sound ids 24/25/26 | never sent | positioned PlaySound at the drop start: `_ww` on the `WW1.txt` skybox, `_space` on the lunar skyboxes, else `_pos`; attenuation 0.25 like the airstrike (**inferred** choice/falloff) |
| Server position during the fall | shared GenericMovement | static | same fixed 1/60 s model: 30 blocks/s², 0.75 slowdown between 10 and 2 above the support, lands on the support voxel. Matches the live client to one frame (landing 5.08 s vs ≈5.1 s for a 214-block drop) |
| First crates of a round | not recorded | on the ground | on the ground (**inferred**; only respawns drop) |
| Pickup | server decides | `CRATE_DISTANCE` 2.5 around the crate | same, against the falling position too (a jetpack can catch it); full players still consume it (unchanged) |
| Ammo crate on thrown/launched tools | `Tool.restock(AMMO_CRATE)` = `min(count + restock, default_count)`; `Weapon.restock(AMMO_CRATE)` tops the reserve (or a reserve-less magazine) up | reset every oriented stock to its SPAWN value (2 grenades) | **fixed 2026-09-27**: tops up (4 grenades, sticky/chem +2 to 4, launchers' reserve to max, clip and cadence kept); see WEAPONS_RETAIL.md "Oriented tools" |
| Late join mid-drop | - | - | CreateEntity carries the current height and fall speed; the client resumes and opens its own chute |
| Max concurrent | one crate per drop point | same | unchanged |
| Announcements | none | none | none |

Implementation: `PickupCrateBehavior(airdrop=True, drop_start_z, drop_cue)`
(`on_respawn` lifts the crate and cues the fly-by, `_advance_fall`
integrates on wall time so a round-robin skipped tick does not stall it),
`EntityRegistry.tick` calls a behavior's optional `on_respawn` before the
respawn CreateEntity, `MapResourceService` enables the drop for map crates
and picks the fly-by sound. Tests: `tests/test_crates_retail.py`.

## 2. Class deployables

All placement values are read from `shared/constants.py`; the audit found
them wired correctly (see `docs/DEPLOYABLE_INVENTORY_2026-09-21.md` for the
stock wallet). Differences and findings:

| Class / item | Retail | Ours | Status |
|---|---|---|---|
| Medic medpack | stock 2/2, +1 per ammo crate, 1.0 s, heal 25 × 3 uses, health 1, place ≤5 | same; heals teammates (owner included) below max health, 3.0 touch radius | matches. `MedPackEntity` is display-only on the client, so who may use it and the touch radius are server rules (**inferred**) |
| Scout landmine | stock 3/5, arm 4 s, trigger 2.5 horizontal × 3 layers, offset -0.5, 100 dmg / 15 block / r 3, health 1, enemies only | same; shot mine detonates | matches (`LANDMINE_EXPLOSION_BLAST_WAVE_RADIUS` 6 is unused by us) |
| Scout radar station | health 45, place ≤10, 1.5 s, one live; client `RadarStationEntity.can_detect_player` shows enemies within **250** blocks (`A1900`) and counts the packet fuse down | 45 s lifetime from `[game] radar_station_lifetime_seconds` (packet-21 fuse); no TeamMapVisibility, the client detects within 250 itself; a new station replaces the owner's old one | **fixed** 2026-09-26, verified live (docs/RETAIL_VALUES.md "Radar station") |
| Miner dynamite | stock **1 initial / 1 max / +1 per ammo crate** (alias `A1627`=1, see WEAPONS_RETAIL.md), fuse 7, r 5, 300 dmg, 7 block, `DYNAMITE_HEALTH` 1 | could not be damaged; max 3 / +3 (mod block) | **fixed**: one-point shell; a hit or nearby blast detonates it (mine precedent, **inferred** consequence). 2026-09-27: wallet now 1/1/1 (the earlier "1/3" row read the modded max) |
| Miner C4 | stock 2/2, +1, r 8, 300 dmg, 7 block, health 1, two live | same; a destroyed charge is removed without a blast | matches values; the silent removal is the earlier **inferred** rule, left unchanged |
| Engineer/Rocketeer rocket turret | stock 2/4 (+2), ammo 10, health 100, detect 30 / track 50, 1.5 s, 180°/s | same (`server/rocket_turret.py`) | matches |
| Engineer disguise | stock 2 initial / 3 max, +3 per ammo crate, 0.5 s | 2 initial, 0.5 s; breaks on firing/throwing; state bit 0x02 in WorldUpdate | ammo-crate restock of disguise is missing (in `server/player.py`, not changed here) |
| Engineer jetpack, Soldier, Gangster (VIP), Zombie | no deployables | - | nothing to audit here |

### Radar finding

`shared/constants.py` labels `A1899`/`A1900`/`A1901` as `RADAR_STATION_HEALTH
= 45`, `RADAR_STATION_LIFETIME = 250`, `RADAR_STATION_RANGE = 45`. IDA shows
the only client read of `A1900` is in `RadarStationEntity.can_detect_player`:
`sq_distance(self.get_position(), player.character.get_network_position())`
compared against `A1900 * A1900`. So **250 is the detection range**, not the
lifetime. `A1899` and `A1901` (both 45) are never read by the client, so they
are server-side values: health and, most likely, a **45 s lifetime** (the two
remaining slots). The client takes the lifetime from the packet-21 fuse
(`set_fuse` stores `lifetime`/`draw_lifetime` and builds the countdown text).
Our old 35 s default was not a retail value (now 45 s, 2026-09-26);
changing it to 45 and relabelling the constants belongs to the owners of
those files.

## 3. Live verification (2026-09-26, dev client, TDM ArcticBase)

1. Client-side: an AmmoCrate created at z=100 over ground 213 fell, opened
   its chute at 8 blocks, descended at 2 blocks/s, dropped the chute at 2
   and landed at 213 (trace above).
2. Server: picked up the crate at (128.5, 252.5) with `/tp`; 25 s later it
   was re-created at z=1 and fell with the chute on the client; standing
   away, it landed at z=215 after ≈5.1 s (server model: 5.08 s). Standing on
   the drop point the player caught it under the chute (≈3.8 s), and the
   cycle repeated every 25 s + fall time.

## 4. Class tables and per-mode class lists (2026-09-27)

All 18 classes were compared slot by slot (melee, primary, secondary,
equipment, constructs, common tools, blocks, ammo) between the stock alias
constants (`aceofspades_source/shared/backup/constants-copy.py`, the A####
block the stock client executes), the renamed decompile and our
`shared/constants.py`. Only two things differed; both now follow stock:

| Item | Stock (alias) | Before | Now |
|---|---|---|---|
| Engineer constructs (`PREFAB_LISTS[CLASS_PREFABS_ENGINEER]`, A475) | 7: caltrop, supertower, ultrabarrier, platform, superminibunker, superdome, fort_wall (`constants-copy.py:935`) | 9: the renamed decompile appended `prefab_superbridge` and `prefab_superpole` | 7. `SetClassLoadout` validation (`server/class_selection.py`), the build allow-list (`server/prefabs.py`) and bot prefab choices all read this table, so an Engineer can no longer select or place Super Bridge / Super Pole (Scout/Medic keep Super Bridge, Miner/Specialist keep Super Pole, as in stock). |
| Dynamite wallet | 1 / 1 / +1 | 1 / 3 / +3 | 1 / 1 / +1 (section 2) |
| TDM class list | `DEFAULT_TEAM_CLASSES` (A93) = Soldier, Scout, Engineer, Miner, Specialist, Medic; the retail lobby lists the same six (`gameRulesPanel.py:181-188`) | + Rocketeer (7 cards, Engineer moved to card 5) | the stock six in stock order (`server/mode_data.py`) |
| Zombie survivor classes | `DEFAULT_TEAM_CLASSES` | + Rocketeer, sorted by id | the stock six in stock order (`modes/zombie.py`); the zombie team stays `[4]` (Fast/Jump Zombie have no picker icon) |
| Bot classes | - | drew from the seven incl. Rocketeer in every mode | `DEFAULT_TEAM_CLASSES`, filtered by `is_class_selectable` (operator class rules + the mode list) like a human pick |

Every other class matched on every slot (Soldier, Scout, Rocketeer, Miner,
Zombie x3, Classic Soldier, Gangsters/VIPs, UGC Builder, Specialist, Medic).
Rocketeer itself is unchanged and still selectable where a mode or operator
list names it. Known client-side gaps (native class picker ignoring
`disabled_tools`/`locked_class`, DLC greying) are native-client work.
`docs/CONTENT_TABLES.md` no longer exists (removed with the 0.0.2 release);
this section replaces its class notes.

Tests: `tests/test_class_selection.py::test_engineer_constructs_match_the_stock_alias_seven`,
`tests/test_mode_data.py`, `tests/test_zombie.py`,
`tests/test_deployable_inventory.py`.
