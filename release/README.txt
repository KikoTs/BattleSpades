BattleSpades Portable Alpha
===========================

1. Extract the complete zip into a writable directory.
2. Run `BattleSpades.exe --check` on Windows or `./BattleSpades --check` on
   Linux/macOS.
3. Edit `config.toml`. Change the admin password from `changeme` before
   exposing the server publicly.
4. Run the same launcher without arguments.

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

The default game listener uses UDP port 27015. Allow that UDP port through the
host firewall and router when accepting players from outside the local network.
Optional Steam registry/A2S advertisement also needs the configured Steam
updater and query UDP ports (defaults 8766 and game port + 1). Valve retired
the legacy list endpoint used by the unmodified 2015 All/Community screen.

Runtime files
-------------

- `config.toml`: server, game, mode, bot, logging, and admin configuration.
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

macOS alpha builds are not signed or notarized and may trigger Gatekeeper. The
release page documents this limitation; no archive should be described as an
Apple-notarized application.

Support diagnostics
-------------------

Include the exact archive name, output of `--check`, operating system version,
CPU architecture, and relevant files from `logs/` when reporting startup bugs.
