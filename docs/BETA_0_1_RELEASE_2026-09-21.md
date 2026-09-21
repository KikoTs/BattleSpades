# Beta 0.1 release and fleet rollout — 21 September 2026

`VERSION` is `0.1.0-beta.1`, the tag is `v0.1.0-beta.1` and players see
"BattleSpades Beta 0.1" in the join greeting. The client uses the same label
(`BattleSpadesClient 0.1.0-beta.1`). The release workflow accepts `v*-beta.*`
tags as well as the older `v*-alpha.*`.

## What is in it

- Bots that fight and move like players:
  [combat](BOT_COMBAT_REALISM_2026-09-21.md) and
  [locomotion](BOT_LOCOMOTION_2026-09-21.md).
- **Strict float evaluation on Linux and macOS.** `setup.py` built the movement
  model with `-ffast-math`. It is a bit-exact port of the retail engine that
  clients predict against; 309 retail parity cases failed on Linux CI by one
  unit in the last place. All 2040 pass with `-fno-fast-math -ffp-contract=off`.
- **A loaded map costs 50 MB instead of 235 MB.** Voxel colours were a Python
  dict (about 100 bytes a voxel). `aoslib.vxl._ColorTable` is a flat
  open-addressing table behind the same operations; output is byte-identical
  after 60,000 random edits on four retail maps. This is what had taken the two
  512 MB Frankfurt hosts offline: the process needed about 385 MB, swapped
  continuously and answered no queries.
- Tests that predated deliberate server rules were repaired (a class change is
  not a scoreboard death; queued mode events settle before `on_tick`).

## Release procedure

1. Set `VERSION`; keep `server/build_info.py` `DISPLAY_RELEASE` in step.
2. Push `main`, then an annotated tag `v<VERSION>`. `release.yml` builds and
   tests six archives and publishes a prerelease with `SHA256SUMS.txt`.
   Nothing is published unless every platform passes, so a failed tag can be
   deleted and pushed again.
3. Build the client with `scripts\build.ps1 -Profile Release -Native`, configure
   `AOS_BUNDLED_SERVER_ROOT` with the extracted Windows server archive and run
   the `package` target.

Verify Linux locally before tagging: a WSL Ubuntu with `build-essential` and
Python 3.12 runs the whole suite in about ten minutes.

## Fleet

| Host | Instances | Architecture |
| --- | --- | --- |
| Main EU `204.168.157.43` | TDM 27015, CTF 27017, Zombie 27021 | x86-64 |
| Frankfurt `92.5.114.145` | Diamond Mine 27029 | x86-64, 498 MiB |
| Frankfurt `92.5.55.224` | Territory Control 27027 | x86-64, 498 MiB |
| USA `137.131.23.125` | TDM 27015, Zombie 27021, VIP 27023 | arm64 |

Each host keeps releases under `/opt/battlespades/releases/`, with
`/opt/battlespades/current` pointing at the live one.

The three Oracle hosts run Oracle Linux 9 (glibc 2.34) while the archives are
built on Ubuntu 24.04 and need glibc 2.38. They carry a private glibc 2.39 in
`/opt/battlespades/glibc` (the units set `LD_LIBRARY_PATH` to it) and its own
loader at `/opt/bs-ld.so` (`/opt/bs-arm64-ld.so` on arm64). Loader and libc
must be the same version, so **the `BattleSpades` launcher's ELF interpreter
has to be repointed at that loader on every upgrade**, in place: the private
path is shorter than the original and is NUL padded over it, which keeps the
file's length and the PyInstaller archive appended to it intact. An unpatched
launcher fails at once with `undefined symbol: __tunable_is_initialized`.
Building the Linux archives against an older glibc would remove this step.

A rollout (operator scripts live outside the repository, beside the private
infrastructure handoff) does this per host: download the archive from the
GitHub release and check its SHA-256; unpack beside the old release; back up
and edit each instance's TOML (greeting, MOTD, `[revival]`); point the unit's
`ReadWritePaths` at the new release's `logs`; run `--check` on every edited
config with the new binary; switch `current`; restart one instance at a time
and require an A2S answer on loopback before the next. Any failure restores
the previous release, configs and units and restarts them.

Result on 21 September 2026: all four hosts and all eight servers moved from
`0.0.3-alpha.9` to `0.1.0-beta.1`, each behind its health gate, with no restore
needed on a live server. A protocol probe through the upgraded EU CTF server
completed handshake, map sync (CRC match), spectator join and live world
updates, and received the four greeting/MOTD chat lines. The Steam helper on
main EU missed one upstream probe during the TDM restart and resumed
advertising by itself. The Frankfurt servers, unresponsive before, answered
again at about 100 MiB each (385 MiB before).

