# Parachute (equipment 72, A370)

Policy, evidence and live measurements, 2026-09-26. Code: `server/player.py`
(`_update_parachute`, `_advance_parachute_physics`,
`_note_parachute_owner_row`, `_parachute_after_move`,
`_parachute_speed_damage`). Tests: `tests/test_parachute.py`.
Supersedes the trigger/lifecycle parts of `FLIGHT_PARACHUTE_2026-09-20.md`;
its model/placement findings still stand.

## What the retail client tells us

| Evidence | Where | Consequence |
| --- | --- | --- |
| No parachute key binding; `hover` (Z) is listed under UGC controls only | `aoslib/config.py`, `controlsTab.py` | A stock client cannot send a "deploy" command. |
| `Character.set_hover` admits only pack 69 | `character.pyd` 0x100233D0 | The ClientData hover bit is never set by a stock Soldier. |
| ClientData has no parachute field | `GameScene.send_client_data` 0x1016AAE0 | Deployment is server-owned. |
| Canopy state comes from WorldUpdate state bit 0x01 for local and remote players (`set_parachute_active`) | `gameScene.pyd` 0x10182900 / 0x10185390 | The owner applies it when the row is processed, i.e. late. |
| Client clears the canopy itself on spawn and death | `character.pyd` 0x1001700B / 0x1003393B | No handoff needed there. |
| Canopy physics: gravity x0.05, ordinary drag, fall distance zeroed every canopy frame | `world.pyd` 0x10012EFB, 0x10012CD0 | Terminal descent 0.05 native = 1.6 blocks/s; a canopy frame erases the fall so far. |
| **`world.pyd` keeps the airborne SPACE request only for jetpack *and parachute* holders** | 0x10012D32..0x10012D48 (`test_retail_world_branches.py`) | The only retail input the engine preserves for chute holders in the air is SPACE. |
| String: "+ Fall from great heights / - Vulnerable when in use"; sounds `commando_item_PARACHUTE_open/loop/close` | `strings/english.py`, `constants_audio.py` | Commando (Soldier) item; open/close are events. |
| `CRATE_PARACHUTE_DEPLOYMENT_HEIGHT = 10`, `REMOVAL_HEIGHT = 2`, `SLOWDOWN = 0.75` | `shared/constants.py` | Supply crates only; not player values. |

The original server is unavailable, so the deploy rule itself is a
BattleSpades decision. SPACE is the natural choice: it works on every stock
client (Steam included), and it is the one input the native mover keeps alive
in the air for a parachute holder.

## Rules

- **Trigger**: a fresh SPACE press while airborne (retail clients and, since
  2026-09-27, the native BattleSpades client, which predicts the edge through
  these same rules), or a fresh hover/Z press (native client extra binding,
  the optional `client_patches/parachute_key_patch.py`, or bot AI). A SPACE
  held from the ground jump is not a press. Bot SPACE is locomotion only.
- **Descending only**: a press during ascent arms the canopy; it opens once
  vertical speed is downward (no jump boost).
- **Minimum clearance 6 blocks** (feet to the nearest ground under the centre
  and the four hull corners). A flat-ground jump peaks at ~1.3 blocks, so hops
  can never float. An armed press stays armed for the rest of the fall and
  opens as soon as the clearance is there (walking off a ledge works).
- **One deploy per fall**, re-armed only by landing or entering water.
- **Closes on**: landing, water, death, unequip, picking up a jetpack, 30 s
  open (`PARACHUTE_MAX_OPEN_SECONDS`, ~48 blocks of canopy descent), or being
  lifted faster than the canopy's own descent (vz < -0.05; an explosion under
  a 0.05-gravity canopy would otherwise become a long float). None of these
  can reopen it before landing.
- **No flight stacking**: a player carrying any jetpack cannot open it.
- **Descent is enforced by physics**: gravity keeps pulling (1.6 blocks/s
  terminal), horizontal speed is the ordinary air-control cap. There is no
  upward or hovering state.
- **Fall damage**: the native canopy zeroes the fall distance every frame, so
  a canopy opened one frame before impact would erase any fall. Authority
  instead charges a landing that involved a canopy the damage of a free fall
  that reaches the same landing speed, never less than the native result. A
  canopy that has braked lands free; a late one pays for the speed it still
  has. Measured (Soldier, 40-block drop, unit test): no canopy 98, press at
  5 blocks clearance refused (98), canopy at 6.5 blocks ~20, canopy at 25
  blocks 0.

All values are module constants in `server/player.py`; a same-named
lower-case attribute on the server config overrides them.

## Owner handoff (smoothness)

The server advertises the canopy (state bit 0x01) immediately; that drives
the HUD, sounds and every observer. The native mover flag is separate
(`_parachute_physics_active`):

