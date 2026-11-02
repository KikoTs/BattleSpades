# Human bot behavior: implementation and validation

This maps the 87 features and 50 rules in [the checklist](BOT_HUMAN_BEHAVIOR_PLAN.md) to their implementation and regression coverage. Code coverage is distinct from a match outcome or subjective human likeness. Use [the gameplay validation guide](BOT_GAMEPLAY_VALIDATION.md) to collect evidence for the revision being reviewed.

Ordinary roaming includes short side checks, with varied per-bot timing, side and angle. The look overlay preserves movement and actions; hazards, combat and precise terrain work preempt it. `test_bot_roaming_attention.py` covers interruption, route changes, angle bounds and variation across a roster.

**G50 remains open:** visual acceptance of movement and behavior in the packaged client is still required. A clean simulation exit or a building destroyed by the tester does not establish bot success.

## Movement and looking

| Features | Implementation | Regression evidence |
| --- | --- | --- |
| F01, F02 | `director.py`: bounded yaw/pitch velocity and acceleration, damped gaze; motors run at 30 Hz alongside the observer stream. | `test_bot_gaze_dynamics.py`, `test_bot_aim_stability.py`, `test_bot_liveness.py` |
| F03, F13 | `locomotion.py`: committed strides, spacing hysteresis and received-pressure retreats; `traffic.py`: bounded yielding. | `test_bot_natural_locomotion.py`, `test_bot_traffic.py` |
| F04, F05, F06 | `traffic.py`, `recovery_skills.py`, `simple_worker.py`: speed-aware braking, corner/takeoff alignment, one short reorientation pause. | `test_bot_traffic.py`, `test_bot_path_following.py`, `test_bot_exact_edges.py` |
| F07 | Gaze is independent of world-space travel; social looks cannot rotate the requested travel vector. | `test_bot_live_travel_motor.py`, `test_bot_social_behavior.py` |
| F08, F09, F10 | Native safety motor and safe-corridor probes control sprint, sneak and crouch; gestures preserve precise traversal. | `test_bot_sprint_decision.py`, `test_bot_traffic.py`, `test_bot_social_behavior.py` |
| F11, F12 | `recovery_skills.py`: verified jump endpoints, precise pillar centring and native airborne steering; `traffic.py`: landing settle. The motor validates the actual sub-cell alignment path instead of a full stride beyond the block, with a tight arrival tolerance. An explicit takeoff bit prevents premature or repeated jump presses. Level gaps remain real jumps. | `test_bot_recovery_skills.py`, native pillar/overhang and level-gap cases in `test_bot_human_contracts.py`, `test_bot_live_travel_motor.py`, `test_bot_traffic.py` |
| F14 | `traffic.py`: persistent identity-derived lane preference only inside verified open corridors. | `test_bot_traffic.py`, `test_bot_natural_locomotion.py` |
| F15 | Server motor cadence matches the 30 Hz remote stream. Client `RemoteMotionInterpolator` advances the native mover without introducing a delayed render history. | Client `aos_protocol168_players_tests`; actual packaged-client observation remains a separate acceptance gate. |

## Perception and reactions

| Features | Implementation | Regression evidence |
| --- | --- | --- |
| F16 | Profile reaction delay for new contacts; awareness hazards preempt ordinary reactions. | `test_bot_combat_tactics.py`, `test_bot_awareness.py` |
| F17, F18 | `simple_worker.py`, `behavior_memory.py`: expiring observed contacts and searches around last-known positions. Hidden roster positions do not refresh the memory. | `test_simple_bot_tactics.py`, `test_bot_path_following.py` |
| F19 | `stimuli.py`, `awareness.py`: real, permitted, expiring cues with uncertainty and bounded investigation. | `test_bot_awareness.py`, `test_bot_architecture.py` |
| F20 | Awareness glances and safe idle approach checks leave the active route in control. | `test_bot_awareness.py`, `test_bot_social_behavior.py` |
| F21, F23 | Threat weighting and target hysteresis retain an appropriate opponent while allowing a new immediate threat to preempt. | `test_bot_natural_locomotion.py`, `test_bot_combat_tactics.py` |
| F22 | Friendly firing-lane tests and safe traffic right of way. Validated action refreshes retain each actor's queue position; a worker refresh before its motor tick defers that entry while ready actors use the bounded commit budget. Replacement commands require fresh motor validation. | `test_bot_combat_tactics.py`, `test_bot_traffic.py`, `test_bot_action_fairness.py` |
| F24, F25 | Visible velocity prediction, weapon envelope, profile accuracy/reaction and bounded motor corrections. | `test_bot_aim_stability.py`, `test_bot_combat_tactics.py`, `test_bot_gaze_dynamics.py` |
| F26 | Reload retreat/cover and owned ammunition support retain the mode's urgent job. | `test_bot_natural_locomotion.py`, `test_bot_cooperative_medical.py` |
| F27 | `human_behavior.py`: local human approach/crouch attention; native friendly taps become targeted, life-scoped attention cues. | `test_bot_social_behavior.py` |

