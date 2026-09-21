# Beta bot behavior plan — 20 September 2026

Status: the cooperative Beta 1.1 layer is implemented and locally verified.
See [the implementation record](BOT_BETA_1_1_IMPLEMENTATION.md) for delivered
behavior, conservative limits, configuration and test evidence. This original
design remains the broader roadmap, not a claim that every proposed tactic
is enabled.

Goal: inexpensive, varied bots whose actions visibly respond to players and
the destructible world. The user's chosen destruction policy is occasional
friendly mischief, without trapping teammates.

The expanded brief includes Medic pairs supporting each other during combat,
Miner-created tunnels and efficient bridges toward the enemy side, and
Marksman outposts with rear cover and mined approaches. The features below
remain proposals for staged implementation, not claims about current bots.

## Findings in the active implementation

- Production uses `SimpleBotBrain` in `simple_worker.py`, via the thread or
  process supervisor. The older `worker.py` is a reference/rollback path;
  adding behaviors there would not improve the active planner.
- Preserve existing direct-VXL navigation, recovery, dig footprints, bounded
  searches, aim/recoil/bursts, loadout variation, mode policies, and the
  authoritative `BotActionGateway`. These are working foundations.
- `SimpleBotBrain.decide()` normally returns combat immediately when a
  visible target exists. There are exceptions inside combat for traversal
  and cover, but most opportunities cannot compete with that early choice.
- Tactical prefab cover accepts names containing `wall`, chooses a placement
  three blocks forward and waits 18–26 seconds between attempts. During
  combat it is reached when wounded, recently damaged and reloading. This
  explains a narrow behavior repertoire without proving its runtime frequency.
- `BotDirector` publishes bounded sensory events, but neither the active
  `simple_worker.py` nor its mode policies consume `frame.stimuli`.
- Existing profiles already contain aggression, caution, teamwork, creativity,
  reaction time and aiming characteristics. Extend their consequences rather
  than introduce another overlapping personality system.
- Action feedback reports kind, accepted flag, position, frame and timestamp.
  It does not provide a general task lifecycle or structured rejection reason.
- Capability inspection confirms Medic medpacks, Miner excavation/explosives,
  and Marksman landmines/radar are available. Landmines and radar share the
  Marksman's equipment slot: a plan cannot assume both are equipped.
- `DeployableActionService.place_medpack()` and `place_landmine()` validate
  class/tool, reach and support, but their inspected execution paths do not
  enforce/debit placement stock or the per-tool placement interval. The bot
  gateway and packet handlers call these methods directly. Shared authoritative
  accounting is therefore a prerequisite for frequent medical/mine tactics;
  worker-side cooldowns alone are insufficient. This is a planning finding,
  not a correction made in this document update.
- Configuration defaults: perception 10 Hz, decisions 8 Hz, main-thread bot
  budget 0.75 ms. The active thread constructor and simple process entry point
  discard `path_requests_per_second`; individual searches remain bounded,
  but this setting is not an enforced aggregate rate limit.

## Decision architecture

Use the existing mode policy to supply strategic intent. Add a small utility
selector for short tactical tasks. Utility means comparing a few legal actions
by usefulness in the current situation, not searching every possible future.

Pipeline:

1. Observe visible contacts, noisy sound locations, committed nearby terrain
   changes, accessible supplies, teammates and public mode objectives.
2. Update bounded memory: last-seen contacts with uncertainty, dangerous
   approaches, failed tasks, recent choices, and known local opportunities.
3. Generate at most a small fixed shortlist of legal tasks.
4. Rank by objective value, need, personality, likely success, travel/time,
   exposure, ammo/blocks, repetition and teammates already doing the job.
5. Commit to the chosen task; execute its steps through the existing motor
   and action gateway. Confirm outcomes from authoritative state.
6. Finish, interrupt, or fail with a reason; learn a small temporary preference
   and select again when appropriate.

Use hard preconditions before scoring: no resources, forbidden terrain,
unsafe friendly placement, unknown target or unavailable tool means ineligible,
not merely a lower score. Scores use bounded normalized inputs. Choose among
near-equal good candidates using a reproducible random seed; never roll a
completely new personality each decision.

