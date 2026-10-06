# Linux retail Steam sidecar

These systemd templates match the verified Hetzner deployment. They assume
an existing, separately supervised TDM server on local UDP 27015 and Linux
x86-64. They do not launch or restart the game server.

- Install `scripts/steam_linux_sidecar.py` and
  `scripts/check_steam_registration.py` in `/opt/battlespades-steam/`.
- Supply Valve's native `steamclient.so`, `libtier0_s.so` and `libvstdlib_s.so`
  under `/opt/battlespades-steam/runtime/`. Download from Valve and verify the
  package against its official manifest. Libraries are not bundled here.
- Create a system account `battlespades-steam` with no interactive login.
  The service's `StateDirectory` manages its writable state directory.
- Copy the service units into `/etc/systemd/system/`.
- Replace the documentation address in `retail-port.nft.example` with the
  host's actual public IPv4 and install it as
  `/etc/battlespades-steam/retail-port.nft`. Check it using `nft -c -f` first.
- Permit UDP 32888 in the host/provider firewall. For this same-host NAT
  arrangement, existing filtering must allow the translated UDP 27015 port.
- Run `systemctl daemon-reload`, then enable/start
  `battlespades-retail-port.service` and `battlespades-steam.service`.

Before deploying on a different host, check port availability, firewall/NAT
ownership, source game port, mode, region, architecture and available memory.
Do not load this example alongside an existing table of the same name.
Do not flush the host firewall or replace its existing ruleset.

See [the deployment record](../../docs/STEAM_VPS_2026-09-21.md) for the live
addresses, verification, limitations, status commands and narrow rollback.

## Several public IPv4 addresses on one host

The retail client connects to UDP 32887 regardless of the advertised game
port. Each separately joinable instance therefore needs a distinct IPv4.
Use a separate sidecar process and writable state directory per instance,
with `--bind-ip <locally-assigned-ipv4>` and its own `--source-port`/`--mode`.
The binding configures both Steam backend traffic and the query listener;
assign the address to the host before starting the service. The default
`0.0.0.0` retains the existing single-IP deployment behavior.

For every address, route UDP 32887 to the intended game's local port and
permit UDP 32888 queries. Provider-side NAT or tunnels also need matching
outbound source translation. An arbitrary public IP that is not locally
assigned cannot be passed as a binding address.

The optional binding's ABI call order and IPv4 encoding have unit coverage.
Multiple simultaneous public-IP registrations still require a deployment
check: verify each Steam endpoint, A2S response and retail join before
migrating players or retiring an old host.

## Registering the SteamID with the AoSPlay list

The sidecar writes its login state and the SteamID Valve assigned to
`/run/battlespades-steam/status.json` (`--status-file`). Point the game server
at it so its heartbeat registers that id:

```toml
[revival]
steam_sidecar_status = "/run/battlespades-steam/status.json"
```

or set `AOS_STEAM_SIDECAR_STATUS` in the server's environment. The heartbeat
only sends `steam_server_id` / `steam_game_port` while the sidecar reports a
logged-on id refreshed within the last three minutes. Clients use it to match
Valve's server list (which they can read through Steam even where aosplay.net
is blocked) to the AoSPlay listing.

## Steam relay hosting (joining through Steam instead of the IP)

Some providers block a server's address and the AoSPlay web services while
Steam itself stays reachable. With relay hosting enabled, the game server runs
`steam-host/battlespades-steam-host` (shipped in the Linux and Windows x64
bundles): it logs on to Steam as a game server and accepts players over
Steam's relay network, forwarding them to the game port. The BattleSpades
client tries this route first and falls back to the IP; Steam connects
directly when it can, so players who are not blocked lose nothing.

1. Give the server user Valve's Steam runtime. With SteamCMD installed as that
   user, `steamclient.so` is at `~/.steam/sdk64/steamclient.so`, which is
   where the helper looks. Otherwise set `runtime_dir` (or
   `AOS_STEAM_RUNTIME_DIR`) to the directory that holds it.
2. Enable it in the server's TOML (or set `AOS_STEAM_HOST=1`):

   ```toml
   [steam_host]
   enabled = true
   ```

3. Optional but recommended: a game server login token keeps the server's
   SteamID across restarts. Create one per running server at
   <https://steamcommunity.com/dev/managegameservers> for App ID 224540, save it
   alone on one line in a file only the server user can read, and point
   `token_file` (or `AOS_STEAM_GSLT_FILE`) at it. Without a token the helper
   logs on anonymously and gets a new SteamID on every start, which clients
   pick up from the next heartbeat.

On start the log shows `Steam relay host ready: app 224540, SteamID ...` and
`Steam relay network: ready`, one pair per application (224540 for owners of
Ace of Spades, 480 for everyone else). The ids reach players two ways: the
heartbeat sends `steam_host_id` / `steam_host_id_480` to the AoSPlay list, and
the server's A2S keywords carry `sdr=<id>` / `sdr480=<id>`, which the Steam
sidecar copies into Valve's server list for players who cannot reach AoSPlay.

A player who arrives this way is `steam:<SteamID64>` to bans, vote kicks and
password lockouts, never `127.0.0.1`. The helper is optional at run time: if it
is missing, crashes or Steam is down, the direct server keeps running and the
helper is retried every 15 seconds.

`battlespades-steam-host` also runs on its own, without the game server
controlling it, for testing:

```sh
./battlespades-steam-host --game-port 27015 --app-id 224540 --token-file /path/to/token
```
