# Bot movement and Zombie behavior implementation plan

Scope: **87 implementation features and 50 Zombie behavior rules**. Checked items have implementation and regression coverage in the [implementation record](BOT_HUMAN_BEHAVIOR_IMPLEMENTATION.md). Client presentation and final acceptance remain separate open gates until their evidence is collected. Competent navigation and timely pressure come before social behavior.

The primary failures are an AFK survivor on a high roof, a survivor midway across a suspended bridge, unexplained falls from buildings, zombies collecting underneath unreachable goals, and abrupt or repetitive movement. Completion requires these situations to work in the client, with a full roster and ordinary game rules.

## Implementation status

The implementation now uses horizontal siege admission, exact-height moving
targets, conservative full support analysis, persistent demolition crews, owned
prefab access routes and native fall/damage outcomes. Natural motor control,
traffic right of way, construction handovers, medical/supply support and bounded
human interactions share the existing mode and action authority.

The previous full-3D 48-block siege gate is removed. Structural additions cause
new analysis; removals from a valid cut preserve the current crew's proof rather
than discarding partial progress. Builders wait for pending grounding analysis.

See the [feature-to-code and regression map](BOT_HUMAN_BEHAVIOR_IMPLEMENTATION.md)
for each numbered item's implementation and evidence. Checkmarks indicate code
and regression coverage, not a claim that every possible map construction can
be beaten or that subjective human likeness has been established.

## 87 implementation features

### Movement and looking

- [x] F01 **Turn curves** — Bound turning speed and angular acceleration so heading changes flow instead of snapping between decisions.
- [x] F02 **Vertical gaze tracking** — Ease pitch changes toward elevated, falling and nearby targets without repeated up/down flicks.
- [x] F03 **Committed strides** — Keep a useful movement direction across small perception changes instead of toggling strafing every update.
- [x] F04 **Predictive braking** — Release movement early enough to stop on the intended floor or work position using native physics.
- [x] F05 **Corner approach** — Prepare turns before corners and shorten strides when clearance narrows.
- [x] F06 **Turn before departure** — Reorient before a large change of travel direction rather than instantly reversing into a wall.
- [x] F07 **Independent gaze and travel** — Looking toward a teammate or sound must not accidentally steer the bot sideways.
- [x] F08 **Terrain aware pace** — Use controlled walking on narrow bridges, roof edges and uncertain footing.
- [x] F09 **Purposeful sprint transitions** — Sprint on safe useful stretches and leave sprint before tight maneuvers or tool work.
- [x] F10 **Stable crouch transitions** — Commit to crouch for clearance, cover or a gesture without rapid posture toggling.
- [x] F11 **Deliberate jump timing** — Choose takeoff position and timing from a reachable landing rather than jumping on a global timer.
- [x] F12 **Landing recovery** — Confirm a landing, stabilize inputs and resume the route without another immediate accidental jump.
- [x] F13 **Controlled retreats** — Step back or reposition when crowded instead of oscillating between forward and backward inputs.
- [x] F14 **Individual walking lines** — Give teammates small, persistent lane preferences inside the verified safe corridor.
- [x] F15 **Smooth remote presentation** — Check the client interpolation of bot motion so valid server inputs do not appear as abrupt visual jumps.

### Perception and reactions

- [x] F16 **Skill based reactions** — Use bounded reaction timing for newly perceived events while preserving urgent hazard responses.
- [x] F17 **Last seen memory** — Remember where a legitimately observed opponent went and age that information.
- [x] F18 **Lost target search** — Check useful cover exits and vantage points after losing sight instead of tracking hidden positions.
- [x] F19 **Sound investigation** — Investigate actual permitted sound cues with an appropriate search area and expiry.
- [x] F20 **Peripheral checks** — Check relevant nearby approaches while holding or moving without abandoning the current route.
- [x] F21 **Threat prioritization** — Weigh recent damage, proximity and mode importance instead of always selecting the closest target.
- [x] F22 **Friendly obstruction awareness** — Adjust firing or movement when a teammate blocks the intended action.
- [x] F23 **Target commitment** — Avoid switching opponents repeatedly when two targets have nearly equal priority.
- [x] F24 **Observed motion anticipation** — Lead a visible moving target using observed velocity and confidence.
- [x] F25 **Skill appropriate aim correction** — Use controlled corrections and range-dependent accuracy without identical perfect tracking across the roster.
- [x] F26 **Reload awareness** — Recognize reload windows and choose a useful safe position or supporting action.
- [x] F27 **Human intent recognition** — Distinguish a nearby ally greeting, following, gesturing or requesting attention from an enemy threat.

