# Weapons: retail parity audit (2026-09-26)

Every weapon, tool and explosive checked against the **stock Steam client**:
`C:\Program Files (x86)\Steam\steamapps\common\aceofspades\aos.pkg`. Its PYZ
(`aos.pkg_extracted\out00-PYZ.pyz`) is byte-identical to the archive embedded
in the live `aos.pkg`. Tests: `tests/test_weapons_retail.py`, plus the updated
`tests/test_weapon_catalog.py` and `tests/test_reversed_combat.py`.

## How the values were recovered

1. **Weapon classes.** Every `aoslib.weapons.*` module and the obfuscated
   `shared.constants` were extracted from the stock PYZ, then executed under
   Python 2.7 with rendering imports stubbed. The resolved class attributes
   were read directly: `damage`, `block_damage`, `range`, `shoot_interval`,
   `reload_time`, `ammo`, `pellets`, `accuracy*`, `clip_reload`,
   `short_ranged_distance`, fuses and counts. Class-body constants are real
   stock values, not decompiled names.
2. **Named constants.** The stock constants are all `A####`. Names come from
   the nonsteam decompile's `A#### = NAME` alias lines, and the value is
   always read from the stock `A####`.
3. **Explosions.** The stock 32-bit `shared.explosionDamageManager.pyd` was
   run as an oracle using the Py2.7-32 interpreter at
   `aceofspades_nonsteam/python/python.exe`, with instrumented damageables:
   - Every `handle_*_damage` wrapper was intercepted to record its arguments.
   - `handle_explosion_damage` was swept over distance, crouch, line of sight
     (LOS), team, self and classic mode.
   - 60 randomised cases match `server/weapons_retail.py` exactly; 22 of them
     are pinned in the tests.
   - `Player.get_line_of_sight_positions` was called from the stock
     `aoslib.scenes.main.player.pyd`.
   - The stock `aoslib.world.pyd` raycast (`sub_10005C20`) was checked with
     headless IDA: the ray end is `start + dir*length`, with no
     normalisation.

> **The nonsteam decompile of `shared/constants.py` is not retail.** After the
> alias section it ends with a later block of *named* constants. That block
> changes the pistol to head 50, 0.3 s, 0.5 s reload and range 800, the
> pickaxe to 0.4 s / 50 player / 9 block, the knife to 0.25 s / 20, the spade
> to 0.4 s, the crowbar to 0.6 s, SMG range to 250, and ROCKET2 damage to 50.
> The rebuilt nonsteam `pistolWeapon.pyc` reads those names. The stock Steam
> class reads `A1124…A1133` (20/45, 0.4 s, 0.6 s, 550). Our
> `shared/constants.py` inherited the modded block, so the combat code must not
> read those names.

## Body parts, hitboxes, headshots

| Item | Retail | Ours now | Status |
|---|---|---|---|
| Part ids | `PART_HEAD=0, TORSO=1, ARMS=2, LEFT_LEG=3, RIGHT_LEG=4` | same | ok |
| Damage tuple order | `(TORSO, HEAD, ARMS, LEGS, LEGS)` (e.g. `SMG_DAMAGE_TORSO` first) | `WeaponProfile.part_damage` + `damage_for_part()` slot map | **fixed**: limbs used to take torso damage, and the head figure was never used |
| Hit order | `aoslib.weapons.hitscan_player`: torso → head → arms → left leg → right leg; the **first** box hit wins | same order (`_ray_hits_target`), now returns the part id | ok |
| Hitbox geometry | `shared.common.hitscan_model` over the class KV6 boxes (`CLASS_BODY_PARTS`) | `_CLASS_HITBOXES` | ok: head, body and leg box sizes re-checked against the stock `kv6/Character_{Soldier,Scout,Miner}_*.kv6` headers. The pivots include `CLASS_BODY_PARTS_OFFSETS` and were not re-derived in this pass. |
| Headshot rule | the head slot of the tuple, times the **victim's** `CLASS_HEADSHOT_DAMAGE_MULTIPLIER` | same | **fixed**: before, a rifle headshot did 70 × the *attacker's* multiplier |
| Class multiplier | `CLASS_DAMAGE_MULTIPLIER` of the class **taking** damage | victim-side (`weapons_retail.victim_damage_multiplier`) | **fixed**: before, it was attacker-side |

