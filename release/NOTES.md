BattleSpades Beta 0.2 brings the recovered retail compatibility work and the
follow-up gameplay and stability fixes into one server release.

## Gameplay and compatibility

- Bots, measured over simulated matches on every map and mode:
  - They cross water. A swim is carried to the far bank instead of ending on
    the bank it started from, so Zombie bots spawned on an islet or in the sea
    reach the survivors, and teams meet on island maps.
  - The Zombie horde chooses between walking in, clawing through and
    collapsing a structure by estimated time, sprints up slopes when its class
    can, and no longer queues behind one teammate's hole.
  - They walk up to an objective on another storey instead of pillaring or
    digging underneath it, and a bot whose local route dead-ends no longer
    paces for seconds while the map-wide search waits its turn.
  - Classic CTF bots no longer stand idle once an intel is dropped far away;
    CTF teams split into carrier, escorts, hunters, raiders and a home guard;
    bots no longer know a thief's position before the minimap marks it.
  - They react to being shot by someone they cannot see, to gunfire,
    footsteps, digging and building within hearing distance, to a teammate
    killed beside them and to burning blocks, and they break off a fight they
    are losing.
  - They use the tools their class carries (landmines, radar, turret, Block
    Cannon, disguise, C4, dynamite), and a hurt or dry bot goes to a crate or
    a medic.
  - The route planner's queue no longer leaves bots waiting for a plan.
- Evaluate 71 of the 77 original achievements on the server, on any server
  including a player's own Create Match, and announce each unlock in game
  (`docs/ACHIEVEMENTS.md`). Six map achievements still need their regions.
- Dedicated servers can be joined through Steam's relay network where their
  address is blocked (`[steam_host]`, off by default; see
  `deploy/steam-linux/README.md`).
- Fix transparent terrain on late join after earlier players dig or destroy
  blocks: newly exposed solid faces now carry their colors into map transfer.
- Correct map lighting, marker/light colors and player block colors. Placed
  prefabs retain the original retail model/team-color blend.
- Keep players connected through stock retail map rotation and preserve the
  normal map loading, team and class selection flow.
- Fix disguise lifetime, stale entity attachments, respawn cleanup and mode
  transitions across Classic CTF, Diamond Mine, VIP, Zombie, Territory Control,
  Tutorial and Map Creator.
- Recover weapon damage, spread, ammunition, restocking, explosion, fire, goo,
  block damage and prefab behavior; improve lag compensation and action timing.
- Improve bot navigation, shore recovery, mining and human slot availability.

## Movement and networking

- Correct class movement speed. The per-class speed scale in InitialInfo is
  now the lobby speed rule alone, as on the original servers, instead of the
  class sprint multiplier, which was being applied twice. A Soldier walks 5.6
  and sprints 11.2 blocks per second (was 7.9 and 15.8), a Classic Soldier
  walks 8 as in Ace of Spades 0.75, and every other class slows by its own
  sprint factor; jetpack travel shortens with it. Zombie Speed
  (`RULE_CLASS_SPEED`) now scales the infected classes once on both client and
  server.
- Give retail clients nominal 30 Hz airborne owner updates while preserving
  the native BattleSpades client's six-tick cadence.
- Improve lost-input recovery, bounded idle backlog handling, jump replay
  delivery and jetpack/parachute handoffs. Server corrections remain enabled.
- Preserve the native jump's first displacement so the server works with the
  optional retail movement fix. Unpatched retail still has a local jump-reset
  defect; this release does not promise zero rollback in the stock client.
- Split remote snapshots from ordered owner updates to reduce reliable-channel
  stalls, with a per-connection reordering guard.

## Hosting and stability

- Reduce voxel-memory overhead, improve map loading and worker lifecycle, and
  add repeatable regression, network-impairment and capped-memory soak tools.
- Yield periodically during background spawn discovery so map rotation keeps
  the live server responsive on macOS as well as Windows and Linux.
- Add private-server password support for the maintained BattleSpades client,
  configurable map constructs and Diamond Mine discovery/cash-in options.
- Update the in-game welcome to [Discord](https://discord.gg/aosbb), where
  players can find the server source code and community support.

## Validation and limits

Every downloadable server archive passes the complete server test suite and
packaged-launcher checks on its own GitHub runner before publication. The
release requires all six Windows/Linux/macOS x86_64/arm64 builds and a complete
SHA-256 manifest.

Before packaging, the final movement compatibility changes passed 1,953
targeted tests on both Windows and Linux, including an independent original
native jump fixture. A six-hour earlier-runtime soak completed under a
536,870,912-byte cap with zero swap, OOMs, failures or unexpected restarts;
peak cgroup memory was 373.10 MiB. That soak predates the final movement edits
and the final background spawn-scan change, and does not establish indefinite
leak freedom or high-player-count coverage.

Native client source is published separately for local building and testing;
this release contains **server downloads only**. Optional retail fix sources
are available in `client_patches/retail_mousefix`; no client binary is built or
attached here. Keep your existing configuration and review the new options
against the supplied defaults when upgrading.

These beta archives are unsigned; macOS archives are not notarized. Verify the
download against `SHA256SUMS.txt`. The server is AGPL-3.0-or-later; third-party
licenses remain in the bundled notices.
