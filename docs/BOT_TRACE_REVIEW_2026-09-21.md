# Native travel trace review, 2026-09-21

`scripts/bot_trace_review.py` reports suspected travel loops and separate
ground-travel pitch statistics. It is a diagnostic, not a bot-quality gate:
no findings cannot establish that bots fight, navigate or cooperate well.
The retained JSONL and reported intervals must remain available for inspection.

## Known false clean result retained

The original review of
`out/bot-field-repro-20260921/egypt-final.jsonl` missed real repeated travel.
It required 80% grounded movement in a 20-second window. Native bot 2 at
simulation time 60–90 seconds repeatedly walked, jumped and dropped along a
terrace: only 170 of 301 samples were grounded. It traveled 199.80 blocks,
ended 2.70 blocks from its starting position and gained only 1.57 blocks
toward an unchanged distant goal, with no combat or terrain action.

The corrected reviewer includes airborne WALK/JUMP/DROP/CROUCH traversal and
explicit navigation planning/recovery pauses. It checks 20- and 30-second
windows, rejects combat/work, deliberate holds, respawns, sampling gaps and
intermediate goal changes, and distinguishes two kinds of suspect interval:

- Repeated coverage: at least 20 blocks traveled, less than 6 blocks net
  displacement, less than 2 blocks goal progress, and at least 70% of the
  latter half's two-block cells also present in the first half.
- Closed return: the same net displacement/progress limits, at least 100
  blocks traveled, but without the repeated-cell criterion. A legitimate
  unsuccessful detour can also satisfy this condition; inspect the trace.

The revised report is `out/bot-field-repro-20260921/egypt-final-reviewed.json`.
It has 405 candidate windows and 113 eligible windows. It flags bot 1 at
15–35 seconds, bot 2 at 60–90 seconds, and bot 5's closed return at 70–90
and 60–90 seconds. Overlapping windows describe the same episode rather
than independent failures. The previous clean result is not valid evidence
that this match avoided loops.

`tests/fixtures/bot_trace/egypt_terrace_loop.json` retains bot 2's positions
(rounded to four decimal places), traversal context, stable goal, source
path and original JSONL hash. It is a regression counterexample used to
correct these diagnostics, **not** independent acceptance evidence for a
subsequent navigation fix.

## Scope and timing

Pitch uses the native orientation vector's direction, with `atan2` so its
magnitude cannot distort the angle. Only grounded movement without combat
or terrain ownership contributes. In the retained match, 8,101 such samples
include two above 45 degrees (bot 3, maximum 52.19 degrees) and no qualifying
greater-than-10-degree pitch sign reversals. This does not describe aim
during combat, falling, construction, or deliberately stationary holds.

Trace `t` is simulation elapsed time. The original trace has no per-row
wall-clock time or intent freshness, so a long host scheduling delay or an
expired retained intent cannot be reconstructed from it. Future runtime
traces also record `wall_t`, `intent_fresh`, `intent_age_seconds`, `spawned`
and `wade`; stale intents and wading are excluded when those fields exist.
The report exposes whether freshness and wall-clock fields were recorded.
Window durations still use simulation seconds, and therefore cannot be
called uninterrupted wall-clock durations. The runtime report also records
the effective bot seed and reviewer SHA-256 separately from production
bot-source hashes.

The runtime harness explicitly sets administrative bot population and the
requested bot count; it does not exercise lobby-driven backfill. Its mode
argument defaults to TDM even with `--config`, and an explicit `--seed`
seeds both global spawn randomness and bot profiles. A normal Create Match
seeds bot profiles but does not seed global spawn randomness. Config and
seed provenance must accompany any claimed reproduction.

Validation: `py -3.12 -m pytest tests/test_bot_trace_review.py -q`
(11 passed); the recorded native trace was re-reviewed without changing
production bot source or the original trace/report.

## Optional support commitment diagnostic

The additional `suspected_support_stagnation` list uses the worker's
`navigation.goal_progress_age` rather than local displacement, coverage age
or exact support coordinates. It requires more than 30 seconds without
recorded strategic progress and at least three fresh, alive support-owner
observations outside the policy's three-block arrival radius/vertical
tolerance. Combat, active/pending terrain actions, wading, arrived holds and
breach-assistance queues do not contribute observations. Brief exclusions,
missing samples, planning waits, recovery movement and small formation-goal
changes do not erase the worker's progress age. The report includes each
bot's maximum observed support age. Its start/end span includes excluded
interruptions; `observed_support_samples` is the actual evidence count.

`london-diagnostic-5.jsonl` bot 3 at 39.8–87.7 seconds has 480 fresh support
samples, no combat/work/wading, 176.11 blocks traveled and only 5.31 blocks
net displacement. Its goal jitters over approximately 9.5 by 9.0 by 5.5
blocks while progress age reaches 47.875 seconds. Geometric loop windows
exclude those changing goals, but the support diagnostic catches the
commitment. The regression fixture is
`tests/fixtures/bot_trace/london_support_stagnation.json`.

