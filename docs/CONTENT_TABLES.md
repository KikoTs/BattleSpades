# CONTENT_TABLES — Ace of Spades 1.x weapon & class stats

Authoritative reference for the Phase-1 content build-out. **Every value here was
extracted from the original compiled client's own constant catalog**, not from the
untrustworthy `aoslib-reversed` dump.

## Sources & method

- **Primary:** `G:\AoSRevival\aceofspades_nonsteam\shared\constants.py` (6672 lines).
- **Weapon attributes:** `G:\AoSRevival\aceofspades_nonsteam\aoslib\weapons\*.py` — each
  weapon class assigns `damage/ammo/shoot_interval/...` from named constants or `A####`
  aliases. Resolved by AST-parsing each class body and looking the tokens up in the
  live `shared.constants` module (game's bundled Python 2.7:
  `G:\AoSRevival\aceofspades_nonsteam\python\python.exe`).
- **Class stats:** the resolved `CLASS_*_MULTIPLIER` / `CLASS_BLOCKS` dicts (constants.py
  ~line 5300+), which already fold in the per-class `SOLDIER_*`, `SCOUT_*`, … constants
  and the newer classes' `A###` aliases.

**Critical caveat — duplicate definitions:** `constants.py` defines many weapon/class
constants **twice** (e.g. `PISTOL_*` at ~line 3255 *and* ~line 6066; `SMG_*` at 3354 and
6099). Python's later binding wins, so the **effective** values are the ones near the
bottom of the file (the ~6000-line block). All numbers below were read from the live
imported module, so they already reflect the winning (last) definition — do **not** grep
the first occurrence and assume it's correct.

### Key enum line numbers (constants.py)

| Enum | Line | Count |
|---|---|---|
| `CLASS_*` (class ids) | 245 | 19 tokens (18 real classes + `CLASS_NOOF` sentinel) |
| `..._TOOL` (weapon/tool ids) | 838 | `NUMBER_OF_WEAPONS = 66` (line 835); 65 selectable + `NOOF_SELECTABLE_TOOLS` sentinel |
| `..._DAMAGE` (damage-type enum) | 931 | 44 |
| `..._KILL` (kill-type enum) | 1123 | 37 |
| `CLASS_ITEMS` (loadouts) | 1477 | — |
| `CLASS_*_MULTIPLIER` / `CLASS_BLOCKS` dicts | ~5290–5600 | — |
| `INITIAL_HEALTH = 100.0` (all classes) | 4811 | — |

---

## 1. Weapons / Tools (all 66)

`ammo` tuple in the weapon files is
`(clip_size, initial_clip, max_reserve, initial_stock, restock_amount)`. Below,
**clip** = clip_size, **reserve** = max_reserve (max carried outside the clip),
**stock** = initial_stock. For throwables/deployables the "ammo" is a simple count
(default_count / initial_count / restock).

`base_damage` for hitscan guns is the **torso** figure; the full body tuple is
`(torso, head, arms, legs, legs)`. `headshot_mult` shown is head÷torso (the actual
in-game head damage is the raw `head` value, not torso×mult). Melee `base_damage` =
the block/entity `damage` attr; the **player** hit uses the separate
`*_HITPLAYER_DAMAGE_AMOUNT` (given in the notes). Explosive `base_damage` = the
projectile's `*_EXPLOSION_DAMAGE`; the `damage` attr on those weapon classes is
`None` (damage is applied by the spawned projectile/entity).

`damage_type` / `kill_type` columns are the enum **ids** from `TOOLS_DAMAGE_TYPE`
(constants.py 980) and `TOOLS_KILL_TYPE` (1164); see §3 for id→name.