The client never reads `CLASS_DAMAGE_MULTIPLIER` or
`CLASS_HEADSHOT_DAMAGE_MULTIPLIER` (only the fall-on-water one), so the side
they apply to comes from the design data:

- Scout is 1.43, and its description says "your health and ammo are limited".
- Soldier is 1.0: "take a beating".
- UGC builder is 0, i.e. invulnerable.
- Miner head is 0.5 (it wears a hard hat).
- Zombie is 0.6, i.e. tanky.

The class multiplier applies to bullets, melee and explosions. The headshot
multiplier applies to bullet head hits only.

## Hit-scan guns (stock class values)

"ammo" = (clip, initial clip, **max reserve**, **initial reserve**, crate
restock). The client names them the other way round: `current_ammo` is the
magazine and `current_clip` is the reserve.

| Tool | torso/head/arms/legs | interval | ammo | reload | range | pellets | block | entity | Before → now |
|---|---|---|---|---|---|---|---|---|---|
| 6 Rifle | 70/150/35/35 | 0.5 | 10,10,50,**30**,50 | 2.5 | 10000 | 1 | 2 | 25 | limbs 70→35; initial reserve 30 recorded |
| 7 SMG | 10/15/10/10 | 0.1 | 25,25,100,100,100 | 1.25 | 350 | 1 | 1 | 15 | ok |
| 8 Minigun | 15/30/15/15 | 0.3→0.1 | 100,100,300,300,300 | 2 | 100 | 1 | 2.5 | 20 | spin model fixed (below) |
| 9 Shotgun | 20/30/12/12 | 1.0 | 5,5,20,20,20 (clip_reload) | 0.5/shell | 60 | 10 | 1 | 25 | limbs 20→12 |
| 10 Shotgun2 | 40/50/50/50 | 1.0 | 2,2,14,14,14 (clip_reload) | 1.0/shell | 20 | 10 | 2.5 | 25 | limbs 40→50 |
| 15 MG | 30/**20**/20/20 | 0.5 (mounted 0.1) | 100,100,400,400,400 | 4.0 | 300 | 1 | 2 | 20 | limbs 30→20 |
| 17 Pistol | 20/**45**/20/20 | **0.4** | 6,6,30,30,30 | **0.6** | **550** | 1 | 3 | 20 | head 50→45, 0.3→0.4, 0.5→0.6, 800→550 |
| 18 Sniper | 50/175/50/50 | 1.0 | 1,1,7,7,7 | 2.0 | 10000 | 1 | 5 | 100 | ok |
| 19 Sniper2 | 34/85/34/34 | 1.1 | 5,5,15,15,15 | 3.0 | 10000 | 1 | 3 | 100 | ok |
| 35 Tommy gun | 30/35/30/30 | 0.12 | 30,30,120,120,120 | 2.0 | 500 | 1 | 1 | 30 | ok |
| 36 Snub pistol | 40/70/30/30 | 0.5 | 6,6,30,30,30 (clip_reload) | 0.75/round | 500 | 1 | 1 | 20 | limbs 40→30 |
| 37 Classic shotgun | 20/30/12/12 | 1.0 | 5,5,45,**20**,20 (clip_reload) | 0.5/shell | 75 | 12 | 1 | 25 | limbs 20→12 |
| 38 Classic SMG | 20/20/20/20 | 0.1 | 25,25,100,100,100 | 1.25 | 100 | 1 | 2 | 20 | ok |
| 53 Auto pistol | 15/30/15/15 | 0.175 | 15,15,50,50,50 | 1.0 | 300 | 1 | 2.5 | 15 | ok |
| 60 Assault rifle | 20/40/20/20 | 0.5 + 3-round burst at 0.1 | 15,15,60,60,60 | 0.9 | 400 | 1 | 2.5 | 20 | ok (burst already matched `A1934`/`A1935`) |
| 61 LMG | 20/37/20/20 | 0.15 | 50,50,250,250,250 | 2.0 | 175 | 1 | 2.5 | 20 | ok |
| 62 Auto shotgun | 20/25/10/10 | 0.35 | 8,8,40,40,40 | 2.5 | 60 | 10 | 2 | 20 | limbs 20→10 |

