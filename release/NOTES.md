BattleSpades Beta 0.3 (0.3.0-beta.1) improves map hosting, Workshop browsing and
LAN discovery, alongside the matching BattleSpades client release.

## Maps and Workshop

- Import original AoS 0.75/0.76 VXL maps directly. Terrain, water, caves and
  colors retain their geometry when loaded into the BattleSpades world.
- Validate malformed maps before replacing the running world, support explicit
  format overrides, and keep collision, bot navigation and map transfers aligned.
- Browse accessible Workshop maps in the server GUI with search, sorting,
  filters, previews and details. Import maps into the configured server maps
  directory; Steam-only content still requires the appropriate Steam access.
- Keep synchronized map downloads and map changes consistent for joining clients.

## Hosting and discovery

- Enable A2S LAN discovery by default and answer the Steam LAN browser queries.
- Expose network and offline hosting options in the GUI and command-line tools.
- Display the existing BattleSpades Classic mode as Classic+, distinguishing it
  from original Classic 0.75/0.76 servers in compatible browsers.
- Improve map-load responsiveness, replay/documentation support and bot handling
  of imported map geometry.

## Release and update compatibility

- Ship portable Windows, Linux and macOS archives for x86_64 and ARM64, including
  the graphical server host, tutorial and map-creator launchers.
- Keep Protocol 168 and the existing component updater format. Server update
  staging preserves configuration, bans, fleet configuration and logs.
- Isolate the shot-diagnostic test from setup log entries so the full release
  suite does not fail when INFO logging was enabled by an earlier test.

These are BattleSpades/retail Protocol 168 servers. Importing an original VXL
map does not turn the server into a 0.75/0.76 protocol endpoint or execute the
original map's Python scripts.

Matching client: https://github.com/KikoTs/BattleSpadesClient/releases/tag/v0.3.0-beta.1

Every archive must pass the complete source suite and packaged-launcher checks
on its platform before publication. SHA256SUMS.txt covers all six archives.
Unsigned builds; verify downloads with SHA256SUMS.txt.
