# Local bot circling investigation, 2026-09-21

The user's local AncientEgypt TDM playtest exposed behavior that the earlier
smoke tests did not establish. Those tests mostly checked worker liveness,
physics movement, action acceptance and complete stalls. Moving around the
same area could satisfy them. A previous graphical check established joining
and spawning, not convincing bot play. Neither should be reported as general
gameplay-quality validation.

## Reproduction

`out/bot-field-repro-20260921/create-match.toml` mirrors the local Create Match
configuration: AncientEgypt, TDM, eight mixed-class bots, process worker and
normal fixed population settings. The smoke runner explicitly controls bot
population and additionally seeds authored spawn selection for a repeatable
fixture. Process scheduling remains asynchronous; equal seeds do not make
two real-time matches identical. No public server is involved.

The native trace records actual positions, orientation, life, strategic goal,
path, actions and motor state at 10 Hz while the complete simulation runtime
executes at 60 Hz. Later traces also record wall time, velocity, sprint and
intent freshness. Source hashes identify the version and any changes during
a run. See `BOT_TRACE_REVIEW_2026-09-21.md` for diagnostic criteria and limits.

The accepted Egypt run used this command from the server repository:

```powershell
py -3.12 scripts/bot_runtime_smoke.py --seconds 180 --bots 8 --map AncientEgypt --mode tdm --worker process --full-runtime --seed 0 --config out/bot-field-repro-20260921/create-match.toml --trace-jsonl out/bot-field-repro-20260921/egypt-candidate-8.jsonl --detect-travel-loops --progress-every 30 --trace-state --json out/bot-field-repro-20260921/egypt-candidate-8.json
```

The paired London command changes the map to `London`, duration to 120 seconds,
seed to 23 and output basename to `london-candidate-8`. Use new output names
when repeating either command so the accepted evidence is not overwritten.
This harness uses the native world, actual AI process, motor and action gateway
without a rendered client or network connection. It therefore complements a
normal Create Match playtest; it cannot replace watching the rendered result.

## Failures retained as evidence

- `first-trace.jsonl`: 120-second exploratory run. It loaded intermediate
  changes and is not a controlled before/after baseline.
- `egypt-final.jsonl`: despite the historical filename, this is a **rejected
  intermediate build**. Bot 2 travelled 199.80 blocks during seconds 60–90,
  ended 2.70 blocks from its starting point and gained only 1.57 blocks toward
  the same distant goal. Bot 5 also repeatedly returned to its starting area.
  The revised review is `egypt-final-reviewed.json`; the original report used
  a reviewer that missed airborne travel and must not be treated as acceptance.
- `egypt-terrace-fix.jsonl`: another **rejected intermediate build**. Braking
  and route catch-up improved the originally identified intervals, but the
  stricter reviewer identified two remaining closed returns by bot 5. Its
  smoke command exits unsuccessfully and retains both intervals in the JSON.
- `egypt-live-steering.jsonl`: **rejected**. A bot waited about nine seconds
  while a coarse corridor search repeatedly consumed the planning grants
  needed for its immediate route, then retraced its approach.
- `egypt-candidate-4.jsonl`: passed the 180-second diagnostic, with 17 eligible
  windows and no loop suspects. The previously inspected planning pause was
  about 0.4 seconds. This was not installed because the paired London run failed.
- `london-candidate-4.jsonl`: **rejected**. Bot 3 repeatedly returned to an
  upper shelf while pursuing an optional squad position on an inaccessible
  lower layer. The recovery watchdog fired but selected the same task again.
- `london-diagnostic-5.jsonl`: the geometric loop check passed, but internal
  diagnostics showed up to 47.875 seconds without progress on that optional task.
  It is **not acceptance evidence**. Passing the loop check alone cannot
  establish that a bot makes useful decisions.
- `egypt-candidate-6.jsonl` and `london-candidate-6.jsonl`: **rejected after
  trace inspection**, although their geometric loop checks passed. In London,
  the moving support target repeatedly reset the goal-distance progress clock.
  In Egypt, bot 0 stayed at one position during seconds 60–90. Some early
  digging swings were accepted, so that entire interval cannot be described
  as rejected work. During seconds 77.2–91.7, it was waiting on recovery:
  113 exact goal changes in 146 samples repeatedly restarted the escape query.