Commitment and a switching margin prevent flickering between equally scored
actions. Permit explicit urgent interrupts for immediate danger and critical
objectives. Keep a single owner of locomotion and one owner of held tool/aim;
legal firing can accompany movement, but digging cannot fight the gun for aim.
Do not interrupt an unsafe airborne traversal midway merely to change goals.

## First behavior packages

| Package | Observable behavior | Completion evidence |
| --- | --- | --- |
| Observe and investigate | Turn toward a nearby shot, approach its uncertain origin, inspect last-seen exits, stop searching when evidence expires | Reaches a useful observation point or consciously abandons stale evidence |
| Engage, cover and relocate | Pick a firing position, peek, burst, seek cover before reloading, change angle after repeated failure | Gains a firing opportunity, reloads safely, or reaches an alternate angle |
| Dig with a purpose | Open a shooting slit, breach a short flank, clear blocked friendly movement, reach a known objective | Intended opening actually exists and is used; no indefinite digging at an unchanged voxel |
| Construct in short bursts | Place affordable cover, add a side piece if needed, use a ladder/platform/bridge where it enables movement | Accepted build reduces exposure or creates a validated usable route |
| Sabotage | Break observed enemy cover, approach a known turret from shelter, interrupt an enemy route | Known cover or deployable loses its tactical value; no map-wide support oracle |
| Cooperate | One bot breaches while another covers; help a wounded teammate; join a nearby player's push without body-blocking | Complementary tasks progress and release their reservations |
| Friendly mischief | Occasionally alter an unoccupied decorative corner or add a small unnecessary but passable construction near activity | Bounded edit completes without damaging footing, blocking exits or interfering with objectives |

Frequent construction should be contextual bursts followed by actual use,
not constant prefab attempts. Carry costs, original cooldowns, build reach,
spawn protection and normal ammunition still apply. When a player destroys
cover, a bot can retreat, switch placement or counterattack rather than
rebuilding the same wall forever.

## Parameters that produce recognizable individuals

Keep skill separate from temperament. A poor shot may still be a brave builder;
a good shot may prefer supporting a teammate. Suggested compact additions to
the existing profile are building appetite, sabotage appetite, curiosity,
patience and a low mischief tendency. Avoid dozens of unrelated random knobs.

Persistent temperament changes task preferences and commitment. Dynamic state
includes health, ammunition, block reserve, recent damage pressure, isolation,
known threats and objective urgency. Pressure rises from observed events and
decays; it affects willingness to peek or reload, not arbitrary aim jitter.

Recent action/site history penalizes pointless repetition. Repeated failure
changes approach; useful repeated actions remain allowed. Squad roles have
short leases and change when needs change. Small seeded timing variations
prevent synchronized firing, rebuilding and patrol turns.

Keep identity preferences across respawns, but clear live targets, active tasks
and invalid reservations. Only bounded, uncertain area knowledge may persist
within a match. Match/map boundaries clear spatial memories and team plans.
Personality never reveals hidden players or their present positions.

## Environment and teammate interaction

Represent opportunities as small records: position, type, observed source,
benefit, estimated cost, expiry and relevant local terrain revision. Discover
them around current contacts, routes and events, not by scanning the whole map.

Track recently built structures/provenance in a bounded sparse registry from
committed gameplay events. Team color is not reliable ownership. For authored
terrain or missing provenance, use geometry and observed tactical use; avoid
pretending every block has a known owner.

Reserve a worksite, cover slot or flank briefly so the team does not all build
on the same cell. A reservation suggests coordination, never blocks a human
action. Release it on death, expiry, changed terrain, disconnect or failure.
Bots react to a player's visible fighting, building and movement without
requiring new chat commands or a client protocol extension for beta.

## Small squads and persistent local projects

A squad is a temporary cooperation agreement, initially two to four bots,
around a nearby purpose. Formation and construction counts are tunable starting
limits. A bot can act independently when no compatible partner is available.
The agreement must make progress without waiting indefinitely for a Medic,
Miner or other missing role.