- **Entity damage** is the stock `<WEAPON>_DAMAGE_ENTITY` value, declared
  between LEGS and BLOCK. Examples: sniper 100 and shotgun pellet 25.
  **Fixed**: bullets used to hit deployables for torso damage.
- **Accuracy** (`accuracy_min`/`accuracy_max`) is recorded per gun. All four
  shotguns share the spread curve min 4 → max 7, +0.5 per shot, −1.0 per
  second, so `_seeded_pellet_directions` is already exact.
- **Minigun** (stock `MinigunWeapon.update`): the interval moves −0.15 per
  second while primary *or secondary* is held, and +0.075 per second when
  released, clamped between 0.10 and 0.30. The gun can only fire once
  `spin_speed` > 0.5, i.e. the interval is below 0.28. Secondary pre-spins
  the barrels without firing. **Fixed**: the server used to hard-reset to
  0.30 after a 0.6 s gap and ignored pre-spin, which rejected legitimate
  0.1 s fire.
- **Zoom** values (e.g. sniper 1.5, sniper2 1.2) are client-only; the server
  does not use them.

## Melee (stock `DiggingTool` classes + `*_HITPLAYER_DAMAGE_AMOUNT`)

| Tool | player hit | block | interval (alt) | Before → now |
|---|---|---|---|---|
| 0 Pickaxe | **40** | 7 | **0.6** | 50→40, 0.4→0.6 |
| 1 Knife | 80 | 1 | **0.5** | 0.25→0.5 |
| 2 Spade | 35 | 5 | **0.8** (1.0) | 0.4→0.8 |
| 3 Superspade | 50 | 7.5 | 0.6 | ok |
| 4 Classic spade | 50 | 3 | 0.3 (0.8) | ok |
| 24 Zombie hand | 70 | 2 | 0.4 | ok (see the victim multiplier: a survivor now takes 70, not 42) |
| 34 Crowbar | 80 | 5 | **0.5** | 0.6→0.5 |
| 44 UGC pickaxe | 0 | 9 | 0.2 | ok |
| 45 UGC superspade | 0 | 7.5 | 0.2 (0.2) | ok |
| 49 Riot stick | 85 (`A1856`) | 1.75 | 0.5 | ok |
| 50 Machete | 100 (`A1859`) | 2 | 0.7 | ok |
| 52 Riot shield | 2 (`A1882`, "Low damage") | **2** | 1.0 | block 0→2; absorption 50% (`A1883`) and knockback 0.5 (`A1884`) unchanged |

Melee has a single player figure (no part tuple), so no headshot multiplier.
`MELEE_RANGE` is 3 and `MELEE_WORLD_RANGE` is 4 (stock), unchanged.

## Explosives (stock `ExplosionDamageManager`)

`handle_explosion_damage(world, damageable, by_player_id, pos, type, radius,
kb_min, kb_max, damage, classic=False)`. The `type` argument is the **kill
type**.

For a player damageable:

- `f = (R² − d²) / R²`, where `d` runs from the blast to the eye position plus
  0.75 down (1.25 when crouched). Nothing happens when `f ≤ 0`.
- LOS: three sight points (eye, +0.9, +1.8; crouched +0.45, +0.9) with weights
  0.5, 0.3 and 0.2. Each ray is `hitscan_accurate(e + 0.1v, v, length=1)`,
  i.e. it covers 10%–110% of the way to the point. `L` is the sum of the
  weights of the visible points.
