# Battle Builder content replication roadmap

Last updated: 2026-07-14

This is the working parity ledger for the post-launch Battle Builder client in
`G:\AoSRevival\AceOfSpades_no_steam_new`. A row is only marked implemented
when the server path and an automated regression test both exist. "Live" means
it has also been observed with two real clients.

## Map bases and spawns

UGC maps: authored spawn zones, bases, and drop entities come from the JSON
sidecar (`.txt`, `.ugc`, or `.json`) next to the VXL. That is the intended
retail format. The VXL itself is only 512 x 512 voxel-column data and contains
no gameplay metadata trailer.

Stock VXL maps: the original official server supplied hand-authored locations;
those coordinates are not embedded in the files we have. Our dry, locally
level terrain fallback rejects water, ocean bed, roofs/platforms with air
beneath them, and candidates invalidated by live terrain edits. It chooses one
stable candidate nearest the team-region centre and varies player spawns only
inside a 24-block perimeter around that base, but
it is not claimed to be the original official coordinate set. CTF's tent/base
and flag entities are now created at those selected anchors.

## Current implementation ledger

| Area | Intended behavior recovered from | Server status | Validation |
|---|---|---|---|
| Commando parachute | Dec 2015 changelog: press Space again after jumping | Manual second airborne press; no descent auto-open | Automated |
| Tombstone | Client grave constants and `GraveEntity` decompile | Grounded, team-coloured, 7 s fuse, 25 damage, radius 3, block damage 3 | Automated; live camera check pending |
| Death camera | `DeathController` has explicit mouse-move orbit path | Stable grounded death anchor; deathcam remains enabled | Client decompile + automated feature switch; live pending |
| RPG/RPG2 player hits | Projectile engine + client collision range | Swept character collision; no high-speed tunnelling | Automated |
| Grenades | Recovered constants.pyc | Standard 230/4, classic 130/15, AP 500/0.5 | Automated |
| Chemical bomb | Recovered A1658-A1676 constants | Type-32 contact projectile, 50 damage, radius 3, block damage 3 | Automated + retail projectile/ammo validation |
| Molotov and block fire | Retail constants plus `MolotovEntity`/`BlockFireEntity` native dispatch | Impact blast creates replicated type-28 fires; 4 s life, timed spread/block damage, 2.5 damage per 0.3 s character burn, 10 s duration, water extinguish, authoritative fire display bit. Wire face stays `FACE_TOP=4`; other faces rotate a model-less particle and tear down GameScene | Automated + retail fire/scene-stability validation |
| Sticky grenade | Recovered A1677-A1694 constants and `AttachedStickyGrenadeEntity` | Type-34 world/player attachment, follows player, 5 s fuse, 200/6, radius 5 | Automated + retail projectile/fuse validation |
| Grenade launcher | `grenadeLauncherWeapon.py`, `GLGrenade`, `process_packet_use_oriented_item` at `0x1018E930` | Server-authoritative type-33 projectile, 75 speed, contact/3 s lifespan, 100/6, radius 4; crashing remote packet-10 constructor suppressed | Automated crash regression + retail fire/explosion/no-crash validation |
| Hand landmine | Packet decompile + A1729-A1743 | Raw-voxel coordinate decode, 4 s arm, 2.5 horizontal range, three vertical layers, 100/15 | Automated; live single-client placement |
| Mine launcher | `mineLauncherWeapon.py` | Type-37 75-speed projectile becomes an armed replicated type-9 landmine on terrain contact | Automated + retail flight/deployment validation |
| Dynamite | Recovered constants | Raw-voxel placement decode, 7 s fuse, 300/7, radius 5 | Automated; live placement/fuse/removal |
| C4 | `C4Entity` decompile and A1745-A1757 | Entity type 38, oriented face placement, two charges, owner detonation, 300/7, radius 8 | Automated; two-client render live-verified |
| Drill gun | `drill.py` and A1483-A1509 | Stops and drills obstructing blocks with entity-caused `DRILL_DAMAGE`, continues until 3 s destroyed blast; player contact explodes | Automated; live pending |
| Engineer Block Cannon | `snowBlowerWeapon.py`, final retail strings (`SNOWBLOWER = Block Cannon`) | Tool 29 spends one shared block, snapshots palette/shot loop, renders a coloured type-24 projectile, commits the last free supported voxel on terrain contact, sends coloured packet 33, and journals the persistent build | Automated + retail live color + clean-process reconnect/VXL validation |
| Miner Super Spade | Retail `handle_superspade_damage` (`0x10082C90`) plus direct packet-37 probe | Centered, axis-aligned 3x3x3 canonical removal; one type-3 area Damage packet, actual-cell block refund. Ordinary spades remain a z column and pickaxes remain single-cell | Automated + direct retail footprint probe |
| Settled terrain repair | Native packet-33 build and exact type-6 packet-37 removal paths | Delayed canonical repair of recent and rejected predicted footprints; bounded/deduplicated, current VXL state at send time, no join/collapse replay | Automated; two-client long-session validation pending |
| Blocksucker | `blockSuckerWeapon.py` and packet 94 | Sanitized remote state/sound/debris relay; 1 s warm-up, 0.2 s pull cadence, block grant | Automated protocol paths; live pending |
| Engineer disguise | `disguiseTool.py`, ClientData/WorldUpdate state bit | Two-charge stock, activation validated against tool/loadout, replicated in state bit 0x02, cleared on firing/death/spawn | Automated + two-client retail `DisguiseBlocks` validation |
| Radar station | `RadarStationEntity`, packet 83, A1893-A1902 | Entity type 36, 250 s life, per-team reference-counted enemy minimap visibility | Automated lifecycle; two-client render live-verified |
| Medic pack | `MedPackEntity`, `medPackWeapon.py` | Entity type 30, visible/team-owned, 25 HP x three uses | Automated entity behavior; two-client render live-verified |
| Deployable health | `HitEntity(20)` client receive path + recovered C4/medpack/radar health | Authoritative nearest-hit routing and blast damage: C4 1 HP, medpack 1 HP, radar 45 HP; packet 20 is visual-only | Automated; impact FX/hit-volume live calibration pending |
| Riot shield | `riotShieldTool.py`, A1881-A1887, Dec 2015 Medic changelog | Held tool 52 absorbs 50% of frontal direct hits; bash deals 2 and applies 0.5 horizontal knockback; model/bash replicate through existing WorldUpdate tool/action fields | Automated; live feel/model validation pending |
| Sniper laser | `sniperWeapon.py` / `sniper2Weapon.py` | `enable_sniper_beam=1`; remote zoom is WorldUpdate action bit 0x40 | Automated packet/feature switch; live pending |
| Gun damage/cadence | Recovered weapon classes and constants.pyc | All selectable hitscan profiles are catalogued. Shotguns expand one trigger into exact seeded pellet traces (Auto Shotgun: ten pellets, 0.35 s, 0.05 minimum accuracy); Machete has zero terrain damage and rejected client dig predictions are repaired | Automated + retail Auto Shotgun/Machete validation |
| Rocket turret | Earlier client/server decompile | Placement, aim, target, ammo, rockets, ten-shot lifetime | Automated; prior live report working |
| Mounted machine gun | Packet 87, `mgWeapon.py`, entity constants, original `ace-server` handler | Type-7 durable placement, yaw/team/join sync, use-key mount/dismount, 100 health and 100/5/radius-3 destruction blast, deployed 0.1 s cadence | Automated; live client model/mount validation pending |
| Flare/light block | `flareBlockTool.py`, live packet-104 capture, `FlareBlockEntity` native lifecycle | Raw-voxel placement, ten-block cost, palette/team colour, entity type 13, water-plane exception, support loss/destruction and late-join sync | Automated; live lighting/model validation pending |
| Explosive knockback | Native `explosionDamageManager.pyd` plus recovered per-warhead constants | Squared body-centre falloff, additive authoritative velocity, LOS/team/self rules for every implemented projectile and deployable | Automated; live feel/partial-cover calibration pending |
| Objective pickups and jetpack crate | Native packet 70/71 handlers, pickup table, crate `Restock(69)` path | CTF intel carry/drop/death/disconnect/capture and late-join state; spoof-safe drop vectors; authored type-6 jetpack crates | Automated; live carry HUD/drop arc pending |
| Zombie Infection | Retail mode constants, class tables, native class menu, and ChangePlayer action 8 | Prep/outbreak phases, Patient Zero, death conversion, zero-delay zombie respawn, survivor timeout win, last-man marker/scoring, sole-zombie disconnect replacement, forced late joins | Automated + three-client retail outbreak/late-join validation |
| Rocketeer Glide Jetpack | Retail `world.pyd` pack switch and class/tool tables | Tool 67 uses 0.0125 vertical thrust and 17 fuel/s: weaker than gravity for low-altitude, long-endurance forward glide; distinct from Engineer tool 68 | Automated + retail input-path activation |
| Same-map round reset | Native end-scene probes and entity/controller ownership audit | Keeps GameScene alive, sends safe GameStats data only, clears transient state and old entity ids, applies pending class/loadout at the shared respawn boundary, rebuilds crates, resets scores, and respawns players | Automated; live Medic-to-Miner retail cycle, correct dynamite and no new dump |

