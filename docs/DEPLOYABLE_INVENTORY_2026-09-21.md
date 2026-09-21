# Authoritative deployable inventory

`server/deployable_actions.py` is the common boundary for human placement
packets and bot intentions. Its stock and cadence now come from
`server/deployable_inventory.py`, using the existing recovered values in
`shared/constants.py`.

| Tool | Spawn | Maximum | Ammo pickup adds | Placement interval |
| --- | ---: | ---: | ---: | ---: |
| Medpack | 2 | 2 | 1 | 1 second |
| Dynamite | 1 | 3 | 3 | 1 second |
| Landmine | 3 | 5 | 5 | 1 second |
| C4 | 2 | 2 | 1 | 1 second |
| Radar station | 1 | 1 | 1 | 1.5 seconds |

Each successful entity creation spends one item and starts its deadline
before replication. Invalid authorization/geometry, empty stock, cooldown,
live-entity caps, and a registry creation failure spend nothing. If a later
broadcast fails, the already-created entity retains its cost and deadline.
The gameplay thread does not yield between preflight, creation and commit.

The existing C4 two-live-charge limit and radar one-live-station limit still
apply in addition to carried stock. Detonation, expiry, healing depletion,
support destruction, ordinary tool changes and project cancellation do not
return carried stock. An actual ammo pickup adds the recovered amount, capped
at maximum, and leaves placement cooldowns intact. Spawn starts a new wallet
and clears its cadence; the subsequent `restock_ammo(type=0)` spawn notification
does not grant a second set of deployables. Both ordinary respawning and bot
creation use this same Player lifecycle.

Oriented dynamite/landmine uses also share this wallet and deadline: alternating
packet 10 with a placement packet cannot create extra stock. Other oriented
projectile wallets are unchanged. Rocket turrets keep their dedicated stock
controller and are debited only there; their placement service enforces the
existing 1.5-second interval. The mounted MG keeps its one-owned-entity limit
and its existing 0.5-second placement-tool interval. Disguise keeps its existing
wallet/cadence. No new bot-only grants were added.

`deployable_stock(player, tool_id) -> int` returns available placements for an
equipped supported tool, including the C4/radar/MG live cap. It deliberately
excludes the short placement cooldown. `deployable_inventory_snapshot(player)`
returns a sorted `tuple[tuple[int, int], ...]` of these tool/count pairs, including
zero counts. Both helpers are read-only: they never initialize a wallet, prune
owner IDs, or grant items. Unsupported/unequipped tools return zero.

Normal Medic packs still heal 25 HP per touch for three uses, as supplied by
the deployment service from `MEDPACK_HEAL_AMOUNT`/`MEDPACK_USES`. The behavior
class's stale claim that production packs full-heal was corrected; healing
balance was not changed.

Validation covers real services, packet decode, BotActionGateway, entity
registry failures, replication failures, Player restock/spawn, RoundLifecycle,
explosive packet-path sharing, radar expiry/support loss, C4 detonation/live
caps, medpack healing/depletion, and preservation of turret/MG/Disguise stock.

Run from the server repository:

```powershell
& C:/Users/todor/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/test_deployable_inventory.py tests/test_equipment_handlers.py tests/test_entities_behaviors.py tests/test_rocket_turret.py tests/test_projectiles.py tests/test_pickups.py tests/test_player_ammunition.py -q
```

The final focused run passed all 148 tests in 63.33 seconds. This includes
preservation/failure checks for MG, Disguise and turret transport failure.
No public-server connections, native builds or packaging are part of this
inventory change.