Maintain one small project record shared by cooperating bots: project ID,
map/mode generation, observed site, intended benefit, current stage, owner,
participant leases, usable approach/exit points, estimated resource cost,
committed changes, last progress and expiry. Keep a short, bounded task graph;
there is no general search across every possible combination of teammates.

Typical stages are assess → prepare → build/breach → occupy/use → maintain →
relocate/retire. Each stage has explicit completion and failure conditions.
"Wall placed" does not complete an outpost until the sniper can occupy it and
see a useful lane. "Bridge built" does not complete a crossing until its
endpoints and the route across it are physically usable.

Assign roles by equipped capability, distance, resources and temperament.
Choose one current route leader or a fixed rally point to avoid two bots
chasing each other's moving formation point. Followers keep lateral separation,
yield in narrow corridors, and briefly hold outside another bot's work area.
Use short-lived reservations rather than rigid formations. A wounded leader
can hand over; a dead leader loses its lease immediately.

Separate movement, observation and tool roles. A Medic placing a pack needs
another member watching the approach. A Miner digging must aim at the actual
voxel while a partner aims down the lane. Do not grant simultaneous incompatible
weapon actions just because a project has multiple tasks.

An unfinished structure remains ordinary world terrain if its builder leaves.
No magical rollback, cleanup or free replacement follows project cancellation.
A nearby teammate can adopt an observed useful project after revalidating it.
Deaths release participation, not erase a physical bridge. Full match/map
changes retire all project state; stale jobs cannot execute in the new world.

## Class-specific projects

### Medic: combat partners and small aid stations

**Medic pair:** two nearby Medics may travel together for a push or escort.
One follows a lead/rally point while the other watches another angle. When
either is hurt, select one pack owner using available stock, placement safety
and need; the other covers. A shield can be used only through existing legal
held-tool behavior, without inventing an aura or protection bonus. After a
real heal, resume the push or withdraw according to danger and resources.

**Combat deployment:** recent damage, expected near-term fighting, nearby
wounded allies and absence of a useful existing pack make deployment valuable.
An exposed Medic may prioritize reaching cover first. The current visible-enemy
early return must allow an urgent medical task to compete with shooting.
The bot equips and places the real medpack at a supported, reachable location;
patients move within its actual touch radius. No remote healing or arbitrary
health-crate spawning is introduced.

**Triage and distribution:** prioritize reachable high-need allies without
letting one patient consume all planning attention. Nearby existing packs,
remaining uses, reserved patients and danger affect the choice. A second
Medic uses another angle or keeps a reserve instead of duplicating a pack on
the same frame. Healthy bots should yield access to injured players.

**Field aid point:** place a pack behind a battle line or at the safe end of a
tunnel, optionally ask an available builder for a side wall, and keep an exit.
Move the aid point when the fight moves or the site becomes exposed. With no
stock, support with ordinary combat or visit a known valid restock source;
there is no infinite healing chain between two Medics.

Current deployment explicitly supplies 25 health per touch and three uses
from `shared.constants`; the behavior class has stale full-heal wording and
a different default. Implementation should clarify this documentation and test
the actual service-supplied values, not change healing balance for bots.

### Miner: assault tunnels, breaches and efficient crossings

**Assault tunnel:** compare the expected time/risk of an existing route with a
bounded tunnel segment toward an observed enemy position, public objective
or enemy-side approach. Work backward from a plausible exit so a tunnel has
a purpose. An unknown or unaffordable continuation is a reason to stop and
reconsider, not keep excavating toward a coordinate forever.

Use the actual Super Spade/drill/other equipped tool and its recovered dig
footprint and cadence. Preserve the standing floor, check ceiling clearance,
and record progress from committed removals. Start with ordinary body-clear
tunnels. Purpose-built low crouch tunnels require their own traversal and
combat regression coverage before enabling them.