- Candidate 7 corrected those land-navigation examples: in Egypt, bot 0
  travelled 170.01 blocks during seconds 60–90 (37.36 net displacement), with
  only two planning-wait samples. Both runs had zero geometric-loop and
  optional-support findings. **London was still rejected**: bot 3 spent 96.1%
  of seconds 45–119.9 wading, with two dry footholds lasting only 0.1 seconds.
  Real excavation occurred, but it did not yield sustained shoreline escape.
  The old review excluded water and therefore could not establish this.

## Causes established so far

- Followers released movement ownership inside their formation radius, then
  followed again after strategic movement pulled them away. Turning a
  stationary leader also rotated the formation position.
- Nearby body waypoints were reused as look targets. Travel gaze now has
  independent eye-height lookahead; combat and excavation retain their aim.
- Slower angular updates overshot a fixed target. Planning waits retained old
  angular velocity, resuming a previous swing when navigation restarted.
- Arbitrary displacement counted as route progress. Spatial coverage now
  survives goal changes; jumping and dropping must not reset its watchdog.
- Sprinting through dense terrace waypoints allowed native momentum to carry
  bots past a lower landing. Route catch-up recognizes actually occupied later
  ordinary landings, and sprinting requires braking room before turns or steps.
- A held direction became stale after the body moved beyond its decision
  position. Ordinary route travel now carries an explicit source and waypoint;
  the live motor steers toward that authorized waypoint and releases a crossed
  waypoint until the next decision. Collision checks, combat aiming and
  deliberate terrain-work aiming retain ownership.
- Long coarse searches starved immediate local planning. A bounded reservation
  now gives needed local planning the next grant after a coarse search slice.
  The existing total rate and expansion limits are unchanged.
- Route-cycle recovery discarded unfinished coarse search progress even after
  excluding the failed edge. It now preserves an incomplete search frontier.
- An optional support destination without a usable corridor endpoint could
  survive repeated failed recovery attempts. A bounded fallback now gives up
  that optional point temporarily after confirmed failure and unsuccessful
  actor progress; critical objectives retain their existing priority.
- A moving teammate could improve straight-line distance without the follower
  moving, or reset that distance baseline. Optional support now accounts for
  actor progress against a stable anchor, pausing committed digging/task gaps.
- Escape continuation used the exact moving destination in its cache key.
  Small destination changes could discard completed planning work before
  the next bounded query ran. Continuation now survives local target drift.
  Remaining queries read current terrain and newly rejected edges; meaningful
  source/goal relocation and changed abilities invalidate the continuation.
- A pre-landing water jump remained leased after native dry contact and could
  immediately launch the actor off a one-cell bank. The recorded foothold was
  `(296, 228, 238)`, reached at seconds 63.7 and 88.6 in London candidate 7.
- Assisted bank excavation could remove the dry floor at layer 238 and select
  the waterbed at layer 239 as its destination. Real damage and map changes
  therefore did not prove a usable dry route. Native tool footprints and
  stable post-landing support must be checked together.

## Native shore corrections

The live physics hook now consumes water commands created before a validated
dry landing, releases the held jump immediately and uses normal directional
inputs briefly to brake remaining momentum. It does not change position,
velocity, gravity or movement limits. Fresh decisions retain control; expiry
also releases the old braking keys.

Bank excavation preserves floor 238 and clears cells 235–237 for standing
headroom. Target selection respects each tool's real excavation footprint and
keeps damage focused on a stable face. Changing cells preserves the attack
cooldown; the water deadline renews only when the actual targeted cell has
disappeared. Four actual London tool scenarios (spade, machete, pickaxe and
superspade) reach two seconds of supported, body-clear dry footing with zero
deaths or recovery respawns. All seven existing native pocket tests retain
their original deadlines and pass. Full-match acceptance is still separate.

## Acceptance status

Candidate 8 is the accepted server build for the captured regressions. Its
180-second AncientEgypt run (seed 0) and 120-second London run (seed 23), each
with eight bots and the native runtime/process worker, completed with unchanged
source and runner hashes. The final reviewer found zero travel-loop,
optional-support-stagnation or water-recovery-stagnation suspects; there were
11 and two eligible geometric review windows respectively. This is scoped
evidence, not proof that every route on every map is natural.

