<div align="center">

# BattleSpades

**A from-scratch, 1:1 server for _Ace of Spades 1.x_ (Battle Builders)**

Python 3 + Cython · ENet · server-authoritative · physics reverse-engineered from the original compiled client

</div>

---

BattleSpades is a clean-room reimplementation of the dedicated server for the classic
**Ace of Spades "Battle Builders" (0.x/1.x)** protocol. It talks to the **original,
unmodified game client** — the physics, netcode, and packet formats were reverse-engineered
from the compiled game and calibrated until the server simulates movement, shooting, and
block edits identically to what the client predicts locally.

The goal: a **complete, correct, hackable** server that anyone can run in one command, so the
classic game stays alive and playable — and so it's a solid base for ports to other languages.

> Works with the stock Steam client, the non-Steam client, and the open-source
> [aceofspades_revival](https://github.com/KikoTs/aceofspades_revival) client build.

## Table of contents

- [Status](#status)
- [Quick start](#quick-start)
- [Portable alpha releases](#portable-alpha-releases)
- [What works](#what-works)
- [Architecture](#architecture)
- [Building from source](#building-from-source)
- [ENet networking](#enet-networking)
- [Configuration](#configuration)
- [Running & hosting](#running--hosting)
- [Commands](#commands)
- [Testing & tooling](#testing--tooling)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Credits](#credits)
- [License](#license)

## Status

**Playable.** The netcode and core gameplay are reverse-engineered and verified against the
real client: movement is frame-accurate, and jumping, shooting, block build/break, grenades,
structure collapse, pickups, deaths/respawns, and bots all work and stay in sync with the
client's world. See [What works](#what-works) and the [Roadmap](#roadmap) for the details and
what's still on the list.

- **1,017** unit/regression tests pass (`py -3 -m pytest tests -q`)
- The executable 50-player capacity gate sustains ~60 Hz with sub-5 ms tick
  p99 on the current Windows/Python 3.12 baseline. See
  [`docs/RUNBOOK.md`](docs/RUNBOOK.md).
- Movement parity: mean client↔server position delta in the **millimetre** range over
  thousands of frames (`py scripts/replay_parity.py` — must stay `ALL PASS`)
- Physics ground truth and every measured constant live in
  [`docs/PROTOCOL.md`](docs/PROTOCOL.md)

## Quick start

For a dedicated server without a Python or compiler installation, use a
[portable alpha release](#portable-alpha-releases). The source workflow below
is intended for development and custom server builds.

You need **Python 3.10–3.12** (3.12 is the primary dev target) and a **C/C++ compiler**
(MSVC Build Tools on Windows, `gcc`/`clang` on Linux/macOS). The compiler builds the
Cython extensions plus the pinned, vendored pyenet/ENet transport. A virtual environment
(`venv`) is recommended so compiled dependencies stay isolated.

**One-liner** — clone, install deps, build the Cython core, and launch:

**Linux / macOS**
```bash
git clone https://github.com/KikoTs/BattleSpades.git && cd BattleSpades && ./scripts/install.sh && python run_server.py
```

**Windows (PowerShell)**
```powershell
git clone https://github.com/KikoTs/BattleSpades.git; cd BattleSpades; .\scripts\install.ps1; python run_server.py
```

Then point any Ace of Spades 1.x client at `your.ip:27015`. Edit `config.toml`
to select the startup map, mode, population, and administrative settings.

<details>
<summary>Manual steps (what the installer does)</summary>

```bash
# (recommended) create + activate an isolated environment first
python3.10 -m venv venv
source venv/bin/activate         # Linux/macOS
# .\venv\Scripts\Activate.ps1    # Windows (PowerShell)

pip install -r requirements.txt          # build/runtime Python dependencies
python setup.py build_ext --inplace      # compile the Cython extensions
python run_server.py                     # start the server on port 27015
```
</details>

## Portable alpha releases

`0.0.3-alpha.4` is packaged as six standalone server archives. Each archive
contains the launcher, Python/native runtime, editable `config.toml`, VXL maps,
KV6 prefabs, plugin directory, and license notices.

| Operating system | Archives |
|---|---|
| Windows | `windows-x86_64`, `windows-arm64` |
| Linux | `linux-x86_64`, `linux-arm64` |
| macOS | `macos-x86_64`, `macos-arm64` |

Download the archive matching the host from the repository's GitHub Releases
page, extract the complete directory, and validate it before opening a public
server:

```powershell
# Windows
.\BattleSpades.exe --check
.\BattleSpades.exe
```

```bash
# Linux / macOS
./BattleSpades --check
./BattleSpades
```

The reconstructed retail tutorial is intentionally a separate program. It is
not a selectable public mode and `run_server.py` cannot start it:

```powershell
# Source checkout
py -3.12 run_tutorial.py --check
py -3.12 run_tutorial.py

# Portable Windows release (use ./BattleSpadesTutorial on Linux/macOS)
.\BattleSpadesTutorial.exe --check
.\BattleSpadesTutorial.exe
```

That launcher locks the process to the original `Training.vxl`, tutorial mode
ID 10, twelve isolated training lanes, and the stock staged inventory flow:
the gallery grants/equips the pistol, then the climb grants the block tool and
spade. It disables bots, plugins, public registration, voting, rotation, and
competitive rules in memory without rewriting `config.toml`.

The reconstructed Map Creator is also an isolated program. It uses the stock
client's native six-tab Construct browser, five-item backpack, prefab ghosts,
Game Data object placer, validation HUD, palette, and raw-voxel editor tools,
while the dedicated process owns persistence:

```powershell
# Source checkout. Retail UGC art remains user-supplied and is never bundled.
py -3.12 run_map_creator.py --check --retail-root G:\AoSRevival\AceOfSpades_no_steam_new
py -3.12 run_map_creator.py --project MyMap --terrain grassland `
  --target-mode ctf --retail-root G:\AoSRevival\AceOfSpades_no_steam_new

# Save into the catalog used by Main Menu -> Map Creator -> Publish Map.
py -3.12 run_map_creator.py --project MyMap --terrain grassland `
  --target-mode ctf --retail-root G:\AoSRevival\aos-nonsteam\src `
  --publish-root G:\AoSRevival\aos-nonsteam\src\hosted_ugc

# Portable Windows release (use ./BattleSpadesMapCreator elsewhere)
.\BattleSpadesMapCreator.exe --project MyMap --terrain grassland `
  --target-mode ctf --retail-root C:\Games\AceOfSpades
```

New projects may use `desert`, `lunar`, `mountain`, `grassland`, `temple`,
`urban`, `marsh`, `snowy`, or `water`. Publish validation supports `tdm`,
`ctf`, `dem`, `mh`, `oc`, `tc`, `vip`, `zom`, and `dia`. The launcher creates
and continuously checkpoints a portable `.vxl`/`.txt`/`.ugc` triplet under
`ugc-projects/` by default. `--publish-root <client>/hosted_ugc` instead writes
that triplet into the exact `maps/` catalog enumerated by the stock Publish Map
menu; an optional same-stem `.png` is its Workshop preview. It requires the
operator's legally installed retail `ugc/maps` and `ugc/kv6` assets;
BattleSpades does not redistribute those proprietary baseplates or models.

No system Python or compiler is needed. Change the default admin password
`changeme` before exposing UDP port 27015. Verify the downloaded zip against
the release's `SHA256SUMS.txt`.

The first macOS alpha is unsigned and unnotarized, so Gatekeeper may require an
explicit operator override. The release does not claim Apple notarization.

## What works

| Area | State |
|---|---|
| **Movement / physics** | Frame-accurate server sim, oracle-calibrated to the compiled client (walk, sprint, crouch, wade, climb, gravity, friction) |
| **Jumping** | Full client↔server-synced jump (input edge-latched, reconciliation calibrated) |
| **Shooting** | All hit-scan guns (rifle, SMG, shotgun, sniper, pistol, MG) driven by the client's own per-weapon damage / fire-rate / clip tables; hit-scan from the reported aim, headshots, tracers at the right spot |
| **Blocks** | Build (BlockLine) + break (spade dig & bullet damage) — the **exact** aimed cell is removed on every client, block-colored debris |
| **Structure collapse** | Cut a structure off from the ground and the disconnected chunk falls (flood-fill detection + client fall animation) |
| **Grenades** | Thrown entity + fuse + bounce physics + blast damage (falloff + line-of-sight) + 3×3×3 block destruction |
| **Pickups** | Ammo / health crates, restock on spawn |
| **Combat lifecycle** | Damage, kills, kill feed, death → grave entity → timed respawn |
| **Game modes** | Team Deathmatch, CTF, Classic CTF, Arena, gangster VIP, and Zombie infection |
| **Map Creator** | Isolated retail-compatible UGC host with nine terrain baseplates, all 373 native catalog entries, prefabs, carving, palettes, 19 Game Data objects, mode validation, preview PNGs, and atomic project checkpoints |
| **Bots** | Isolated process worker with voxel navigation, fair perception/aim, class actions, and phase-aware CTF/Classic/VIP/Zombie/Arena roles |
| **Map transfer** | Full VXL streaming with correct CRC validation |
| **Admin / chat** | Player + admin command set, team management |

## Architecture

```
BattleSpades/
├── run_server.py       # entry point (async event loop + logging)
├── run_tutorial.py     # isolated retail tutorial entry point
├── run_map_creator.py  # isolated retail UGC editor entry point
├── config.toml         # all server settings
├── setup.py            # Cython build definition
│
├── aoslib/             # Cython core (compiled)
│   ├── world.pyx       #   movement physics, boxclipmove, grenade/entity sim
│   ├── vxl.pyx         #   byte-faithful VXL map format
│   └── kv6.pyx         #   voxel model format
├── shared/             # Cython wire layer (compiled)
│   ├── packet.pyx      #   every packet's read/write (the protocol)
│   ├── bytes.pyx       #   ByteReader/ByteWriter
│   └── glm.pyx         #   vector math
│
├── server/             # server logic (pure Python)
│   ├── main.py         #   60 Hz sim loop, WorldUpdate broadcast, entities, grenades
│   ├── player.py       #   per-player state, input buffering, reconciliation
│   ├── combat_runtime.py  # shooting, block damage, collapse
│   ├── world_manager.py   # map ops, block mutation, flood-fill
│   ├── connection.py   #   ENet peer + handshake
│   └── bots.py         #   bot AI
├── protocol/           # packet dispatch + runtime decoders
├── modes/              # tdm / ctf / classic_ctf / arena / vip / zombie
├── commands/           # player + admin commands
├── plugins/            # optional plugin hooks
├── maps/               # stock .vxl maps (shipped)
├── scripts/            # build + reverse-engineering / verification tooling
├── tests/              # pytest suite
└── docs/               # six focused server/operator/reversal references
```

**Design principles**

- **Server-authoritative** — the server re-simulates every player at a fixed **60 Hz**;
  the client predicts locally and is reconciled via per-player WorldUpdate self-rows.
- **Cython where it counts** — physics, map ops, and (de)serialization are compiled; game
  logic stays in readable Python.
- **Bounded hot paths** — asyncio/ENet work and gameplay packet drains have
  explicit budgets; WorldUpdate serialization is shared by equivalent clients.
- **Non-blocking logging** — formatting and I/O use a bounded background queue;
  slow sinks drop records instead of stalling the 60 Hz simulation.

## Building from source

The Cython extensions must be compiled before first run (and re-compiled after editing any
`.pyx`). **Stop the server before rebuilding** — a running server locks the compiled
`.pyd`/`.so` files.

```bash
python setup.py build_ext --inplace
# or the convenience wrapper:
python scripts/build.py
```

Requires a working C toolchain:

| Platform | Toolchain |
|---|---|
| **Windows** | [Visual Studio Build Tools](https://visualstudio.microsoft.com/downloads/) → "Desktop development with C++" (MSVC + Windows SDK) |
| **Debian/Ubuntu** | `sudo apt install build-essential python3-dev` |
| **Fedora/RHEL** | `sudo dnf install gcc python3-devel` |
| **macOS** | `xcode-select --install` |

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for cross-compilation notes (the project ships on
**Windows x64** and **Linux amd64/arm64**).

## ENet networking

BattleSpades uses **[pyenet](https://github.com/piqueserver/pyenet)** and
[ENet](http://enet.bespin.org/) for reliable UDP—the same transport family used by the
original game (protocol version 168, single channel, range-coder compression).

The pinned pyenet 1.3.17 wrapper and ENet 1.3.17 C sources live under
`vendor/pyenet/` and compile as the `enet` extension during `setup.py build_ext`.
This avoids PyPI's obsolete `Cython<1` dependency and missing macOS/ARM wheels,
so every release target builds the same source with the pinned Cython 3 toolchain.

## Configuration

Everything lives in [`config.toml`](config.toml). Highlights:

```toml
[server]
name = "BattleSpades Server"
port = 27015
max_players = 32
tick_rate = 60          # server simulation rate — keep at 60 (client-paired, physics-calibrated)

[game]
default_mode = "tdm"    # tdm | ctf | cctf | arena | vip | zombie
default_map = "ArcticBase"
respawn_time = 5.0
friendly_fire = false

[bots]
enabled = true
population_mode = "backfill"
fill_target = 12
max_bots = 12
reserve_human_slots = 2
difficulty = "mixed"    # casual | normal | hard | mixed
worker = "process"
perception_hz = 10
decision_hz = 8
path_requests_per_second = 24
main_thread_budget_ms = 0.75
seed = 0

[teams]
team1_name = "TEAM1_COLOR"   # string-table IDs the client localizes (renders "Blue"/"Green")
team2_name = "TEAM2_COLOR"

[admin]
password = "changeme"        # CHANGE THIS before hosting publicly
```

> Never save `config.toml` with a UTF-8 **BOM** (e.g. PowerShell `Set-Content -Encoding utf8`)
> — the BOM breaks `toml.load` and the server silently falls back to defaults.

For local tweaks that shouldn't be committed, use `config.local.toml` (gitignored).

## Running & hosting

```bash
python run_server.py
```

To host directly, forward **UDP `27015`** (or your configured game port) and set
a real `admin.password`. The ENet socket answers direct A2S/LAN queries. The
optional `[steam]` bridge registers app `224540` with Valve's current registry;
also forward its updater and query UDP ports (defaults `8766` and game port +
1). Valve retired the legacy UDP list used by the unmodified 2015 in-game
browser, so verify registration with the source-tree checker described in the
[runbook](docs/RUNBOOK.md#steam-master-server-listing).

## Commands

**Player** — `/help`, `/kill`, `/team <blue|green>`, `/score`, `/players`,
`/pm <player> <msg>`, `/me <action>`, `/stats`, `/ping`

**Admin** (after `/admin <password>`) — `/kick`, `/ban`, `/mute`, `/unmute`,
`/tp <player>`, `/god`, `/map <name>`, `/mode <tdm|ctf|cctf|zom|vip|mh|tc|dia|dem|oc|arena>`, `/restart`, `/say <msg>`,
`/fog <r> <g> <b>`, `/time`, `/balance`, `/bots status`,
`/bots fill <count>`, `/bots add <count> [team]`,
`/bots remove <count|name|all>`, `/bots difficulty <casual|normal|hard|mixed>`

## Testing & tooling

```bash
py -3 -m pytest tests/ -q       # unit/regression tests (currently 866 passing)
py scripts/replay_parity.py     # offline movement-parity check (must be ALL PASS)
```

The `scripts/` directory also holds the reverse-engineering rig used to build this server:
an in-game **physics oracle / console** (`game_console.py`, `auto_join.py`,
`oracle_experiments.py`) that drives the real client to extract ground-truth physics and
replay it through the Python engine. Details in [`docs/RUNBOOK.md`](docs/RUNBOOK.md).
Retail top-screen announcement packets, localization parameters, team-color
IDs, and the recovered template variables are documented in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Documentation

- [`docs/ADMIN_GUIDE.md`](docs/ADMIN_GUIDE.md): every config option, rule,
  command, and plugin hook.
- [`docs/GAMEPLAY.md`](docs/GAMEPLAY.md): modes, official map sets, gameplay
  invariants, and bots.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): service ownership and data flow.
- [`docs/PROTOCOL.md`](docs/PROTOCOL.md): packets, retail evidence, and the
  reverse-engineering workflow.
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md): build, operate, diagnose, soak, release.
- [`docs/HANDOFF.md`](docs/HANDOFF.md): current state, known gaps, and evidence.

## Roadmap

See [`docs/HANDOFF.md`](docs/HANDOFF.md) for the live backlog. In short:

- **Near term** — end-of-round scoreboard screen, per-player scoreboard column, HUD round
  timer; polish grenade/collapse visuals; reconnect-lifecycle hardening.
- **Content** — more maps, weapons, and classes; finish CTF/Arena scoring parity.
- **Long term** — the project is intentionally a clean, documented base so it can be **ported
  to other languages** (Go, Rust, …) if/when the community wants to carry it forward.

## Contributing

Contributions welcome — especially maps, game modes, and platform build reports. Please:

1. Keep `py -m pytest tests/ -q` and `py scripts/replay_parity.py` green.
2. Rebuild Cython (`python setup.py build_ext --inplace`) after editing any `.pyx`.
3. Read [`docs/RUNBOOK.md`](docs/RUNBOOK.md) and [`docs/PROTOCOL.md`](docs/PROTOCOL.md)
   before touching netcode or physics — those values are hard-won measurements.

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Credits

- Built for the **Ace of Spades revival** effort. Companion open-source client build:
  [KikoTs/aceofspades_revival](https://github.com/KikoTs/aceofspades_revival).
- _Ace of Spades_ was created by Ben Aksoy / Jagex. This is an independent
  server reimplementation for preservation and play; it does not ship the
  original client executable or proprietary game code. Portable server
  releases include the project's tracked VXL/KV6 gameplay content.
- Networking via [pyenet](https://github.com/piqueserver/pyenet) / [ENet](http://enet.bespin.org/).
- Community fixes: build & setup improvements from [@TylerJaacks](https://github.com/TylerJaacks).

## License

MIT — see [`LICENSE`](LICENSE).
