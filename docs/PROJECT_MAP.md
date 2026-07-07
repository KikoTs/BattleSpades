# Project Map — `G:\AoSRevival\`

A single-page guide to which folder does what, so a future session (or a future you, returning from a break) doesn't have to rediscover the tree.

---

## The three folders that matter

```
G:\AoSRevival\
├── BattleSpades\           ◀── THE WORKING REPO. Server source. Edit here.
├── aceofspades_nonsteam\   ◀── Canonical game client + build pipeline. Read-only reference.
├── aoslib-reversed\        ◀── 1:1 reverse-engineering of original aoslib. Read-only reference.
│
├── archive\                ◀── Old / failed / duplicate experiments. Don't edit, don't delete.
└── (other folders)         ◀── Unrelated to BattleSpades work — leave alone.
```

### `BattleSpades\` — this repo

Single workpath. The Python 3 + Cython server we are actually building.

```
BattleSpades\
├── run_server.py           Entry point. py run_server.py
├── setup.py                Cython build. py setup.py build_ext --inplace
├── config.toml             Live server config (port 27015, mode, map…)
├── requirements.txt
│
├── server\                 Async server core (py3)
│   ├── main.py             BattleSpadesServer class, 3 asyncio loops
│   ├── connection.py       Per-peer ENet connection + handshake + decrypt
│   ├── player.py           Server-side Player + InputState
│   ├── world_manager.py    VXL map state + spawn anchors
│   ├── combat_runtime.py   Combat / hitscan / block actions
│   ├── a2s_query.py        Steam A2S server-browser intercept
│   ├── runtime_vxl.py      Wraps aoslib.vxl with z-axis adapter
│   └── …
│
├── shared\                 Reversed Cython packet/bytes/glm (.pyx)
├── aoslib\                 Reversed Cython vxl/world/kv6 (.pyx)
├── protocol\               Server-side packet handler registry + tolerant decoders
├── modes\                  Game modes (CTF stub, TDM stub, Arena stub)
├── commands\               /cmd chat-command system
├── plugins\                Plugin scaffolding
│
├── maps\                   *.vxl map files (gitignored)
├── tests\                  pytest suite (test_reversed_*.py is the active set)
├── scripts\                Build + investigation scripts
│   ├── build.py            Wraps `setup.py build_ext --inplace`
│   ├── probe_originals.py  py2 probe of original game's APIs
│   ├── probe_findings.json Latest probe output
│   └── physics_parity_report.py  User's physics-vs-client parity audit
├── docs\
│   ├── GOAL.md             1:1 feature surface + phased roadmap
│   ├── PROBE_FINDINGS.md   Verified original-game API contracts
│   ├── PROJECT_MAP.md      THIS FILE
│   └── TEST_HARNESS.md     Plan for headless py2 bot + py3 driver
├── CLAUDE.md               Guide for future Claude sessions
├── logs\                   Runtime logs (gitignored)
├── build\                  Cython build output (gitignored)
└── build_*.log             Latest build outputs (gitignored)
```

### `aceofspades_nonsteam\` — the canonical client

The original Ace of Spades 1.x client and its build pipeline. **Don't edit; don't move; treat as a read-only library.**

What we use it for:
- **Original game binaries**: `enet.pyd`, `shared/packet.pyd`, `shared/bytes.pyd`, `shared/lzf.pyd`, `shared/glm.pyd`. The headless test bot loads these as the wire-format ground truth (see [TEST_HARNESS.md](TEST_HARNESS.md)).
- **Original `shared/constants*.py`**: source of `PROTOCOL_VERSION`, team IDs, class definitions, weapon profiles, mode IDs, etc. Our `shared/constants.py` mirrors this.
- **Live client to point at our server**: `cd ..\aceofspades_nonsteam && .\python\python.exe run.py +debug +connect 127.0.0.1:32887`.
- **Decompiled-but-not-really client logic**: a few `.py` files in `aoslib/` (e.g. `web.py`, `tools.py`) are real source recovered from the original; useful when guessing at what the client expects.