| id | Name (TOOL const) | Category | base_dmg | head | fire_int (s) | clip | reserve | reload (s) | pellets | range | block_dmg | dmg_type | kill_type |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | PICKAXE | melee (dig) | 7 blk / 50 player | — | 0.4 | — | — | — | — | — | dig | 0 PICKAXE_DAMAGE | 0 WEAPON |
| 1 | KNIFE | melee | 1 blk / 20 player | — | 0.25 | — | — | — | — | — | — | 1 KNIFE_DAMAGE | 0 WEAPON |
| 2 | SPADE | melee (dig) | 5 blk / 35 player | — | 0.4 | — | — | — | — | — | dig | 2 SPADE_DAMAGE | 0 WEAPON |
| 3 | SUPERSPADE | melee (dig) | 7.5 blk / 50 player | — | 0.6 | — | — | — | — | — | dig | 3 SUPERSPADE_DAMAGE | 0 WEAPON |
| 4 | CLASSIC_SPADE | melee (dig) | 3 blk / 50 player (2nd: 5) | — | 0.3 (2nd 0.8) | — | — | — | — | — | dig | 4 CLASSIC_SPADE_DAMAGE | 0 WEAPON |
| 5 | BLOCK | deployable-tool | — | — | 0.5 | — | — | — | — | — | — | — (None) | 0 WEAPON |
| 6 | RIFLE (ClassicRifle) | rifle | 70 | 150 | 0.5 | 10 | 50 | 2.5 | 1 | 10000 | 2 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 7 | SMG | smg | 10 | 15 | 0.1 | 25 | 100 | 1.25 | 1 | 250 | 1 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 8 | MINIGUN | mg | 15 | 30 | 0.3 (spins up) | 100 | 300 | 2.0 | 1 | 100 | 2.5 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 9 | SHOTGUN | shotgun | 20 | 30 | 1.0 | 5 | 20 | 0.5 (per-shell) | 10 | 60 | 1 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 10 | SHOTGUN2 | shotgun | 40 | 50 | 1.0 | 2 | 14 | 1.0 (per-shell) | 10 | 20 | 2.5 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 11 | GRENADE | grenade | 230 blast | — | 0.5 | 4 (start) | — | — | — | r=4, fuse=2.5 | — | 7 GRENADE_DAMAGE | 0 WEAPON |
| 12 | RPG | launcher | 140 blast | — | 0.7 | 1 | 3 | 1.5 | — | r=4 (blast 6) | 5 | 8 ROCKET_DAMAGE | 4 ROCKET |
| 13 | RPG2 | launcher | 50 blast | — | 0.75 | 3 | 3 | 1.0 (clip) | — | r=4 | — | 9 ROCKET2_DAMAGE | (RPG2→ROCKET2) |
| 14 | DRILLGUN | launcher | 50 blast | — | 0.2 | 1 | 3 | 4.0 | — | r=3, blkdmg 5 | — | 10 DRILL_DAMAGE | 6 DRILL |
| 15 | MG (mounted) | mg | 30 | 20 | 0.5 | 100 | 400 | 4.0 | 1 | 300 | 2 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 16 | ROCKET_TURRET | deployable-tool | 100 blast | — | 1.5 | 4 (count) | — | — | — | r=3 | — | — (None) | 4 ROCKET |
| 17 | PISTOL | pistol | 20 | 50 | 0.3 | 6 | 30 | 0.5 | 1 | 800 | 3 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 18 | SNIPER | sniper | 50 | 175 | 1.0 | 1 | 7 | 2.0 | 1 | 10000 | 5 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 19 | SNIPER2 | sniper | 34 | 85 | 1.1 | 5 | 15 | 3.0 | 1 | 10000 | 3 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 20 | LANDMINE | deployable-tool | 100 blast | — | 1.0 | 5 (start 3) | — | — | — | r=3 (blast 6) | — | 15 LANDMINE_DAMAGE | 0 WEAPON |
| 21 | DYNAMITE | deployable-tool | 300 blast | — | 1.0 | 1 (count) | — | — | — | r=5, fuse=7 | — | 16 DYNAMITE_DAMAGE | 0 WEAPON |
| 22 | FLAREBLOCK | deployable-tool | — | — | 0.5 | — | — | — | — | — | — | — (None) | 0 WEAPON |
| 23 | PREFAB | deployable-tool | — | — | 0.5 (2nd 0.5) | — | — | — | — | — | — | — (None) | 0 WEAPON |
| 24 | ZOMBIEHAND | melee | 2 blk / 70 player | — | 0.4 | — | — | — | — | — | — | 17 ZOMBIE_DAMAGE | 0 WEAPON |
| 25 | BOMB | objective | 500 blast | — | 0.0 | — | — | — | — | r=7, fuse=10 | — | 19 BOMB_DAMAGE | 17 BOMB |
| 26 | DIAMOND | objective | — | — | 0.0 | — | — | — | — | — | — | — (None) | 0 WEAPON |
| 27 | SHRAPNEL | special | (uses BlockTool) | — | — | — | — | — | — | — | — | 6 WEAPON_DAMAGE | 19 SHRAPNEL |
| 28 | ZOMBIE_PREFAB | deployable-tool | — | — | — | — | — | — | — | — | — | — (None) | 0 WEAPON |
| 29 | SNOWBLOWER | launcher | 10 blast | — | 0.2 | — | — | 3.0 | — | r=5 | 0 | 20 SNOWBALL_DAMAGE | 21 SNOWBALL |
| 30 | INTEL | objective | — | — | 0.0 | — | — | — | — | — | — | — (None) | 0 WEAPON |
| 31 | CLASSIC_GRENADE | grenade | 130 blast | — | 0.5 | 4 (start) | — | — | — | r=2, fuse=3.0 | — | 22 CLASSIC_GRENADE_DAMAGE | 0 WEAPON |
| 32 | ANTIPERSONNEL_GRENADE | grenade | 500 blast | — | 0.5 | 4 (start) | — | — | — | r=2, fuse=2.5 | — | 23 ANTIPERSONNEL_GRENADE_DAMAGE | 0 WEAPON |
| 33 | MOLOTOV | grenade | 50 blast | — | 1.0 | 3 (count) | — | — | — | r=4 | 3 | 24 MOLOTOV_DAMAGE | 0 WEAPON |
| 34 | CROWBAR | melee | 5 blk / 80 player | — | 0.6 | — | — | — | — | — | — | 26 CROWBAR_DAMAGE | 0 WEAPON |
| 35 | TOMMYGUN | smg | 30 | 35 | 0.12 | 30 | 120 | 2.0 | 1 | 500 | 1 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 36 | SNUB_PISTOL | pistol | 40 | 70 | 0.5 | 6 | 30 | 0.75 | 1 | 500 | 1 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 37 | CLASSIC_SHOTGUN | shotgun | 20 | 30 | 1.0 | 5 | 45 | 0.5 (per-shell) | 12 | 75 | 1 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 38 | CLASSIC_SMG | smg | 20 | 20 | 0.1 | 25 | 100 | 1.25 | 1 | 100 | 2 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 39 | NULL | special (unused/placeholder) | — | — | 0.0 | — | — | — | — | — | — | — (None) | 0 WEAPON |
| 40 | FAKE_PISTOL | special (unused/placeholder) | — | — | 0.0 | — | — | — | — | — | — | — (None) | 0 WEAPON |
| 41 | UGC | special (UGC editor) | — | — | 0.5 (2nd 0.5) | — | — | — | — | — | — | — (None) | 0 WEAPON |
| 42 | UGC_PREFAB | special (UGC editor) | — | — | 0.1 | — | — | — | — | — | — | — (None) | 0 WEAPON |
| 43 | PAINTBRUSH | deployable-tool | — | — | 0.05 | — | — | — | — | — | — | — (None) | 0 WEAPON |
| 44 | UGC_PICKAXE | melee (UGC, dig) | 9 blk / 0 player | — | 0.2 | — | — | — | — | — | dig | 28 UGC_PICKAXE_DAMAGE | 0 WEAPON |
| 45 | UGC_SUPERSPADE | melee (UGC, dig) | 7.5 blk (2nd 7.5) / 0 player | — | 0.2 (2nd 0.2) | — | — | — | — | — | dig | 29 UGC_SUPERSPADE_DAMAGE | 0 WEAPON |
| 46 | UGC_RPG2 | launcher (UGC) | 50 blast | — | 0.5 | 1 | 1 | 1.0 (clip) | — | r=4 | — | 30 UGC_ROCKET2_DAMAGE | 27 UGC_ROCKET2 |
| 47 | UGC_DRILLGUN | launcher (UGC) | 50 blast | — | 0.2 | 1 | 3 | 4.0 | — | r=3 | — | 33 UGC_DRILL_DAMAGE | 28 UGC_DRILL |
| 48 | UGC_SNOWBLOWER | launcher (UGC) | 10 blast | — | 0.2 | — | — | 3.0 | — | r=5 | — | 32 UGC_SNOWBALL_DAMAGE | 29 UGC_SNOWBALL |
| 49 | RIOTSTICK | melee | 1.75 blk player | — | 0.5 | — | — | — | — | — | — | 34 RIOTSTICK_DAMAGE | 0 WEAPON |
| 50 | MACHETE | melee | 2.0 blk player | — | 0.7 | — | — | — | — | — | — | 35 MACHETE_DAMAGE | 0 WEAPON |
| 51 | MEDPACK | deployable-tool (heal) | — (heals) | — | 1.0 | 2 (count) | — | 1.5 | — | — | — | — (None) | — |
| 52 | RIOTSHIELD | melee/equipment | 2 blk player | — | 1.0 | — | — | — | — | — | — | 36 RIOTSHIELD_DAMAGE | 0 WEAPON |
| 53 | AUTOMATIC_PISTOL | pistol | 15 | 30 | 0.175 | 15 | 50 | 1.0 | 1 | 300 | 2.5 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 54 | CHEMICALBOMB | grenade | (projectile-set) | — | 1.0 | 4 (start 2) | — | — | — | (projectile) | — | 6 WEAPON_DAMAGE | 31 CHEMICALBOMB |
| 55 | GRENADE_LAUNCHER_WEAPON | launcher | (projectile-set) | — | 0.35 | 1 | 5 | 2.0 | — | (projectile) | — | 37 GRENADE_LAUNCHER_DAMAGE | 32 GRENADE_LAUNCHER |
| 56 | RADAR_STATION | deployable-tool | — | — | 1.5 | 1 (count) | — | — | — | — | — | — (None) | 33 RADAR_STATION |
| 57 | STICKY_GRENADE | grenade | (projectile-set) | — | 1.0 | 4 (start 2) | — | — | — | (projectile) | — | 39 STICKY_GRENADE_DAMAGE | 34 STICKY_GRENADE |
| 58 | MINE_LAUNCHER | launcher | (projectile-set) | — | 0.35 | 1 | 5 | 2.0 | — | (projectile) | — | 40 MINE_LAUNCHER_DAMAGE | 35 MINE |
| 59 | C4 | deployable-tool | (projectile-set) | — | 1.0 | 2 (count 2, restock 1) | — | — | — | (projectile) | — | 41 C4_DAMAGE | 36 C4 |
| 60 | ASSAULT_RIFLE | rifle | 20 | 40 | 0.5 | 15 | 60 | 0.9 | 1 | 400 | 2.5 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 61 | LIGHT_MACHINE_GUN | mg | 20 | 37 | 0.15 | 50 | 250 | 2.0 | 1 | 175 | 2.5 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 62 | AUTO_SHOTGUN | shotgun | 20 | 25 | 0.35 | 8 | 40 | 2.5 | 10 | 60 | 2 | 6 WEAPON_DAMAGE | 0 WEAPON |
| 63 | BLOCK_SUCKER | deployable-tool | — | — | 0.2 | — | — | 1.0 | — | — | — | 42 BLOCK_SUCKER_DAMAGE | 0 WEAPON |
| 64 | DISGUISE | special | — | — | 0.5 | 3 (start 2) | — | — | — | — | — | — (None) | 0 WEAPON |

