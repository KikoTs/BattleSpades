# Replication Parity Stabilization Design

**Date:** 2026-07-10

**Status:** Approved

## Objective

Make BattleSpades a strictly server-authoritative dedicated server whose
movement, voxel world, player presentation, combat, and round lifecycle remain
in sync across the server and every connected stock Ace of Spades 1.x client.

The result must eliminate random local rollback, phantom collision, invisible
remote weapons, inaccurate server hit geometry, incorrect firing behavior, and
clients remaining permanently on the end-of-round screen.

## Ground-truth policy

The compiled stock client is the protocol and behavior authority. Existing
BattleSpades code, comments, tests, reversed Python ports, prior reports, and
recent commits are evidence to evaluate, not ground truth.

Every behavioral change must be supported by all of the following:

1. a deterministic reproduction of the observed failure;
2. the relevant client packet/state path recovered from IDA or a direct client
   runtime probe;
3. a failing automated test or replay that detects the mismatch;
4. a minimal implementation change;
5. an offline regression run;
6. a live two-client confirmation on the stock client.

Recent commits `4344331`, `9935f22`, and `81c8b44` are reviewed as external
work. Passing tests or commit messages do not establish parity by themselves.

## Architecture

### Isolated parity rig

Replication work runs on an isolated validation server rather than the active
public server.

- The validation server uses a dedicated port, initially `27016`.
- Its map and mode are fixed per scenario.
- Two stock clients run with independent game-console and tracer ports.
- Client A performs the action under test.
- Client B observes the replicated result.
- The server, Client A, and Client B are sampled at the same scenario markers.
- Restarting the validation server never interrupts the active public server.

The current `run_server.py` hardcodes `config.toml` and has no safe `--help` or
alternate-config path. The validation rig therefore needs an explicit launcher
that accepts a validation configuration without rewriting the live config.

### Evidence capture

Every scenario records a single correlated artifact containing:

- scenario name and marker;
- server loop count and monotonic time;
- client loop counts;
- decoded incoming and outgoing packet fields;
- raw packet bytes for the packet types under investigation;
- server player state;
- server voxel state for affected coordinates;
- Client A player/world state;
- Client B player/world state;
- reconciliation outcomes such as NO-OP, ADJUST, or SNAP;
- pass/fail reasons.

A mismatch stops the scenario immediately and preserves the artifact. A failed
scenario must never be converted into a passing test by weakening tolerances
without new client evidence.

### Client contract ledger

The reverse-engineering notes map each replicated field to:

- packet id and byte offset;
- reader/writer type and signedness;
- client handler function and IDA address;
- client object field or method affected;
- server producer and consumer;
- runtime probe that verifies the interpretation.

The existing headless IDA audit target is the hash-verified client module:

```text
tmp/ida-audit/gameScene-audit.pyd
SHA-256 3c4bae35f955eaa5c3f0cbfdfa5ce7e9bbc277293de849e3e6219c4cba67c1e7
IDA session: gamescene_audit
```

Client modules outside `gameScene.pyd`, including character, weapon, world,
and mode code, receive separate hash-verified audit databases when required.

## Authority model

### Movement and stance

The server is authoritative. The client predicts locally for responsiveness.
The server consumes authenticated `ClientData` frames and replays their input,
orientation, stance, and action state in client loop order.

The server's self WorldUpdate row must identify the exact client history entry
that corresponds to the reported authoritative position and velocity. A row
must not be stamped with an unrelated global server loop or a client loop whose
input has not produced the reported state.

`PositionData` is validation evidence and an exceptional teleport/spawn input;
it is not the normal movement authority. Client-position pinning is excluded
from the final design.

Movement validation includes walking, backing up, strafing, diagonal movement,
sprinting, crouch enter/exit, crouch movement, sneaking, jumping, stairs, wall
collision, ledges, and input delivery in jittered bursts.

### Voxel world

The server owns the canonical solid/color state of every voxel.

Build, damage, destroy, and collapse operations are atomic at the protocol
boundary. The server does not leave a partial mutation that cannot be described
by the packet sent to clients. Unsupported or over-limit operations are rejected
before mutation.

Collapse must preserve both world parity and the stock falling-building visual.
The current `chunk_check = 0` workaround deliberately disables the client's
native collapse animation and is not accepted as the final behavior. The client
collapse algorithm, limits, seed/order rules, and packet fields must be recovered
and compared with the server implementation. If identical native execution
cannot be guaranteed, the replacement protocol must explicitly communicate the
authoritative collapsed set while still triggering the correct visual path.

After every voxel scenario, affected coordinates are queried on the server and
both clients. A reconnect/full-map sync is also checked to ensure transient
packet parity matches durable map state.

### Player presentation

Replicated gameplay state and observer presentation are distinct contracts.
Presentation includes:

- equipped tool and visible model;
- stance and movement animation;
- aim/orientation;
- primary and secondary fire animation;
- reload state;
- zoom and deployment state;
- fire, jetpack, disguise, parachute, water/goo, and pickup state.

The WorldUpdate bytes following the action byte are not assigned new meanings
from variable names or reports alone. The current source identifies at least one
of them as a state bitfield, while the external R4 report calls it a tool byte.
IDA and marker-byte replay must settle that contradiction before `packet.pyx`
is changed or rebuilt.

Tool/model replication follows the actual client transition path, whether that
is CreatePlayer/loadout data, a dedicated tool packet, class state, or another
verified field. The implementation must not make a weapon visible by toggling
unrelated parachute, disguise, or water-state bits.

### Combat

The server validates tool selection, ammunition, reloads, firing cadence,
target geometry, range, and damage. It reconstructs the same shot geometry the
stock client uses.