**Breach team:** one Miner cuts the opening, a partner covers behind it, and a
Medic can establish support at the entry. Followers wait clear of the tool
footprint. On breakthrough, confirm the opening and then hand movement to the
assault member. If the defender rebuilds it, compare continued breaching with
another entrance instead of locking both teams into an endless repair loop.

**Bridge crew:** choose a reachable landing and price the complete useful
crossing, including required support and exit access. Compare legal block
lines with available platform/bridge prefabs. Divide non-overlapping reachable
segments among builders while another teammate guards the approach. Building
from the far bank is an option only after someone actually reaches it.

"Faster bridges" means better selection, batching where the ordinary player
action supports it, and parallel work—not a bot-only placement speed or free
blocks. Stop before extending into an unreachable endpoint. Builders approach
the enemy side while respecting the actual spawn/objective protection rules.
Navigation must recognize and use a completed bridge; otherwise the project
has achieved nothing for the team.

**Demolition:** equipped dynamite/C4 can replace slow digging where it has a
clear benefit. Reserve a safe work zone, choose a retreat before placement,
and release teammates only after the authoritative explosion/terrain update.
Do not rely on a protected teammate surviving the blast or on globally known
enemy structure supports. Large-collapse cost belongs in the decision budget.

### Marksman: outposts, rear security and relocation

**Choose a site:** sample a few reachable positions near a relevant lane.
Score useful firing angles, exposure, range, rear approaches, access/escape,
nearby teammates and travel/build cost. Highest elevation alone is not a good
outpost. A position must work with the actual scoped weapon and current map.

**Prepare and occupy:** use existing cover first. If needed, place a small
platform or side/rear cover from the equipped prefab set. Leave a firing gap
and an exit; cover must not intersect the bot's body or obstruct its weapon.
Reserve the perch so several snipers choose complementary angles rather than
stacking. Proceed in useful stages: an immediate rear wall may be enough;
elaborate fortification is optional if time and resources permit.

**Secure approaches:** a mine-equipped Marksman may place a limited number of
mines on plausible enemy access routes—such as a rear stair, side entrance or
the turn into an outpost. Pick sites with an actual traversable approach,
not a random ring around the sniper. Retain the normal arming delay and stock.
Current mines trip on enemies rather than their placing team; explosion damage
and terrain collapse still require friendly-route and structural checks.
Avoid mining the sole friendly exit or supports needed by the outpost itself.

A radar-equipped Marksman instead deploys radar where useful and uses only
its existing game-sanctioned information. A mine-capable partner may secure
that outpost; one bot never gains both slot alternatives for convenience.
Hidden enemy mines are not automatically revealed to the AI by the registry.

**Fight and adapt:** alternate sensible observation/shot/reload periods, check
the rear when alerted, and vary the firing point when threatened. A mine firing
or nearby explosion is evidence to investigate, not a live coordinate feed
for the attacker. Prefer relocation when the firing lane has gone quiet, the
objective moves, close threats approach, or repeated attacks destroy the site.
Camp patience and danger tolerance vary by bot; no arbitrary timer forces
abandonment of a productive position.

### Engineer: forward strongpoints and route maintenance

**Strongpoint:** choose a useful defended crossing or battle approach, build
modest cover, and deploy a turret only if that secondary was selected. Check
its actual firing lane so a new wall does not immediately obstruct it. A
turret-equipped Rocketeer can contribute the same capability where legal.

**Route maintenance:** restore a known useful damaged bridge segment or clear
a friendly passage using ordinary build/dig actions, with other bots holding
off the worksite. A turret cannot be repaired or refilled unless a real game
action supports it; if it is lost or exhausted, replan within actual stock.
Recognize when repeated rebuilding is a losing fight and move the strongpoint.

### Commando: assault and covering movement

**Crossing cover:** take a firing position while the Miner builds, fire legal
bursts at visible targets or briefly at a last-seen opening with uncertainty,
then advance when the crossing is ready. Suppressive behavior does not grant a
new suppression damage effect or permit tracking through walls.

**Breach entry:** approach a finished opening, choose the equipped close-range
or explosive option where appropriate, and move through with space between
teammates. A Commando with the parachute can use a validated descent route;
the chosen equipment slot still determines whether grenades are available.

