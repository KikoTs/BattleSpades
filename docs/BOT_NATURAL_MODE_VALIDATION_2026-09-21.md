# Local natural movement and objective validation

## Handoff status: final validation interrupted

The user requested stopping work and committing both repositories for a Mac
handoff. The three final native runs and their owned worker processes were
stopped immediately, before completion. No final-source acceptance is claimed.
Partial traces/logs are preserved as `*-user-stop-final-partial` under
`out/bot-natural-modes-20260921/`; `final-validation-user-stop.json` records their
last simulated times. The existing completed `*-candidate.json` reports refer
to the earlier candidate1 source, **not** the final carrier fixes. Resume the
two 120-second VIP runs and the 240-second CTF run after the handoff. The expanded
review suite last passed 43 tests before the user stopped work.

## Controlled VIP baseline

The initial run used the authored Alcatraz map from the original retail VIP
playlist, eight bots, the process worker, full authoritative gameplay and seed
23. It ran for 120 simulated seconds in 120.31 wall-clock seconds. The report's
`source_unchanged_during_run` is true; it stores hashes of all bot AI Python
modules and the exact smoke runner. No public server was used.

Command (PowerShell output was redirected to the corresponding `.log`):

```text
py -3.12 scripts/bot_runtime_smoke.py --seconds 120 --bots 8 --map Alcatraz --mode vip --worker process --full-runtime --seed 23 --config out/bot-field-repro-20260921/create-match.toml --trace-jsonl out/bot-natural-modes-20260921/vip-alcatraz-baseline.jsonl --progress-every 30 --trace-state --json out/bot-natural-modes-20260921/vip-alcatraz-baseline.json
```

The baseline records phase, authoritative VIP IDs/alive states, team respawn
permissions, team scores, actor scores and the existing native motion, aim,
action and terrain diagnostics. Later harness runs also record the published
VIP/CTF/base objective snapshots once per trace sample.

Observed behavior:

- Both VIPs were selected at second 10. Their guards initially held useful
  positions about 3.3–6.2 blocks away; stationary guarding is not a failure.
- One VIP died near second 95.7. Its team correctly lost respawn permission and
  switched to sudden-death assault. No round was won within this sample.
- The opposing attacker immediately abandoned its offensive role and headed
  roughly 215 blocks home. From seconds 101.7–119.9, every surviving non-VIP on
  that team guarded while two opposing survivors remained alive. The existing
  policy made mop-up unreachable whenever its own VIP was alive. The run passed
  runtime health but fails objective acceptance on this concrete transition.
- Attacker 0 spent substantial time approaching a higher enemy base from a
  lower floor. Terrain topology advanced from 0 to 37, and a long corridor was
  finally selected near second 115. Accepted digging was real work; the run
  does not establish whether that long detour would finish.

Raw quiet-sample pitch maxima include excavation cooldowns and must not be
called ordinary travel. The ordinary travel filter recorded attacker 3 at a
maximum 2.34 degrees pitch; attacker 0 briefly reached 41.71 degrees just after
an excavation handoff. The trace is retained for the aim agent's exact context
analysis. Native yaw/pitch reversals measure changes in turn direction, not
simply crossing zero or a claim about subjective animation quality.

## Objective gates

`scripts/bot_objective_review.py` checks VIP objective ownership and records
selection/death/round-score transitions, role counts, escort distances and
attack approach distances. Its sustained findings cover a VIP drifting into a
generic task, escort goals far from the live VIP, and every attacker returning
to guard after the opposing VIP dies. Immediate defence of a threatened VIP,
combat, deliberate useful holds, respawns and sampling gaps are distinguished.

`scripts/bot_ctf_review.py` records authoritative flag pickups, carrier samples
and capture-score increases. It flags a live carrier retaining a goal away
from its own capture base for ten quiet seconds. A run with no pickup is
reported as not exercising carrier execution; liveness is not capture success.

The smoke runner's optional `--detect-objective-abandonment` flag requires a
VIP/CTF trace and fails missing objective telemetry or those findings. Reports
contain the reviewer hash. The protected-combat role
`combat_objective:<mode_role>` retains mode identity in the review.

The new objective tests plus the existing travel/water review tests pass:

```text
py -3.12 -m pytest tests/test_bot_objective_review.py tests/test_bot_ctf_review.py tests/test_bot_trace_review.py -q
39 passed in 0.25 seconds
```

Follow-up acceptance will repeat Alcatraz VIP, then CityOfChicago VIP and
CastleWars CTF after production source is frozen. All three are present in the
original retail mode playlists. Results must state objective events actually
observed; a short match is not required to manufacture a winner.