This is a separate suspect signal, not a proof of an unreachable current
endpoint. `corridor_endpoint_failure` means a stored endpoint failure exists;
it need not match the latest moving goal. The report records that field
when available but does not rely on it. Goal-age resets can also hide a
repeated unsuccessful commitment: candidate 6 London bot 3 demonstrates
this limitation. A clean age check alone is therefore insufficient evidence
of useful team progress. Physical-stagnation interpretation also needs care:
an accepted melee action is not, by itself, proof of a terrain change, and
these traces do not record local topology progress.

Updated reviewer validation: 16 tests passed, including native support-goal
jitter, short interruptions, true progress resets and deliberate holds.

## Physical task evidence

Each bot now reports `longest_stationary_navigation`, a diagnostic-only
measurement of its longest contiguous fresh navigation/recovery/excavation
run within one block of its starting position. It ignores deliberate
arrivals, non-navigation holds, visible combat and discontinuous samples.
Unlike the geometric loop check, it allows zero movement and route-breach
work so a rejected dig or repeated planning wait remains visible. It does
not change either failure-candidate list or assert that a long run is a bug.

The record includes newly observed accepted/rejected feedback counts,
distinct from an old accepted tuple retained for many seconds. A successful
gateway swing is not proof of a removed block. New runtime traces also
contain `topology_version` and the authoritative current/pending
`terrain_target` cell, solid flag and damage. The reviewer counts observed
topology and per-target state changes when available; missing fields are
reported as unavailable. Global topology may change due to another bot, and
target samples can miss changes between polls, so these are evidence for
inspection rather than proof of local productivity.

Candidate 6 remains declined evidence: London bot 3's changing optional
goal repeatedly resets the worker age, while Egypt bot 0 stays at the same
XY with crouch-height oscillation and repeated melee feedback. The added
physical metric exposes the latter without misrepresenting accepted swings
as confirmed terrain progress. Both original JSONL/runtime reports remain
unchanged; the separate `*-reviewed.json` artifacts contain current metrics.

Updated reviewer validation: 18 tests passed, including feedback-event
deduplication, terrain evidence, physical movement, holds and sampling gaps.

## Stable support-progress clock

New traces expose `navigation.support_no_progress_time`. The reviewer
prefers this field whenever present, including zero, and falls back to
`goal_progress_age` only for older traces. Findings name their `age_basis`
and `max_support_stall_seconds`; per-bot `support_stall_max_seconds` and
`support_age_bases` make the metric's source explicit. The old general
goal-age maximum remains available as a separate measurement.

The new clock accumulates active optional-support decision time without a
four-block improvement in the actor's distance toward a stable
`support_progress_anchor`. It pauses across contact/task gaps longer than
one second and during breach work. Arrival, meaningful actor progress, a
support-target relocation of at least sixteen blocks, or a cooldown reset
the clock. Consequently its seconds are active worker support time, not a
promise of contiguous wall-clock observation. The same greater-than-thirty
threshold and ownership/arrival exclusions apply.

Regression coverage retains the earlier 47.875-second diagnostic and also
checks that a growing actor-progress clock is detected when the old
goal-progress clock is repeatedly zero. A present zero in the new clock
must never fall back to an old stale age. Updated validation: 20 tests passed.

## Water recovery episodes

`water_recovery_episodes` starts an episode on native wading and ends it
only after one continuously observed second of stable dry footing. New
traces use `dry_safe` (grounded, not wading, and the authoritative spawn
safety check); older traces use grounded and not wading. Every record names
its `dry_footing_basis`. Brief dry flashes do not count as recovery, and
sampling gaps cannot establish a continuous dry second. Death or the trace
ending closes an unfinished episode with the corresponding reason.

Each episode reports duration, observed time, wading fraction, path length,
net displacement, spatial bounds, water-recovery role fraction, and the
available topology/target-state changes. The diagnostic-only
`suspected_water_recovery_stagnation` list contains episodes lasting at
least thirty seconds, at least 80% wading, and confined to a bounding-box
diagonal of at most 32 blocks. At least 90% temporal/state sampling coverage
is required. These are inspection candidates, not automatic proof of a
navigation defect; real work is reported but does not suppress the finding.
No existing travel or support gate criterion changed.

The retained native candidate 7 London bot 3 episode runs from 42.7 to
119.9 seconds: 77.2 seconds, 96.2% wading, 492.867 blocks traveled, 11.322
blocks net displacement, and a 27.352-block bounding-box diagonal. Its
brief dry footholds do not end the episode. Two directly observed target
state changes confirm some actual work without establishing a successful
exit. The full-rate regression fixture is
`tests/fixtures/bot_trace/london_water_recovery.json`; its source/hash are
retained. Egypt candidate 7 has no water episodes.

Updated reviewer validation: 24 tests passed, including the recorded dry
flashes, genuine long-swim progress, authoritative `dry_safe` precedence,
and sparse-sample limits. This addition changes only offline diagnostics.
