# Battle Builder content replication roadmap

Last updated: 2026-07-10

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
level terrain fallback is safe (no water, ocean bed, or roof candidates), but
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
| Chemical bomb | Recovered A1658-A1676 constants | Contact explosive, 50 damage, radius 3, block damage 3 | Automated |
| Sticky grenade | Recovered A1677-A1694 constants and `AttachedStickyGrenadeEntity` | World/player attachment, follows player, 5 s fuse, 200/6, radius 5 | Automated |
| Grenade launcher | `grenadeLauncherWeapon.py`, `GLGrenade` | 75 speed, contact/3 s lifespan, 100/6, radius 4 | Automated |
| Hand landmine | Packet decompile + A1729-A1743 | Fixed coordinate decode, 4 s arm, 2.5 horizontal range, three vertical layers, 100/15 | Automated; live pending |
| Mine launcher | `mineLauncherWeapon.py` | 75-speed projectile becomes an armed replicated landmine on terrain contact | Automated; live pending |
| Dynamite | Recovered constants | Fixed placement decode, 7 s fuse, 300/7, radius 5 | Automated; live pending |
| C4 | `C4Entity` decompile and A1745-A1757 | Entity type 30, oriented face placement, two charges, owner detonation, 300/7, radius 8 | Automated; live pending |
| Drill gun | `drill.py` and A1483-A1509 | Stops and drills obstructing blocks with entity-caused `DRILL_DAMAGE`, continues until 3 s destroyed blast; player contact explodes | Automated; live pending |
| Blocksucker | `blockSuckerWeapon.py` and packet 94 | Sanitized remote state/sound/debris relay; 1 s warm-up, 0.2 s pull cadence, block grant | Automated protocol paths; live pending |
| Engineer disguise | `disguiseTool.py`, ClientData/WorldUpdate state bit | Activation validated against tool/loadout, replicated in state bit 0x02, cleared on firing/death/spawn | Automated wire state; live pending |
| Radar station | `RadarStationEntity`, packet 83, A1893-A1902 | Entity type 32, 250 s life, per-team reference-counted enemy minimap visibility | Automated lifecycle; live pending |
| Medic pack | `MedPackEntity`, `medPackWeapon.py` | Entity type 31, visible/team-owned, 25 HP x three uses | Automated entity behavior; live pending |
| Sniper laser | `sniperWeapon.py` / `sniper2Weapon.py` | `enable_sniper_beam=1`; remote zoom is WorldUpdate action bit 0x40 | Automated packet/feature switch; live pending |
| Gun damage/cadence | Recovered weapon classes and constants.pyc | All selectable hitscan profiles are catalogued; expanded parity assertions cover every gun row | Automated |
| Rocket turret | Earlier client/server decompile | Placement, aim, target, ammo, rockets, ten-shot lifetime | Automated; prior live report working |

## Remaining live/parity work

1. Run two-client scenarios for tombstone camera, every placed entity model,
   radar visibility, disguise appearance, and remote Blocksucker effects.
2. Add entity-health hit routing for destructible radar/C4/medpack models; their
   recovered health is known, but ordinary hitscan currently prioritizes
   players and voxels.
3. Reproduce and calibrate knockback for every explosive. Damage, radius, fuse,
   collision, LOS, and block effects are authoritative; knockback is not yet
   applied server-side.
4. Capture one packet trace per late weapon to verify client-local projectile
   creation does not duplicate the server CreateEntity transition.
5. Acquire original official-server stock-map coordinate tables if exact
   historical stock spawn/base placement is required. Safe fallback placement
   cannot prove an unavailable hand-authored coordinate.

## Evidence locations

- Changelog supplied by the project owner: the attached Dec 2015+ news dump.
- Recovered Python client: `G:\AoSRevival\aceofspades_decompiled`.
- Exact retail bytecode constants: `G:\AoSRevival\AceOfSpades_no_steam_new\shared\constants.pyc`.
- Native client decompile findings: `docs/REPLICATION_IDA_FINDINGS.md` and the
  current IDA batch logs under ignored `tmp/`.
