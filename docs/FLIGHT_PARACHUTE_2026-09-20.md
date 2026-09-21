# Flight and parachute audit, 2026-09-20

The native movement arithmetic remains tied to original-client evidence. This
pass fixes canopy prediction and lifecycle; it does not label a guessed server
fuel policy as retail, or change original resource constants to conceal a bug.

## Recovered data and its limits

Original `shared/constants.py:4612` supplies these resource values:

| Pack | Protocol id | Start delay | Capacity / ignition cost | Drain / second | Refill / second | Refill delay after damage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Rocket | 66 | 0.25 s | 100 / 10 | 75 | 10 | 2 s |
| Glider | 67 | 0.25 s | 100 / 10 | 17 | 9 | 2 s |
| Engineer | 68 | 0.25 s | 100 / 10 | 18 | 3 | 0.5 s |
| UGC Builder | 69 | 0.1 s | 100 / 0 | 0 | 100 | 0.1 s |

Under BattleSpades' existing scheduling policy, uninterrupted usable fuel is
1.2, 5.29, and 5 seconds respectively for the combat packs. Complete empty-to-full
refill takes 10, 11.11, and 33.33 seconds when damage does not interrupt it.
The original client receives activity and fuel from the server: the original
dedicated-server binary is unavailable. The exact server activation/refill
ordering and exhaustion latch are therefore **compatibility policy**, not
recovered server code. Increasing endurance materially would be a named balance
change and is not part of this source-preserving fix.

The existing Glider uses native passive flight plus active thrust to approach
level flight. That combination is an explicit BattleSpades policy; the separate
native gravity and thrust equations are verified original instructions.

Original `world.pyd` movement at `0x10012B80`, parachute gravity at `0x10012EFB`,
uses `0.05 * dt * gravity` followed by the ordinary vertical drag, and resets
the falling-distance accumulator on every active-canopy step. It does not
instantly clamp an already-fast downward velocity. At gravity 1 the sustained
descent approaches 0.05 native velocity, or 1.6 blocks/second. A maintained
40-block descent regression takes roughly 25 seconds and produces no damage.

There is no confirmed original deployment-height constant for a player. The
10-block deploy and 2-block removal constants belong to **supply crates**, and
must not be applied to Soldier. The original `Character.set_hover` only admits
UGC Builder. BattleSpades retains its documented explicit airborne **Z** key
extension for Soldier; this trigger is not claimed as a recovered retail rule.

## Placement recovered from original Character

`aoslib/models.py:422-447` maps equipment 72 to `parachute.kv6` and the separate
first-person `parachute_firstperson.kv6`.

- `Character.set_parachute_model`, `0x10014B20`: both DisplayLists use
  `z_offset=0`, `size=0.12`. `0x1006CD7A-0x1006CD81` constructs the integer zero
  passed as the pivot offset; `0x10014EC6` and `0x1001522C` construct size 0.12.
- `Character.draw_fps_parachute`, `0x10062A20`: draw only when equipped, active,
  and a model exists. Its world transform is `translate(x, -z+1.5, y)` followed
  by `rotate(yaw, 0, 1, 0)` in original OpenGL coordinates. In native canonical
  coordinates the anchor is `(eye.x, eye.y, eye.z-1.5)`. This is a world-space
  attachment; pitch must not rotate the canopy with the camera.
- Remote `Character.draw`, `0x10053600-0x100539E7`: the same equipment/activity
  gates, then draw `parachute_model` beneath the existing body transform before
  the held tool, without another translation. It is an independent attachment,
  not a replacement for hands or weapon.

The client header `world/parachute.hpp` contains these rendering constants.
Raw decompilation and disassembly are archived locally in
`tmp/flight-parachute-20260920/`; IDA databases/binaries were not modified.
Original `character.pyd` SHA-256:
`52ec520d83fe9e0ed8338a1038b176c272752e81d65e9f923fe90036aaa107f7`.
Original `shared/constants.py` SHA-256:
`e87ee5db062d66e54fa17d21d2c04d4df1bcca88ae28bc9a77034ad9b39d965c`.

## Fixed behavior

- Soldier deployment predicts in the same native step as the accepted airborne
  key edge. The same path also works offline.
- An older closed owner snapshot cannot retract a more recent local deploy.
  Acknowledged deployment rejection remains authoritative. Reordered and
  duplicate snapshots are inert, and movement rewind retains the canopy state
  that actually supplied gravity for each frame.
- Landing closes both authoritative and native canopy state in that landing
  frame. Holding Z through landing cannot reopen it on the next jump.
- Death clears replicated canopy activity, not only the native physics flag.
  Removing the equipment clears the active state.
