# Purposeful bot gaze

The previous motor added difficulty-dependent combat hand error to every look,
including an unchanged travel heading. A deterministic eight-second motor
benchmark at 10 Hz measured 29–33 yaw reversals during the final seven seconds
of straight travel. The motor was repeatedly correcting error it had generated.

The director now distinguishes existing authorized intentions:

- Longer route travel and noncombat focus use no random aim error. Small
  direction changes have a 1–2 degree yaw deadzone and a brief, personality-based
  dwell. Changes of at least 12 degrees immediately update the committed gaze.
- Purposeful turns limit acceleration changes, including braking into a heading.
  Existing per-bot speed and acceleration limits remain upper bounds. Movement
  requests remain in world space and are converted to native yaw-relative keys.
- Short authorized route steps (at most 2.25 blocks from the worker's source to
  its waypoint) use prompt, noise-free traversal aiming. Their bounded accurate
  controller bypasses relaxed gaze dwell, as terrain actions do.
- Visible combat retains its previous tracking, reaction, alignment and difficulty
  noise contract. Selected world-cell actions bypass gaze dwell and aim precisely.
- No-look decisions hold the current orientation and clear angular momentum.
  No random scanning, extra world probes, or unbounded history was added.

`tests/test_bot_gaze_dynamics.py` supplies the repeatable measurement. Reports
are `out/bot-field-repro-20260921/gaze-motor-before.json` and
`gaze-motor-after.json`; both use the same fixed profile and noise seeds.

| Normal-difficulty motor measurement | Before | After |
| --- | ---: | ---: |
| Straight-heading yaw reversals in seven seconds | 33 | 0 |
| Straight-heading dwell below 2 degrees/second | 14.3% | 100% |
| Two route-corner sequence reversals | 24 | 1 |
| Corner peak angular jerk, radians/second cubed | 289.28 | 79.62 |
| Time to within 3 degrees of a 90-degree travel turn | 0.5 s | 1.1 s |

The single remaining corner reversal follows the intended 90-to-45-degree
direction change. These corner jerk and settling numbers describe longer travel
and guard focus; combat, precise actions and short traversal steps do not use
the slower travel turn. All noncombat gaze modes remove random hand error.
Native landing, ordinary live steering, aim alignment and VIP marker
lifecycle regressions are checked separately. These measurements establish motor
behavior; full-match movement and visual acceptance require their own traces.

The native London excavated shelf replay initially exposed a regression: applying
the relaxed turn to every short step delayed its eight-block escape to 28 seconds.
Each tight raised step could complete before the camera turn settled, changing
the effective native key heading during that step. Keeping prompt traversal aim
for short steps restores the original 14.33-second escape, below the unchanged
15-second limit. Extra direction probes and altered key quantization were tested
only as runtime experiments and were not adopted. The five native pocket cases
pass with the existing health, displacement and deadline assertions intact.

Focused final checks: 33 gaze/live-travel tests, five native pocket replays,
26 aim/water/shore/VIP checks, and both native Miner/partner construction gates
pass. The cooperative harness supplies its fixture destination through the new
per-brain mode-policy boundary; production navigation and physics stay active.

Objective pickups also invalidate stale weapon aiming. `PlayerSnapshot.can_shoot`
mirrors the live combat rule: a burdensome carrier may shoot/dig only when its
mode permits shooting with intel. The director checks this before aiming, in the
60 Hz observation hook, and before queued commitment. It clears pending shots,
digs, their cooldown look, bursts and weapon-owned replication pulses; unrelated
building and deployable actions retain their existing service permissions.
Ten focused lifecycle tests include pickup between motor and commit, a completed
shot followed by a tool switch, and the mode that permits carrier shooting.