## Navigation and terrain

| Features | Implementation | Regression evidence |
| --- | --- | --- |
| F28, F29 | Horizontal siege admission; exact moving-body goal height; full-height support tracing. Generic spawn selection prefers available dry cached columns while retaining explicit sea spawns and destroyed-base fallbacks. Static objective/post height normalization remains intact. | `test_zombie_horde.py`, `test_bot_human_contracts.py`, `test_bot_policies.py`, `test_map_metadata_retail.py`, `test_spawn_lock.py` |
| F30, F31, F32 | Full capsule clearance, next-footing checks, narrow centerlines and supported avoidance. | `test_bot_path_following.py`, `test_bot_exact_edges.py`, `test_bot_traffic.py` |
| F33, F34 | Native-profile jump/drop feasibility, swept clearance and drop-cost decisions. | `test_bot_recovery_skills.py`, `test_bot_voxel_navigation.py` |
| F35 | Regional bank recovery, actual swim/wade state, legal shore exits and stable landing metadata. A committed retreat retains movement until it reaches a new approach or its bounded watchdog redirects it. A bobbing swimmer's climb starts from the real waterbed, retaining an actual placed ledge when one exists. Air cannot become a takeoff platform. | `test_bot_water_crossing.py`, `test_bot_shore_landing.py`, `test_bot_water_landing_motor.py`, `test_bot_recovery_skills.py`; DoubleDragon replay and eight DragonIsland timing replays |
| F36 | Moving targets update endpoints while committed route segments and hysteresis preserve useful work. | `test_bot_mode_commitment.py`, `test_bot_navigation_progress.py` |
| F37 | Relevant topology invalidates route cells and added grounding invalidates structural proof. Partial successful cuts survive removals. | `test_bot_route_invalidation.py`, `test_zombie_horde.py` |
| F38, F39 | Per-life failed-cell avoidance, real-displacement watchdog, bounded retry/backoff and alternative assignments. A retry retains its clock; bobbing and replacement step zero do not count. Committed required terrain changes and settled higher landings earn progress. Ordinary recovery inherits the time already spent stalled in a skill. | `test_bot_navigation_progress.py`, partial-excavation/handoff cases in `test_bot_recovery_skills.py`, `test_bot_human_contracts.py`; London timing/hash-seed replays |
| F40 | Approach flood and structural anchor search compare alternate access with digging, owned ascent and collapse. | `test_zombie_horde.py`, `test_bot_recovery_skills.py` |
| F41, F42 | Humans have right of way; verified alcoves/backsteps, stable bot priority, queue and bounded reconsideration. | `test_bot_traffic.py` |
| F43 | Followers use a bridge/breach/prefab only after committed terrain confirms a complete usable route. | `test_bot_cooperative_project_physics.py`, `test_bot_human_contracts.py` |
| F44 | Route invalidation and native air control handle disappearing support; airborne steps retain their precise controller. | `test_bot_route_invalidation.py`, `test_bot_exact_edges.py`, `test_bot_recovery_skills.py` |
| F45 | Route smoothing only crosses verified capsule-clear runs; corners and exact affordances remain explicit. | `test_bot_path_following.py` |

## Construction and demolition

| Features | Implementation | Regression evidence |
| --- | --- | --- |
| F46 | `project_sites.py`: connected whole-span bridge proposal with bank/exit, resource and support checks before the first build. | `test_bot_project_sites.py`, `test_bot_cooperative_project_physics.py` |
| F47, F50 | `structure_collapse.py`: all grounding under the real adjacency rule, conservative bounded frontier and legal minimum cut; distant anchors remain relevant to edits. | `test_zombie_horde.py`; native highroof/skybridge reports |
| F48 | Distinct legal cutter standing positions; the selected voxel must have a visible reachable face, including partially occluded foundations. | `test_bot_human_contracts.py`, `test_bot_native_breach_feedback.py` |
| F49 | Approach, swing cadence/footprint and affordable owned material/ascent costs select walking, building, digging or collapse. | `test_zombie_horde.py`, `test_bot_recovery_skills.py`; native siege reports |
| F51 | Stable horde roles and reserved cutting positions; access builders wait while the first grounding proof is pending. | `test_zombie_horde.py`, `test_bot_human_contracts.py` |
| F52, F53 | Native build-step plans and authored Zombie Hand/Bone geometry with complete headroom, support and reachable landings. Real failed cells are avoided. A bounded 64-site search includes flank/rear attachments when the next forward stair is blocked. | `test_bot_schematic_physics.py`, `test_bot_human_contracts.py`; production long-bridge replay |
| F54 | Site-aware shelter, cover, outpost, rampart and access schematics; role/threat-facing placement and useful occupancy. | `test_bot_schematics.py`, `test_bot_cooperative_projects.py`, `test_bot_schematic_physics.py` |
| F55 | A still-useful damaged bridge is revalidated and repaired with owned material before reuse. | `test_bot_cooperative_projects.py` |
| F56 | Owned tools/wallet checks, real native costs, mining and bounded resupply errands. | `test_bot_project_sites.py`, `test_bot_cooperative_medical.py`, `test_bot_human_contracts.py` |
| F57 | Partial bridge/breach/rampart and schematic work can transfer to a capable teammate. Pending commands and dead-life identity never transfer. | `test_bot_cooperative_projects.py`, `test_bot_cooperative_continuity.py`, `test_bot_schematics.py` |
| F58 | Relevant support additions revoke the proof and recalculate cut/approach cost; real damage/removals maintain the crew's progress. | `test_zombie_horde.py`; repairing-target matrix cases |
| F59 | Structural success waits for native terrain change and gravity/damage. The harness no longer moves a human down after collapse. | `test_bot_native_breach_feedback.py`, `test_bot_gameplay_review.py`; highroof native reports |

