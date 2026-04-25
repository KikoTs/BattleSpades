# Test Harness Plan — Headless Bot Client + CLI Driver

> **Why this exists:** the developer is the bottleneck. Every "fire up the game, click connect, watch what happens" iteration costs a minute of human time. This plan replaces that loop with `py harness.py --scenario X` — one command, structured output, exit code.
>
> **The constraint:** for wire-compatibility, the bot client must run under `py2` (Python 2.7 32-bit) and use the **original** game's compiled `.pyd` modules from `../aceofspades_nonsteam/`, never our reversed `.pyx`. If our reversed code matches the originals, the bot succeeds. If they diverge, the bot fails *exactly the way a real client would*.

---

## 1. Architecture at a glance

```
┌────────────────────────────────────────────────────────────────────────┐
│  py harness.py --scenario spawn_walk --timeout 30                      │
│  (Python 3, this repo)                                                 │
│   ├─ build cython if stale                                             │
│   ├─ start server:  py run_server.py  (subprocess, log → tmp file)     │
│   ├─ wait for port 32887 to listen                                     │
│   ├─ start bot:     py2 testbot/run.py --scenario spawn_walk           │
│   │                  (subprocess, log → tmp file, JSON event stream)   │
│   ├─ wait for bot exit (or timeout)                                    │
│   ├─ stop server (SIGINT, then SIGKILL fallback)                       │
│   ├─ collect: server log, bot log, bot JSON events                     │
│   ├─ run scenario assertions                                           │
│   └─ exit 0 / 1   +  pretty-print failure summary                      │
└────────────────────────────────────────────────────────────────────────┘
                          │
              localhost:32887 ENet UDP
                          │
        ┌─────────────────┴─────────────────┐
        │                                   │
   BattleSpades                       testbot/run.py
   server (this repo)                 (Python 2.7 32-bit)
   our Cython aoslib/shared           imports:
   listening on :32887                  enet.pyd       (../aceofspades_nonsteam)
                                        shared.packet.pyd (original wire format)
                                        shared.bytes.pyd
                                        shared.lzf.pyd
                                        shared.glm.pyd
```

**Crucial property:** the bot uses *the game's own packet definitions*. If our server emits a malformed `InitialInfo`, the bot's `shared.packet.InitialInfo.read()` raises — same failure mode as the live client. The bot is a strict conformance checker.

---

## 2. The bot client (`testbot/`)

A new top-level folder in this repo, **only ever run under `py2`**.

```
testbot/
├── run.py              # entry: argparse, scenario dispatch, JSON event log
├── client.py           # ENet client + handshake + decrypt/decompress + packet dispatch
├── packet_io.py        # thin wrapper: encode/decode using shared.packet, prefix bytes, lzf
├── scenarios/
│   ├── __init__.py     # registry
│   ├── connect_only.py # connect → InitialInfo → disconnect
│   ├── full_handshake.py
│   ├── spawn_walk.py
│   ├── spawn_chat.py
│   ├── spawn_shoot.py
│   ├── spawn_build.py
│   ├── reconnect.py
│   └── multi_bot.py    # N bots in one process
└── conftest_paths.py   # sys.path setup pointing at ../aceofspades_nonsteam/
```

### 2.1 What `client.py` must do

It mirrors the receive-side of [server/connection.py](../server/connection.py), but inverted:

