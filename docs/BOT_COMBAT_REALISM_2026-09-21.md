# Bot combat realism pass — 21 September 2026

Goal: bots that are fun to fight — no frozen or lost bots, fights that look
like players fighting, and more of the game's mechanics visibly in use. All
work is in the active `SimpleBotBrain` path; navigation, the motor and the
authoritative gateway are unchanged apart from the hooks named below.

Every finding came from 10-bot native matches traced at 10 Hz
(`scripts/bot_runtime_smoke.py --trace-jsonl`) and summarized with the new
`scripts/bot_fun_review.py`, which reports per-bot kills/deaths, stand-still
time, enemy-visible versus firing time, role mix, tools held and action results.
No public server was used.

## Defects found in the baseline

| Finding (AncientEgypt TDM, seed 7, 180 s, unless stated) | Cause | Fix |
| --- | --- | --- |
| Two Soldiers 18 blocks apart "fired" at each other for 12–15 s; no shot left either gun and neither moved | Below skill 0.82 the brain aimed one block under the eye. A terrace lip hid both torsos while the heads were visible, so the director's lane probe vetoed every shot. Planted weapons never move | `aim_offset` aims at the torso only when the torso ray is clear; `fire_blocked` notices wanted-but-unaccepted shots and closes in along a real route |
| A Marksman held its outpost for 48 s with a pickaxe, watching enemies at 76 blocks while being shot | The bolt-action rifle carries 8 rounds. Bots never drew a secondary weapon, and the cooperative hold kept the empty gunner in place | Per-weapon ammo is published; `_weapon_tool` draws a loaded sidearm; outpost/cover/strongpoint/sabotage tasks end with `ammo_exhausted` |
| One bot walked 700 blocks along the map border for three minutes and never met an enemy | Goal distance stalled for 6 s while rounding its own base, which requested coarse corridor guidance. The atlas models walks and two-block drops only, so its only "route" circled the map | `_corridor_is_absurd` rejects guidance longer than 2.2× the direct trip + 60 blocks and remembers the rejection for 45 s |
| Double-barrel owners held at 25–30 blocks and never hit | The shotgun doctrine reached 30 blocks; the gun stops at 20 | `envelope_for` clamps ideal/hard range to the weapon's `max_range` |
| Bots alternated `investigate_sound` and `chase_last_seen` several times a second, replanning each time | Two layers own the same errand under different goal keys; expiring evidence started errands that were cancelled on the next decision | Hunt goals within 8 blocks share one route; evidence needs 2 s of life to start an investigation |
| CastleWars CTF: a raider reached 3 blocks from the flag at full health, then stopped to duel the guard and died | Objective routing is released within 12 blocks of the objective so "combat in the objective area keeps normal footwork" | Within 18 blocks an `attack_intel` raider keeps its route to the pickup and returns fire on the way |
| CastleWars CTF: raiders arrived one at a time; deaths were spread across the whole field | No grouping | `_should_rally`: a lone raider at 38–58 % of the way waits only while an ally is coming up within 70 blocks, never under fire, and leaves as soon as anyone is alongside or ahead. Raiders also use the per-life flank approach |
| CastleWars CTF, 240 s: four of ten bots (ids 0, 3, 6, 9) never moved; one travelled 6 blocks. No flag pickup | `player_id % 3 == 0` made permanent sentries on one fixed cell: two of five per team | `CTFBotPolicy._is_defender` keeps the one or two live teammates nearest home on guard (none below three players), with a 6-block margin against role swapping. A respawn beside the base relieves the guard, who joins the attack |

## New behavior

- **Guards walk a beat.** CTF guards move between posts 7–15 blocks around the
  base every 16 s and take a forward picket 26–34 blocks toward the enemy
  intel every third leg. Demolition, Territory Control and Diamond Mine
  sentries rotate posts inside their fortification radius every 18 s
  (`_guard_beat`). Beats are identity-staggered, not random per frame.
