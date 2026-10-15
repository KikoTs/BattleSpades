BattleSpades Server 0.3.0-beta.2 adds the community and Hardcore playlists and
hardens their player admission and round lifecycle.

## Game modes

- Thirteen curated unofficial modes, themed arenas, mode-aware bots, rules
  messages and objective markers. See docs/UNOFFICIAL_MODES.md for the playlist.
- Ten Hardcore variants of the standard modes, with five class roles, validated
  equipment, shared movement rules, body armour and controlled medpack healing.
  Hardcore requires the updated native client; ordinary and community modes
  retain their existing retail compatibility.
- Late race joiners can complete checkpoints and finish without a missing
  penalty entry interrupting the round.
- Hot Potato grants late joiners three lives and clears a departing carrier's
  pickup and life tracking immediately.
- Restarting a community match clears previous participants, eliminations,
  results and timers before waiting for the next ready roster.
- Non-Zombie Hardcore variants replace forged zombie selections with a legal
  human role at the join boundary.
- Hardcore bot strategies retain the base mode's flag, VIP and other objective
  policies. The standalone Steam sidecar accepts all community and Hardcore codes.

Thanks to DmitrySenpai for game mode development and fixes.

The following map and hosting improvements from Beta 0.3 remain included.

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

Matching client: https://github.com/KikoTs/BattleSpadesClient/releases/tag/v0.3.0-beta.4

Every archive must pass the complete source suite and packaged-launcher checks
on its platform before publication. SHA256SUMS.txt covers all six archives.
Unsigned builds; verify downloads with SHA256SUMS.txt.