- `damage = D · f · L`.
- When not classic: × `SELF_EXPLOSION_DAMAGE_REDUCTION` (0.5) for your own
  blast, otherwise × `TEAM_EXPLOSION_DAMAGE_REDUCTION` (0.5) if the victim is
  on `TEAM_NEUTRAL` (1).
- The impulse is `(kb_min + f·(kb_max − kb_min)) · L`, directed from the blast
  to the network position.

A non-player damageable takes `D · (R² − d²) / R²` at its own position, with
a single all-or-nothing ray.

**Fixed** in `main._apply_blast`:

- It used pyspades 0.75's `min(D, 4096/d²·D/100)` curve with full damage
  inside 1 block.
- It used a single eye ray.
- There was no self or neutral reduction.
- Knockback was not scaled by LOS.
- Entities had the same wrong curve.
- Now, when a blast's kill type and damage match a stock handler, that
  handler's radius, knockback and classic flag are used whatever the caller
  passed. That corrects placed dynamite (radius 5 → 8) and landmines
  (3 → 6).

| Handler | kill | radius | kb min/max | damage | Before → now |
|---|---|---|---|---|---|
| grenade | 3 | 4 | 0.5/1.0 | 230 | curve fixed |
| classic grenade | 22 | **9.0** (blast wave) | 0.1/0.1 | 130, **classic** | catalog radius 2→9 |
| antipersonnel grenade | 23 | **6.0** | 0.25/0.5 | 500 | catalog 2→6 |
| rocket (RPG) | 4 | **6.0** | 0/0.25 | 140 | catalog 4→6 |
| rocket2 (RPG2) | 5 | **6.0** | 0/0.25 | **40** | 50→40 (spec + catalog), catalog r 4→6. 2026-09-27: the thrower also gets 0/0.25 -- the 1.0/1.5 `ROCKET2_EXPLOSION_SELF_KNOCKBACK_*` pair is the mod tail, read by no stock code |
| UGC rocket2 | 27 | 4.0 | 0/0.25 | 50 | ok |
| drill / drill destroyed | 6 | 3.0 / 3.5 | 0.01/0.1, 0.1/0.2 | 50 / 95 | ok |
| UGC drill | 28 | 3.0 | 0.01/0.1 | 50 | ok |
| rocket turret (destroyed) | 18 | 3.0 | 0.2/1.0 | 100 | ok |
| rocket turret rocket | 18 | 3 | 0.1/0.3 | 50 | catalog 100→50 |
| corpse | 12 | 3 | 0.05/0.1 | 0 | ok |
| landmine | 14 | **6.0** | 0.75/0.75 | 100 | placed mine used 3 → 6 via override |
| mine launcher | 35 | **6.0** | 0.75/0.75 | 100 | catalog 3→6 |
| dynamite | 15 | **8** | 0.1/0.15 | **300** | placed 5→8; thrown spec 100→300 |
| molotov | 24 | 4 | 0/0.1 | 50 | ok |
| airstrike | 16 | 6 | 1.0/2.0 | 400 | ok |
| bomb | 17 | 7 | 2.0/3.0 | 500 | ok |
| snowball | 21 | 5.0 | 0.3/0.3 | 10 | ok |
| GL grenade | 32 | 4 | 0/0.25 | 100 | ok |
| radar station | 33 | 3.0 | 0/0.1 | 7 | ok |
| sticky grenade | 34 | 5 | 0.75/0.1 (stock order) | 200 | ok |
| C4 | 36 | 8 | 0.1/0.15 | 300 | ok |

There is no stock handler for the chemical bomb or the UGC snowball. The
chemical bomb has no blast at all (see the next section); the UGC snowball
keeps its caller's figures and is marked unverified.