- **Cover on reload.** With an empty gun and a visible enemy the bot ducks if a
  crouch already breaks the sight line, otherwise steps behind terrain within
  7 blocks (at most 14 rays, once per second, never toward the shooter). It stays
  down until the reload finishes (`cover_reload`) instead of standing back up
  into the same fire. The old backpedal remains the fallback on open ground.
- **Planted shooters displace.** Snipers and machine gunners hold a spot for
  4–15 s depending on caution, then take a lateral stride; a hit while planted
  forces the move at once.
- **Sidearms and tactical reloads.** A pistol is drawn when the primary is
  dry, when its chamber is empty with an enemy inside 28 blocks, or when an
  enemy comes within 13 blocks of a scoped rifle (holstered again past 22).
  Between fights a magazine at or below 40 % is topped up.
- **Spade at arm's length.** An empty clip with an enemy within 3.2 blocks
  swings the melee tool rather than reloading.
- **Grenade evasion.** Thrown explosives and lit friendly charges inside
  blast radius + 3 trigger a short sprint away at survival priority. Rockets in
  flight and unlit/enemy mines never qualify. Noticing depends on skill, so
  casual bots are still caught.
- **Jetpack leaps in combat.** Pack owners with reserve fuel hop 5–7 blocks
  sideways to a validated dry landing when hit, or occasionally by temperament,
  through the existing traversal flight. They keep watching the enemy in the
  air. Cooldown 9–15 s.
- **Flank approaches.** On maps with bases at least 140 blocks apart, a share of
  lives (higher for creative/cautious bots, lower for aggressive ones) first
  heads for a staging point 34–80 blocks off the centre line. Chosen once per
  life from identity and life number; released on arrival, contact, midfield
  or after 75 s. The bot first leaves its base by the ordinary route and only
  peels off past 10 % of the way (see the map-matrix note below).
- **Loadouts.** A primary with under 30 blocks of reach is swapped for a
  longer-ranged alternative 60 % of the time. The swap draws from its own
  stream, so every other seeded roster decision keeps its place.
- **Chat.** `server/bot_ai/banter.py`: kill, long shot, melee, explosive,
  streak, revenge, repeat-death, suicide and start/end lines. About a third of
  bots never type. Limits: one bot line per 6 s globally, 25–55 s per bot, a
  queue of three, typing delay, fighting bots wait, and lines older than 7 s are
  dropped. `[bots] chatter = false` disables it. `Player.kill` notifies
  `BotDirector.on_player_killed`.

Fairness is unchanged: only the worker's own perception, public team anchors,
damage-source facts already sent to a victim, and the worker's voxel copy are
used. Every shot, swing, reload and tool change still passes the gateway.

## Measured effect

AncientEgypt TDM, 10 bots, seed 7, 180 s, process worker, full runtime. The
final column is the finished tree, including every change in this note, and
passed `--detect-travel-loops`:

| Measure | Baseline | Pass 2 | Final |
| --- | ---: | ---: | ---: |
| Deaths (all bots) | 17 | 24 | 21 |
| Team score total | 2050 | 2850 | 2400 |
| Longest stand-still | 15.4 s | 6.7 s | 7.3 s |
| Bots that never saw an enemy | 1 | 0 | 0 |
| Accepted / rejected shots | 311 / 66 | 369 / 31 | 357 / 62 |
| Time on the shared centre line | 54.5 % | 42.0 % | 47.8 % |

Seen live in the final run: one combat jetpack leap (3 s, 5.1 blocks sideways,
1.5 blocks of rise, fuel 100 → 67.5, no damage, landed grounded), 12 samples
of grenade evasion, 30 of `cover_reload`, 901 of flank approach. Only one bot
owned a usable pack, so the leap is confirmed working rather than common.
Process scheduling is asynchronous, so equal seeds do not make two matches
identical: read the columns as direction, not a benchmark.