Two hours later both Frankfurt hosts had starved again. It was not a leak: old
worlds are freed on rotation and memory returns to the same figure for the same
map. Memory depends on the map. Process memory after loading, without bots:
76-137 MiB for most maps, about 192 MiB for Frontier, Alcatraz and Classic, and
**319 MiB for MayanJungle**, because every filled underground voxel carries its
own colour entry and that map is tall and solid. Bots add 30-60 MiB. The
rotation had reached a heavy map on a service capped at `MemoryMax=320M`.
Those two hosts now rotate only maps that fit (Frontier and MayanJungle removed
from Diamond Mine; Alcatraz from Territory Control, which leaves it
CityOfChicago alone); the 8 GB and 16 GB hosts keep every map. Storing one
fill colour per column instead of one per voxel would remove the limit and is
the obvious next step; it changes re-serialized map bytes, so it needs the same
byte-for-byte comparison the colour table had.

Two things the first attempts taught: run `--check` from the service's working
directory (the AI child process enters its parent's directory, and the `opc`
home is closed to the `battlespades` user), and stop a 512 MB host's server
before checking, because a check loads a map and spawns a child.

Every instance now has:

```toml
join_greeting = "Welcome, {player}! {release} | Build (UTC): {build_date}"
motd = [
    "Official AoS Revival server: XP and ranks count here | https://aosplay.net",
    "New in Beta 0.1: bots that run, flank, take cover and fight like players.",
    "Play fair, build big. Source: github.com/KikoTs/BattleSpades",
]

[revival]
enabled = true
base_url = "https://www.aosplay.net"
public_host = "<host IPv4>"
server_id = "<host IPv4>:<port>"
region = "europe"        # "us-west" on the USA host
official = true
require_identity = false
```

## XP: one step is left to the network owner

aosplay.net already lists the six EU/USA servers as official, as
administrator-managed entries renewed by A2S probes. XP and statistics need
more than the listing: the game server must authenticate to the master with
its own `aos_srv_*` token, and an administrator must mark that registration
`stats_trusted`. Tokens are shown once by the admin console and were never
installed on the hosts; they cannot be recovered, and minting them any other
way means writing to the production database, which was not done.

Each instance is ready for its token. The units read
`EnvironmentFile=-/etc/battlespades/<instance>.env` (`config.env` on the
Frankfurt hosts), created `0640 root:battlespades` with a commented
placeholder. To finish, for every server in the admin console: issue its token,
enable "stats trusted", then on the host:

```sh
sudoedit /etc/battlespades/tdm.env      # AOS_MASTER_WRITE_TOKEN=aos_srv_...
sudo systemctl restart battlespades@tdm
journalctl -u battlespades@tdm -n 20 --no-pager   # no "AOS_MASTER_WRITE_TOKEN is not set"
```

Until then the server logs that warning once at start and plays normally.

## Steam browser: one row per public IP, and Oracle blocks the port

The retail client joins UDP 32887 whatever game port a Steam row advertises,
so one public IP can carry one joinable row. Main EU carries TDM (see
[the VPS record](STEAM_VPS_2026-09-21.md)); CTF and Zombie on the same address
would send players into TDM. Listing them needs additional IPv4 addresses on
that host.

The three Oracle hosts could each carry one row, but Oracle's VCN security
list drops UDP 32887 before it reaches the instance: a temporary NAT rule on
the USA host counted zero packets while queries were sent to it, and was
removed again. No Oracle credentials are in the handoff. For each Oracle
instance, add two stateless-or-stateful ingress rules in the subnet's security
list (source `0.0.0.0/0`, protocol UDP): destination port 32887 and
destination port 32888. After that the verified main-EU procedure applies, with
two differences: the instances sit behind 1:1 NAT, so the redirect must match
the port only (`udp dport 32887 redirect to :<game port>`, no `ip daddr`), and
firewalld needs `--add-port=32888/udp`. The USA host is arm64; Valve ships no
arm64 `steamclient.so`, so its helper would have to run under x86-64 user-mode
emulation, which is untested. Advertising a row before its port is reachable
would put a dead server in everyone's browser, so nothing was enabled there.

## Open issue: interpreter crash in one test ordering

`tests/test_surface_corridor.py` run as a whole crashes on Linux under WSL:
`SystemError: error return without exception set` or `unknown opcode` on
Python 3.12.3, a segmentation fault on 3.12.14, always inside pure-Python
`navigation_atlas` loops. Its physics cases alone pass (47), its four
pure-search cases alone pass, and the rest of the suite passes (8167). With
the extensions built under AddressSanitizer and `PYTHONMALLOC=malloc` all 51
pass with no report, so no out-of-bounds access by our native code was found;
the crash needs CPython's small-object allocator and that ordering. GitHub's
runners have not shown it. The same signature appears intermittently on the
Windows development machine, which also runs 3.12.3.
