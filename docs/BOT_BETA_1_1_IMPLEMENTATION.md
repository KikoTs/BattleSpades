# BattleSpades Beta 1.1 — cooperative bots

Implemented in the active `SimpleBotBrain` server path. This is a lightweight
worker-side task layer over the existing native movement, combat, class and
authoritative action systems. It uses no model service, LLM or GPU inference.

## Behavior

- Medics form temporary pairs with a single leader. One claims an injured
  patient and can place a real medpack during visible combat; the other keeps
  fighting. Injured bots approach existing usable packs. Healing is confirmed
  by health changes; stock, the original 25-point heal and three uses are real.
- Marksmen choose a nearby reachable firing point, optionally build equipped
  rear cover, secure a safe approach with their selected mine or radar, and
  occupy the perch. Mine and radar are equipment alternatives. Close threats,
  destroyed construction and quiet lanes cause abandonment or relocation.
- Miners open short validated body-clear breaches and build complete short
  crossings with an affordable dry landing. Partners cover from outside the
  work area and physically use the completed route. Reaching the near bank
  cannot count as a successful crossing.
- Equipped builders make useful cover; turret-capable builders can establish
  a strongpoint. Fighters support nearby projects and humans' pushes without
  cyclic follow chains or unlimited followers. Existing class-valid jetpack,
  parachute, weapon and excavation movement remains in the normal motor.
- Bots investigate uncertain sounds and remembered contacts. Hidden players
  do not update remembered positions. Exposed enemy turrets/radar can be shot
  through normal combat, using the actual entity hit volume and a clear ray.
- Depleted bots seek visible real health, ammo or block crates as appropriate.
  A disappearance alone does not count as successful resupply.
- Friendly mischief is a rare single decorative block after useful work,
  close to friendly activity. It never removes footing or deploys explosives.
  The local geometry must preserve exits; incomplete crowded perception
  disables it. The team cooldown is 90 seconds.

## Limits and failure handling

Tactical evaluation is staggered around 2 Hz. Each life has at most 24 contact
memories and 16 recent outcomes; the worker retains at most 128 life records.
There are at most three active projects per team, with a leader and up to three
partners. Logs retain only the latest 256 task events. Work reservations expire
and never restrict a human's actions.

Prefab geometry is loaded from the real KV6 registry at map setup. Candidate
counts, geometry size, footprints and walk checks are bounded. Optional
construction is limited to 256 proposed cells per team per ten seconds, with
at most 128 cells per action. Rejections consume this *planning allowance*,
not game inventory; they cannot generate a retry storm.

Every task commit has a request ID. Retries return the recorded result or
reject old requests without committing twice. Authoritative spawn generation
invalidates intents even for respawns without a death. Queued bot prefabs are
checked again between commit batches and cannot refund an old life into a
new inventory. Construction completion requires matching action feedback and
actual cells; deployables require an actual entity.

The aggregate path budget now applies to both thread and process workers.
Denied work preserves routes/frontiers and resumes fallback searches rather
than classifying scheduling delay as impassable terrain. Admission covers
surface routes and corridor slices; the existing separately bounded water
recovery search is not included in that counter.

## Configuration

```toml
[bots]
behavior_version = "cooperative"
friendly_mischief = true
path_requests_per_second = 24
```

`behavior_version = "classic"` selects the previous tactical behavior while
retaining authoritative inventory, lifecycle and planning-budget fixes.
`friendly_mischief = false` disables decorative pranks independently.

The beta uses conservative local projects. It does not introduce arbitrary
cross-map tunneling, low crouch tunnels, unbounded bridges, free resources,
turret repair/refill powers, or new class equipment. Explosive attack handling
and validated flight remain in the existing combat/traversal systems; a
general coordinated demolition planner is not enabled by this layer.

## Verification

All tests and runtime matches for this change are local; no connection is made
to the public legacy server. Detailed final run results and package checks are
recorded in [the final audit](../out/bot-beta-1.1/audit.md).

The final focused run passed **205 tests**, including native bridge/tunnel
traversal, healing, deployable accounting, construction lifecycle, task
transactions, planning admission, route invalidation and join greetings.
Local full-runtime matches passed at 12, 24 and 48 bots; the process worker
also recovered after a deliberate restart, and the classic fallback passed.
These are automated native/headless checks, not a visual gameplay review.

The final 12-bot London run measured a 1.18 ms server-tick p99 and 0.53 ms
bot-subsystem p99 on this machine. At 48 bots those figures were 3.16 ms and
1.24 ms: stable in the stress gate, but above the 0.75 ms bot soft target.
The shipped default remains 12 bots. Short local runs do not establish
long-session performance or hosting capacity on other machines.

The native construction gate uses the real motor, native player physics,
gateway, terrain commits and inventory. A four-block bridge costs four blocks;
an 18-cell tunnel credits the mined blocks while preserving floor and ceiling.
Both the Miner and partner must reach the actual far-side landing cell.

Useful commands:

```powershell
py -3.12 -m pytest tests/test_bot_cooperative_medical.py tests/test_bot_cooperative_projects.py tests/test_bot_cooperative_safety.py tests/test_bot_task_transactions.py tests/test_bot_cooperative_project_physics.py -q
py -3.12 scripts/bot_cooperative_project_physics.py --help
py -3.12 scripts/bot_runtime_smoke.py --seconds 60 --bots 12 --map London --worker thread --full-runtime --seed 211 --json out/bot-beta-1.1/local.json
```

Related details: [project geometry](BOT_PROJECT_SITES_2026-09-21.md),
[deployable accounting](DEPLOYABLE_INVENTORY_2026-09-21.md), and
[join greeting/build metadata](JOIN_GREETING_2026-09-21.md).
