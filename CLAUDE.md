# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Big Picture: Three-Repo Revival Project

This worktree is the **server** in a three-folder Ace of Spades 1.x revival ecosystem under `G:\AoSRevival\`. Future Claude instances must understand all three to operate effectively — sibling folders are *additional working directories* configured in this session and are read-authoritative for protocol/physics decisions:

| Folder | Role | Authority |
| --- | --- | --- |
| `BattleSpades/` (this repo) | New Python 3 + Cython **server** being built from scratch. Goal: support every packet/feature the original 1.x client expects, plus more. | The implementation |
| `../aoslib-reversed/` | 1:1 reverse-engineering of the original `aoslib`/`shared` Cython modules from the game. Contains the **packet/world/vxl/kv6 ground truth** — `shared.packet` (127/127), `aoslib.vxl` (61/61), `aoslib.world` (172/172), `aoslib.kv6` (28/28) all reference-matched. Tests in `aoslib-reversed/tests/` dual-run under Python 2.7 (32-bit, against the original `.pyd`) and Python 3 (against the restored `.pyx`). | Source of truth for serialization & physics behavior |
| `../aceofspades_nonsteam/` | Source-recovered standalone **client** (Python 2 + bundled runtime). Run with `.\python\python.exe run.py +connect 127.0.0.1:32887`. Has a `aceofspades_decompiled/` subfolder with **readable decompiled `.py` source** for many modules originally shipped as `.pyd` — useful when you need to read real client logic instead of guessing from the compiled binary. | Source of truth for what the client actually sends/expects |

**Critical knowledge:** the `shared` and `aoslib` packages in this repo are deliberate copies/derivatives of the matching directories in `aoslib-reversed`. When debugging a packet or physics behavior, the *first* place to look is `../aoslib-reversed/shared/packet.pyx`, `aoslib/world.pyx`, etc. The protocol spec is at `../aoslib-reversed/aosprotocol.1x.md` (1647 lines, packet IDs 0–127+). To validate against the real client compiled binary, use the `aosdump/` folder under `aoslib-reversed/` (Python 2.7 32-bit + original `.pyd` files).

## Architecture

### Runtime (entry points)

- [run_server.py](run_server.py) → boots `BattleSpadesServer` from [server/main.py](server/main.py).
- [config.toml](config.toml) → loaded via [server/config.py](server/config.py) into a `ServerConfig` dataclass. Note the dataclass defaults differ from `config.toml` in places (default port 32887 vs 27015; `default_map = "classicgen"` vs `"CityOfChicago"`) — `config.toml` overrides win.

### Three concurrent asyncio loops in `BattleSpadesServer.start()`

1. `_network_loop` — pumps `host.service(0)` at 60 Hz; ENet events are processed synchronously, but received packets are dispatched via `asyncio.create_task(_on_receive)`.
2. `_game_loop` — fires at `config.tick_rate` (default 60 Hz). Updates each player, A2S handler, and game mode `on_tick`.
3. `_world_update_loop` — broadcasts `WorldUpdate` snapshots at the tick rate.

ENet host uses `compress_with_range_coder()` and a single channel. `host.intercept` is wired to [server/a2s_query.py](server/a2s_query.py) so Steam server browser / LAN HELLO queries are answered out-of-band on the same UDP socket.

### Connection lifecycle (the handshake)

[server/connection.py](server/connection.py) is the heart of the join flow. The order is non-obvious and must match the real client:

1. ENet `CONNECT` → `Connection` created, no player yet.
2. Client sends **SteamSessionTicket (105)** — its `ticket` becomes `connection.steam_key`. **Every subsequent received packet is XOR-decrypted with this key** (`Connection.decrypt`). After this, `send_connection_data()` runs, which sends `InitialInfo` → map sync → `StateData` → `SkyboxData` → `ExistingPlayer` for each existing player.
3. Map sync is gated on receiving **MapDataValidation (60)** from the client (CRC handshake). Then `MapSyncStart` (prefix `0x32`) → many `MapSyncChunk` (prefix `0x31`) → `MapSyncEnd` (prefix `0x31`). The `prefix` byte before each compressed payload selects the framing, see below.
4. Client sends **NewPlayerConnection (15)** → `_on_new_player` allocates a `Player`, broadcasts `CreatePlayer (28)`, sends `SetHP` damage_type=2 as the spawn-HP signal, and notifies the game mode.
5. Pre-join packets (before player exists) are routed by `handle_pre_join_packet`; post-join packets go through [protocol/packet_handler.py](protocol/packet_handler.py) via `@register_handler(packet_id)` decorators.

### Packet framing & compression — the `prefix` byte

Outbound packets all flow through `Connection.send(data, reliable=True, prefix=0x30)`. The wire format is `[prefix_byte][lzf_compressed_payload]`. Important quirks:

- [server/util.py](server/util.py) `lzf_compress` / `lzf_decompress` is **not real LZF** — it's a chunking-only wrapper (32-byte chunks, length-prefix bytes). Real compression is delegated to ENet's range coder. Don't "fix" this without understanding why.
- `prefix` byte choices in the codebase: `0x30` (default, raw), `0x31` (LZF-compressed framing — used for `StateData`, `MapSyncChunk`, `MapSyncEnd`, `MapDataValidation`), `0x32` (`MapSyncStart`).
- Inbound: `data[0] == 0x31` triggers `lzf_decompress`; otherwise the prefix byte is just stripped. Then `decrypt(data)` XORs with the steam ticket if set.

### Packet definitions and runtime decoding

- **Cython-defined wire packets** live in [shared/packet.pyx](shared/packet.pyx) (mirrors `aoslib-reversed/shared/packet.pyx`, 127 classes). Each class has a `.id`, `.read(reader)`, `.generate()` returning a `ByteWriter`.
- [protocol/runtime_packets.py](protocol/runtime_packets.py) exists because the live client sometimes sends layouts that differ from the strict reversed schema (e.g. `ClientData`'s 15-byte minimum, fixed-point orientations). `decode_runtime_packet(packet_id, payload)` is tried first; on `None`, the strict `shared.packet` reader is used. Add new tolerant decoders here, not by hacking the Cython.
- [shared/bytes.pyx](shared/bytes.pyx) provides `ByteReader` / `ByteWriter` and is also a 1:1 restoration. **Endianness, fixed-point scaling (`/64.0` for positions, `/8192.0` for orientations), and signed-magnitude (sign bit in 0x8000) all matter** — see `_fromfixed` in [protocol/runtime_packets.py](protocol/runtime_packets.py).

### Cython modules — what's compiled vs imported

[setup.py](setup.py) builds these in-tree (`build_ext --inplace`):
- `shared.bytes`, `shared.glm`, `shared.packet`
- `aoslib.vxl`, `aoslib.kv6`, `aoslib.world`

The world physics engine (`aoslib.world.World`, `Player`, `Grenade`, etc.) is wrapped by [server/runtime_vxl.py](server/runtime_vxl.py) (`ServerVXL` adapts the z-axis convention) and exposed via [server/world_manager.py](server/world_manager.py). Server `Player` objects in [server/player.py](server/player.py) compose a `WorldPlayer` from `aoslib.world` for collisions/movement.

### Game logic layers

- [modes/](modes/) — `BaseMode` + `CTFMode`, `TDMMode`, `ArenaMode`. Selected via `config.default_mode` and `modes.get_mode_class()`. Hooks: `on_mode_start/end`, `on_tick`, `on_player_join`, etc.
- [commands/](commands/) — `/cmd` chat commands. `CommandHandler` routes; `admin.py` / `player.py` / `server_commands.py` star-imported in `commands/__init__.py` register handlers via `@register_command`.
- [server/combat_runtime.py](server/combat_runtime.py) — singleton `CombatSystem` accessed via `get_combat_system(server)`. Damage/hitscan/block placement live here; weapon profiles in [server/game_constants.py](server/game_constants.py).
- [server/game_constants.py](server/game_constants.py) — derives `WEAPON_PROFILES`, tool-id frozensets, team IDs, etc. from `shared.constants`. Anything that needs a constant from the original game should source from `shared.constants` (the reversed value), not redefine it.

### Team IDs — wire vs internal

Be careful: `internal_team_to_wire()` and `wire_team_to_internal()` in [server/connection.py](server/connection.py) translate between server-internal team IDs and what the wire format uses. `TEAM_SPECTATOR` and `TEAM_NEUTRAL` are non-playable — joining clients that request them are coerced to `DEFAULT_WIRE_TEAM` (`TEAM1`).

## Common Commands

**Two Python versions are on PATH** — use them explicitly:
- `py` → Python 3.12 (64-bit) — server, build, pytest, modern code.
- `py2` → Python 2.7.18 **32-bit** — required for talking to the original game's compiled `.pyd` modules under `../aceofspades_nonsteam/aoslib/` and `../aceofspades_nonsteam/shared/`. Test clients written against the real wire format must run under `py2`.

```bash
# Build the Cython extensions (required after editing any .pyx)
py setup.py build_ext --inplace
# or equivalently:
py scripts/build.py