Independent inspection also checked the previously rejected intervals:

- Egypt bot 0, seconds 60–90: 143.95 blocks travelled, 34.37 net displacement,
  with combat, investigation and accepted excavation. The longest stationary
  interval was 7.9 seconds with actual changes to the targeted terrain.
- London bot 3, seconds 45–119.9: zero wading samples out of 750, versus 721
  previously. It progressed along the dry bank. The longest stationary
  interval across this London trace was 3.4 seconds.
- After 0.75 seconds of sustained ordinary travel, 6,809 Egypt samples and
  1,918 London samples had maximum absolute pitch 6.87 and 15.191 degrees.
  Combat, excavation and transitional aiming are deliberately outside that
  check.

The earlier scoped suite passed 612 tests before the final recovery fixes.
After those fixes, 208 focused navigation/budget/shore tests and all seven
existing native water-pocket tests passed. Four actual London bank scenarios
exercise the native spade, machete, pickaxe and superspade footprints; the
landing/motor regressions and 24 trace-review tests also passed. The long
all-map matrix was not completed.

The frozen candidate was installed into the normal
`BattleSpadesClient/dist/bin/server`: 203 runtime files, retaining the existing
configuration, client settings, maps and assets. `dist-install.json` records
hashes, preserved settings and the previous-runtime backup. Installed
`BattleSpades.exe --check` passed, including the real child worker; the native
local-host smoke passed protocol 168 bootstrap against AncientEgypt on
localhost. No public/legacy server was contacted and no release was published.

The normal installed client's Create Match → Local Match GUI also loaded
AncientEgypt with all eight bots and the dated Beta 1.1 greeting. Initial
spectator selection exposed a separate client camera issue: the first local
CreatePlayer arrived before the world session existed, and its life state was
never applied after session construction. Applying that boundary after setup,
continuing neutral spectator ClientData for roster readiness and hiding only
personal HUD widgets corrected the observed case. Camera, HUD and pause tests
passed in the final Release build.

Both normal client executable names were then backed up and replaced with the
tested binary, SHA256
`CC5F86CEE4B0A464CE9F49F9757F2380C4A1AFAA290B530017A59CE806AD33AC`.
`client-spectator-install.json` records the installation and unchanged settings.
The graphical retest used that installed `dist/bin/aos.exe`, the normal account
and Create Match → Local Match → Spectate, again on AncientEgypt with eight
bots. Sampled observations from the match clock 14:35 through 13:23 showed:

- Initial third-person chase with live named bots, without default first-person
  hands, health or ammunition displayed for the spectator.
- Bots moving across terraces and engaging at the temple entrance and beside
  the sphinx; visible damage/kill feedback and team scores advancing to 2–4.
- Automatic replacement of a dead chase target and a changed target after a
  left click (Juno22 to Bolt).

These were sampled screenshots of a live match, not a continuous recording or
an all-map gameplay review. They establish the repaired join/chase/render path
and visible movement/combat in that interval, while the native traces provide
the detailed movement/aim evidence. The test match was disconnected and the
test client closed normally; the updated normal distribution is ready for the
user's playtest. No release has been published.

## Native digging check at the Egypt stall

`tests/test_bot_native_breach_feedback.py` replays bot 0's position
`(229.49290466308594, 267.4670715332031, 221.7423553466797)` and Machete target
`(229.5, 268.5, 221.5)` against AncientEgypt. It explicitly excavates the starting
column, then uses the native ray and normal bot gateway/combat services. Three
legal swings accumulate 2, 4, then lethal damage; both authored target cells at
z=221 and z=222 disappear, with real topology changes and mutation callbacks.
A retry only 0.1 seconds after the first swing is correctly rejected and changes
neither damage nor terrain. This focused test passed in 1.04 seconds.

The candidate-6 trace likewise contains accepted swings between rejected ones
and changing dig targets. Rejected feedback alone does not prove a broken dig
ray or 30 seconds of failed actions. The independently established long wait is
recovery continuation: during seconds 77.2–91.7 the actor stayed at exactly one
position while 113 target-coordinate changes kept the escape query at index 0
or 1. The native digging test isolates the action contract; it does not recreate
all earlier terrain edits or establish that the complete recovery is fixed.
