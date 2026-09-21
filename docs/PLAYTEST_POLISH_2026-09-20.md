# September 20 gameplay polish

This pass addresses the reported flight failures, fast-class jump corrections,
turrets exploding beside themselves, stepping Rocketeer corpses, terrain grain,
and stacked translucent prefab previews. It changes both the server and the
native BattleSpades client. An original dedicated server is unavailable, so this
is not a claim of whole-server retail equivalence.

## Movement and flight

The preceding server reversal left the native client using older movement
approximations. The native mover now follows the verified server port's explicit
float stores, horizontal orientation normalization, independent opposing inputs,
contact-owned airborne state, peer impulses, uncrouch clearance, climb handling,
and class-specific landing curves. A fabricated crouch/water force was removed.
The landing-damage conversion also rounds the float product before integer
truncation; a 31-block Soldier fall yields 70 damage instead of 69.

Both live prediction and delayed replay retain the native jump displacement.
The cached owner-position rewind previously returned a newly launching player
to ground contact and could interrupt a pack's takeoff. Owner checkpoints are
no longer suppressed during flight or until an airborne player releases Space.

Active glider 67 now selects the existing passive-flight gravity branch, in both
server physics and client prediction/replay. Its separate wire passive flag is
replicated with advertised activity. The existing thrust and fuel constants
remain unchanged: the glider nearly balances gravity and drains 17 units/s,
whereas rocket 66 drains 75 units/s and provides stronger upward thrust.
Engineer pack 68 retains its gradual ascent. Activation, release, exhaustion,
and refill use matching authority/prediction recurrences.

The glider's server activity policy is a deliberate response to the requested
behavior. The original client proves the distinct passive branch; it does not
prove an unavailable original dedicated server's fuel/activity policy.

## Turrets

The controller supplied a monotonic timestamp to a projectile engine that ages
shots with wall-clock time. On the first update, each rocket appeared older
than its lifetime and exploded near the turret. Spawn now uses the projectile
engine's own clock; controller cooldowns remain monotonic.

Native turret drawing now converts the retail +Y barrel convention correctly,
including pitch around its actual elevation axis. Targets beyond the barrel's
allowed elevation cannot monopolize acquisition. Owned turret rockets also
stay at their world origin: first-person muzzle adjustment requires correlation
with an explicitly sent handheld launcher action, including type, origin, and
velocity. A bounded tracker prevents weapon/class switches from confusing the
two sources. Offline sandbox turrets use the same draw convention.

## Presentation

Rocketeer corpses interpolate the 30 Hz authority stream over a bounded 1/30 s
window, without extrapolating beyond its latest position. The body and death
camera share that position. Packet 36 still explodes at the exact authoritative
point, and player-generation guards remain in place.

Terrain's intentional grain is sampled through a separate mip chain, reducing
minification noise while retaining close detail and the unchanged original AO
atlas. Prefab ghosts first establish their nearest visible surface, then blend
one translucent layer. Rear/interior faces no longer accumulate opacity in
mesh order. The visible ghost retains depth for later world effects.

## Verification and limits

- Original-client arithmetic: 1,344 reachable vectors match exact XYZ float
  velocities and jump initiation; all 18 class profiles match 10 official
  fields. The fixture retains 256 explicitly excluded raw hover/non-UGC states.
  This fixture stops before collision and is not a whole-program oracle.
- Server: 191 focused flight/input/replication/turret tests passed. Turret tests
  use the real projectile engine and verify enemy impacts in four directions,
  survival across differing clock origins, and reachable-target selection.
- Native movement, delayed jump replay, glider altitude/release, turret source
  correlation, and corpse interpolation regressions passed.
- Connected Scout, Zombie, and Specialist tests used their actual speed
  multipliers, sprinting, jumping, strafing, crouching, and rapid turns. Local
  and 100 ms delayed/jittered tests recorded zero applied position/velocity
  correction. Two initial Zombie harness runs failed only their hardcoded
  block-tool request: that class's loadout has no tool 5. Movement-only reruns
  passed; Scout's real block placement echo passed locally and with delay.
  The first trace filenames used incorrect class names; their numeric class
  IDs were checked against the official constants before the final handoff.
  Explicit Soldier 0 local/delayed and Miner 3 delayed runs also passed with
  zero raw or applied corrections. Soldier block echoes passed. Miner's first
  build attempt correctly failed because its official starting block stock is
  zero; the subsequent movement-only run passed.
- All three flight packs passed connected 30-second local and delayed runs,
  including activation, release, depletion and refill. Local runs had zero
  raw/applied corrections and no owner ACK gaps above seven input loops.
- GPU readback tests use the production renderer: overlapping/reversed ghost
  surfaces produce identical pixels, opaque geometry occludes them, and zero
  opacity writes nothing. Grain variation falls from 3.46491 nearby to 0.527046
  when minified. DX11 ran; all five shader backends compiled.
- Frozen server: 27 embedded-module comparisons passed across three launchers.
  The verified native world DLL is unchanged. The candidate's `--check` passed.

Detailed logs, source verification, trajectory CSVs, GPU captures, and staging
receipts are retained under `tmp/playtest-polish-20260920`. Captures are isolated
GPU scenes, not a recreation of the user's exact camera. Full-suite timing and
final staging results are recorded in that directory's final verification report.

## Focused playtest

1. Sprint and repeatedly jump with Soldier and another fast class, including
   steps, slopes, looking up/down, crouching, and building while moving.
2. Hold Space from the ground with rocket 66 and Engineer 68; verify ascent.
   Switch to glider 67 and check near-level flight and slower fuel use. Release,
   land, refill, exhaust the pack, and try another launch.
3. Place a turret facing an enemy in each direction and at different elevations.
   Verify that the gun follows the rocket direction and rockets reach targets.
4. Die while wearing a flight pack; observe the corpse and death camera before
   the explosion. Check a prefab preview over existing terrain from several
   angles, and examine distant/grazing sand faces for reduced speckle shimmer.
