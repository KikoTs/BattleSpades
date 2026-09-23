# Gameplay, Modes, Maps, and Bots

BattleSpades is authoritative: clients submit inputs and actions, while the
server owns health, ammo, inventory, movement, voxel mutations, entities,
objectives, deaths, scores, and round transitions. Retail clients must see the
same class, held tool, feature switches, and terrain that the server validates.

Use `modes/`, `server/game_rules.py`, `server/game_constants.py` and their
focused tests to resolve implementation questions. This guide describes source
behavior; an older frozen release may predate local changes. Validation and
packaging are covered in [RUNBOOK.md](RUNBOOK.md).

## Match Lobby modes

The ten public rows were recovered from the shipped Match Lobby. `mode_id`,
title strings, class menus, atmosphere, clock, and shared map resources are
sent for every registered mode.

| Code | Mode | Retail clock | Implementation |
|---|---|---:|---|
| `tdm` | Team Deathmatch | 15 min | playable |
| `ctf` | Capture the Flag | 30 min | playable |
| `cctf` | Classic CTF | 90 min | playable; CTF scene with classic flag |
| `zom` | Zombie/Infection | 10 min | playable |
| `vip` | VIP | 15 min | playable |
| `mh` | Multi-Hill | 25 min | rotating shared hills, control score, airstrikes |
| `tc` | Territory Control | 25 min | territory ownership, capture and control scoring |
| `dia` | Diamond Mine | 15 min | diamond discovery, carrying and cash-in objectives |
| `dem` | Demolition | 15 min | build phase, destructible/repairable team bases |
| `oc` | Occupation | 15 min | bomb possession, scoring and detonation lifecycle |

`arena` is a BattleSpades extension and is not one of the retail hosting rows.
The objective modes have implemented state machines and focused tests. Complete
retail/native match acceptance is still required before claiming exact parity.

## Official playlist map lists

These are the exact basenames from the retail `playlists/*.txt` files:

- TDM: AncientEgypt, ArcticBase, Atlantis, BlockNess, CastleWars,
  DoubleDragon, DragonIsland, Frontier, GreatWall, Invasion, London,
  LunarBase, MayanJungle, SpookyMansion, TheColosseum, TokyoNeon.
- CTF: Atlantis, BlockNess, CastleWars, DoubleDragon, Invasion, TokyoNeon.
- Classic CTF: Crossroads, Hiesville, ToTheBridge, Trenches, WinterValley,
  WW1, Classic.
- Zombie: AncientEgypt, ArcticBase, Atlantis, BlockNess, BranCastle,
  CastleWars, DoubleDragon, DragonIsland, Frontier, GreatWall, Invasion,
  London, MayanJungle, SpookyMansion, TheColosseum, TokyoNeon.
- VIP and Territory Control: Alcatraz, CityOfChicago.
- Multi-Hill: AncientEgypt, Atlantis, BlockNess, BranCastle, CastleWars,
  DoubleDragon, DragonIsland, Frontier, GreatWall, Invasion, London,
  LunarBase, MayanJungle, SpookyMansion, TheColosseum.
- Diamond Mine: AncientEgypt, ArcticBase, Atlantis, BlockNess, BranCastle,
  CastleWars, DoubleDragon, DragonIsland, Frontier, GreatWall, London,
  LunarBase, MayanJungle, SpookyMansion, TheColosseum, TokyoNeon.
- Demolition: Atlantis, BlockNess, CastleWars, DoubleDragon, DragonIsland,
  Frontier, GreatWall, LunarBase, TokyoNeon.
- Occupation: AncientEgypt, ArcticBase, Atlantis, BlockNess, BranCastle,
  DragonIsland, Frontier, GreatWall, Invasion, London, LunarBase, MayanJungle,
  SpookyMansion, TheColosseum.

Dedicated servers may run other compatible VXL files. `[lobby].map_rotation`
controls the operator catalog; an empty list discovers installed maps.

## Implemented mode invariants

TDM scores cross-team kills, sends player and team score packets, and ends once
the configured target or clock is reached.

CTF owns two visible base zones, ground intel entities, carrier minimap
visibility, pickup/drop/capture, death/disconnect/team-change drops, optional
touch return, optional timed return, optional own-intel-at-base scoring, and a
carrier shooting gate shared by client and server. Classic CTF locks both teams
to Deuce, uses the ordinary CTF scene with `classic=1`, hides its disabled
weapons, enables carrier fire by playlist default, and disables auto-return.

