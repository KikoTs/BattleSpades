# Mode commitment and VIP role continuity

The unmodified 120-second Alcatraz VIP native trace is retained at
`out/bot-natural-modes-20260921/vip-alcatraz-baseline.jsonl`.
Around 95.7 seconds the Blue VIP died. Green attacker 3 changed from pursuit
to `vip_guard_formation` and headed approximately 215 blocks home despite
three opposing survivors. The policy's `enemy_vip is None` guard override
made its existing mop-up branch unreachable while the friendly VIP lived.

Other deterministic regressions established that IDs 2 and 4 could both be
escorts with no attacker, and seven real objective roles could be replaced
by the optional human-follow task because their priority was below 0.9.
An interrupted formation also kept its obsolete partner lease.

## Changes

- VIP roles use the friendly bot roster and reserve an attacker. A designated
  escort remains with the friendly VIP after the enemy VIP dies; attackers
  continue toward the opposing authored anchor to find remaining survivors.
- An escort routes to the observed VIP position with a six-block arrival
  band. It does not invent a same-height point outside a ledge. The VIP
  rallies to a nearby friendly behind the opposing public marker, or home;
  recent received damage instead produces a bounded retreat from that known
  source. Neither behavior queries hidden enemy roster positions.
- Worker-owned `ModePolicyMemory` retains roles for up to eight seconds
  across brief friendly roster changes and suppresses small (three-block)
  anchor motion. Meaningful moving-objective relocation remains immediate.
  Map/mode epochs, phases, player lives/classes/teams, carrier changes, and
  objective ownership/death invalidate the old commitment. State is capped
  at 64 observers and has explicit reset/forget hooks.
- `mode_objective_committed` distinguishes winning jobs from optional
  generic team pressure. Cooperative construction and following release
  ownership when such a job arrives. Urgent personal supplies may borrow
  at most four seconds, within six blocks and three blocks of detour.
  Immediate escape roles cannot stop for supplies.
- Defensive/survival/escort decisions expose a separate `watch_position`
  derived from a public opposing approach. The worker/motor owns conversion
  to a level, deliberate gaze; ground navigation points are not look targets.

## Verification and limits

`tests/test_bot_mode_commitment.py` covers the captured VIP phase regression,
small-team roles, escort jitter, authoritative reset boundaries, legal
knowledge, objective takeover, and bounded medical stops. Existing policy,
cooperative continuity, medical, project and safety suites are retained.
The pre-fix tests recorded nine failures before these changes.

These are decision/lifecycle regressions, not proof of natural movement on
every map. The native post-change VIP run and the worker/motor acceptance
gates remain separate required checks. No release files are installed here.
