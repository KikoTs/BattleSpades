# Community modes: revised playlist

The normal selector now contains 13 community modes. Seven prototypes were
retired: False Wall, Tunnel Rats, Lockdown, Paint Raid, Smuggler's Run, Reverse
Siege and Scrapyard Engineers. Their old codes remain available for existing
configurations, but they are excluded from the GUI list and generated presets.

## Playlist

| Display name | Stable config code | Objective | Arena |
|---|---|---|---|
| Prop Hunt | `block_hunt` | Hiders: E samples colour, fire disguises (unlimited); shovel digs but cannot hurt players. Hunters find everyone. | LanternQuarter |
| Crumbling Run | `crumbling_run` | Touch checkpoints in order. Platforms crumble behind you; /retry returns to your checkpoint. | FoundryRun |
| Extraction | `last_exit` | Collect valuables and hold the announced exit to extract. Only extracted loot scores. | DuskwaterDocks |
| Survival | `night_shift` | Clear six enemy waves, then reach evacuation. Repair the yellow barricade for a team resupply between waves. | BlackPineOutpost |
| Bridge Race | `bridge_chicken` | Build across the gap and deliver your relay to the far island. Falling drops your relay. | RelayViaduct |
| Tower Wars | `towerfall` | Build around your tower, then destroy enemy supports. A falling core loses the round. | TwinBellFoundry |
| Spleef | `floor_tax` | Hold shovel fire to cut the floor. Grenades knock players back. Fall through three floors and you are out; last survivor wins. | Glassworks |
| Hot Potato | `hot_potato` | Melee-tag another player to pass the cursed intel. The fuse costs a life; three lives each. | FuseCourtyard |
| One in the Chamber | `one_in_the_spade` | One lethal shot. A kill earns another; when empty use your spade. First to 15 wins. | TheKiln |
| Gun Game | `borrowed_arsenal` | Earn two kills with each weapon to advance. Finish the weapon ladder to win; death keeps your progress. | ArsenalArcade |
| King of the Hill | `kingmaker` | Hold the marked hill uncontested to score for your team. First to 90 points wins. | CrownGardens |
| Red Light, Green Light | `red_light_raid` | Race through checkpoints. Freeze on RED; moving resets you. /retry returns to your checkpoint. | SignalSprint |
| Spadeball | `spadeball` | Carry or throw the intel into the opposing goal. Spades only; first to five goals wins. | SpadeStadium |

## What changed

- Survival (old Night Shift): six finite waves, at most eight live enemies;
  clearing a wave starts an eight-second intermission. Dead squadmates return,
  ammunition refills, then the next wave begins. After wave six, hold evacuation
  for six seconds. Clearing enemies always advances the wave; optional repairs
  cannot block progression. The exposed yellow barricade is objective B.
  Rebuild its top row with the block tool for a one-time squad ammo resupply.
  The last two enemies receive moving minimap markers. Failed bot admission
  ends the round with an explanation after 20 seconds rather than hanging.
- Crumbling Run: a new 960-block switchback route with twelve checkpoints,
  thirteen-block lanes, alternate paths around short gaps, and safe reset pads.
  Ground contact schedules four centered floor cells for removal after 0.3s;
  holes restore after eight seconds, deferring occupied cells. The floor is
  one voxel thick, with structural beams beside the running lanes.
  Each player sees only their next checkpoint.
- Spleef replaces Floor Tax. Hold shovel fire to remove aimed 2x2 floor cells
  at 10 Hz within five blocks. Grenades refill every twelve seconds and retain
  their normal knockback, with health damage disabled. Three independently
  supported floors, a smaller combat area, spectator stands, and sudden death
  after 90 seconds. Bots seek rivals, dig their floor and throw grenades.
- Hot Potato: a compact chase arena, a twelve-second fuse that persists across
  passes, three lives, a 10 Hz small carrier marker and a one-second fuse HUD.
  The player who just lost a life does not immediately receive the next bomb.
- Gun Game replaces Borrowed Arsenal: two kills per weapon, five weapons,
  progress retained on death, and a win at the end of the ladder.
- King of the Hill replaces Kingmaker: every teammate can score; enemies in
  the eight-block hill stop scoring. First team to 90 points wins.
- Extraction opens after one minute. Loot markers disappear on pickup and
  reappear at their actual drop locations. The exit is marked from the start.
- Survival, Extraction, Tower Wars and King of the Hill offer Soldier, Scout,
  Miner, Engineer and Medic, with class-appropriate fixed weapons, blocks,
  shovel and grenades. Medic carries a medpack. Mechanic-specific modes keep
  their constrained kits. Class choices are advertised in the actual menu.
- Fixed objectives use native packet-43 objective volumes and packet-44 clears,
  including late-join replay. Moving markers use small retail assets and
  packet-41 updates. The native client caps community world indicators at
  18â€“48 pixels at 1080p; official server presentation keeps its existing rules.

Every arena includes its authored sky, fog and ambient sound, plus up to twelve
real lamp lights placed in existing voxels.

## Compatibility and movement

All thirteen modes accept retail clients and use their existing packet layouts.
Do not send ProgressBar(65): the shipped retail renderer crashes on that packet.
Instructions arrive after joining and again at each round; `/funrules` repeats
these instructions, and `/funscore` shows the standings.

The updated native client advertises BSCF v4. In Crumbling Run only, the server
then advertises `BATTLESPADES_RACE_MOMENTUM=1`. Server simulation and client
prediction/replay use the same velocity-based acceleration curve: 65% at rest,
building to 135%, capped. Releasing input retains ordinary braking. Bots on the
new course use that profile too. Retail and older native owners keep stock
movement; they are not given unmatched server velocity boosts. In mixed-client
races these are different movement profiles, so use updated native clients for
competitive timing comparisons. Hardcore continues to require BSCF v3 or newer.

## Run and regenerate

```powershell
py -3.12 run_server_gui.py --config configs/unofficial/night_shift.toml
py -3.12 run_server.py --config configs/unofficial/night_shift.toml --check
py -3.12 scripts/build_fun_maps.py --overwrite
py -3.12 scripts/audit_fun_maps.py
```

Presets use the regular server port. Stop the existing instance before starting
one on the same port, or supply `--port` with an unused port. A new round waits for a human instead of consuming rounds with bots alone.
Normal modes backfill to six total players; Survival owns its finite wave population.
Generating the pack overwrites generated maps/presets, so retain operator edits
first. The revamp saved the previous files under `build/fun-revamp-backup`.

[Map atlas](arena-previews/arena-atlas.png) and [structural audit](arena-previews/audit.json)
are generated from the actual VXL files, including bedrock connectivity,
spawn clearance, objective reachability, and single-block collapse probes.
Tests exercise complete Survival waves, actual repair builds, Spleef knockback,
production-map bot races through the native mover and the actual bot motor,
marker updates, late joins, class selection, and safe round resets.
These checks establish functionality; human playtesting still determines pacing
and whether the modes are enjoyable.

Live two-peer integration checks: `py -3.12 scripts/smoke_fun_modes.py`. This
starts isolated servers, joins with native-v4 and stock-ticket peers, verifies
the negotiated profile and authoritative kit, and checks live world updates.
A stock-ticket native peer checks the retail wire path; it does not substitute
for testing the Steam retail renderer itself.
