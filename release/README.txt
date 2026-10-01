BattleSpades Portable Beta 0.2
=============================

Part of the open AoS Revival project: https://aosplay.net
Server source: https://github.com/KikoTs/BattleSpades
Client source: https://github.com/KikoTs/BattleSpadesClient

Beta 0.2 adds retail compatibility, terrain and map-transition fixes,
movement delivery improvements, recovered game modes and weapons, and
stability improvements. Behavior, configuration and verification details
are in the server repository's docs and release/NOTES.md. Join the community
at https://discord.gg/aosbb.

1. Extract the complete zip into a writable directory.
2. Run `BattleSpades.exe --check` on Windows or `./BattleSpades --check` on
   Linux/macOS.
3. Edit `config.toml`. Change the admin password from `changeme` before
   exposing the server publicly.
4. Run the same launcher without arguments.

Hosting with the window (easiest)
---------------------------------

Run `BattleSpadesServer.exe` on Windows or `./BattleSpadesServer` on
Linux/macOS (Linux: `BattleSpadesServer.desktop` starts it from a file manager).
Pick a name, mode and map, then press START. The window runs the normal
server from this folder with the same `config.toml`, and saves only settings
the server accepts.

- Host: name, mode, map rotation, players, bots, passwords, and "Import from
  Steam Workshop…", which copies your subscribed Workshop maps into `maps/`
  (Steam does not need to be running). Steam P2P is on by default on Windows: friends using the retail client with the Steam relay
  drop-in can join through Steam without port forwarding.
- Network: the game port (32887, the original game's port, is used if the
  file still has the 27015 sample), Steam server browser listing, optional
  automatic port forwarding (UPnP/NAT-PMP), a connection check that reports
  only what it can measure, a firewall helper, and router guides.
- Console: the live log, plus admin commands such as `say`, `kick`, `ban`,
  `map`, `restart` and `bots add 4`.
- Advanced: every config.toml setting, with the file's own comments as help.

Closing the window stops the server gracefully, and asks first if players
are online.

Per-session local hosting
-------------------------

All three launchers accept `--config <path>` without modifying the editable
`config.toml` beside the executable. Relative map, prefab, plugin, and state
paths in that temporary TOML still resolve from this extracted server folder.
The normal server also accepts `--port <1..65535>` as an in-memory override;
Tutorial and Map Creator support the same port override. This is the contract
used by the maintained retail client when it starts a hidden local server.

Retail tutorial
---------------

`BattleSpadesTutorial.exe` on Windows (or `./BattleSpadesTutorial` on
Linux/macOS) is the isolated Training.vxl tutorial server. Run its own
`--check` first, then launch it without arguments. It is deliberately separate
from `BattleSpades`: changing `config.toml` to mode `tut` cannot expose the
tutorial through the normal public server entrypoint.

Retail Map Creator
------------------

`BattleSpadesMapCreator.exe` on Windows (or `./BattleSpadesMapCreator` on
Linux/macOS) runs the isolated hosted UGC editor. It is not selectable through
`BattleSpades` or `config.toml`. Point `--retail-root` at a legally installed
Ace of Spades directory containing `ugc/maps` and `ugc/kv6`:

    BattleSpadesMapCreator.exe --check --retail-root C:\Games\AceOfSpades
    BattleSpadesMapCreator.exe --project MyMap --terrain grassland --target-mode ctf --retail-root C:\Games\AceOfSpades

To make an authored map appear in the stock Publish Map menu, pass the client
catalog root (the `hosted_ugc` directory, not its `maps` child):

    BattleSpadesMapCreator.exe --project MyMap --publish-root C:\Games\AceOfSpades\hosted_ugc --retail-root C:\Games\AceOfSpades

Projects are saved as sibling `.vxl`, `.txt`, and `.ugc` files under
`ugc-projects/` unless `--output-dir` or a project path is supplied.
`--publish-root` is mutually exclusive with `--output-dir`; it saves the same
triplet under `hosted_ugc/maps`, which is the retail authored-map catalog.
Supported terrains are desert, lunar, mountain, grassland, temple, urban,
marsh, snowy, and water. The retail baseplates and KV6 catalog are proprietary
client assets and are deliberately not included in this archive.

The default game listener uses UDP port 27015 (the server window switches a
fresh install to 32887). Allow the configured UDP port through the host
firewall and router when accepting players from outside the local network.
Optional Steam registry/A2S advertisement also needs the configured Steam
updater and query UDP ports (defaults 8766 and game port + 1). Valve retired
the legacy list endpoint used by the unmodified 2015 All/Community screen.

Runtime files
-------------

- `config.toml`: server, game, mode, bot, logging, and admin configuration.
- `BattleSpadesServer`: the desktop window for hosting (see above).
- `state/`: server-window settings and match results kept between restarts.
- `maps/`: VXL maps available to `/map` and startup configuration.
- `prefabs/`: KV6 models required by classes and game modes.
- `plugins/`: optional trusted Python plugins.
- `client_patches/`: retail-client compatibility hooks and installation notes.
- `steam-runtime/`: instructions for optional operator-supplied Steam files.
- `logs/`: created on first normal server start.
- `bans.json`: created after the first persistent ban.

Plugins execute arbitrary Python code inside the server process. Install only
plugins whose source you trust.

Seamless live `/map`, `/mode`, and voted-map transitions require the bundled
`client_patches/session_transition_patch.py` in each retail client. Follow
`client_patches/INSTALL.txt`, then restart that client once. The patch retains
the existing authenticated connection while the normal map loader runs.

macOS beta builds are not signed or notarized and may trigger Gatekeeper. The
release page documents this limitation; no archive should be described as an
Apple-notarized application.

Support diagnostics
-------------------

Include the exact archive name, output of `--check`, operating system version,
CPU architecture, and relevant files from `logs/` when reporting startup bugs.

License
-------

Copyright (c) 2026 Kiril Tsanov. BattleSpades is free software under the GNU
Affero General Public License v3.0 or later (LICENSE; summary in
LICENSING.md). There is NO WARRANTY. If you run a MODIFIED server that other
people connect to, AGPL section 13 requires you to offer those players its
complete source code, for example with a link in [server].motd in config.toml.
Releases up to v0.1.0-beta.1 were MIT-licensed and remain so. Third-party
components and Ace of Spades game content keep their own terms
(THIRD_PARTY_NOTICES.txt).