**Dynamite stock (2026-09-27).** Stock `DynamiteWeapon.ammo =
(A1627, A1628, None, None, A1629)` with `A1627 = 1`, `A1628 = A1629 = A1627`
(`aceofspades_source/shared/backup/constants-copy.py:2601-2603`, the alias
block the stock client runs): a Miner carries **one** stick, starts with one
and an ammo crate restocks one. The named `DYNAMITE_STOCK = 3` /
`DYNAMITE_RESTOCK_AMOUNT = DYNAMITE_STOCK` came from the nonsteam MOD block;
`shared/constants.py` STOCK RESTORE now sets 1 / 1 / 1, which
`server/deployable_inventory.py` enforces. Test:
`tests/test_deployable_inventory.py::test_dynamite_wallet_matches_stock_alias_one_one_one`.

## Molotov fire and Chemical Bomb goo (2026-09-26)

Recovered with headless IDA on the stock `aoslib.scenes.main.gameScene.pyd`
and `aoslib.character.pyd`, plus the stock constant dump.

| Evidence | Meaning |
|---|---|
| `ENTITIES[28]` = `BlockFireEntity`, `ENTITIES[31]` = `BlockGooEntity` | both are server-created surface patches (CreateEntity) with a fuse, a static point light (`BLOCKFIRE_LIGHT_RADIUS`) and a hot->cold colour ramp over `fuse / A1764` (`BLOCKFIRE_MAX_LIFESPAN` 4.0). Fire adds smoke and a looping sound; goo has neither and its ramp is white -> (20,255,50) -> green (A2432..A2434). |
| `MolotovEntity`, `ChemicalBombEntity` (27, 32) | flying projectiles, `set_bouncing(False)`, `set_stop_on_collision(True)`, gravity x1.0 (A1648/A1665). `on_delete` only plays the land sound (or the water variant). |
| `BlockManager` damage table | type 24 `handle_molotov_damage` = radius 4 (A1649); type 25 `handle_blockfire_damage` and type 43 (A417) `handle_chemical_bomb_damage` = single block. `on_single_block_damaged` spawns chemical-bomb debris for type 43. |
| `ExplosionDamageManager` | has `handle_molotov_damage` (50, r4, knockback 0/0.1) but no chemical-bomb handler, although every other late explosive (GL, sticky, radar, mine, C4) has one. So the chemical bomb deals no blast damage and digs no crater. |
| WorldUpdate action bit `0x20` | `Character.set_is_on_fire`: ignite sound, burning-character smoke, burn-out or water sound when it clears. |
| WorldUpdate state bit `0x08` | `Character.set_touching_goo`: chemical burn loop A2920, ended by A2921 (A2922 in water). Nothing else; it is not a water flag. |
| SetHP type 3 (A984) | burn sound + `BURN_INDICATOR_TIME` flash; used for both fire and goo ticks. |
| Tool description | Chemical Bomb: "Dissolves blocks over time / Create damaging Goo". |
| Stock constants | Molotov stock 3, speed 40/35. Chemical Bomb stock 4 / initial 2 / restock 2, speed 50 (A1663) / 25 (A1664), radius 3 (A1666), damage 50, block 3. `BLOCKFIRE_*`: character 2.5 per 0.3 s for 10 s within 3; block 0.7 per 0.4 s; lifespan 4.0; spread 5 attempts, radius 2, chance 0.3, every 0.5 s; initial radius 2; falling distance 1.0. |

Server behaviour now:

- Molotov: stock blast (type 24), then up to five BlockFire patches on the
  exposed voxels nearest the impact; each spreads once from a shared budget
  of five. A fire whose voxel disappears drops onto the voxel below with its
  remaining life (`BLOCKFIRE_MAX_FALLING_DISTANCE`). Players whose body (eye
  down to feet) is within 3 of a burning voxel ignite for 10 s (2.5 HP per
  0.3 s, `BLOCKFIRE_KILL`, SetHP type 3, action bit 0x20); water puts them
  out. With friendly fire off a thrower's teammates do not ignite; the
  thrower does.