### Rocketeer and Specialist: opportunistic flanks

**Rocketeer:** use validated pack-specific gaps or elevated routes to create
another angle, preserving enough fuel/landing safety for the chosen task.
Do not expand to arbitrary vertical routes until planner estimates agree with
the actual server resource profile and movement capabilities of that bot.

**Specialist:** exploit a Miner's opening, pressure a confined enemy position
with the selected close-range weapon, or use an equipped area projectile to
discourage an observed approach. It must keep a friendly route through the
fight and respect actual self/team damage. Grenade-launcher, chemical and
sticky options depend on selected slots, not class name alone.

## Combined encounters and variation

These are project patterns with optional roles, not fixed cinematic scripts:

| Situation | Possible team response | What changes the response |
| --- | --- | --- |
| Enemy controls a river crossing | Marksman watches the bank, Miner builds a short crossing, Medic supports the near side | Landing becomes unsafe, stock runs low, enemy damages bridge, an existing crossing becomes preferable |
| Player builds a bunker | Commando draws visible fire, Miner selects a side breach, Medic stays at a safe rally point | Defender changes exit, a teammate opens another route, breach cost rises, critical objective moves |
| Two Medics meet a wounded push | One deploys, one covers; they alternate responsibility as the push advances | Existing pack is usable, patient relocates, remaining stock/uses change, safe approach disappears |
| Marksman finds a productive perch | Build rear cover, secure an enemy access route with mines or ask a capable partner, occupy the firing point | Lane becomes irrelevant, site is compromised, supply path closes, close-range threat arrives |
| Human begins a useful bridge | An available builder takes a reachable unfinished segment; a fighter watches the approach | Human changes direction, new geometry invalidates the worksite, project no longer reaches useful terrain |
| Friendly objective comes under pressure | Retain a defender, strengthen a useful approach, move medical support nearer cover | Threat shifts, too many allies already defend, another objective becomes urgent |
| Enemy discovers an assault tunnel | Hold an observation point, choose a second opening or temporarily defend the exit | Fresh evidence favors another route; no need to rebuild the same failed plan |

Cap active construction projects per team and permit only one active project
membership per bot initially. Brief support can be supplied without permanently
joining. A team should retain ordinary fighters while specialists work.
Project costs and scarcity make choices matter; unrestricted deployment would
erase the need for teamwork.

Small social behavior can arise from a temporary preference for teammates who
recently helped or advanced together. Bound and decay it within the match;
it must not produce permanent bodyguard chains, follow-loop pairs, retaliation
against friendly players, or universal squads of the same composition.

Mischief remains a rare personality expression after useful team needs. A bot
may add a harmless decorative flourish to its completed site, but never place
explosive pranks, seal players in, sabotage active friendly projects or use
team reservations to take control away from a human.

## Class/project data and hardening prerequisites

1. **Capability records:** derive equipped tools, allowed prefabs, stock,
   cooldowns, placement support/range and movement profile from current
   authoritative data. Class names only influence preferences. Add bounded
   observed support state needed for triage and project decisions, including
   pack uses, deployment status and accessible routes where appropriate.
   Exact stock is only needed for self/cooperating teammates; it does not
   expose hidden opponent inventory or future choices.
2. **Shared inventory enforcement:** audit medpack, landmine and other
   deployables at the common service boundary. Validate then commit entity,
   inventory and placement cadence consistently for humans and bots. Rejected
   or duplicate attempts cannot consume twice, create free devices or refresh
   a life. Entity limits and resupply paths must be explicit and tested.
3. **Geometry metadata:** build/cache prefab footprints, support contacts,
   usable apertures and traversal dimensions outside the decision hot path.
   Describe geometry first; assign tactical roles after validation. Filename
   matching alone is insufficient for an outpost, bridge or rear wall.
4. **Project confirmations:** attribute accepted actions and later world
   results to project/task IDs. Support depletion, mine detonation, destroyed
   terrain, a missing participant and a changed objective invalidate only
   relevant steps. Resupply and stock consumption are actual server actions.
