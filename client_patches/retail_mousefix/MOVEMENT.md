# Retail movement compatibility

The movement patch applies to the local character on every server. There is
no server-address list or custom handshake. Disable it with `+legacymovement`
or an empty `aos_movementfix.disabled` file, then restart the game.

## What was compared

The reference server is the current working tree of `G:/AoSRevival/BattleSpades`,
including its uncommitted movement changes. That tree was read and tested;
no server files or running server settings were changed by this task.

- `server/player.py: Player.update` advances native physics from its current
  position and never rewinds it to a previously advertised owner snapshot.
- `_consume_input_frame` matches retail's one-frame locomotion/aim latch while
  applying the current crouch geometry. Changing that client ordering would
  break the current agreement.
- `record_input_frame`, `_salvage_late_frame` and `_consume_input_frame` handle
  duplicate/reordered inputs and refill small lost-label gaps. A lost packet
  containing a button edge can still cause a legitimate correction.
- `docs/RETAIL_JUMP_RESTORE.md` identifies the extra retail launch-frame reset.
  Revival's `tools/patch_character_jump_restore.py` omits that exact statement.

## Runtime implementation

`aos_movementfix.py` observes `aoslib.character` becoming available, checks the
actual loaded Character/World files against the inspected retail SHA256 values,
and verifies that the three target methods are still native. Unsupported or
already patched binaries are skipped; other loader features continue working.

The hook scopes one live `Character.update_alive` call to the current local
character. After its first `Player.update` completes with `jump_this_frame`,
it suppresses only the following positional `set_position(x, y, z)` call that
exactly matches `character.network_position`. A different position call or
second physics step disarms the exception. Scope is restored even if the
original method raises, and nested remote updates cannot consume it.

Thus the launch keeps its actual post-physics position and velocity. Jump
sounds, jump rules, collision resolution, teleports, movement history, replay,
walking and ordinary authoritative corrections retain their stock behavior.
No reliable-input transport, redundant packet stream, clock offset, speed,
gravity, correction threshold or flight capability is added.

The static World extension type rejects ordinary Python attribute assignment.
For this verified CPython 2.7 x86 build, `gc.get_referents` obtains its dictionary
through the real dictproxy's traversal instead of guessing memory offsets.
The replacement invalidates the type lookup cache with `PyType_Modified`.
Installation rolls back on failure. All changes are in process memory;
EXE, PKG and PYD files are not rewritten.

References: [CPython 2.7 dictproxy traversal](https://github.com/python/cpython/blob/v2.7.18/Objects/descrobject.c)
and [type-cache invalidation](https://docs.python.org/3/c-api/type.html#c.PyType_Modified).

## Validation and limits

- 53 existing loader/UI/mouse checks and 18 movement-boundary checks pass on
  Python 2.7 x86. The bootstrap tests include the movement feature switch.
- 1,711 current BattleSpades checks pass across `test_retail_binary_movement`,
  `test_reversed_movement_engine`, `test_movement_jitter` and
  `test_input_loss_recovery`. These cover retail arithmetic, collision rules,
  input timing and loss recovery; they do not prove every live route is exact.
- `smoke_movement.py` loads the SHA256-verified original 32-bit `world.pyd` and
  a synthetic VXL map in an isolated process. 480 frames, including walk,
  sprint, crouch, turns and eight jumps, match the native reference position
  and velocity exactly. Eight stale launch resets are suppressed; ordinary
  position changes and restoration of the exact native descriptors pass.
  The test uses a small Character shell to isolate the identified statement;
  it is not a substitute for full-game reconciliation measurements.
- The actual retail client loads the hook and logs the first local Character
  update after spawning. Automated Space taps did not produce the first-jump
  suppression marker; a physical-keyboard jump is still unverified in game.
  The log distinguishes installation, the first local update and the first
  suppressed stale restore so these stages can be checked independently.

The walking predictor was not replaced because the inspected tests and code
already agree with the current server. This patch removes one confirmed local
reset, not all possible rubber-banding. Terrain changes, packet loss and flight
state handoffs can still require server corrections. Long multiplayer and
high-latency playtests remain useful, particularly against other server builds.