- Chemical Bomb: no blast and no crater. Every exposed solid voxel within the
  radius-3 footprint (A1666, the list BlockManager precomputes; at most 48)
  gets a BlockGoo patch for 4 s. Goo dissolves its voxel with Damage type 43
  (0.7 per 0.4 s, which eats map voxels in about 2.8 s and leaves built
  blocks) and drops onto the voxel below when its voxel goes. A player whose
  hull touches a goo voxel burns 2.5 HP per 0.3 s only while touching
  (`CHEMICALBOMB_KILL`, SetHP type 3, state bit 0x08); same friendly-fire
  rule. Goo's timers are not in any client binary; because the client's goo
  is literally the block-fire entity (it even reads the block-fire lifespan)
  it reuses the `BLOCKFIRE_*` cadence. That part is inference, not
  recovered data.
- Late joiners receive live fire/goo entities with their remaining fuse.

Live check (dev client, port 27048, 2026-09-26): goo patches (35) and fire
patches render and expire after 4 s; a bot standing in goo showed
`is_touching_goo` on the observer and lost 2-3 HP per tick; a bot next to a
molotov showed `is_on_fire` on the observer and burned to death; the
thrower's own HUD HP drained in both cases.

Tick spikes: the native `VXL.find_unsupported_chunks` released the GIL
around every tiny collapse walk. With the bot-AI thread busy, each release
cost a full switch interval (~15.6 ms at the Windows timer), dozens of times
per explosion. The walks now keep the GIL. Under a synthetic CPU-bound
competitor thread, explosions went from 15-340 ms to 0.3-8 ms. The first
blast and first kill of a match also paid 100+ ms importing
`server.explosions` / `server.kill_feed` lazily, so the server imports them at
startup.

## Damage modifiers

| Modifier | Retail | Ours now | Status |
|---|---|---|---|
| Class damage / headshot | `CLASS_DAMAGE_MULTIPLIER` / `CLASS_HEADSHOT_DAMAGE_MULTIPLIER` on the victim | victim-side, all sources (headshot on bullets only) | **fixed** |
| Jetpack | `JETPACK_PROPERTIES[pack][7]` (`JETPACK_DAMAGE_MULTIPLIER` index): 2.0 for 66/67, 1.0 for 68/69 | damage taken ×field 7 while `jetpack_active` | **added** (server-only field, no client reader; inference from its name) |
| Riot shield | 50% frontal absorption when displayed (`A1883`), 0.5 knockback on bash (`A1884`) | unchanged | ok |
| VIP | `VIP_DAMAGE_MULTIPLIER` 0.5 | `modes/vip.py` `modify_incoming_damage` | ok (not changed) |
| Self / neutral explosion | 0.5 / 0.5, skipped when classic | implemented | **fixed** |
| Friendly fire | the manager applies the knockback and the server's `add_damage` decides the HP | FF off: push kept, no HP loss (policy kept) | ok |

## Not recoverable / not implemented (no invented numbers)

- **Range falloff.** `short_ranged_distance = 25` (shotguns) and
  `WEAPON_DAMAGE_MULTIPLIER_THRESHOLD = 0.3` exist only as data. No client
  code reads them, and the original server is gone. Both are recorded in the
  catalog (`short_ranged_distance`) but not applied.
- **Block penetration.** `block_penetration = 2.5` for every gun is sent in
  ShootPacket but used only by the missing server. Bullets still stop at the
  first voxel.
- **Explosion class multiplier.** Applying the victim class multiplier to
  explosions follows the "effective health" reading; the manager itself does
  not apply it.

## Follow-ups outside this change (other owners)

- `shared/constants.py` still carries the modded values: `PISTOL_*`,
  `PICKAXE_*`, `KNIFE_*`, `SPADE_SHOOT_INTERVAL`, `CROWBAR_SHOOT_INTERVAL`,
  `SMG_RANGE` 250, `ROCKET2_EXPLOSION_DAMAGE` 50, `PICKAXE_DAMAGE_AMOUNT` 9.
  Combat now reads the catalog instead.