1. **Connect** to `localhost:32887` via `enet.Host` + `peer.connect()` with protocol-version data byte. Match the client's `compress_with_range_coder()`.
2. **Send `SteamSessionTicket(105)`** — for offline/local tests, an empty ticket is fine; the steam_key XOR layer can be configured off (server already handles missing key). Optionally, support a captured real ticket loaded from disk for ticket-validation testing later.
3. **Receive loop** — strip prefix byte; if `0x31` → `lzf_decompress`; XOR-decrypt with steam_key if set; dispatch by `data[0]` packet ID.
4. **Inbound packet dispatch** — for each packet ID we know about, parse with the matching `shared.packet.<Class>` and store in a per-bot state struct (`map_received`, `state_data`, `existing_players`, `our_player_id`, ...). Emit a JSON event per inbound packet to stdout (one line per event).
5. **`MapDataValidation` reply** — required to unblock map sync. Parrot the CRC the server sends, or send `0` and watch the warning.
6. **`MapSyncStart`/`MapSyncChunk`/`MapSyncEnd`** — collect chunks into a buffer; verify length against `MapSyncStart.size`; compare optional `crc32` against expected.
7. **`NewPlayerConnection(15)`** — send with `name`, `team`, `class_id` from scenario config.
8. **`ClockSync(0)` reply** — every received `ClockSync`, echo back so server doesn't time us out.
9. **`ClientData(4)` ticking** — once spawned, scenarios can drive an input loop at 60 Hz that emits ClientData packets with desired input flags / orientation.
10. **Clean disconnect** — `peer.disconnect()` and drain.

### 2.2 What `packet_io.py` does

```python
# Encoding (outbound):
def encode(packet, prefix=0x30):
    body = bytes(packet.generate())          # shared.packet writes header byte itself
    framed = lzf_chunk(body)                 # match server util.lzf_compress chunking
    return chr(prefix) + framed

# Decoding (inbound):
def decode(data, steam_key=None):
    prefix = ord(data[0])
    body = data[1:]
    if prefix == 0x31:
        body = lzf_decompress(body)          # shared.lzf.decompress
    if steam_key:
        body = xor(body, steam_key)
    pid = ord(body[0])
    cls = PACKET_CLASSES.get(pid)
    if cls is None: return ('unknown', pid, body)
    inst = cls()
    inst.read(shared.bytes.ByteReader(body[1:]))
    return (cls.__name__, pid, inst)
```

### 2.3 JSON event stream

Every meaningful event the bot observes prints **one JSON line to stdout**:

```json
{"t": 0.014, "evt": "connected", "peer": "127.0.0.1:32887"}
{"t": 0.020, "evt": "sent", "name": "SteamSessionTicket", "id": 105, "len": 1}
{"t": 0.045, "evt": "recv", "name": "InitialInfo", "id": 114, "len": 612, "fields": {"server_name": "...", "map_name": "London", "checksum": 592649088}}
{"t": 0.046, "evt": "recv", "name": "MapSyncStart", "id": 55, "fields": {"size": 524288}}
{"t": 0.812, "evt": "spawned", "player_id": 0, "x": 64.0, "y": 256.0, "z": 60.0}
{"t": 5.000, "evt": "scenario_done", "result": "ok"}
{"t": 5.001, "evt": "exit", "code": 0}
```

This makes assertions trivial in the harness — grep + jq, or `json.loads` per line. **Keep field truncation generous; we want to see what the server sent, not a hash of it.**

---

## 3. The harness (`harness.py`)

Single Python 3 script in repo root.

### 3.1 CLI

```
py harness.py --scenario <name> [options]

  --scenario NAME       required; one of the registered scenarios
  --port PORT           server port (default 32887)
  --map NAME            override config map for this run
  --mode NAME           override config mode for this run
  --bots N              N=1 default; multi_bot scenario can use more
  --timeout SECS        kill after this many seconds (default 30)
  --keep-logs           don't delete tmp logs after run
  --no-build            skip cython build (default: build if .pyx newer than .pyd)
  --server-log LEVEL    DEBUG/INFO; passed via env to run_server.py
  --bisect              if scenario fails, re-run with finer logging
  --record PATH         dump full bot JSON event stream to PATH
  --replay PATH         load a recorded event stream and assert subset matches
```

### 3.2 Lifecycle

