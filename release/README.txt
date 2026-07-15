BattleSpades 0.0.1 Alpha
========================

1. Extract the complete zip into a writable directory.
2. Run `BattleSpades.exe --check` on Windows or `./BattleSpades --check` on
   Linux/macOS.
3. Edit `config.toml`. Change the admin password from `changeme` before
   exposing the server publicly.
4. Run the same launcher without arguments.

The default game listener uses UDP port 27015. Allow that UDP port through the
host firewall and router when accepting players from outside the local network.

Runtime files
-------------

- `config.toml`: server, game, mode, bot, logging, and admin configuration.
- `maps/`: VXL maps available to `/map` and startup configuration.
- `prefabs/`: KV6 models required by classes and game modes.
- `plugins/`: optional trusted Python plugins.
- `logs/`: created on first normal server start.
- `bans.json`: created after the first persistent ban.

Plugins execute arbitrary Python code inside the server process. Install only
plugins whose source you trust.

macOS alpha builds are not signed or notarized and may trigger Gatekeeper. The
release page documents this limitation; no archive should be described as an
Apple-notarized application.

Support diagnostics
-------------------

Include the exact archive name, output of `--check`, operating system version,
CPU architecture, and relevant files from `logs/` when reporting startup bugs.
