# Local hosting, offline mode and server launch options

BattleSpades can host a game without a master server, account service or Steam
relay. Players connect directly to its UDP game port. The master provides public
discovery and optional account/statistics services; it does not run the match.

## Start without a master

For the console executable:

```powershell
.\BattleSpades.exe --offline
.\BattleSpades.exe --offline --config .\lan.toml --port 32887
.\BattleSpades.exe --check --offline --config .\lan.toml
```

From source, replace `BattleSpades.exe` with `python run_server.py`. For the
desktop host, use `BattleSpadesServer.exe --offline` or
`python run_server_gui.py --offline`, then start the server from its window.
The desktop host forwards the option every time it starts/restarts its child.

The editor server accepts the same network options:
`BattleSpadesMapCreator.exe --offline` or `python run_map_creator.py --offline`.
Its project remains locally editable without a cloud owner identity. The game
client forwards its own `--offline` option to both Create Match and Map Creator
children. Install the compatible server bundle and game/editor assets first;
the offline client reports a missing server component without downloading it.

Use a normal installed map and prefab set; offline mode does not download
missing game content. On the same machine, connect the client directly to
`127.0.0.1:<port>`. On a LAN, use the hosting machine's LAN address and that port.
The listener and `[server] lan_discovery` setting stay enabled as configured,
so local A2S discovery works independently of public registration. A firewall
must allow the game port for other machines to connect.

`--offline` applies these changes in memory before configuration validation:

| Service | Offline behavior |
| --- | --- |
| Revival master | No registration, heartbeats, remote account validation or statistics uploads |
| Required identity | Disabled; clients can join using local profile names without a master-issued join ticket |
| Official/ranked bridge | Disabled; local play does not grant authenticated public account statistics |
| Legacy Steam master | Disabled, including the requirement for successful registration |
| Steam relay hosting | Both relay paths disabled, including an inherited `AOS_STEAM_HOST=1` |
| Automatic update notices | Disabled |
| Desktop networking helpers | No public-IP lookup or master ping; automatic/manual port forwarding and update checks disabled |

External-service sections are replaced with disabled defaults for this process,
so obsolete master URLs or missing Steam runtimes cannot prevent offline play.
Other game settings, direct connection passwords, bans, local logs, bots and maps
still apply. `config.toml` and environment variables are not rewritten.

A client that submits a master join ticket while the server is offline is
rejected without contacting the master. Select the client's offline profile
and connect by address instead. Offline names are local identities; they do not
prove ownership of an online account. Local match behavior and per-match
progress still work, while online account authentication and synchronization
are unavailable. Explicit desktop utilities such as opening a website or the
public Workshop still require internet access; use installed maps when offline.

## Select another master

```powershell
.\BattleSpades.exe --master-url https://master.community.example
.\BattleSpades.exe --master-url http://127.0.0.1:8000 --check
.\BattleSpadesServer.exe --master-url https://master.community.example
```

Use an **origin**, with an optional port and trailing slash. API paths, query
strings, fragments, credentials in URLs, invalid ports and plain HTTP to
non-loopback machines are rejected before startup. HTTP is permitted only for
the exact hosts `localhost`, `127.0.0.1` and `[::1]`; use HTTPS for a master hosted
on another LAN machine. The server appends its `/api/master/...` routes itself.

Master selection precedence is `--master-url`, then `AOS_MASTER_URL`, then
`[revival] base_url` in the configuration. The CLI value is applied before TOML
validation, so it can replace an obsolete configuration URL. Supply that
deployment's own `AOS_MASTER_WRITE_TOKEN` when enabling registration and account
services. Changing the URL does not create accounts or copy credentials,
statistics or permissions between masters. See [MASTER_SERVER.md](MASTER_SERVER.md)
for deploying a compatible master and relay services.

`--master-url` changes the Revival API service and the desktop host's master
reachability probe. It does not change the separate update-manifest URL.
`--offline` takes precedence if both options are supplied: no master request is
made. Startup options are not saved to the configuration file.

## Server options

`BattleSpades.exe --help` lists the executable's accepted arguments.

| Option | Purpose |
| --- | --- |
| `--config PATH` | Use a TOML file for this process; relative asset paths remain rooted at the server installation |
| `--port PORT` | Override the UDP game port for this process |
| `--offline` | Disable the external hosting services listed above |
| `--master-url ORIGIN` | Select the Revival master without editing TOML |
| `--version` | Print the server version and exit |
| `--check` | Validate configuration, assets, native modules and worker startup; honors the offline/master overrides |
| `--fleet PATH` | Start enabled instances from a fleet TOML manifest |
| `--update` | Download and stage a server update; does not replace the running installation |
| `--workshop-download ID_OR_URL ...` | Download public maps into `world.maps_path`, then exit |
| `--steam-p2p` | Host through the retail Steam relay helper |
| `--steam-p2p-bridge PATH` | Select the relay helper executable |
| `--steam-p2p-port 0..999` | Select the helper's Steam virtual port; default 168 |
| `--control-stdin` | Allow a supervising launcher to send `shutdown` or close stdin for graceful shutdown |
| `--status-file PATH` | Write state snapshots for a supervising launcher |

Offline mode rejects `--update`, `--workshop-download` and `--steam-p2p` rather
than silently enabling an online service. The fleet supervisor rejects
`--offline` and `--master-url`; configure each fleet instance's external-service
sections and master origin in its own TOML instead. If using configuration-only
offline hosting, also remove any `AOS_STEAM_HOST=1` environment override. The
dedicated `--offline` switch is the stronger option for a single host because it
also blocks ticket-triggered master requests.

The desktop executable accepts `--offline`, `--master-url`, `--version`,
`--self-check` and `--help`. Other hosting settings are selected in its window.
These flags concern the dedicated server; client launch options and quick-join
URLs belong to the client documentation.

## Verification

`tests/test_offline_launch.py` covers URL validation, launch/check routing,
in-memory overrides, environment-variable precedence, refused offline service
requests and desktop command forwarding. It also starts a real server with an
unreachable local master, required identity and Steam hosting configured, waits
for readiness, obtains a localhost UDP A2S reply, and shuts down cleanly.