Single-projectile weapons and multi-pellet weapons are investigated separately.
The current evidence that a single-projectile `ShootPacket` carries raw aim does
not establish shotgun pellet geometry. Shotgun pellet count, seed expansion,
spread distribution, packet count per trigger, and cadence are recovered from
the client before server logic changes.

Hit geometry is derived from the client character collision/damage code and
verified against standing, crouched, head, torso, leg, edge, diagonal-corner,
moving-target, and distance-boundary scenarios. Visual model dimensions alone
do not define the authoritative hitbox.

Fire-rate validation counts accepted shots and resulting damage over fixed
intervals while recording client trigger times and server receipt times. Network
jitter tolerance is limited to a verified scheduling allowance and must not
permit sustained firing above the client weapon cadence.

### Match lifecycle

The server process remains alive across rounds. The network-visible state moves
through:

```text
active round -> winner/end event -> statistics screen -> fresh round
```

The client receives the verified `StateData`/statistics fields required to enter
and leave the end screen. Starting the next round clears ended state, rebuilds
or reuses the map according to the mode contract, resets objectives and timers,
and respawns eligible players.

Holding an asyncio task reference is not sufficient proof. The lifecycle test
must observe both clients leave the statistics screen and resume normal packet
processing in the new round without reconnecting or restarting the server.

## Workstream order

### 1. Movement and stance reconciliation

Movement is first because incorrect authoritative positions corrupt collision,
observer animation, hitboxes, and combat measurements.

The audit resolves these existing contradictions before implementation:

- documentation describing one input per server tick versus code draining an
  entire buffered batch in one tick;
- historical recommendations to omit self rows versus the active configuration
  including them;
- historical offsets of `+2`, current offset `-1`, and exact-loop client lookup;
- fixed `1/60` simulation steps versus the stock client's observed variable
  frame rate;
- server coast behavior when no fresh frame is available.

### 2. Voxel mutation and collapse

Placement, damage accumulation, destruction, collapse, animation, and full-sync
durability are validated after movement is stable.

### 3. Player presentation

Tool/model and animation state are recovered and implemented without changing
combat authority.

### 4. Combat geometry and timing

Hitboxes, shotgun spread, per-weapon cadence, reload, ammo, and damage are
validated using stable movement and presentation state.

### 5. Match lifecycle

The end-to-next-round transition is corrected last because its scenario resets
all other state and serves as the final long-session integration test.

## Acceptance criteria

### Movement

- Zero SNAP events after scenario warm-up.
- No recurring ADJUST loop during steady movement.
- At the acknowledged client loop, authoritative position error remains below
  the client's linear `0.1`-block correction threshold.
- Client B observes the correct crouch, sprint, jump, and orientation state.
- Input jitter and bounded bursts do not drop or duplicate accepted frames.

### Voxel parity

- Single and multi-cell supported builds match on all three authorities.
- Unsupported builds mutate none of them.
- Every supported dig/damage type removes the same coordinates.
- Large and disconnected collapses produce identical final solidity and the
  expected client visual effect.
- Reconnecting clients receive the same durable voxel state.

### Presentation

- Representative tools from every category display correctly to Client B.
- Idle, walk, crouch, jump, aim, fire, secondary fire, zoom, deployment, and
  reload transitions are visible and return to the correct idle state.
- Tool changes do not toggle unrelated state bits.

### Combat

- Standing and crouched head, torso, legs, edges, and corners agree with client
  hit behavior.
- Moving-target resolution remains correct at accepted input loops.
- Every weapon respects verified range, cadence, ammo, and reload behavior.
- Shotgun pellet directions and damage agree for known aim/seed cases.

### Lifecycle

- Both clients enter the statistics screen after a forced win.
- Both leave it after the configured sequence.
- A new round starts exactly once.
- Players respawn and continue sending/receiving gameplay packets.
- The server process and network listener remain alive throughout.

## Testing strategy

Each behavioral task follows red-green-refactor:

1. Add the smallest automated test or replay that reproduces the mismatch.
2. Run it and confirm the expected failure.
3. Implement only the behavior required by that test and client evidence.
4. Run the focused test.
5. Run the related subsystem suite.
6. Run the complete pytest suite.
7. Run physics replay where movement or collision is affected.
8. Run the isolated two-client scenario.
9. Commit the isolated workstream change.

Existing tests that encode a disproven assumption are corrected only after the
new client evidence is recorded in the test and reverse-engineering notes.

## Failure handling and safety

- Validation uses a separate port and log set.
- The public server is not restarted during investigation.
- `config.toml` is not rewritten to run experiments.
- Invalid actions fail atomically before authoritative mutation.
- Protocol safety caps reject the complete action; they do not sparsify or
  partially apply an action that clients regenerate differently.
- Native client or Cython crashes preserve faulthandler output and the last
  scenario marker.
- Generated binaries are rebuilt only after their source-level behavior has a
  failing test and an exact packet contract.
- Unrelated dirty files and user changes are never included in workstream
  commits.

## Non-goals

- Making movement client-authoritative.
- Replacing stock-client behavior with a visually similar custom protocol.
- Disabling collapse visuals as the permanent parity solution.
- Rewriting every replication component before measuring it.
- Expanding anti-cheat beyond validation required for the recovered stock
  protocol.
- Restarting the server process between ordinary rounds.

## Delivery

Each workstream produces:

- updated reverse-engineering notes;
- deterministic reproduction artifacts;
- focused automated tests;
- the minimal implementation;
- offline verification output;
- a two-client validation result;
- one reviewable commit.

The final integration gate runs all scenarios sequentially through at least one
complete round transition and reconnect, confirming that all player activity
continues to replicate without accumulated desynchronization.