### Navigation and terrain

- [x] F28 **Vertical siege activation** — Start relevant analysis for high targets without requiring a zombie to be within a small 3D radius first.
- [x] F29 **Reachable floor goals** — Plan toward an attack position or usable entrance, rather than the ground directly below the survivor.
- [x] F30 **Body clearance lookahead** — Check several upcoming steps with actual standing and crouched body clearance.
- [x] F31 **Narrow bridge traversal** — Align with the bridge center, manage pace and negotiate turns without walking off.
- [x] F32 **Roof edge protection** — Detect unsupported next steps and prevent accidental survivor or infected falls.
- [x] F33 **Landing feasibility checks** — Validate destination support, headroom and the full jump trajectory before committing.
- [x] F34 **Drop selection** — Compare the health and positional cost of a drop with a safer route before taking it.
- [x] F35 **Water access and exits** — Find usable shore approaches, water-level targets and legal exits from low terrain.
- [x] F36 **Moving destination updates** — Refresh the route endpoint as the target moves without discarding useful completed route segments.
- [x] F37 **Terrain change invalidation** — Recheck routes promptly when relevant blocks are removed, built or collapsed.
- [x] F38 **Failed segment memory** — Remember a failed local crossing and avoid immediately attempting the same unchanged failure.
- [x] F39 **Progress watchdog** — Detect lack of real route progress and escalate recovery rather than repeatedly issuing the same input.
- [x] F40 **Alternative entrance search** — Search the far end of a bridge, the back of a building and other access points before choosing a siege plan.
- [x] F41 **Chokepoint yielding** — Negotiate two-way narrow traffic instead of mutually blocking the only passage.
- [x] F42 **Human personal space** — Give nearby human teammates room to move, especially beside ledges and while building.
- [x] F43 **Usable route handoff** — Followers wait for a verified usable construction segment before stepping onto it.
- [x] F44 **Footing loss recovery** — React to disappearing support with legal braking, retreat or air control.
- [x] F45 **Clearance checked route smoothing** — Remove unnecessary waypoint zigzags only when the connecting movement is physically traversable.

### Construction and demolition

- [x] F46 **Complete bridge planning** — Plan a connected crossing from a safe starting bank to a usable destination before building the first segment.
- [x] F47 **Suspended platform analysis** — Trace the survivor's platform to its real anchors, including long bridges and multiple supports.
- [x] F48 **Reachable demolition positions** — Assign each cut to a legal standing position from which the selected tool can actually reach it.
- [x] F49 **Time based strategy comparison** — Compare walking, climbing, digging, building and collapse using real travel and tool costs.
- [x] F50 **Feasible support cuts** — Choose removable support sets that can actually disconnect the structure under server collapse rules.
- [x] F51 **Parallel siege crews** — Divide useful demolition and access work across compatible crews with separate positions.
- [x] F52 **Connected stairs and ramps** — Build or dig steps with usable rise, headroom and landings throughout the ascent.
- [x] F53 **Builder working platforms** — Preserve footing and an exit while extending a route or cutting a structure.
- [x] F54 **Useful construction geometry** — Build cover, entrances and firing positions that suit the site and task instead of placing arbitrary prefabs.
- [x] F55 **Critical route repairs** — Repair a broken useful crossing or staircase when that beats starting a new route.
- [x] F56 **Material planning** — Account for available blocks, acquisition time and resupply before selecting a construction strategy.
- [x] F57 **Construction handover** — Let another capable bot continue verified partial work when the builder dies or must leave.
- [x] F58 **Counter repairs** — Recognize when a survivor is rebuilding the attacked support and change work allocation or approach.
- [x] F59 **Authoritative collapse confirmation** — Wait for real terrain and player-state changes before treating demolition as successful.

### Teamwork and mode competence

