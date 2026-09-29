# Retail fleet migration assessment — 2026-09-27

The user confirmed that the Hetzner TDM appeared successfully in the retail
game, then requested the other servers be listed. They subsequently chose
to abandon further Oracle setup and seek inexpensive replacement hosting
with several public IPv4 addresses. No replacement hosting was purchased.

## Current state

- Hetzner `204.168.157.43`: TDM, CTF and Zombie respond. Existing TDM Steam
  registration and retail port alias remain healthy. No remote changes to
  this host in this assessment. Some human players were present.
- Oracle TC `92.5.55.224`: UDP 27027 responds. External packets reach that
  port but not 32887/32888, as observed by a bounded ingress capture.
  Steam runtime and a disabled service were staged; the service was never
  started. Temporary firewalld forwarding rules had 180-second timeouts.
  No permanent retail firewall rules were added. Further rollout cancelled.
- Oracle DIA `92.5.114.145`: UDP 27029 does not respond; process/cgroup
  observations show significant memory throttling on the 498 MiB host.
  No changes made.
- Oracle USA `137.131.23.125`: TDM, Zombie and VIP respond. ARM64 requires
  different handling for the x86-64 native Steam runtime. No changes made.

These eight instances represent six distinct modes: TDM, CTF, Zombie, TC,
DIA and VIP, with regional duplicates of TDM and Zombie. Consolidating into
Europe would remove the existing US location; that decision remains open.

## Provider comparison

Verified on official public pages/configurator on 2026-09-27. Prices are
estimates before VAT; checkout country, quota and availability still apply.

OVHcloud VPS-3 2027 in Germany (Limburg): 6 vCores, 12 GB RAM, 100 GB NVMe,
EUR 12.24/month with **No commitment** selected. Headline EUR 10.40/month
requires EUR 124.80 annual upfront payment. Additional IPv4 is advertised
at EUR 1.99/address/month, up to 16 additional addresses per VPS; one IPv4
is included. Public configurator was inspected without submitting an order.

| Total public IPv4s | Additional addresses | Monthly server + IP estimate |
| --- | --- | --- |
| 6 | 5 | EUR 22.19 |
| 8 | 7 | EUR 26.17 |
| 16 | 15 | EUR 42.09 |

These are address capacity estimates, not guarantees of CPU capacity for
that many busy games. Test representative maps, bots and tick performance.

Sources:
- https://www.ovhcloud.com/en-ie/vps/
- https://www.ovhcloud.com/en-ie/vps/configurator/
- https://www.ovhcloud.com/en-ie/vps/options/
- https://www.ovhcloud.com/en/vps/vps-ip/

Keeping the existing Hetzner Cloud VM is an alternative: additional
Floating IPv4s cost EUR 3/month each, so seven add EUR 21/month on top of
the existing VM bill. Primary IPs are not a substitute for multiple
simultaneous IPv4 addresses on one Cloud VM.

- https://docs.hetzner.com/cloud/floating-ips/overview/
- https://docs.hetzner.com/cloud/servers/primary-ips/faq/

## Proposed migration sequence

1. Resolve budget, number of listings and whether US presence is required.
2. Provision an x86-64 Linux target and initially two public IPv4s.
3. Copy current game configuration/maps and build or deploy matching
   x86-64 binaries; do not copy ARM64 executables from the USA host.
4. Run each game on its own internal port. Bind a separate Steam sidecar
   process/state directory to each locally assigned IPv4; map that IP's
   UDP 32887 to its game and expose UDP 32888 for queries.
5. Validate two independent Steam registrations, external A2S replies,
   actual retail joins and representative load before expanding IP count.
6. Migrate the remaining games. Retire old services only after validation.

The local sidecar now accepts `--bind-ip`. Unit coverage verifies the
Steam SDK binding call precedes local-user creation and uses the expected
IPv4 integer encoding; the focused suite passes 35 tests. This change is
not deployed on the existing Hetzner service. Simultaneous public-IP
registration remains an integration check for the new target.

Operational evidence is local in `tmp/steam-expansion-20260927/`.
Credentials are not included in this record.