- Removed packs clear their ignition hold clock. Dead players cannot re-ignite
  or recharge a live flight ability from stale held controls. Resource numbers,
  release requirement, damage refill lockout, and class-specific motion are
  retained.

## Verification

The targeted server run passed **1,781 tests**, including 1,600 original-binary
movement cases, plus parachute, flight authority, jetpack lifecycle, jump
correction and corpse tests. Three subsequently added full-range packet-path
tests also passed. The native tutorial-session executable built and passed with
new deployment/ACK ordering, no-equipment, 40-block descent and landing cases.

Full-range tests consume real ClientData through server simulation/replication.
At 60 Hz, from an unobstructed airborne start with forward held, the documented
policy reaches exhaustion at approximately:

| Pack | Horizontal distance | Time from initial hold |
| --- | ---: | ---: |
| Rocket 66 | 8 blocks | 1.5 s |
| Glider 67 | 56 blocks | 5.6 s |
| Engineer 68 | 15 blocks | 5.3 s |

These are regression baselines for the implementation, not original-server
distance measurements. Exhaustion tests continue holding the key while fuel
returns: no free repeated ignition occurs until release and a fresh start delay.

## Native presentation and lifecycle follow-up

The frontend now loads both original canopy meshes independently of the held
weapon and hands. First person submits the FPS mesh to the world pass at the
eye anchor minus 1.5 on canonical Z, with yaw only. Remote players submit the
ordinary canopy at their interpolated body anchor while equipment 72 and the
authoritative deployed bit are present. Both use the original 0.12 scale and
zero pivot offset. Landing/death remove the draw through those same state
gates. Dedicated shared slots 16/17 do not consume remote tool or arm slots;
map teardown and the separate model-gallery scene invalidate them.

Two further native lifecycle defects were found and repaired:

- Offline developer class switching now applies the selected class movement
  profile and original default equipment, and equipped packs consume held
  SPACE. Previously the profile/equipment stayed on the initial class and
  the one-frame jump input could never satisfy the ignition delay.
- Death now retires local ignition and regeneration clocks. A delayed
  pre-death active owner row cannot resurrect live thrust or recharge the
  dead player's fuel pool. The independent jetpack-corpse effect remains
  responsible for death presentation.

Follow-up server verification passed 1,648 tests including original-binary
movement vectors. New authority coverage verifies that holding deploy through
landing cannot grant immunity on a second fall. Three additional resource
regressions verify the exact recovered damage lockout and refill rates through
full capacity. Native session tests cover offline bounded flight and delayed
post-death activity, and pure attachment tests verify original placement,
scale, upright direction and all four cardinal yaw orientations. Graphical
canopy appearance still needs an in-game visual comparison; pure transform
tests do not establish pixel-level screenshot parity.

Combat endurance numbers above remain the recovered resource values. There
was no evidence of a units/clamp discrepancy in online native motion: the
original x86 applies Engineer air acceleration at 0.1 of class acceleration,
versus 0.5 for the Rocket and Glider. A longer Engineer fuel budget would be a
deliberate balance change rather than a recovered original-game correction.

## Original input and legacy-server audit

The additional original-client input trace confirms that the maintained Z
binding must not be described as the retail parachute control:

- `aoslib/config.py` binds SPACE to `jump` and Z to `hover`; the original
  `controlsTab.py` lists `hover` under UGC controls only. There is no separate
  parachute binding in either table.
- `Character.set_jump` at `0x100239D0` forwards jump to a controlled prefab or
  writes ordinary `world_object.jump`, subject to weapon-deployment gates.
  It neither sets parachute activity nor sends a parachute command.
- `Character.set_hover` at `0x100233D0` admits only the UGC Builder pack and
  otherwise clears `world_object.hover`. Soldier's Z extension deliberately
  bypasses this original gate.
- `GameScene.send_client_data` at `0x1016AAE0` samples ordinary jump and hover
  (`0x1016B84C`, `0x1016B8E1`). There is no outgoing parachute-active field.
  The extension reuses ClientData's hover bit; it is not a new packet type.
- The original Character calls `set_parachute_active(False)` on spawn and
  death (`0x1001700B`, `0x1003393B`). The gameplay assignment comes from the
  incoming world-state handler at `0x10182900`, which calls
  `parent.set_parachute_active(...)` at `0x10185390`. Local and remote canopy
  activity therefore follow the server's replicated state.

These client instructions do not reveal the original server's deployment
condition. In particular, they do not prove automatic deployment, a height
threshold, or an airborne SPACE trigger. A legacy server can ignore the Z
extension; an acknowledged closed canopy remains authoritative. Deployment
and longer fuel endurance on `192.248.177.80:32887` are consequently not
claimed verified by the local mechanics tests.

