# Contributing to BattleSpades

Thanks for helping keep classic Ace of Spades alive! Contributions of all sizes
are welcome — bug fixes, maps, game modes, docs, and **platform build reports**
(especially "it built/ran on X arch") are all valuable.

## Getting set up

```bash
git clone https://github.com/KikoTs/BattleSpades.git
cd BattleSpades
./scripts/install.sh          # or scripts\install.ps1 on Windows
python run_server.py
```

See [`docs/BUILDING.md`](docs/BUILDING.md) for toolchain requirements.

## Before you open a PR

1. **Tests pass:** `py -m pytest tests/ -q` (currently 87 passing).
2. **Movement parity holds:** `py scripts/replay_parity.py` prints `ALL PASS`.
3. **Rebuild Cython** if you edited any `.pyx`/`.pxd`:
   `python setup.py build_ext --inplace` (stop the server first — it locks the
   compiled files).
4. Keep changes focused and describe *what you observed* — this project is
   measurement-driven.

## Working on netcode or physics? Read first

The movement model, packet layouts, and reconciliation timing are **reverse-
engineered measurements**, not guesses. Before changing them, read:

- [`docs/PHYSICS_CALIBRATION.md`](docs/PHYSICS_CALIBRATION.md) — every measured constant + the extraction workflow
- [`docs/NETCODE_RECONCILIATION.md`](docs/NETCODE_RECONCILIATION.md) — the client's exact correction algorithm
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — how to run the client-as-oracle rig to verify against the real game

If you change a physics constant or a packet field, **verify it against the real
client** (the tooling in `scripts/` exists for exactly this) and note the
measurement in your PR.

## Style

- Game logic in readable Python (`server/`, `modes/`, `commands/`); only put the
  hot path (physics, VXL, (de)serialization) in Cython (`aoslib/`, `shared/`).
- Match the surrounding code's conventions. Comments should explain *why*
  (especially a measured constant or a protocol quirk), not narrate the code.
- Don't commit build artifacts (`.pyd`/`.so`/generated `.c`), logs, or local
  config — `.gitignore` already covers them.

## Reporting bugs

Include: OS + arch, Python version, which client you tested with (stock Steam,
non-Steam, or the `aceofspades_revival` build), and the relevant `logs/` output.
For gameplay desync, note what the **server** did vs. what the **client showed** —
that split is usually where the bug lives.