- no predicting owner (bots, connection-less) or the native BattleSpades
  client: physics follows the advertised state in the same step;
- retail owner: physics switches on the label the owner is expected to first
  move with the new state. The moment an owner self row carrying a changed
  bit is queued (`record_owner_anchor`), the target label is
  `max(S + 3, N + 2) + round(RTT * 60)`, where `S` is the label whose
  simulation queued the row and `N` the newest label already received. Both
  edges (open and close) use it, so a landing followed by an immediate jump
  keeps the owner's canopy tail too. A change no row has carried after 30
  frames is applied anyway.

### Measurements

Validation server on ArcticBase + tracer dev client (stock `character.pyd`
with the jump-restore patch), class Soldier with equipment 72, per-frame
client sampler (position, velocity, canopy flag, ADJUST/SNAP counters,
matched history error) joined with a per-label server hook. Onset is the first
frame whose vertical speed leaves the free-fall recurrence.

| Link | Deploys | Exact onset (0 corrections) | One-frame miss |
| --- | ---: | ---: | ---: |
| Loopback, 25-40 block drops, `S + 3` alone | 9 | 7/9 | 2 |
| Loopback, 25-block drops, final rule | 10 | 8/10 | 2 |
| 80 ms RTT (`udp_lag_proxy --delay-ms 40`), 59-block cliff walk-off | 10 | 6/10 | 4 |

The client's own delay varies by one frame with the phase of its render loop
against the server tick (the same 2-or-3 behaviour documented for jetpacks
in `RETAIL_JUMP_RESTORE.md`); no ClientData field reveals it (the unnamed
byte is a mod-16 frame counter). A one-frame miss produces one smoothed
0.13-0.2 block correction about 12-18 frames after opening. Under latency the
stock client then keeps re-ADJUSTing against its own replayed history (which
it rewrites one frame out of phase) until per-frame motion drops below 0.1
blocks; positions already agree, so those follow-ups do not move the player,
they only count. Landing closes had zero corrections in every run, including
holding SPACE through touchdown (auto bunny-jump with the canopy tail).
The previous code applied canopy physics on the deploy frame itself, 3+
frames before the owner could (not measured live; a 3-frame miss is well past
the 0.1-block ADJUST threshold by the next rows).

Negative checks live: five flat-ground double-tap hops never opened; presses
at under 6 blocks clearance never opened and the landing took full damage.

### Remaining work

- An exact onset would need ClientData arrival timestamps to estimate the
  client frame phase (input buffering is outside the parachute code).
- `replication.py` sends jetpack transitions as urgent reliable owner rows;
  the canopy bit currently rides the ordinary owner cadence (up to 6 frames
  later while airborne, then the handoff). Adding parachute transitions to
  that urgent path would shorten the visual delay; the handoff already keys
  off the actual queued row, so no retuning is needed.
- `client_patches/parachute_key_patch.py` sets the local `world_object.hover`,
  which skips gravity in the stock mover for any pack; with that patch the
  owner floats while Z is held and will be corrected. Stock clients should
  use SPACE.
- Bots do not decide to open a canopy yet (they would do so through
  `action_hover`).

## BattleSpades canopy (BSFP v2, 2026-10-01)

Request: "falling with the parachute is way too slow when deployed at slow
fall speed". The stock mover (re-checked in `world.pyd`: `fmul 0.05` at
0x10012EFD, then the ordinary `/(1+dt)` drag) has no deploy blend, clamp or
minimum: a canopy opened at the top of a fall (SPACE again after the jump, or
the queued Z press, opens at vz ~ 0) creeps from rest toward the 0.05 native
= 1.6 blocks/s terminal, ~25 s for 40 blocks. A fast body brakes with a
one-second time constant toward the same terminal.

Native BattleSpades clients that advertise `BSCF` now receive
`BALANCED_FLIGHT_V2` (`server/flight_profile.py`), whose canopy uses gravity
scale 0.15625 (5 blocks/s terminal) plus a **free-fall floor**: while the body
is slower than that terminal it falls with ordinary gravity, capped at the
terminal; at or above it the stock canopy step runs unchanged. So a slow
deploy reaches 5 blocks/s in ~10 frames (40 blocks in ~8 s), and a fast one
still brakes with the stock drag. Both sides implement it identically
(`aoslib/world.pyx` `parachute_gravity_scale`/`parachute_free_fall_floor`,
native `step_player`), and the free-fall-equivalent landing damage uses the
same canopy step. Landing at 5 blocks/s equals a ~0.5-block free fall: no
damage. Stock clients, bots and BSCF v1 natives keep 0.05 with no floor.
Tests: `tests/test_flight_balance.py` (`..._canopy_...`), native
`test_player_movement` and `test_tutorial_session`.