5. **Coordination safety:** short role/site leases, current life generation,
   leader handover, duplicate-heal/build prevention, narrow-space yielding,
   safe demolition withdrawal, and urgent interrupts need focused invariants.
6. **Behavior parameters:** reuse caution/teamwork/aggression and add only
   consequential knobs: partner commitment, healing reserve, project effort
   limit, outpost patience, mine coverage preference and rebuilding tolerance.
   These affect selections within legal constraints, never health, fire rate,
   reach, stock or hidden information.

Friendly-mischief preconditions are deliberately stricter than enemy sabotage:
no friendly damage, occupied footing removal, bridge/support demolition,
spawn/objective interference or enclosure of a teammate. Validate prefab
footprint and local escape corridors; if the bounded check cannot establish
safety, reject the prank. Keep it infrequent with a team-wide cooldown and a
small mutation allowance. Do not promise a global connectivity proof from a
local check: ambiguous structural edits are ineligible.

## CPU and memory budget

The expensive parts are raycasts, path searches, placement validation, terrain
collapse, collision updates and replication—not a handful of utility scores.
Measure all of them, including costs after an AI action is accepted.

- Keep physics at its existing cadence. Reuse existing perception/combat
  decisions; reconsider strategic work more slowly, provisionally 1–2 Hz,
  unless an important event invalidates it. These rates are tuning proposals.
- Cap shortlisted actions, candidate positions, raycasts, path expansions,
  pending jobs, memories and reservations. Expensive jobs resume in slices.
- Enforce aggregate planning/query budgets across both worker backends, with
  fair round-robin service and reserved urgent work. No bot-ID starvation.
- Share immutable geometry/opportunity caches where useful. Team contact
  knowledge remains team-specific. Invalidate geometry by affected region
  rather than redoing every plan after an unrelated voxel edit.
- Coalesce superseded requests and terrain events. A stale build or shot is
  cancelled, not executed after a worker backlog. Under load, defer optional
  construction/mischief before combat, collision and survival handling.
- Add per-bot/team build and demolition allowances, including footprint size
  and structural-risk checks. Tiny actions can trigger large collapses, so
  mutation cost cannot be bounded by action count alone.
- Keep the existing 0.75 ms main-thread target as a budget to verify, not a
  measured guarantee. Benchmark worker CPU, main-thread p95/p99, whole-server
  tick time, packet traffic and memory at 12/24/48 bots on the actual host.
- A Python thread does not remove CPU cost or GIL contention. Keep one bounded
  worker; compare the existing process option only if profiling warrants it.
  No LLM, GPU inference or expensive online learning is required by this plan.

## Hardening before expanding behavior

Introduce a small task lifecycle: proposed → approaching → executing → waiting
for world confirmation → completed/failed/interrupted. Typed feedback separates
protected area, no stock, obstructed aim, unsupported placement, stale world and
cooldown from actual completion. Retry limits and backoff are reason-specific.

All intents retain map/mode/life generations, expiry, current ownership and
finite/bounded parameter checks. New task IDs avoid duplicate commits. Queued
prefab requests are not counted as completed until actual placement finishes.
Failure feedback should stop a bad plan, not turn every failure into a generic
navigation recovery. Preserve existing exceptional-worker recovery.

Extract new decision, task, opportunity and memory modules instead of adding
another large block to the existing 4,000+-line worker. Suggested modules:
`task_selection.py`, `tasks.py`, `opportunities.py`, `memory.py`,
`team_tasks.py`, and `behavior_metrics.py`. Keep `profiles.py`, `messages.py`,
`director.py` and `gateway.py` as the integration boundaries. Implement only
the modules needed by the first tested behavior slice.

## Delivery sequence and beta gates

1. **Measure and expose reasons.** Baseline current bots; record task reason,
   duration, useful outcomes, rejected actions and all relevant CPU costs.
   Wire the ignored aggregate planning budget and verify both backends.
2. **Memory and task selection.** Consume sensory events, add commitment and
   bounded repetition memory, and retain current combat/navigation fallback.