## Entity-type coverage (0-39)

| IDs | Retail entity family | Current state / dependency |
|---:|---|---|
| 0-1 | Flag, base tent | Implemented for CTF with dry authored/fallback anchors |
| 2 | Helicopter | Pending a mode/map path that actually creates it |
| 3-6 | Ammo, health, block, jetpack crates | Implemented, including authored positions, touch/restock, respawn, and join sync |
| 7 | Mounted machine gun | Implemented: place, mount, fire, health, blast, join sync |
| 8 | Rocket turret | Implemented: place, track, aim, fire ten rockets, destroy |
| 9-10 | Landmine, dynamite | Implemented |
| 11 | Grave/tombstone | Implemented; live death-camera presentation still pending |
| 12 | Corpse | Client packet/model known; explicit corpse-explosion gameplay remains pending |
| 13 | Flare block | Implemented, including water placement and light cleanup |
| 14-16 | Bomb, diamond, intel pickups | Intel implemented for CTF; bomb/diamond wait on Demolition/Diamond Mine modes |
| 17 | Airstrike | Pending Multi-Hill/game-mode trigger and strike controller |
| 18-20 | Ammo/health/block drop points | Metadata types known; mode-driven timed spawning pending |
| 21-24 | Rocket, rocket2, drill, snowball | Implemented server-authoritative projectiles |
| 25 | Capture point | Pending Territory Control/Multi-Hill modes |
| 26 | Tank | Client entity known; no recovered active stock-mode creation path yet |
| 27-28 | Molotov, block fire | Implemented projectile, impact, persistent fire, damage, spread, and visuals |
| 29 | UGC entity | Packet/table support exists; full PlaceUGC editor lifecycle remains pending |
| 30 | Medpack | Implemented placement, behavior, health, destruction, join sync; observer render live-verified |
| 31-35 | Block goo, chemical bomb, GL grenade, sticky, attached sticky | Types 32-35 are implemented through safe CreateEntity paths; chemical, GL, and sticky were retail-fired without the stale packet-10 constructor crash. Block goo remains mode-dependent |
| 36 | Radar station | Implemented placement, behavior, health, destruction, join sync; observer render live-verified |
| 37 | Projectile mine | Implemented and retail-validated as the Mine Launcher flight object before type-9 arming |
| 38 | C4 | Implemented placement, behavior, health, destruction, join sync; observer render live-verified |
| 39 | Riot-shield entity class | Equipped shield uses tool 52, not placement; held defense/bash implemented |