- [x] F60 **Mode job arbitration** — Resolve capture, defense, escort, survival and resource tasks according to the active mode.
- [x] F61 **Role distribution** — Allocate suitable assault, construction and support roles using the roster and current terrain.
- [x] F62 **Leapfrog advances** — Let teammates advance and cover one another without making the whole group stop together.
- [x] F63 **Useful escorts** — Protect a VIP, carrier or vulnerable human from useful positions rather than crowding their body.
- [x] F64 **Interception** — Intercept a legitimately known target along a plausible route instead of always chasing its current position.
- [x] F65 **Shared work reservations** — Reserve tool positions, construction cells and approach lanes so teammates do not invalidate one another's actions.
- [x] F66 **Complementary flanks** — Use different useful approaches when the team already covers the direct route.
- [x] F67 **Stranded ally assistance** — Help a reachable stranded teammate with a useful local route when the mode permits it.
- [x] F68 **Supply and medical support** — Coordinate health, ammunition and material help with the ally's actual need and owned abilities.
- [x] F69 **Guard handovers** — Rotate ordinary watches without leaving a critical objective or human unprotected.
- [x] F70 **Adaptive travel spacing** — Spread out on open ground and form an orderly queue at narrow access points.
- [x] F71 **Round adaptation** — Reassign work after infection, deaths, objective changes and changes in the surviving human roster.

### Personality and interactions

- [x] F72 **Idle observation** — Look around at sensible landmarks and approaches when there is no urgent task.
- [x] F73 **Nearby ally attention** — Briefly look toward a nearby friendly human who approaches or deliberately attracts attention.
- [x] F74 **Crouch replies** — Recognize an ally's short crouch greeting and sometimes answer with a brief safe gesture.
- [x] F75 **Friendly tap reactions** — Acknowledge a teammate's harmless hit with a look or short gesture without retaliating.
- [x] F76 **Route invitations** — Briefly indicate a completed bridge, entrance or staircase to a nearby ally through gaze and posture.
- [x] F77 **Restrained celebrations** — Occasionally acknowledge a completed objective after the immediate danger has passed.
- [x] F78 **Construction inspection** — Briefly check completed work when safe, then transition visibly to the next useful task.
- [x] F79 **Shared attention** — Notice a nearby ally investigating an observable event and check the same relevant area.
- [x] F80 **Session familiarity** — Remember recent friendly interactions within the round so responses feel consistent rather than identical every time.
- [x] F81 **Persistent temperaments** — Give bots recognizable cautious, assertive or helpful tendencies without disabling necessary competence.
- [x] F82 **Independent timing** — Stagger reactions and idle behaviors so the roster does not turn or crouch in synchronization.
- [x] F83 **Visible error recovery** — After a failed action, visibly reassess and recover instead of freezing or repeating it indefinitely.
- [x] F84 **Optional contextual quips** — Allow rare, clearly bot-authored game chat tied to real events, under server controls.
- [x] F85 **Courtesy after yielding** — Occasionally acknowledge letting a teammate pass, with a short gesture when safe.
- [x] F86 **Visible problem solving** — Inspect a blocked route, choose a tool and begin useful work so pauses have a clear purpose.
- [x] F87 **Remember successful counters** — Reuse successful approaches to recurring player constructions within the round after checking the new geometry.

## 50 Zombie behavior rules

These are invariants and acceptance checks around the features, rather than another count of optional personality behaviors. They apply to normal Zombies and Classic+ Zombies, using each mode's actual equipment and rules.

### Authority and fair information

- [x] G01 **Mode legal target knowledge** — Use only information the mode grants to the infected or the bot has legitimately perceived.
- [x] G02 **Local sensing boundaries** — Local reactions, hearing and immediate attacks must obey their visibility and range requirements.
- [x] G03 **Native movement limits** — Use ordinary speed, jump, acceleration and collision rules for the current class.
- [x] G04 **Legal action cadence** — Respect tool reach, facing, cooldowns, damage and line-of-sight validation.
- [x] G05 **Owned equipment only** — Never plan with a block tool, grenade, prefab or movement ability the bot does not have.
- [x] G06 **Real material accounting** — Pay construction and supply costs through authoritative inventory changes.
- [x] G07 **Protected terrain compliance** — Honor protected areas, indestructible cells and map boundaries during planning and execution.
- [x] G08 **Round and infection consistency** — Preserve the correct team, kit, target set and respawn restrictions after every role transition.
- [x] G09 **No navigation teleports** — Do not move bots through geometry or teleport them to conceal route failures.
- [x] G10 **No forced human outcomes** — Never move or kill a human directly to make the siege succeed; falls must follow ordinary physics.

### Movement and survival safety