Zombie owns survivor/infected phase transitions, Patient Zero selection,
permanent conversion, role-safe classes, respawns, last-survivor visibility,
zombie damage/speed rules, and mode-aware bot policy.

Zombie survivor bots roam instead of holding their spawn. Before the outbreak
every third survivor fortifies near the survivor base while the rest scout.
Each survivor walks its own leg between public map points (pickups, team
anchors, rings around the survivor base), preferring points away from other
survivors and their destinations. It keeps a leg until it arrives and lingers
briefly, makes no progress for 15 seconds, or passes the leg deadline. A
zombie within 28 blocks makes it hold and fire; within 9 blocks it backs away
while still firing. Roam, hold, and kite roles are committed, so optional
team errands cannot pull survivors back into one cluster.

VIP owns one boss per team, VIP health, markers, team respawn lockout, sudden
death, elimination, sub-round score, role reset, and gangster class menus.

Every mode uses the same crash-safe end sequence and session transition
service. A map or mode change validates and preloads its replacement before
the old scene receives `MapEnded(52)`. Maintained clients acknowledge
`LoadingMenu` and reload over the retained connection; incompatible clients
receive reason 18 without being sent new-scene packets.

## Classes, equipment, and construction

Movement profiles use every class's own recovered table entry, including
acceleration, crouch/sneak speed, water friction, uphill sprint permission,
and fall thresholds. Fast/Jump Zombie, Specialist, and Medic no longer inherit
partial Soldier/Zombie profiles. Classic Soldier and Jump Zombie cannot sprint
uphill, matching the client tables. These values are checked against a fixture
extracted from the original Python constants and verified against its Python 2
bytecode.
Disabling water fall damage also passes a zero water-damage multiplier to the
native mover, matching the client's class constructor and its landing result.

Class/loadout selection is transactional. `ChangeClass(78)` and
`SetClassLoadout(13)` can arrive in either order, but one normalized selection
is committed only at a life boundary. Disabled/cross-class tools cannot survive
normalization or pass deployable authorization.

Block placement, lines, digging, prefabs, flare blocks, collapse, paints, and
repairs commit through authoritative services after their matching movement
frame. Inventory is reserved before deferred work and refunded if a mutation
loses validation. Joiners receive a full VXL snapshot plus a bounded mutation
journal, so edits made during transfer are not lost.

Map metadata and sidecars may provide team spawn regions, bases, entities,
pickups, fog, skybox, ambience, and static flare lights. Fallback resources are
anchored to dry surfaces when authored positions are absent. Official-client
assets are referenced by stock names; custom maps receive full map sync.

## Bot runtime

Bots are ordinary server-owned `Player` objects. They use the same spawn,
class, inventory, movement, combat, terrain, entity, damage, score, death, and
replication paths as humans. The 60 Hz motor stays on the authoritative thread;
perception, behavior selection, and voxel navigation run in a supervised thread
by default, or a child process with `bots.worker = "process"`. Both backends use
the same direct voxel planner with versioned, expiring messages.

The navigation stack combines bounded local voxel search, incremental surface
guidance, and a semantic navigation atlas. Walk, jump, drop, short fuel-gated
flight, digging, and construction are checked against live terrain and normal
inventory rules. Fresh intents may survive unrelated terrain edits, but cannot
authorize traversal through a new block. Water/edge penalties,
pickup deadlines, progress watchdogs, danger interrupts, ammo/health recovery,
mode objectives, and close-threat priority prevent long-lived idle goals.

Fair perception uses field of view, voxel line of sight, delayed last-seen
memory, approximate sound stimuli, reaction time, bounded turn acceleration,
and correlated aim error. A bot cannot fire at an occluded exact position or
bypass a disabled tool, empty magazine, class rule, or objective lock.

The detailed navigation contract and its limits are in
[BOT_NAVIGATION.md](BOT_NAVIGATION.md). Operational validation commands are in
[RUNBOOK.md](RUNBOOK.md); configuration and `/bots` controls are in
[ADMIN_GUIDE.md](ADMIN_GUIDE.md).