The final focused audit passed 51 server tests across `test_jetpack.py`,
`test_parachute.py`, and `test_flight_authority_flow.py`. Current native-dev
tutorial-session, player-movement, jetpack-death, and retail-movement-core
executables also passed directly; the last suite compared 18 class profiles
and 1,344 reachable original arithmetic vectors without a mismatch. This
audit required no further movement or input source changes.

## Requested local balance and negotiated deployment (follow-up)

The later request explicitly authorized longer travel on our local BattleSpades
server. The new **BattleSpades balanced profile** changes fuel policy while
retaining the recovered native thrust, drag, class acceleration and canopy
transforms. It is a deliberate balance change, not another claimed retail
recovery. The earlier original-resource tables remain the compatibility baseline.

| Pack | Capacity / ignition | Drain per second | Powered budget | Measured horizontal travel | Initial hold to exhaustion |
| --- | ---: | ---: | ---: | ---: | ---: |
| Rocket 66 | 100 / 10 | 30 | 3 s | 27.80 blocks | 3.283 s |
| Glider 67 | 100 / 10 | 9 | 10 s | 113.05 blocks | 10.283 s |
| Engineer 68 | 100 / 10 | 7.5 | 12 s | 34.37 blocks | 12.283 s |

Travel is measured through real ClientData ingestion, authoritative simulation
and WorldUpdate generation, from an unobstructed airborne start with forward
and the configured jump key held. It is approximately 3.5, 2 and 2.3 times the
previous respective baselines. Rocket retains its stronger vertical character;
its proposed six-second budget was reduced to three seconds to limit launch
height. UGC Builder retains the separate original unlimited editor ability.

All three combat packs recharge at 20 units/second **only while grounded or
wading**, after releasing thrust and one second without active thrust. Empty
to full requires five seconds of actual refill. Airborne coasting, releasing,
tapping or holding an exhausted pack cannot refill it. Existing per-pack
post-damage lockouts remain (2, 2 and 0.5 seconds), and exhaustion still requires
a physical release plus a fresh 0.25-second ignition delay. Death and removed
equipment cannot retain a live ability. Ground contact is determined by the
authoritative mover, not by a client assertion.

Soldier's configured hover/deploy key (default Z) opens its equipped parachute
while airborne. Pressing it during ascent queues deployment until downward
velocity reaches zero; it does not reduce gravity during ascent or boost a
jump. Release does not cancel the queued request. Landing, death and removing
the equipment clear both pending and deployed state, and holding the key
through landing cannot open the next fall. No arbitrary minimum height was
added. The original canopy gravity and per-frame fall-distance reset preserve
the roughly 25-second, 40-block descent without damage.

The HUD shows the actual configured keyboard binding and current deployment
status; equipped combat packs show hold-jump and release/land-to-recharge
instructions. Training uses the same balanced profile.

**Upgrade both server and native client.** Native live connections explicitly
advertise `BSCF` version 1 after the length-delimited SteamSessionTicket payload.
The marker is outside the authentication ticket and does not change the XOR
key. Only negotiated connections receive the `BSFP` version 1 InitialInfo
trailer and use its resource rules. Its six rates and idle delay are bounded
fixed-point values; malformed/truncated, unknown-version and unbounded data
are rejected. Native bootstrap copies this server-authored profile into the
session before prediction starts, including after map rollover. Old clients
receive unchanged InitialInfo bytes and the original resource contract;
updated clients connecting to servers without the profile keep that baseline.

Verification includes negotiated/stock handshake isolation, exact wire bytes,
bounded decoder rejection, finite full flights, airborne tap-refill rejection,
ground/damage refill gates, and a 40-block Soldier descent through real
ClientData and replicated state. The actual loopback ENet gameplay gate also
validated the negotiated profile on initial join and again after voted map
rollover. No public-server connection is used for this follow-up.

Final verification passed **90 server tests** and all five current native-dev
executables: `aos_protocol168_session_tests`, `aos_tutorial_session_tests`,
`aos_game_hud_tests`, `aos_player_movement_tests`, and `aos_jetpack_death_tests`.
The new native timeline checks compare position and fuel against the actual
server packet path for all three combat packs at frames 60 and 360. The first
fixture draft omitted the advertised class speed scale and used invalid zero
gravity; correcting those fixtures produced parity without changing production
movement. Commands/results are recorded in
`tmp/flight-balance-20260920/verification.txt`.

The final abuse audit found no live combat class/equipment refill shortcut:
unchanged selections are ignored, changed selections end the old life and
commit on respawn, and ordinary ammo restock does not refill fuel. Full fuel
on respawn and an authoritative Jetpack Crate pickup remain intentional.