3. **Medic pair first.** Fix shared deployment accounting, add brief partner
   leases and medical task selection during combat. Verify the real pack
   heals a reachable ally, stock/uses deplete, the partner covers and both
   resume their purpose without duplicating placements or chasing each other.
4. **Marksman outpost.** Add bounded site selection, geometry-aware cover,
   mine/radar alternatives, occupancy and relocation. Verify complementary
   perches, preserved exits, normal mine behavior and no free slot equipment.
5. **Miner assault project.** Add costed multi-step excavation/crossings and
   route uptake. Combine cover, breach/flank and medical support around an
   enemy bunker. Test broken landings, failed exits, destroyed cover and
   ordinary human changes to the worksite. Keep low crouch tunnels separate.
6. **Broader mixed squads.** Add Engineer strongpoints/maintenance and
   capability-valid Commando/Rocketeer/Specialist support, with graceful
   fallback when the map, roster or selected loadout lacks a needed role.
7. **Sabotage and occasional friendly mischief.** Add only after provenance,
   resource accounting, local safety checks and action budgets work.
8. **Local beta soak and tuning.** Exercise bots with and without human-style
   interference, multiple maps/modes, seeds, destroyed bridges, blocked
   tunnels, denied builds, exhaustion, worker restarts and map transitions.

Acceptance requires actual interactions, not movement distance alone:

- Useful completed builds/breaches, uptake of cover/routes, known enemy assets
  disrupted, support actions, objective participation and contextual variety.
- No perpetual dig/build/reload loops, excessive task switching, identical
  repeated failed prefabs, hidden-enemy tracking or synchronized team cycles.
- Mischief scenarios preserve teammates' footing and exits; uncertain cases
  are rejected. Construction stock and all gameplay restrictions remain real.
- No unbounded queues/memory or unacceptable tick/replication spikes during
  sustained terrain edits. Compare seeded before/after cases at fixed loads.
- Existing navigation, fairness, lifecycle, combat and action-gateway
  regressions pass. Local playable observation must confirm the bots look
  responsive; aggregate test counts alone cannot establish that.

Specific scenario gates added by the class-cooperation brief:

- Two Medics travelling together under real incoming fire place one useful
  pack, heal by actual contact, preserve resource accounting and resume their
  task. A pack destroyed, depleted or already serving the need changes their
  choice. They cannot remain invulnerable through infinite mutual deployment.
- A Marksman occupies and fires from its constructed outpost; rear cover
  preserves exit/line of fire. A mine-equipped variant arms a legal approach
  mine, an enemy triggers the ordinary behavior, and friendly movement does
  not trigger it. A radar variant cannot also place a mine without a partner.
- A Miner completes a useful tunnel and a bridge in separate real-terrain
  scenarios, and another bot actually uses each route. No inflated placement
  rate or block grant; unbuildable/expensive continuations end the project.
- If a participant dies, a bot class/loadout changes, a human edits the site,
  a blast removes support or a worker restarts, no stale task executes and
  the remaining squad either adapts or releases the project.
- Sparse/disabled-class rosters, asymmetric teams and solo bots remain active
  without waiting for a perfect squad. Concurrent projects stay within query,
  construction, entity and network budgets under the configured bot loads.

Ship behind a behavior-version/config switch so beta can retain the current
planner if a new behavior regresses. This is a staged extension of the active
planner, not a replacement of the tested movement engine before release.

## Design references

The utility selector and commitment approach follows established game-AI
techniques. The particular task set, limits and gameplay policy above are
proposals for BattleSpades, not performance claims from these references.

- David “Rez” Graham, [An Introduction to Utility Theory](https://www.gameaipro.com/GameAIPro/GameAIPro_Chapter09_An_Introduction_to_Utility_Theory.pdf).
- Bill Merrill, [Building Utility Decisions into Your Existing Behavior Tree](https://www.gameaipro.com/GameAIPro/GameAIPro_Chapter10_Building_Utility_Decisions_into_Your_Existing_Behavior_Tree.pdf).