- `server/dig_profiles.py`: pickaxe block 9 should be 7. The intervals are
  read from the modded constants (spade 0.4, pickaxe 0.4, knife 0.25,
  crowbar 0.6). Riot shield block is 0 but stock is 2.
- `server/deployable_actions.py` passes dynamite radius 5 and landmine
  radius 3. `_apply_blast` now overrides these with the stock 8 and 6, but
  the caller should be corrected too.
- `server/player.py` ammo (done for guns; oriented tools done 2026-09-27):
  - Spawn should grant `initial_reserve` (rifle 30, classic shotgun 20).
  - A crate should add `restock_amount` capped at the max, not reset.
  - `clip_reload` weapons should load one round per `reload_time`.
  - The catalog now carries all three fields.

## Oriented tools, rules audit 2026-09-27

`ORIENTED_STOCK_AMMO` (`server/player.py`) holds each thrown/launched
tool's stock `(magazine max, magazine initial, reserve max, reserve
initial, crate restock)`:

| Tool | Stock | Spawn | Ammo crate |
|---|---|---|---|
| Grenade / Classic / AP grenade | `Tool` 4 / 2 / restock 4 | 2 | `min(count + 4, 4)` = 4 |
| Molotov | `(3, 3, None, None, 3)` | 3 | 3 |
| Sticky / Chemical bomb | `(4, 2, None, None, 2)` | 2 | `min(count + 2, 4)` |
| RPG | `(1, 1, 3, 3, 3)` | 1 + 3 | reserve `min(r + 3, 3)`, clip kept |
| RPG2 | `(3, 3, 3, 3, 3)`, `clip_reload` | **3 + 3 = 6** (was 9: `RPG2_AMMO_MAX = 6` is the mod tail) | reserve `min(r + 3, 3)` |
| Drill | `(1, 1, 3, 1, 2)` | 1 + 1 | reserve `min(r + 2, 3)` |
| Grenade / mine launcher | `(1, 1, 5, 3, 5)` | 1 + 3 | reserve `min(r + 5, 5)` |

- The crate used to RESET every oriented stock to its spawn value (and the
  launcher clip and cadence): after a crate the stock client showed 4
  grenades while the server allowed 2, so throws 3-4 were ghost grenades.
  It now tops up exactly like `Tool.restock` / `Weapon.restock(AMMO_CRATE)`
  and the native `WeaponReplicationState::restock_from_ammo_crate`.
- RPG2 (`clip_reload = True`) reloads ONE rocket per 1.0 s; the inferred
  launcher reload refilled the whole clip after one reload time.
- Blast knockback timing: stock `GameScene.process_packet_damage` ->
  `ExplosionDamageManager.handle_damage` dispatches on the Damage(37) TYPE
  alone (`damage_functions`: 7-13, 15, 16, 18-24, 30, 33, 37-41; oracle run
  of the stock pyd) and pushes the local character. Every blast whose
  terrain Damage(37) is such a type is therefore predicted by stock clients,
  so the server applies the push on the third accepted input frame (the
  verified Snowball timing) and always sends that packet, even over air.
- Fall rules: `ROCKET_JUMP_FALL_DAMAGE_MULTIPLIER` 0.2 after an own RPG/RPG2
  self-push, `ZERO/MAX_FALL_DAMAGE_AIR_TIME` scaling, a lethal fall credits
  the last enemy damager within `PLAYER_INTERACTION_EXPIRY_SECONDS` 5.
- `RULE_ONE_HIT_KILL` applies only to `ONE_HIT_KILL_WEAPONS` kill types.
- Hit damage is rounded once, in `Player.damage` (it was also rounded per
  hit with a floor of 1 before `RULE_WEAPON_DAMAGE`).
- The Snowblower honours `TeamInfiniteBlocks(82)`.
- Burn and goo ticks keep the raw 2.5 (no class multiplier): no evidence
  that the stock server scaled them; left as is.