- [x] G11 **Whole body clearance** — A route needs body and head clearance, not just a free point at the feet.
- [x] G12 **Supported next steps** — Check the intended next support surface before walking onto it.
- [x] G13 **Explicit drop decisions** — A fall requires a deliberate, evaluated movement decision rather than incidental waypoint overshoot.
- [x] G14 **Verified jump endpoints** — Both takeoff and landing must be usable under the real movement profile.
- [x] G15 **Preserve working footing** — Do not remove the builder's support or the only safe exit without a verified alternative.
- [x] G16 **Protect nearby humans from crowding** — Do not push or obstruct a human teammate at an edge or critical movement point.
- [x] G17 **Safe route occupancy** — Respect usable capacity and direction on narrow bridges, ramps and stairs.
- [x] G18 **Abort changed routes safely** — Stop or recover when a necessary route cell disappears instead of blindly continuing forward.
- [x] G19 **Water state consistency** — Make water movement and shore-exit decisions using actual grounded, wading and swimming states.
- [x] G20 **Reject stale intents** — Commands from a previous life, map or expired worker frame cannot keep a bot moving off an edge.

### Siege coordination and progress

- [x] G21 **Actionable goals** — A target underneath the survivor is not a successful route endpoint unless it enables a legal attack or useful work.
- [x] G22 **Executable work assignments** — Each dig or build job needs a reachable work position and the required clearance.
- [x] G23 **Exclusive work slots** — Avoid conflicting builders, shared standing cells and teammates blocking a cutter's reach.
- [x] G24 **Maintain an active assault** — Keep capable bots doing useful access or demolition work while other roles screen approaches.
- [x] G25 **No indefinite piles below targets** — Detect an elevated survivor with a blocked crowd and redistribute the horde.
- [x] G26 **Bound stalled decisions** — A stalled plan must trigger a bounded recovery and then a materially different strategy.
- [x] G27 **Measure actual progress** — Credit verified displacement, usable route extension or real structural damage; attempted commands alone do not count.
- [x] G28 **Cap rejected retries** — Repeated invalid actions must change the goal, work position or strategy.
- [x] G29 **Invalidate structural assumptions** — Relevant terrain edits must invalidate cached access and support analyses.
- [x] G30 **Retire obsolete sieges** — Cancel or revise work when the survivor dies, moves away, changes team or falls to another level.

### Construction exploits and difficult terrain

- [x] G31 **Trace long bridge anchors** — Investigate the real access and supports beyond the small neighborhood below the survivor.
- [x] G32 **Account for all supports** — A proposed collapse must include every remaining grounding connection under the server's adjacency rules.
- [x] G33 **Reject futile digging loops** — Do not keep excavating below a platform when that excavation cannot enable access or collapse.
- [x] G34 **Handle overhangs and stacked levels** — A valid destination height alone does not prove an ascent has clearance.
- [x] G35 **Validate buried approaches** — Digging toward a concealed or buried target must preserve a traversable and survivable route.
- [x] G36 **Reconsider repaired supports** — A survivor undoing the crew's work must trigger a revised effort estimate and allocation.
- [x] G37 **Bound unsupported pillar attempts** — Stop repeated pillar or staircase attempts that lack materials, clearance or a connected destination.
- [x] G38 **Bound decoy investigations** — Expired sound or attention cues cannot indefinitely divert the whole horde from useful mode work.
- [x] G39 **Resist target oscillation** — Player baiting must not repeatedly reset every bot's route or siege assignment.
- [x] G40 **Honest unreachable handling** — Recheck alternatives, cover plausible exits and retain useful pressure when no legal route exists; record the reason in diagnostics.

### Personality limits and validation

- [x] G41 **Interaction cooldowns** — Rate-limit responses per bot and per partner so repeated crouches or hits cannot create an endless loop.
- [x] G42 **Urgent tasks preempt gestures** — Combat, footing hazards and critical mode jobs interrupt idle or social behavior immediately.
- [x] G43 **No friendly retaliation** — A teammate's attention gesture or harmless hit never authorizes an attack on that teammate.
- [x] G44 **No collective idling** — The horde cannot all enter social or waiting behavior while capable bots have useful assault work.
- [x] G45 **No synchronized rituals** — Avoid roster-wide crouch loops, identical look sweeps and pointless conga lines.
- [x] G46 **Bound bot chatter** — Keep optional quips infrequent, event-based and clearly attributable to bots.
- [x] G47 **Protect server timing** — Bound and fairly schedule planning work without starving a high target or stalling the gameplay loop.
- [x] G48 **Reproducible variation** — Seed behavior per bot and round, record the seed, and avoid rules hardcoded to test coordinates or player IDs.
- [x] G49 **Test full rosters and adversarial geometry** — Verify multiple crews, seeds, heights, spans, widths and human placements, including live worker scheduling.
- [ ] G50 **Earn completion with observed behavior** — Require timing, fall-rate, route-quality and client movement evidence from the exact installed build.