What's **not** there anymore:
- An `aceofspades_decompiled\` subfolder used to live here. It was an AI-generated server-rewrite attempt mixed with proxy logging shims. Moved to [`archive\nonsteam-aceofspades_decompiled`](#archive). Its `proxy/*.pyd` were duplicates of `shared/*.pyd`.

### `aoslib-reversed\` — protocol + physics ground truth

Reverse engineering of the original `aoslib` Cython modules. **Read-only reference**; do not edit from this repo.

What we use it for:
- **Protocol spec**: [`aosprotocol.1x.md`](../../aoslib-reversed/aosprotocol.1x.md) — 1647 lines, all packet IDs, every field documented.
- **Wire-format reference**: `shared/packet.pyx` (127/127 classes restored), `shared/bytes.pyx`, `aoslib/vxl.pyx`, `aoslib/world.pyx`, `aoslib/kv6.pyx` — the full reversed Cython source.
- **Restoration notes**: `docs/world-restoration.md`, `vxl-restoration.md`, `kv6-restoration.md`.
- **Conformance test suite**: `tests/test_packets.py` etc. — dual-runs under `py2` (against original `.pyd`) and `py3` (against the restored `.pyx`).

The packet classes in our `BattleSpades/shared/packet.pyx` are descended from this reversed source.

---

## archive\

Everything moved here is intentionally kept (not deleted) because some of it has reusable logic. **Treat as cold storage.**

| Path | Origin | Why archived |
| --- | --- | --- |
| `BattleSpades-ace.py` | was `BattleSpades\ace.py\` | Old `ace.py`-style server attempt with `acelib/aceserver/acemodes/acescripts`. Has working logic worth referencing but is far from where the new server should be. |
| `BattleSpades-aceofspades` | was `BattleSpades\aceofspades\` | A previous AI-driven effort: complete game-tree copy + experimental code. Mostly non-working junk. |
| `nonsteam-aceofspades_decompiled` | was `aceofspades_nonsteam\aceofspades_decompiled\` | Proxy-logging shim layer + an AI-generated `server\aosserver\` rewrite attempt. The `proxy\*.pyd` files inside duplicate `aceofspades_nonsteam\shared\*.pyd`. |

If we ever want to revive a piece of this, copy out only the file we need; don't restore the whole tree.

---

## Things at `G:\AoSRevival\` outside the three working folders

These are unrelated to the current effort. **Don't read, don't edit, don't archive — they're someone else's working state**:

`AceOfSpades_no_steam_new`, `ace-server`, `ace.py`, `aceofspades_decompiled` (root level — separate from the one we archived), `aceofspades_dedicated_server`, `aos.pkg_extracted`, `aos_bak_new_piglet`, `aos_macos`, `aos_web_server`, `bakup from nonsteam`, `*.rar` files, loose `.py` files at root, etc.

If you find yourself reading something at `G:\AoSRevival\<X>` that isn't in the three working folders or `archive\`, stop and check whether it's actually relevant.

---

## How to run things

```powershell
# Build cython (after any .pyx edit)
cd G:\AoSRevival\BattleSpades
py setup.py build_ext --inplace

# Run the server
py run_server.py

# Run tests (active suite is test_reversed_*.py)
py -m pytest tests/ -v

# Probe original game APIs (py2 only — needs 32-bit Python 2.7)
py2 scripts\probe_originals.py --out scripts\probe_findings.json

# Connect the real client at our local server
cd G:\AoSRevival\aceofspades_nonsteam
.\python\python.exe run.py +debug +connect 127.0.0.1:32887
```

`py` = Python 3.12 (64-bit). `py2` = Python 2.7.18 (32-bit, required for the original game's `.pyd` modules). Both on PATH.

---

## When in doubt

1. **Looking for a packet's wire layout?** → `..\aoslib-reversed\aosprotocol.1x.md` and `..\aoslib-reversed\shared\packet.pyx`.
2. **Looking for what the client *actually* sends?** → load `..\aceofspades_nonsteam\shared\packet.pyd` under `py2` and inspect, or capture traffic.
3. **Looking for our handler?** → `BattleSpades\protocol\packet_handler.py` (`@register_handler`).
4. **Wondering "is this a real implementation or an old AI attempt?"** → if it's in `archive\`, it's an old attempt. Trust `BattleSpades\` and the two reference repos.