# Run the server (default port 27015 from config.toml; client expects 32887 by default)
py run_server.py

# Run tests — note conftest.py ignores several test files; pass them explicitly when needed
py -m pytest tests/ -v
py -m pytest tests/test_combat.py -v       # explicit (would be ignored by glob otherwise)
py -m pytest tests/test_reversed_combat.py # the "_reversed_" tests are the active suite

# Run a Python 2 script against the real game .pyd files (e.g. headless test client)
py2 path/to/script.py
```

**Key Python-2 import gotcha:** `aoslib.network` (the game's ENet wrapper) cannot be imported standalone — at module init it transitively pulls in `aoslib.web` → `aoslib.scenes.frontend.serverInfo` → `aoslib.gui` → `aoslib.text` → `pyglet`. For headless tools, use `enet.pyd` (pyenet, available standalone) plus `shared.packet.pyd` / `shared.bytes.pyd` / `shared.lzf.pyd` from `../aceofspades_nonsteam/` directly.

`tests/conftest.py` collects-ignores `test_combat.py`, `test_movement_engine.py`, `test_packets.py`, `test_spawn_handshake.py`, `test_world_update.py` — the parallel `test_reversed_*.py` files are the active equivalents that run against the reversed `shared.packet`/`aoslib.world` implementations.

The local client to test against:
```powershell
cd G:\AoSRevival\aceofspades_nonsteam
.\python\python.exe run.py +debug +connect 127.0.0.1:32887
```

## Working with Packets

Adding or fixing a packet generally means:

1. Find its definition in `../aoslib-reversed/shared/packet.pyx` and its spec entry in `../aoslib-reversed/aosprotocol.1x.md`.
2. Mirror the change into [shared/packet.pyx](shared/packet.pyx) and rebuild.
3. If the live client sends a layout that the strict reader rejects, add a tolerant decoder in [protocol/runtime_packets.py](protocol/runtime_packets.py) returning a `@dataclass(slots=True)`.
4. Register/extend a handler with `@register_handler(packet_id)` in [protocol/packet_handler.py](protocol/packet_handler.py).
5. For verification against the original binary, use `aoslib-reversed/tests/test_packets.py` dual-runs (`py2` vs `py`) — see `aoslib-reversed/README.md`.

## Logging

`logs/log.txt` is appended on every run. `config.toml` `[logging] suppress_packets = [...]` hides high-frequency packet IDs (default: `2 WorldUpdate`, `4 ClientData`, `11 SetColor`, `56`, `57`) from debug output. The connection layer auto-formats every non-suppressed packet's hex + decoded fields, which is the primary debugging surface — read it before guessing.
