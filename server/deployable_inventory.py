"""Per-life deployable stock shared by human packets and bot intentions.

Only spawn and an accepted ammo restock grant items. Retiring a placed entity
never refunds its cost. Existing turret/disguise wallets and owned-entity limits
remain authoritative; snapshots observe them without creating inventory.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import shared.constants as C

if TYPE_CHECKING:
    from server.player import Player


@dataclass(frozen=True)
class DeployableStockRule:
    initial: int
    maximum: int
    restock: int
    interval: float


STOCK_RULES: dict[int, DeployableStockRule] = {
    int(getattr(C, f"{name}_TOOL")): DeployableStockRule(
        initial=int(getattr(C, f"{name}_INITIAL_STOCK")),
        maximum=int(getattr(C, f"{name}_STOCK")),
        restock=int(getattr(C, f"{name}_RESTOCK_AMOUNT")),
        interval=float(getattr(C, f"{name}_SHOOT_INTERVAL")),
    )
    for name in ("MEDPACK", "DYNAMITE", "LANDMINE", "C4", "RADAR_STATION")
}
_INTERVALS = {
    tool: rule.interval for tool, rule in STOCK_RULES.items()
} | {
    int(C.ROCKET_TURRET_TOOL): float(C.ROCKET_TURRET_SHOOT_INTERVAL),
    int(C.MG_TOOL): float(C.MG_SHOOT_INTERVAL),
}
_SNAPSHOT_TOOLS = frozenset(_INTERVALS) | {int(C.DISGUISE_TOOL)}
# Retail places deployables continuously while LMB is held over a valid ghost
# (Character.update_weapon -> use_weapon_primary each update), so consecutive
# packets arrive one shoot_interval apart on the client clock; admit arrival
# jitter against that schedule.
DEPLOYABLE_CADENCE_GRACE = 0.05


def reset_deployable_inventory(player: Player) -> None:
    """Grant retail initial stock and clear placement cadence for a new life."""

    player.deployable_stock = {
        tool: rule.initial for tool, rule in STOCK_RULES.items()
    }
    player._deployable_next_use = {}


def restock_deployable_inventory(player: Player) -> None:
    """Add each tool's retail restock amount without clearing its cooldown."""

    for tool, rule in STOCK_RULES.items():
        player.deployable_stock[tool] = min(
            rule.maximum, max(0, player.deployable_stock.get(tool, 0)) + rule.restock
        )


def _carried_stock(player: Player, tool: int) -> int:
    """Read the existing wallet; an uninitialized player has no free items."""

    if tool in STOCK_RULES:
        rule = STOCK_RULES[tool]
        stock = getattr(player, "deployable_stock", {}).get(tool, 0)
        return min(rule.maximum, max(0, int(stock)))
    if tool == int(C.ROCKET_TURRET_TOOL):
        return min(int(C.ROCKET_TURRET_STOCK), max(
            0, int(getattr(player, "rocket_turret_stock", 0))
        ))
    if tool == int(C.DISGUISE_TOOL):
        return max(0, int(getattr(player, "disguise_stock", 0)))
    return int(tool == int(C.MG_TOOL))


def deployable_ready(player: Player, tool: int, now: float) -> bool:
    """Check stock and placement cadence before attempting entity creation.

    Authorization, geometry and live-entity caps belong to the action service.
    This gate also covers dynamite/landmine sent via the oriented-item path.
    """

    return (
        tool in _INTERVALS
        and math.isfinite(now)
        and now + DEPLOYABLE_CADENCE_GRACE
        >= getattr(player, "_deployable_next_use", {}).get(tool, 0.0)
        and _carried_stock(player, tool) > 0
    )


def commit_deployable_use(player: Player, tool: int, now: float) -> None:
    """Commit a preflighted creation once, before broadcasting its entity.

    The gameplay thread cannot interleave another placement between preflight
    and commit. Turrets debit their dedicated controller wallet instead.
    """

    if tool in STOCK_RULES:
        player.deployable_stock[tool] -= 1
    if not hasattr(player, "_deployable_next_use"):
        # Custom-mode MG/turret owners can use their existing entity/controller
        # stock without a Player wallet; recording cadence grants no items.
        player._deployable_next_use = {}
    # Scheduled from the earlier due time: a held trigger's packet admitted
    # inside the jitter grace never raises the sustained placement rate.
    previous_due = float(player._deployable_next_use.get(tool, 0.0))
    player._deployable_next_use[tool] = max(now, previous_due) + _INTERVALS[tool]


def deployable_stock(player: Player, tool: int) -> int:
    """Read remaining placements for an equipped tool, excluding cooldown.

    C4 and occupied MGs additionally honor their live-entity caps. No inventory state is
    initialized or pruned here, making this safe for immutable bot snapshots.
    """

    tool = int(tool)
    if tool not in _SNAPSHOT_TOOLS or tool not in (getattr(player, "loadout", ()) or ()):
        return 0
    stock = _carried_stock(player, tool)
    server = getattr(getattr(player, "connection", None), "server", None)
    registry = getattr(server, "entity_registry", None)
    if registry is None:
        return stock
    if tool == int(C.C4_TOOL):
        live = sum(
            1 for entity_id in set(getattr(player, "_c4_entity_ids", ()) or ())
            if (entity := registry.get(entity_id)) is not None and entity.alive
        )
        return min(stock, max(0, int(C.C4_STOCK) - live))
    if tool == int(C.RADAR_STATION_TOOL):
        # Retail: placing a new station replaces the owner's live one, so a
        # live station does not block the next placement (stock still does).
        return stock
    if tool == int(C.MG_TOOL):
        from server.entities.machine_gun import MachineGunBehavior

        return int(not any(
            entity.alive
            and isinstance(entity.behavior, MachineGunBehavior)
            and entity.behavior.owner_id == int(player.id)
            and entity.behavior.carrier_id is not None
            for entity in registry.all()
        ))
    return stock


def deployable_inventory_snapshot(player: Player) -> tuple[tuple[int, int], ...]:
    """Return equipped supported tools and available stock, including zeroes."""

    return tuple(
        (tool, deployable_stock(player, tool))
        for tool in sorted(_SNAPSHOT_TOOLS.intersection(
            int(item) for item in (getattr(player, "loadout", ()) or ())
        ))
    )
