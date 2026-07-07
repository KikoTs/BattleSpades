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

## The full game — content build-out 🗺️

The 1.x client ships far more than the server implements today. The goal is the
**complete** game, built dependency-first so foundational systems are shared and
modes stack on top of them (the systems built for TDM get reused for Zombie with
different props). The full inventory: **13 game modes**, **66 weapons/tools**,
**18 classes**, **40 entity types**, plus deployables, projectiles, and jetpacks.

Layered plan (`data → systems → modes → presentation`):

1. **Foundations — data:** a real per-weapon profile for all 66 tools and real
   per-class stats + loadouts, sourced from the original client's constants.
2. **Foundations — systems:** a tickable **entity behavior system** (respawns,
   touch/damage hooks, server-side collision), a **projectile engine** (grenades,
   molotov, RPG rockets, drill, snowball, mine-launcher), **deployables** (turrets,
   landmines, C4, dynamite, medpacks, radar), and status effects (fire, disguise,
   jetpack).
3. **Modes:** Team Deathmatch, Capture the Flag (+ Classic), Territory Control,
   Multi-Hill, **Zombie**, VIP, Demolition, Occupation, Diamond Mine, Tutorial,
   and the UGC map-creator mode — each composing the systems above.
4. **Presentation:** end-of-round scoreboard, per-player scores, HUD round timer,
   sounds, minimap objectives, and vote-kick.

The full protocol surface (~119 packets) is kept and documented in
[`PROTOCOL.md`](PROTOCOL.md); each feature lights up the packets it needs.

- More stock and community maps.
- Configurable game rules per mode.

## Long term / community 🌍

The codebase is deliberately clean and heavily documented so it can be **carried
forward or ported** (Go, Rust, …) if the community wants to. The reverse-
engineered protocol, physics constants, and netcode contracts in `docs/` are the
reusable core — whatever language runs it. If enough people pick it up, the
classic game comes back to life.

Companion client build: [KikoTs/aceofspades_revival](https://github.com/KikoTs/aceofspades_revival)
(builds the original client from source so client-side mods can ship too).