CastleWars CTF, 10 bots, seed 23, process worker, full runtime. The runs
differ in length, so raw counts are shown:

| Measure | Baseline (240 s) | After (300 s) |
| --- | ---: | ---: |
| Bots that never left their post | 4 | 0 |
| Deaths (all bots) | 27 | 42 |
| Raids reaching 12 blocks of the enemy intel | 1 (in a 300 s intermediate run) | 6 |
| Flag pickups | 0 | 1 |
| Captures | 0 | 0 |

No capture has been demonstrated yet: the single carrier died one second
after the pickup inside the enemy base. Both CTF runs passed
`--detect-objective-abandonment`.

London TDM (seed 23, 150 s) passed `--detect-travel-loops` with the combat
changes included.

## Tests

`tests/test_bot_combat_tactics.py` (27) and `tests/test_bot_banter.py` (7)
cover each behavior above, including the recorded standoff geometry, the
border detour, rate limits, the director's chat packet and the fairness
exclusions. One existing test required an interaction rule rather than a
change of expectation: the silent-gun response yields to the blocked-footwork
recovery ladder.

All 751 bot tests, including the 36-map matrix, passed in one run. The
remaining modules were not completed in a single process (see the native crash
below). Five `tests/test_class_selection.py` cases fail identically on the
untouched baseline (`player.deaths == 1` after a class-change kill) and are
unrelated to this work.

`_corridor_is_absurd` first used only a ratio and rejected the legitimate
150-block wall detour in `test_surface_corridor.py`; it now also requires the
route to exceed 280 blocks.

## Pre-existing native instability

Running `tests/test_surface_corridor.py` repeatedly crashes the interpreter
intermittently: access violations inside pure-Python loops
(`navigation_atlas._build_clearance_and_passages`,
`world_manager.find_unsupported_chunks`), a silent exit, and once a pytest
`INTERNALERROR ... 'str' object has no attribute 'co_argcount'`. Those are
symptoms of heap corruption by native code, surfacing wherever Python touches
the damaged memory. The untouched baseline crashed in 1 of 4 runs and this tree
in 3 of 6; the samples are too small to call that a difference, and none of
these changes touch native code. The worker's one `SystemError` (below) fits
the same picture. No live match crashed in eight runs, but this deserves its
own investigation (the native world object and VXL mutation paths are the
suspects) before a long-running public server.

## Map-matrix lessons

`tests/test_bot_map_matrix.py` runs every shipped map for 35 s with seed 0 and
fails any bot held in one spot for 8 s. It is a chaotic fixture: two of these
changes failed it without touching navigation.

- Weighting loadouts with `rng.choices` shifted the seeded stream, gave the
  DragonIsland roster different classes and exposed a latent 8.2 s shoreline
  trap. Fixed by keeping the original draw and using a separate stream.
- Flanking from spawn sent a TokyoNeon bot out of its base by another exit and
  into a stairwell dead end that it cycled for the whole run. That trap is in
  existing navigation, but the feature did lead bots onto less-tested ground,
  so flankers now clear the base on the validated route first.

Bisecting with one feature disabled at a time (`simulate_map` runs the brain
in-process, so a monkeypatch is enough) separated these from real regressions.
The TokyoNeon stairwell and the DragonIsland shoreline remain latent for other
seeds and rosters.

## Limits

- One CTF run logged `SystemError: error return without exception set` from
  the `SurfaceNode` constructor inside the planner. The worker's existing
  per-bot recovery reset that controller and the match continued. It did not
  recur in the next 300 s run and no changed code is on that stack; cause
  unknown.

- Headless traces show what bots did, not how it looks. A Create Match
  playtest is still needed for the leap, ducking and chat cadence.
- Cover search is local and geometric; it does not reason about a second enemy.
- Flank staging points are validated only as dry standable cells. Navigation
  may still dig or swim to reach one.
- No grenade lobbing over cover: the director's lane probe rejects an occluded
  throw, and a ballistic solver was out of scope.
