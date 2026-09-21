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

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for toolchain requirements.

## Before you open a PR

1. **Tests pass:** `py -m pytest tests/ -q`.
2. **Movement parity holds:** `py scripts/replay_parity.py` prints `ALL PASS`.
3. **Rebuild Cython** if you edited any `.pyx`/`.pxd`:
   `python setup.py build_ext --inplace` (stop the server first — it locks the
   compiled files).
4. Keep changes focused and describe *what you observed* — this project is
   measurement-driven.
5. Update the existing maintained guide and check its local links/commands.
   Distinguish source behavior from fresh runtime evidence; do not present a
   previous run's count or a retained executable as validation of new edits.

## Working on netcode or physics? Read first

The movement model, packet layouts, and reconciliation timing are **reverse-
engineered measurements**, not guesses. Before changing them, read:

- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — packet catalog, measured movement invariants, and extraction workflow
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
- Keep build work in `build/`, frozen executables in `dist/`, portable bundles
  in `release-dist/`, and diagnostic runs in `tmp/` or `logs/`. Replace obsolete
  outputs instead of accumulating dated directories at the repository root.
- Update the relevant reference linked from `README.md` when behavior changes.
  Keep investigation logs and dated validation reports out of `docs/`; code,
  tests, and reproducible commands are the sources of current behavior.

## Reporting bugs

Include: OS + arch, Python version, which client you tested with (stock Steam,
non-Steam, or the `aceofspades_revival` build), and the relevant `logs/` output.
For gameplay desync, note what the **server** did vs. what the **client showed** —
that split is usually where the bug lives.