## Teamwork and mode competence

| Features | Implementation | Regression evidence |
| --- | --- | --- |
| F60, F61 | `policies.py`, mode hooks and unofficial controllers arbitrate the actual mode's jobs, class permissions and roster quotas. | `test_bot_policies.py`, `test_bot_objective_roles.py`, `test_bot_strategy_rotation.py`; 35-mode matrix |
| F62 | Work crews cover a builder, then relay followers across the verified route. One runner advances while other participants cover; death/stall/arrival releases relief. | `test_bot_cooperative_projects.py`, `test_bot_cooperative_project_physics.py` |
| F63 | VIP/carrier formation spacing, escort anchors and medic/human support positions. | `test_bot_mode_commitment.py`, `test_bot_cooperative_continuity.py` |
| F64 | CTF/OC/public-target interception uses mode-permitted objectives and plausible route goals. | `test_bot_objective_roles.py`, `test_bot_policies.py` |
| F65 | `team_tasks.py`, schematic sites and siege crews reserve cells, owners and distinct work positions. | `test_bot_task_transactions.py`, `test_bot_cooperative_projects.py`, `test_zombie_horde.py` |
| F66 | Persistent flank/ring assignments vary by seeded roster slot without reshuffling every frame. | `test_bot_strategy_rotation.py`, `test_zombie_horde.py` |
| F67 | Visible stationary stranded allies can receive a complete, affordable legal bridge when it beats walking; movement/death revokes uncommitted rescue. | `test_bot_cooperative_projects.py` |
| F68 | Medical, ammunition, supply stock and mining support use actual need, inventory and bounded route detours. | `test_bot_cooperative_medical.py`, `test_bot_mode_commitment.py` |
| F69 | VIP relief retains the outgoing guard until actual arrival at the protected player's level; failed relief retains coverage. Ordinary mode rotations keep defense quotas. | `test_bot_human_contracts.py`, `test_bot_strategy_rotation.py` |
| F70 | Safe wide-ground lanes become a stable queue at narrow access. Completed routes relay followers. | `test_bot_traffic.py`, `test_bot_cooperative_projects.py` |
| F71 | Map/mode/life/team/kit boundaries reset ownership; carrier and infection changes preempt ordinary cadence immediately. | `test_bot_transition_soak.py`, `test_bot_slot_identity.py`, `test_bot_carrier_mobility.py`, `test_bot_mode_commitment.py` |

## Personality and interactions

| Features | Implementation | Regression evidence |
| --- | --- | --- |
| F72 | Safe routine holds get stable, bounded, seeded idle gaze dwells. | `test_bot_social_behavior.py` |
| F73, F74, F75 | Visible local ally approaches, crouch edges and native friendly taps produce delayed short acknowledgement, never friendly attacks. | `test_bot_social_behavior.py` |
| F76, F78 | Confirmed construction completion triggers one inspection/invitation gaze; no acknowledgement for an attempted or rejected build. | `test_bot_social_behavior.py`, `test_bot_cooperative_projects.py` |
| F77 | Actual public capture/claim events permit a safe occasional objective acknowledgement. | `test_bot_social_behavior.py`; native mode scoring path |
| F79 | An ally's gaze can draw attention only to a fresh event also visible to the bot. | `human_behavior.py::_observe_partners`; social visibility/urgency regressions |
| F80 | Bounded per-partner encounter history, repeated-cue consumption and cooldowns persist through the current life within the round. | `test_bot_social_behavior.py` |
| F81, F82 | Persistent profiles/temperaments and per-identity seeded timing; shared human-response cooldown prevents collective rituals. | `test_bot_strategy_rotation.py`, `test_bot_social_behavior.py`, `test_bot_natural_locomotion.py` |
| F83, F86 | Rejected work, blocked routes and failed cells cause bounded reconsideration, tool-facing work and alternative roles with diagnostic reasons. | `test_bot_native_breach_feedback.py`, `test_bot_navigation_progress.py`, `test_bot_recovery_skills.py` |
| F84 | Contextual construction/breach/bridge/objective/yield quips share the existing sparse chatter limiter and bot name attribution. Chatter remains optional and off by default. | `test_bot_banter.py` |
| F85 | Occasional short yield acknowledgement retains the exact safe yield movement; danger immediately cancels the glance. | `test_bot_social_behavior.py::test_courtesy_acknowledgement_retains_yield_and_is_bounded_by_real_danger` |
| F87 | Successful method preference survives death within the round, then fresh geometry/resources must pass admission. Failure aversion and contacts remain life-local. | `test_bot_human_contracts.py`, `test_bot_cooperative_projects.py` |

