BattleSpades Beta 0.2 brings the recovered retail compatibility work and the
follow-up gameplay and stability fixes into one server release.

## Gameplay and compatibility

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