**Notes / caveats**

- **Melee `base_damage`:** first value is the block/entity `damage` attr; the "player"
  value is the `*_HITPLAYER_DAMAGE_AMOUNT` constant (SPADE 35, SUPERSPADE 50, PICKAXE 50,
  KNIFE 20, CROWBAR 80, ZOMBIEHAND 70, CLASSIC_SPADE 50). RIOTSTICK/MACHETE/RIOTSHIELD have
  **no** `*_HITPLAYER_DAMAGE_AMOUNT` constant — only the small block `damage` shown.
- **Projectile-set explosives** (CHEMICALBOMB 54, GRENADE_LAUNCHER 55, STICKY_GRENADE 57,
  MINE_LAUNCHER 58, C4 59): the weapon class has `damage=None`; there is **no** top-level
  `*_EXPLOSION_DAMAGE` / `*_RADIUS` constant in constants.py. Those values are supplied by
  the projectile/entity spawn code (not resolvable from the constant catalog) — flagged
  explicitly rather than guessed.
- **RPG2 (id 13) kill_type:** `TOOLS_KILL_TYPE` has no explicit entry (`.get` → default);
  by name it maps to `UGC_ROCKET2_KILL`/`ROCKET2` family. **MEDPACK (id 51) kill_type:**
  also not in the map (it's a heal tool).
- **MINIGUN** fire interval starts at 0.3 and ramps down while firing
  (`shoot_interval_active_alteration_per_second`); 0.3 is the initial value.
- **Shotguns** reload shell-by-shell (`clip_reload=True`); the per-shell reload time is
  shown. SNIPER/SMG/etc. reload the whole clip at once.
- `range` for RIFLE/SNIPER/SNIPER2 = 10000 = effectively hitscan-infinite.
- All guns fire 1 pellet unless noted; only the 4 shotguns fire multiple.

---

## 2. Classes (all 18)

`health` = `INITIAL_HEALTH = 100.0` for **every** class (constants.py line 4811); there is
no per-class health constant. The **SPECIALIST** gains extra health only when the
`SPECIALISM_EXTRA_HEALTH` specialism is active (runtime, not a static class stat).
`accel/sprint/crouch/jump/water_friction/headshot/damage/fall` values are the resolved
entries of the `CLASS_*_MULTIPLIER` / `CLASS_FALLING_DAMAGE_*` / `CLASS_BLOCKS` dicts.
`fall_dmg` = (min_dist, max_dist, max_dmg). `jetpack` from `CLASS_ITEMS` equipment slot
(constants.py 1477). `crouch_sneak_mult` is 0.5 for all except FAST_ZOMBIE (0.25).

| id | Name | accel | sprint | crouch | jump | water_fric | health | head_mult | dmg_mult | fall (min/max/dmg) | jetpack | blocks (start/max) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | SOLDIER | 0.7 | 1.4 | 0.5 | 1.2 | 8 | 100 | 1.0 | 1.0 | 10/40/100 | — | 200/1000 |
| 1 | SCOUT | 0.7 | 1.45 | 0.5 | 1.5 | 8 | 100 | 1.5 | 1.43 | 10/30/100 | — | 400/1000 |
| 2 | ROCKETEER | 0.7 | 1.1 | 0.5 | 1.0 | 12.0 | 100 | 1.5 | 1.43 | 10/10/10 | JETPACK2 / JETPACK_NORMAL | 500/1500 |
| 3 | MINER | 0.7 | 1.4 | 0.5 | 1.2 | 8 | 100 | 0.5 | 1.1765 | 10/40/100 | — | 0/1000 |
| 4 | ZOMBIE | 0.5 | 1.65 | 0.5 | 1.5 | 4.0 | 100 | 1.0 | 0.6 | 10/60/100 | — | 1000/2000 |
| 5 | CLASSIC_SOLDIER | 1.0 | 1.33 | 0.5 | 1.0 | 8 | 100 | 1.0 | 1.0 | 6/26/100 | — | 25/100 |
| 6 | GANGSTER_1 | 0.7 | 1.5 | 0.5 | 1.2 | 8 | 100 | 1.2 | 1.0 | 10/40/100 | — | 500/1200 |
| 7 | GANGSTER_2 | 0.7 | 1.5 | 0.5 | 1.2 | 8 | 100 | 1.2 | 1.0 | 10/40/100 | — | 500/1200 |
| 8 | GANGSTER_3 | 0.7 | 1.5 | 0.5 | 1.2 | 8 | 100 | 1.2 | 1.0 | 10/40/100 | — | 500/1200 |
| 9 | GANGSTER_4 | 0.7 | 1.5 | 0.5 | 1.2 | 8 | 100 | 1.2 | 1.0 | 10/40/100 | — | 500/1200 |
| 10 | GANGSTER_VIP_1 | 0.7 | 1.5 | 0.5 | 1.2 | 8 | 100 | 1.2 | 1.0 | 10/40/100 | — | 500/1200 |
| 11 | GANGSTER_VIP_2 | 0.7 | 1.5 | 0.5 | 1.2 | 8 | 100 | 1.2 | 1.0 | 10/40/100 | — | 500/1200 |
| 12 | ENGINEER | 0.7 | 1.25 | 0.5 | 1.0 | 8 | 100 | 1.5 | 1.1765 | 10/40/100 | JETPACK_ENGINEER | 2000/3000 |
| 13 | UGCBUILDER | 1.0 | 3.0 | 0.5 | 1.0 | 1.0 | 100 | 0 | 0 | 255/255/0 | JETPACK_UGCBUILDER | 1/1 |
| 14 | FAST_ZOMBIE | 1.1 | 3.0 | 0.25 | 2.5 | 12.0 | 100 | 2.5 | 0.5 | 6/20/500 | — | 500/1000 |
| 15 | JUMP_ZOMBIE | 0.5 | 1.0 | 0.5 | 3.0 | 8 | 100 | 1.5 | 0.5 | 15/25/100 | — | 500/1000 |
| 16 | SPECIALIST | 0.85 | 1.55 | 0.5 | 1.5 | 8 | 100 (+specialism) | 1.5 | 1.1765 | 10/40/100 | — | 400/1000 |
| 17 | MEDIC | 0.6 | 1.35 | 0.5 | 1.2 | 8 | 100 | 1.0 | 1.0 | 10/40/100 | — | 900/2000 |

> **`CLASS_NAMES` gotcha (constants.py 283):** `CLASS_ROCKETEER` (id 2) is labelled
> `'ENGINEER'` and `CLASS_ENGINEER` (id 12) is labelled `'ENGINEER2'` in the client's
> name table — the display names are swapped relative to the const names. The stat rows
> above are keyed by the numeric id, so they're unaffected, but watch this when wiring
> UI/strings.

### Default loadouts (`CLASS_ITEMS`, constants.py 1477)

Order per class: primary / secondary / equipment / melee. `DEFAULT_TEAM_CLASSES`
(line 266) = SOLDIER, SCOUT, ENGINEER, MINER, SPECIALIST, MEDIC (the standard team
selectable set). `DEFAULT_CLASS = CLASS_CLASSIC_SOLDIER` (line 1474).

| Class | Primary | Secondary | Equipment | Melee |
|---|---|---|---|---|
| SOLDIER | MINIGUN(8), ASSAULT_RIFLE(60) | RPG(12), RPG2(13) | GRENADE(11), ANTIPERSONNEL_GRENADE(32) | SPADE(2), KNIFE(1) |
| SCOUT | SNIPER(18), SNIPER2(19) | PISTOL(17), AUTOMATIC_PISTOL(53) | LANDMINE(20), RADAR_STATION(56) | PICKAXE(0), KNIFE(1) |
| ROCKETEER | SMG(7) | ROCKET_TURRET(16), GRENADE(11) | JETPACK2, JETPACK_NORMAL | SPADE(2), PICKAXE(0) |
| ENGINEER | SMG(7) | ROCKET_TURRET(16), SNOWBLOWER(29), MINE_LAUNCHER(58) | JETPACK_ENGINEER, DISGUISE(64) | PICKAXE(0) |
| MINER | SHOTGUN(9), SHOTGUN2(10) | DRILLGUN(14), BLOCK_SUCKER(63) | DYNAMITE(21), C4(59) | SUPERSPADE(3) |
| ZOMBIE | ZOMBIEHAND(24) | — | — | — |
| CLASSIC_SOLDIER | RIFLE(6), CLASSIC_SMG(38), CLASSIC_SHOTGUN(37) | — | CLASSIC_GRENADE(31) | CLASSIC_SPADE(4) |
| GANGSTER_1..4 / VIP_1/2 | TOMMYGUN(35) | SNUB_PISTOL(36) | MOLOTOV(33) | CROWBAR(34) |
| UGCBUILDER | UGC_DRILLGUN(47) | UGC_SNOWBLOWER(48) | JETPACK_UGCBUILDER | UGC_SUPERSPADE(45) |
| FAST_ZOMBIE | ZOMBIEHAND(24) | — | — | — |
| JUMP_ZOMBIE | ZOMBIEHAND(24) | — | — | — |
| SPECIALIST | AUTO_SHOTGUN(62), SMG(7) | AUTOMATIC_PISTOL(53), GRENADE_LAUNCHER_WEAPON(55) | CHEMICALBOMB(54), STICKY_GRENADE(57) | SPADE(2), MACHETE(50) |
| MEDIC | LIGHT_MACHINE_GUN(61), SHOTGUN2(10) | RIOTSHIELD(52) | MEDPACK(51) | PICKAXE(0), RIOTSTICK(49) |

All classes additionally get `CLASS_COMMON_TOOLS` (block/paintbrush/flareblock/prefab/
pickups etc.) — see `CLASS_COMMON` / `CLASS_COMMON_TOOLS` in constants.py.

Team → class groupings (constants.py 266–281): `DEFAULT_TEAM_CLASSES`,
`CLASSIC_TEAM_CLASSES` (CLASSIC_SOLDIER), `MAFIA_TEAM_CLASSES` (GANGSTER_1..4),
`UGC_TEAM_CLASSES` (UGCBUILDER), `ZOMBIE_TEAM_CLASSES` (ZOMBIE, FAST_ZOMBIE, JUMP_ZOMBIE).

---

## 3. DAMAGE & KILL enum maps

### DAMAGE enum — 44 entries (constants.py line 931)

`0 PICKAXE_DAMAGE, 1 KNIFE_DAMAGE, 2 SPADE_DAMAGE, 3 SUPERSPADE_DAMAGE,
4 CLASSIC_SPADE_DAMAGE, 5 CLASSIC_SPADE_SECONDARY_DAMAGE, 6 WEAPON_DAMAGE,
7 GRENADE_DAMAGE, 8 ROCKET_DAMAGE, 9 ROCKET2_DAMAGE, 10 DRILL_DAMAGE,
11 DRILL_DESTROYED_DAMAGE, 12 ROCKET_TURRET_DAMAGE, 13 CORPSE_DAMAGE, 14 GRAVE_DAMAGE,
15 LANDMINE_DAMAGE, 16 DYNAMITE_DAMAGE, 17 ZOMBIE_DAMAGE, 18 AIRSTRIKE_DAMAGE,
19 BOMB_DAMAGE, 20 SNOWBALL_DAMAGE, 21 ROCKET_TURRET_ROCKET_DAMAGE,
22 CLASSIC_GRENADE_DAMAGE, 23 ANTIPERSONNEL_GRENADE_DAMAGE, 24 MOLOTOV_DAMAGE,
25 BLOCKFIRE_DAMAGE, 26 CROWBAR_DAMAGE, 27 MG_DAMAGE, 28 UGC_PICKAXE_DAMAGE,
29 UGC_SUPERSPADE_DAMAGE, 30 UGC_ROCKET2_DAMAGE, 31 UGC_SUPERSPADE_SECONDARY_DAMAGE,
32 UGC_SNOWBALL_DAMAGE, 33 UGC_DRILL_DAMAGE, 34 RIOTSTICK_DAMAGE, 35 MACHETE_DAMAGE,
36 RIOTSHIELD_DAMAGE, 37 GRENADE_LAUNCHER_DAMAGE, 38 SOME_DAMAGE, 39 STICKY_GRENADE_DAMAGE,
40 MINE_LAUNCHER_DAMAGE, 41 C4_DAMAGE, 42 BLOCK_SUCKER_DAMAGE, 43 UNKNOWN_DAMAGE`

### KILL enum — 37 entries (constants.py line 1123)

`0 WEAPON_KILL, 1 HEADSHOT_KILL, 2 MELEE_KILL, 3 GRENADE_KILL, 4 ROCKET_KILL,
5 ROCKET2_KILL, 6 DRILL_KILL, 7 FALL_KILL, 8 FORCED_TEAM_CHANGE_KILL, 9 TEAM_CHANGE_KILL,
10 CLASS_CHANGE_KILL, 11 ENTITY_KILL, 12 CORPSE_KILL, 13 GRAVE_KILL, 14 LANDMINE_KILL,
15 DYNAMITE_KILL, 16 AIRSTRIKE_KILL, 17 BOMB_KILL, 18 ROCKET_TURRET_KILL, 19 SHRAPNEL_KILL,
20 HEALTHCRATE_HP, 21 SNOWBALL_KILL, 22 CLASSIC_GRENADE_KILL, 23 ANTIPERSONNEL_GRENADE_KILL,
24 MOLOTOV_KILL, 25 BLOCKFIRE_KILL, 26 VIP_MODE_KILL, 27 UGC_ROCKET2_KILL, 28 UGC_DRILL_KILL,
29 UGC_SNOWBALL_KILL, 30 SOME_KILL, 31 CHEMICALBOMB_KILL, 32 GRENADE_LAUNCHER_KILL,
33 RADAR_STATION_KILL, 34 STICKY_GRENADE_KILL, 35 MINE_KILL, 36 C4_KILL`

> Damage-type / kill-type per tool are the `TOOLS_DAMAGE_TYPE` (constants.py 980) and
> `TOOLS_KILL_TYPE` (1164) maps; the ids are already inlined in the §1 table.

---

## Global combat constants (constants.py)

- `INITIAL_HEALTH = 100.0` (4811) — universal player HP.
- `PLAYER_RADIUS = 0.45` (614); `PLAYER_CENTER_VERTICAL_OFFSET = 0.75`,
  `CROUCHING_PLAYER_CENTER_VERTICAL_OFFSET = 1.25` (620–621).
- `VIP_DAMAGE_MULTIPLIER = 0.5` (314) — VIP gangsters take half damage.
- `QUANTIZED_INTERVAL_BLOCK_DAMAGE = 0.25` (166) — block-damage quantization step.
- `NUMBER_OF_WEAPONS = 66` (835); `NUM_JETPACK = 5` (907):
  `NO_JETPACK, JETPACK_NORMAL, JETPACK2, JETPACK_ENGINEER, JETPACK_UGCBUILDER` (ids 65–69).