1. **Build check** — stat each `*.pyx` vs its `.pyd`/`.cp*-win_amd64.pyd`; rebuild if any source newer. Skip if `--no-build`.
2. **Free-port check** — confirm `:32887` not in use; if it is, fail loudly (don't kill the user's other process).
3. **Spawn server** with `subprocess.Popen([sys.executable, 'run_server.py'], stdout=PIPE, stderr=STDOUT, env={...})`. Stream output to `tmp/server-<run>.log` and to memory ring buffer.
4. **Wait-for-listen** — poll `socket.connect(('127.0.0.1', 32887))` UDP-style (or just sleep 1s — UDP doesn't refuse). Better: wait for the server log line "Server started:".
5. **Spawn bot(s)** with `subprocess.Popen(['py2', 'testbot/run.py', '--scenario', name, ...])`, stdout = JSONL events. One subprocess per bot (`--bots N` spawns N).
6. **Drain & assert** — read bot JSON events as they arrive (line-buffered). Apply scenario assertions live; on failure, capture a snapshot.
7. **Tear down** — bot exits naturally → SIGINT server → wait 3s → SIGKILL if needed.
8. **Report** — print colored pass/fail summary; on fail, dump last 50 lines of server log + last 20 bot events.

### 3.3 Scenario assertions

Each scenario file declares its expected events (or a tolerant predicate). Example:

```python
# testbot/scenarios/full_handshake.py
NAME = "full_handshake"
TIMEOUT = 15

def script(client):
    """Run inside py2."""
    client.connect()
    client.send_steam_ticket(b"")
    client.expect("InitialInfo", timeout=5)
    client.expect("MapSyncEnd", timeout=10)
    client.send_map_data_validation(crc=client.last_initial_info.checksum)
    client.expect("StateData")
    client.expect("ExistingPlayer", optional=True)
    client.send_new_player_connection(name="bot0", team=0, class_id=0)
    client.expect("CreatePlayer")
    client.expect("SetHP")
    client.idle(2.0)               # tick ClientData/ClockSync
    client.disconnect()

# in harness, asserted automatically: TIMEOUT not exceeded, no exception, exit 0
```

### 3.4 Why subprocess instead of in-process

Because the bot is `py2` and the harness is `py3` — they cannot share an interpreter. The JSON event stream over stdout is the IPC channel. This is also nice isolation: a bot crash can't take down the harness.

---

## 4. Initial scenario library (build in this order)

Each shippable as a self-contained file in `testbot/scenarios/`:

| # | Scenario | What it proves | Acceptance |
| --- | --- | --- | --- |
| 1 | `connect_only` | ENet handshake works; SteamSessionTicket parsed | server logs `New connection`, bot disconnects clean |
| 2 | `initial_info` | Server emits valid `InitialInfo` | bot parses without exception, all required fields present |
| 3 | `map_sync` | Map transfer round-trips | chunks total == declared size, no decompression error |
| 4 | `state_data` | StateData layout sane | bot parses, mode_type matches `--mode` flag |
| 5 | `full_handshake` | Whole join sequence | bot reaches "spawned" event ≤10s |
| 6 | `idle_30s` | No spurious disconnects | bot stays connected 30s, ClockSync responses received |
| 7 | `spawn_walk` | ClientData ticking + WorldUpdate | bot sends 60 ClientData/s, server echoes pos in WorldUpdate |
| 8 | `spawn_chat` | Chat broadcast | bot sends ChatMessage, sees its own message echoed |
| 9 | `spawn_shoot` | ShootPacket validation | bot fires, observes ShootPacket broadcast (or rejection log) |
| 10 | `spawn_build` | BlockBuild round-trip | bot places block, server broadcasts BlockBuild |
| 11 | `reconnect` | Slot reuse | bot disconnects + reconnects, gets same/fresh player_id, no zombies |
| 12 | `multi_bot` (2) | Two clients see each other | both bots in `existing_players`, world updates carry both |
| 13 | `team_change` | ChangeTeam → kill+respawn | bot sends ChangeTeam, server kills + respawns on new side |
| 14 | `grenade_throw` | UseOrientedItem grenade | (once grenades implemented) bot throws, sees explosion entity |
| 15 | `place_mg` / `place_c4` / etc. | Each placeable | (once implemented) one scenario per entity type |

Scenarios 1–6 should pass *today* if the current handshake actually works end-to-end. Anything red there is our first list of bugs.

---

## 5. Iteration loop (the actual workflow)

```
┌───────────────────────────────────────────────────┐
│  edit server code in this repo                    │
│  (e.g. fix InitialInfo to stop hardcoding the     │
│   map filename)                                   │
└───────────────────────┬───────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────┐
│  py harness.py --scenario initial_info            │
│  → exit 0 = green, exit 1 = red + diagnostics     │
└───────────────────────┬───────────────────────────┘
                        │
                        ▼
                 green ─┴─ red ──► read failure summary, fix, repeat
                  │
                  ▼
        run regression batch: harness.py --all
        (loops every scenario, reports matrix)
```

When things get weird, useful escape hatches:

- `harness.py --scenario X --keep-logs --server-log DEBUG` → full server logs preserved.
- `harness.py --scenario X --record runs/2026-04-25-bug.jsonl` → save the bot's view forever.
- `harness.py --scenario X --replay runs/golden/full_handshake.jsonl` → assert nothing regressed against a golden capture.
- For 1:1 wire conformance: capture packets between the **real** game client and a real ace.py server (or a recorded session) into `golden/`, then `--replay` against ours.

---

## 6. Build / port concerns

- **Build cache:** Cython rebuilds are slow. Harness mtimes `*.pyx` vs `*.pyd`; only rebuilds what's stale. `--no-build` skips entirely.
- **Port collision:** If another server already holds `:32887`, fail with a clear error. Optional `--port 0` would pick a free port and pass it to both sides via `--port` flag (server config gains a CLI override — small change to [run_server.py](../run_server.py)).
- **Windows signal handling:** [run_server.py](../run_server.py) already has the `add_signal_handler` Windows fallback. Harness should send `CTRL_BREAK_EVENT` on Windows, `SIGINT` on POSIX.
- **Zombie processes:** harness uses a `try/finally` to ensure both subprocesses are reaped even on harness crash.

---

## 7. What this enables (the payoff)

Once this lands:

- Each fix in [Phase 0 of GOAL.md](GOAL.md#phase-0--stabilise-the-handshake--framing-fix-what-we-have) gets a scenario. Pass = shipped.
- New packet handlers added in Phase 1 are validated by a scenario before merge.
- Anti-cheat thresholds in Phase 2/3 can be tested by a "cheating bot" scenario (teleport, fast-fire) that we expect the server to reject.
- We can run the entire scenario matrix in CI on every commit. Regressions caught immediately.
- The agent (me) can iterate without you. You change the prompt; I change the code, run the harness, read the JSON, fix until green.

---

## 8. Concrete first deliverables (smallest viable cut)

So we don't disappear into framework-building. Ship these in order, each independently useful:

1. **`testbot/run.py` with one scenario `connect_only`** — ENet connect, send empty `SteamSessionTicket`, read 1 packet, disconnect. ~100 lines. Prints JSON.
2. **`harness.py` minimum** — start server, run `connect_only`, kill server. No build cache, no scenario library. ~80 lines.
3. **Scenarios 2–5** (`initial_info`, `map_sync`, `state_data`, `full_handshake`) — each adds one layer of the handshake.
4. **Scenarios 6–8** (`idle_30s`, `spawn_walk`, `spawn_chat`) — first scenarios that exercise input/output, not just receive.
5. **Build cache + port override + recording**.
6. **`multi_bot`** — only after a single bot is rock-solid.

After step 4, the iteration loop is real. Everything beyond that is polish on a working tool.

---

## 9. Non-goals (deliberately out of scope for now)

- Full `aoslib.network` import — the client's network layer drags in the entire scene graph. We use raw `enet.pyd` instead.
- Pyglet, audio, rendering — none of it runs headlessly anyway.
- Replaying real Steam tickets — for offline tests an empty ticket is fine; the auth path can be tested separately later.
- Performance benchmarking — a 32-bot stress scenario is later, after correctness.
- An MCP server wrapping all this — appealing, but a CLI is faster to write and equally driveable. Promote to MCP only if we end up running scenarios from inside model conversations dozens of times per session.
