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
