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
  client-predicted locally with authoritative 30 Hz observer replication).
- **Core gameplay, verified live against the real client:** movement, jump,
  shooting (hit-scan from the client's reported aim), block build/break with
  exact-cell client↔server VXL sync, grenades (bounce physics + blast + block
  destruction), floating-structure collapse, ammo/health crates, damage/kills/
  respawn/grave, map streaming.
- **Game modes:** TDM, CTF, Arena, Gangster VIP, and Zombie Infection.
  Infection includes the native preparation/outbreak phases, permanent
  survivor conversion, zero-delay zombie respawns, last-survivor radar, and
  late-join role enforcement.
- **Human-like bot foundation:** supervised process isolation, bounded
  versioned messages, dynamic Recast/Detour terrain, fair LOS/last-seen/sound,
  delayed team sightings, natural aim/locomotion, TDM combat, basic objective
  policies, class loadouts, mining/bridge recovery, deployables, oriented
  projectiles, DetourCrowd steering, movement affordances, shared prefab
  placement, construction reservations, resource seeking, and cover utility.
  This is a playable foundation, not the end of the bot roadmap.
- **Objective pickups and resource crates:** CTF intel carry/drop state is
  authoritative and late-join safe; ammo, health, block, and authored jetpack
  crates restock through the retail packet paths.
- **One-command install & build**, architecture/runbook/engineering docs, and
  729 automated tests.
- **Production capacity gate:** a 15-minute, 50-player run holds 59.999 Hz with
  4.842 ms tick p99 and zero gameplay or telemetry drops.

## Near term 🔜

- **Bot interaction polish:** richer projectile tactics, statistical aim/hit
  calibration, glider route tuning, and an operator-facing rendered debug view.
  Zombie bots now have a dedicated global-survivor/contact motor, native
  base/Fast/Jump variants, 3x3x3 claw breaching, and authorized Zombie-prefab
  climb recovery; two-retail-observer feel validation remains open.
- **Bot objective acceptance:** phase-aware roles now cover CTF/Classic CTF
  capture, escort and defence; Zombie preparation, regrouping and last-man
  pursuit; and VIP formation, guard, flank and sudden-death behavior. Headless
  `cctf`/VIP/Zombie runtime gates pass. Deterministic score contributions and
  two clean retail observers with no rollback or crash dump remain open. The
  strict 15-minute 12-bot performance soak and in-match worker-restart gate are
  complete.

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
3. **Modes:** Team Deathmatch, Capture the Flag (+ Classic), **Zombie**, and
   **VIP** are implemented. Territory Control, Multi-Hill, Demolition,
   Occupation, Diamond Mine, Tutorial,
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
