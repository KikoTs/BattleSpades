# Full-roster gameplay validation

The gameplay matrix uses native character physics, the production director,
normal perception, owned class tools, authoritative action validation, terrain
collapse, objective scoring, respawns and natural mode transitions. Each case
runs in its own process; the native world and accelerated clock are global.
The accelerated supervisor executes the same worker batch inline. Use the
runtime smoke separately to check real worker scheduling and restarts.

## Reproduce

```powershell
py -3.12 scripts/bot_gameplay_matrix.py --scope all --seed 0 --seed 7 --seed 19 --bots 16 --seconds 360 --jobs 3 --targets --out-dir tmp/bot-gameplay-final
py -3.12 scripts/bot_gameplay_matrix.py --scope core --maps-per-mode 2 --skip-primary-map --seed 0 --seed 7 --seed 19 --bots 16 --seconds 360 --jobs 2 --out-dir tmp/bot-gameplay-final-secondary
```

The primary catalog covers 12 objective/combat modes (including
Classic+ Zombies), 10 hardcore variants, and 13 active community modes.
`nor` is a protocol placeholder without a launchable server implementation;
tutorial and editor bots are intentionally passive. Retired community modes
can be included explicitly with `--include-retired`. Custom cases use their
authored generated arena and connected idle participants, allowing the real
round-start rules to run. Night Shift retains its population ownership and
has capacity for enemy waves.

`--targets` adds rooftop, sealed-room, water-level and buried challenges for
VIP, Zombies and Classic+ Zombies. These are **16 attackers against one
connected last player**, using normal mode markers and class permissions.
The fixture streams neutral client inputs so the target falls normally when
its floor is removed. VIP auto-balance is disabled for this deliberately
asymmetric initial roster. Challenge telemetry stops at the original target's
defeat, before later rounds can change the original result.

Reports retain 4 Hz traces for every bot, actions, damage, deaths, objective
events, custom mode snapshots, construction across round resets, rejection
reasons, role histories, planning metrics, and the actual gameplay-source
SHA-256. Execution without an exception is separate from objective progress.
Review flags identify cases to investigate; a successful defense or contested
objective is not automatically a bot defect because the attackers did not win.
`--resume` reuses only completed reports with the same sources, audit version,
roster size and duration. `--mode vip --targets-only` selects focused challenges.

## Runtime contracts covered

- Night Shift's population-owner early return never advanced the population
  timer. Every director tick returned before perception and motors. Scheduling
  the next check before yielding to mode-owned population restores combat and
  wave progress without taking population control from the mode.
- Schematic planning knew approximate objective radii but not the protected
  spawn/base rectangles. The reservation service now publishes its exact
  immutable exclusions to planners; rejected footprints are discarded before
  builders commit.
- Attackers could repeatedly walk over an underground VIP without opening the
  floor. A failed walk to the nearby public crown now permits deliberate
  downward excavation with the owned tool's real footprint and a verified
  safe landing. Crouched bodies resolve their actual support surface rather
  than aiming beneath the floor.
- After terrain destruction, Territory Control bots could fight below the
  still-elevated capture volume. The director now supplies the exact public
  bounds. One nearby builder restores access, and a bot inside a hostile
  volume holds its position to complete the capture.
- Survival helpers all chose an optional barricade instead of protecting
  living humans. A bounded repair crew, rotating human guards, and remaining
  fighters divide the work; fallen guards are replaced by living allies.
  Audits record the actual wave number, since restarting wave one after a
  loss is not progression.

Team support/guard roles rotate on stable, seeded 45-second leases. Carriers
retain their scoring job; urgent escorts and interceptions remain local.
Horde approach/crew choices vary with the seed and round, preserve target
hysteresis, and credit active excavation. Survivor/base fortification builds
a bounded compound of shelter, overwatch and cover using shared cell claims,
supported BlockLines, entrances, and usable standing positions.

Finite simulations do not establish subjective fun or prove every destructible
map state is reachable. Keep exact failing traces and add a regression rather
than counting a clean process exit as evidence of competent play.