## Remaining live/parity work

1. Run two-client scenarios for tombstone camera, every placed entity model,
   radar visibility, disguise appearance, flare lighting, MG mounting, carried
   intel, burning players, and remote Blocksucker effects.
2. Calibrate explosive partial-cover weighting, same-team physical push, and
   local RPG2 self-boost reconciliation against two real clients. The complete
   impulse path and per-warhead constants are now authoritative.
3. Capture observer-camera presentation for late-weapon hand/throw animations;
   projectile transitions and crash-safe entity types are now validated.
4. Implement mode-gated entity families next: bomb/diamond objectives,
   capture points, airstrikes, drop points, then full UGC placement.
5. Acquire original official-server stock-map coordinate tables if exact
   historical stock spawn/base placement is required. Safe fallback placement
   cannot prove an unavailable hand-authored coordinate.

## Evidence locations

- Changelog supplied by the project owner: the attached Dec 2015+ news dump.
- Recovered Python client: `G:\AoSRevival\aceofspades_decompiled`.
- Exact retail bytecode constants: `G:\AoSRevival\AceOfSpades_no_steam_new\shared\constants.pyc`.
- Native client decompile findings: `docs/REPLICATION_IDA_FINDINGS.md` and the
  current IDA batch logs under ignored `tmp/`.