## Acceptance scenarios and measurements

### Required scenarios

Run these through ordinary tools, native player physics, damage, terrain collapse, inventory costs and map updates. Include normal Zombies and Classic+ Zombies. Retain failures and their seeds.

1. AFK last survivor on a roof with an ordinary stair entrance, including an entrance on the far side.
2. AFK last survivor near the top of the map with the infected well outside the current 48-block analysis gate.
3. Survivor halfway across a suspended bridge with access from one end, then from both ends.
4. Survivor on a platform with several independent supports and diagonal grounding connections.
5. Survivor who removes the route behind them while infected are approaching.
6. Survivor who repairs supports while a demolition crew attacks them.
7. Survivor who changes platform, jumps away or falls while a siege is in progress.
8. Human or survivor bot on an intact roof receiving a friendly tap or crouch greeting; no unexplained fall is acceptable.
9. Single-width bridge carrying traffic in both directions while a builder and followers cooperate.
10. Water-level survivor, flooded approach and infected stranded below the main terrain.
11. Buried survivor or hidden room, using only the information that the mode legally grants the infected.
12. A builder or cutter killed mid-project, with another capable bot taking over.
13. A deliberately impossible route under the active tool/material/protection rules, with truthful diagnostics and useful fallback behavior.
14. Calm friendly interactions followed immediately by a threat, terrain loss or urgent objective change.

Vary elevation by 8, 32, 64, 128 and 192 blocks where the map permits it; bridge spans by 8, 24, 64 and 128 blocks; and bridge widths by 1, 2 and 3 blocks. Exercise 4, 8, 16 and 24 infected with multiple seeds. Use pairwise coverage for the broad matrix and exhaustive variants around each reproduced failure. Keep separate validation seeds and maps that were not used to tune decisions.

### Initial performance targets

These are proposed acceptance budgets to verify and tune against real tool costs, not current performance claims.

- Detect a stalled individual route after roughly 3 seconds without verified movement or useful work. Repeated failure must cause a different strategy within a further 5 seconds.
- For the small standard fixtures, begin a useful access, demolition or interception action within 5 seconds of actionable target knowledge and a feasible available crew. Record planning and travel time separately.
- Measure the 95th-percentile time to a legal threat or verified structural counter against a skilled human's repeated runs with the same start, class, tools and materials. Initial target: no more than twice that median plus 10 seconds. Do not invent a universal arrival limit for every tower or material budget.
- A six-minute attempt with no meaningful pressure fails even if the target is eventually reached. Log which constraints prevented an earlier useful strategy.
- Require zero unexplained falls on intact safe-floor fixtures. Report deliberate drops, enemy-induced falls, collapse-induced falls and navigation failures separately.
- Count pressure using real attack reach, a usable route onto the survivor's level, or an effective support cut. Horizontal proximity alone is never success.
- Social responses should be short and individually staggered. Begin with a maximum 0.8-second gesture and a minimum 10-second repeat cooldown, then review the result in the client.
- Review client recordings for angular snaps, start/stop oscillation, repeated jumps, crowding, idle loops and missed opportunities. A clean simulation exit does not establish natural movement.

### Completion evidence

Record the source revision and local changes, installed executable hashes, map/geometry identity, mode rules, roster, seed, wall-clock duration, simulated duration, worker mode and termination reason. Keep per-bot role changes, route failures, positions, orientation, authoritative tool results and terrain changes.

Measure time to first useful action, first viable access, first legal attack or effective collapse, and target defeat separately. A target falling by itself or a fixture starting with invalid footing cannot count as a successful siege.

Each delivered feature needs its relevant focused regression, a multi-bot scenario where applicable, and a client check for visible behavior. Earlier reports remain evidence for their own source revision; they do not mark this backlog complete.
