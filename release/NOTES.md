BattleSpades Server 0.3.0-beta.3 is the companion server for BattleSpades Client
0.3.0-beta.5 and its expanded Create Match and offline play support.

- Includes all ten Hardcore variants and thirteen curated unofficial modes,
  their arenas, and mode-aware bots. The matching client now exposes these
  modes directly in Create Match.
- Fixes portable archives omitting the nested community arenas, objective
  metadata, and Hardcore/community presets. These modes now load their
  intended maps instead of silently falling back to a flat world.
- Retains the tested Beta.2 gameplay rules while fixing packaged mode content.
- Supports local matches without a login or available master server through
  --offline. The matching client bundles the server for offline hosting.
- Keeps Protocol 168, existing configurations, and component update support.

Thanks to DmitrySenpai for game mode development and fixes.

Matching client: https://github.com/KikoTs/BattleSpadesClient/releases/tag/v0.3.0-beta.5

Portable Windows, Linux, and macOS archives for x86_64 and ARM64 include the
server GUI, tutorial, and map-creator launchers. Each platform runs the full
source suite and packaged-launcher checks before publication.
Unsigned builds; verify downloads with SHA256SUMS.txt.
