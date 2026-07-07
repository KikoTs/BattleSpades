# BattleSpades Roadmap

Where the project is and where it's going. This is the "vision" doc — the
per-fix detail lives in [`RUNBOOK.md`](RUNBOOK.md), [`PHYSICS_CALIBRATION.md`](PHYSICS_CALIBRATION.md),
and [`NETCODE_RECONCILIATION.md`](NETCODE_RECONCILIATION.md).

## The mission

A **complete, correct, hackable** open-source server for classic _Ace of Spades_
(Battle Builders / 1.x) that anyone can run in one command — so the game stays
playable and hostable, and so it's a clean base others can build on or port.

## Done ✅

- **Netcode & physics reverse-engineered from the compiled client** and
  calibrated to millimetre-level movement parity (server-authoritative, 60 Hz,
  self-row reconciliation).
- **Core gameplay, verified live against the real client:** movement, jump,
  shooting (hit-scan from the client's reported aim), block build/break with
  exact-cell client↔server VXL sync, grenades (bounce physics + blast + block
  destruction), floating-structure collapse, ammo/health crates, damage/kills/
  respawn/grave, map streaming.
- **Game modes:** TDM, CTF, Arena. **Human-like bots** for solo testing.
- **One-command install & build**, docs, tests (87 passing).

## Near term 🔜

- **End-of-round experience:** final scoreboard / stats screen, per-player
  scoreboard column, and the HUD round-timer countdown (server sends no timer
  packet yet).
- **Scoring parity:** finish CTF and Arena scoring to match the original exactly.
- **Visual polish:** confirm/tune grenade landing vs. the client's local sim and
  the falling-block collapse animation on the stock client.
- **Stability:** reconnect / ENet peer-lifecycle hardening for long-running
  public servers.

## Content 🗺️

- More stock and community maps.
- Additional weapons, tools, and classes from the 1.x arsenal.
- Configurable game rules per mode.

## The big one — native ENet 🧩

Today the only hard-to-port dependency is **ENet** (via the compiled C
`pyenet` binding). Distributing to a new OS/architecture means building ENet for
that target — the "compile ENet three times for three arches" tax.

**Plan:** replace `pyenet` with a **native Go implementation of the ENet
protocol**, so the server (or a networking sidecar) can be produced as a single
static binary per platform with no per-arch C build. This unlocks:

- drop-in **multi-platform** distribution (Windows x64, Linux amd64/arm64, more);
- a cleaner path to embedding the transport in ports to other languages.

## Long term / community 🌍

The codebase is deliberately clean and heavily documented so it can be **carried
forward or ported** (Go, Rust, …) if the community wants to. The reverse-
engineered protocol, physics constants, and netcode contracts in `docs/` are the
reusable core — whatever language runs it. If enough people pick it up, the
classic game comes back to life.

Companion client build: [KikoTs/aceofspades_revival](https://github.com/KikoTs/aceofspades_revival)
(builds the original client from source so client-side mods can ship too).