## Early policy diagnostic

`vip-alcatraz-policy-diagnostic.{json,jsonl,log}` reran the same scenario while
the separate director fix was being completed. Its source hash changed during
execution, so this run is deliberately excluded from final acceptance. It does
provide a useful integration contrast: after a VIP died at second 60.2, two
attackers selected `vip_mop_up` while a third teammate continued guarding. The
objective and travel reviewers reported no findings. Main server CPU time was
7.48 seconds over the 120-second match; tick p95/p99 were 0.612/1.034 ms. CPU
time here excludes the separate bot worker process.

## First frozen-source native candidates

All three candidates used eight process-worker bots, full native gameplay and
seed 23. Production hashes were identical across the three runs, with no source
or runner changes during any run. The JSON reports and complete 10 Hz traces
are under `out/bot-natural-modes-20260921/`; `candidate-summary.json` separates
automated gates from the additional manual carrier finding below.
Before the follow-up run, the original JSON, JSONL and log artifacts were copied
to the corresponding `*-candidate1` names and their SHA256 digests verified.
The original summary is retained as `candidate1-summary.json`.

| Artifact stem | Simulated seconds | Main-process CPU seconds | Tick p95 / p99 ms | Geometric eligible / candidate windows | Observed objective outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| `vip-alcatraz-candidate` | 120 | 11.000 | 0.978 / 1.701 | 4 / 256 | Both VIPs survived; guarding and attacking exercised |
| `vip-chicago-candidate` | 120 | 8.625 | 1.180 / 1.668 | 0 / 166 | One VIP killed, one round scored, next VIP round started |
| `ctf-castlewars-candidate` | 240 | 22.641 | 1.190 / 2.089 | 12 / 522 | Two flag pickups, 92 carrier samples; no completed capture |

CPU values exclude the separate worker process. All automated objective,
geometric loop, support and water-stagnation findings were empty. Objective
telemetry covered all 9,600 / 9,600 / 19,200 actor samples respectively.
The geometric gate only accepts sustained quiet travel toward a stable distant
goal; Chicago had no eligible windows, so its zero findings do not establish a
blanket absence of every possible loop. Stationary navigation intervals reached
at most 3.9 seconds on Alcatraz and 5.1 seconds on Chicago. Useful stationary
guards are excluded from that measure.

Chicago provides the complete VIP outcome: VIP 6 died at second 61.9, team 3
scored at second 79.2, intermission ended at 86.2, and the next VIP selection
completed at 96.2. The Alcatraz run contains no VIP death or round victory.

The separate `settled-gaze-review.json` uses only continuous ordinary travel
after a 0.75-second settling interval. Its baseline Alcatraz 1,179 samples peak
at 16.945 degrees absolute pitch; candidate Alcatraz 2,305 samples peak at
9.126 degrees, and candidate Chicago 1,870 samples at 0.441 degrees. None exceed
45 degrees. This filter excludes combat, excavation and their immediate
handoffs; the different match outcomes prevent treating these as a universal
measure of subjective movement quality.

### Manual CTF follow-up required

The first carrier switched to its capture base on the next worker snapshot and
moved 20.4 blocks toward home before being killed. The second carrier selected
the correct home goal but retained a pickup-side excavation continuation. At
seconds 138.5–143.3, bot 1 repeatedly attempted rejected spade attacks on cell
`(167, 267, 225)` while remaining nearly stationary; damage stayed at 3 and
topology at 85. It died without making return progress. Normal CTF rejects
shooting/spade attacks while carrying the burdensome intel, so a home goal alone
does not establish an executable return route. The trace records rejection as a
boolean, not a detailed reason; the authoritative restriction is verified in
`CombatRuntime.handle_shot`.

This episode is shorter than the ten-second goal-abandonment gate and did not
trip it. Consequently, passing the automated reports does **not** mark this CTF
candidate fully accepted. Carrier route/capability handling requires a focused
follow-up, and no completed CTF capture has yet been demonstrated by these runs.

The follow-up reviewer now rejects stationary carrier excavation with at least
three distinct rejected melee feedback frames over three seconds and unchanged
observed target state. Accepted work, actual target changes, body progress and
sample gaps finish the episode; repeated copies of one rejection do not count
as new attempts. It detects the actual candidate1 failure at seconds
138.4–141.5 with five distinct rejections. The expanded review suite passes
43 tests. New traces also record the authoritative carried entity, burden,
shooting permission and feedback reason. This closes the gap where a correct
debug goal could hide an unusable execution route.
