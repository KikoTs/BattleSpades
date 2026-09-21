# Movement with purpose

This pass removes periodic strafing and automatic combat bunny-hopping. It
does not add random animation or increase the worker's decision frequency.

`CombatFootwork` keeps one bounded state per bot. A stride holds its world
heading while the target makes small movements, ends after actual displacement
or a short deadline, and allows a firing pause. Received damage, reloading,
health, weapon distance and the existing personality profile influence the
next stride. Stable per-bot variation prevents the roster sharing one rhythm.
Separate entry/exit distances prevent approach/retreat oscillation at a weapon
range boundary. Collision recovery still overrides blocked strides; an
intentional firing pause is not recorded as a failed movement attempt.

An evasive hop requires a new recent damage event, reduced health, a clear dry
runway and the existing native clearance checks. One old hit cannot trigger
repeated hops. No position, velocity or movement-limit overrides were added.

Combat keeps an escort, carrier or defence route alive instead of discarding
and recreating it every frame. Distant incidental enemies also cannot pull an
attacker away from a committed mode objective. Bots can return fire while
moving on that route. Terrain actions retain their matching tool and precise
aim. A visible enemy VIP or flag carrier takes priority over an ordinary
target after the normal visibility checks; public markers never grant a shot
through terrain.

Arrived guards watch a public opposing approach at eye height. This combines
with the director's travel gaze changes and the mode policy's role/anchor
memory, documented separately.

The final movement/tactics/gameplay suite passed 105 tests (excluding the
five native excavation-pocket cases, which are verified separately). Policy,
cooperative ownership, planning and trace-review coverage passed 188 tests.
The new locomotion cases exercise actual decision boundaries, including
retaining a previously created route during incidental combat. Native match
evidence and installation status belong in
`BOT_NATURAL_MODE_VALIDATION_2026-09-21.md`.

The client presentation path was checked separately: WorldUpdates feed
`RemoteMotionInterpolator`, which blends shortest-arc yaw and independent
pitch over the snapshot interval. The renderer uses that interpolated
orientation for the head. Its existing protocol-player tests passed, including
the -179/+179 degree seam. This pass therefore changes the bot's upstream aim
decisions; it does not add another client interpolation delay. Passing those
tests is not a substitute for observing a live match.

## Flag pickup and legal actions

Manual inspection of the first CastleWars trace found a carrier whose home
goal was correct but whose escape continuation still targeted a dig on the
pickup approach. The normal CTF rules forbid shots and melee digging while
carrying the burdensome flag, so that work was rejected until the carrier died.
The first candidate is retained as rejected evidence for this case.

Player perception now includes `can_shoot`, derived from the same live burden
and mode rule used by the combat service. Pickup, drop, or a capability change
invalidates the worker's local route, escape and breach state before its normal
decision throttle. Restricted carriers do not select combat targets or dig
edges; a stale breach step also has a direct rejection fence. The director
retires stale weapon work at the motor, physics observation and commit
boundaries. Existing legal construction/deployable actions remain available.

Nine carrier mobility regressions exercise real worker/planner decisions,
including pickup and drop within the decision interval, an actually solid
stale breach target, and a mode that explicitly permits shooting with intel.
The trace reviewer now detects repeated rejected carrier excavation even when
the displayed strategic destination still points home. A complete capture
remains a separate gameplay outcome from selecting the correct return goal.