## Zombie rule enforcement

| Rules | Enforcement and evidence |
| --- | --- |
| G01, G02 | Perception/stimulus filtering, mode-supplied public targets, LOS/range checked attacks; `test_bot_architecture.py`, `test_bot_awareness.py`, hidden-VIP regression. |
| G03, G04 | Native mover, real tool gateway and combat runtime; native recovery/project/prefab physics and breach feedback tests. |
| G05, G06 | Owned equipment, authoritative stock and actual block cost; class tools/project/native prefab tests. |
| G07 | Protection/bounds checks in proposals and authority; project sites, schematic and gateway tests. Construction-only exclusions do not silently prohibit legal demolition. |
| G08 | Mode/infection/team/kit transitions and stale-life rejection; transition soak, carrier, slot identity, Zombie/Classic+ mode tests. |
| G09, G10 | Runtime bots receive ordinary inputs; harness challenge movement uses client inputs, fall/damage/collapse use native authority. Setup positioning is explicitly separate. |
| G11, G12, G13, G14 | Capsule-clear support and evaluated jump/drop affordances; exact edge/path/recovery/native movement tests. |
| G15, G16, G17, G18 | Protected working footing, full connected route confirmation, traffic priority/capacity and edited-route abort; project, traffic and native physics tests. |
| G19, G20 | Actual water state and life/epoch/intent expiry; water crossing/landing, worker safety and identity tests. |
| G21, G22, G23 | Exact-height actionable endpoints, reachable visible cut faces, exclusive work slots; horde, human contracts, project transaction tests. |
| G24, G25, G26 | Stable capable assault/access crews, pending-proof ring distribution, real stalled-progress escalation; horde and navigation tests plus full-roster challenge matrix. |
| G27, G28 | Only actual native terrain work/body displacement/committed routes count; rejected commands have bounded retry and failed-site memory. Human contract and native breach tests. |
| G29, G30 | Relevant additions invalidate structural assumptions; removed cuts preserve valid proof; moved/dead/infected targets retire old work. Horde, route invalidation and mode commitment tests. |
| G31, G32 | Bounded support graph traces remote anchors and all real grounding adjacency. Conservative frontier cannot claim a collapse proof. Structural and long-span native cases. |
| G33, G34, G35 | Feasible dig/ascent routes preserve real footing/headroom; failed cells and work progress prevent unchanged excavation loops. Buried/hidden/stacked matrix and native recovery tests. |
| G36, G37 | Support repairs revoke proof; wallet/clearance/complete owned geometry gates bound pillars/prefabs. Repairing-target matrix and prefab human contracts. |
| G38, G39 | Cues expire; role/target changes alone cannot reset the stalled clock; a prey moving toward a stationary hunter is not hunter progress. Awareness/horde/human contract tests. |
| G40 | Bounded-frontier/no-cut/retry reasons remain in diagnostics; legal access alternatives continue without invented terrain or human outcomes. Native skybridge reports retain failed attempts. |
| G41, G42, G43, G44, G45, G46 | Per-partner/bot/team cooldown, danger and mode preemption, no friendly attacks, no assault crouch rituals, sparse optional chatter. Social, horde, banter and mode commitment tests. |
| G47, G48 | Sliced structural/path jobs, bounded caches/memory, fair scheduling and seeded per-identity variation. Action queue refreshes preserve full-roster turns and do not spend the commit budget on an unvalidated replacement. Planning/perception budget, action fairness, worker safety and strategy rotation tests; live server tick telemetry. |
| G49 | The gameplay matrix supports full rosters, multiple modes/seeds and last-player challenges. Record source fingerprints and distinguish execution errors, contested outcomes and failed behavioral gates. |
| G50 | Record installed hashes, timing and route/fall telemetry, then observe the same build in the packaged client. Visual acceptance remains open. |
