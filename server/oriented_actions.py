"""Authoritative oriented-projectile action shared by packets and bots."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from server.class_selection import active_tool_authorized

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player


class OrientedActionService:
    """Validate, spawn, consume, and replicate one oriented item use.

    This method runs on the gameplay thread and delegates projectile creation
    to the existing authoritative engine. Malformed primitive values, invalid
    class/loadout/tool state, cadence, or empty stock fail without consuming
    inventory.
    """

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server

    def use(
        self,
        player: "Player",
        *,
        tool_id: int,
        position: tuple[float, float, float],
        velocity: tuple[float, float, float],
        fuse: float,
    ) -> bool:
        """Submit one packet-equivalent oriented projectile action."""

        tool_id = int(tool_id)
        if not active_tool_authorized(player, tool_id):
            return False
        now = time.monotonic()
        can_use = getattr(player, "can_use_oriented_item", None)
        if callable(can_use) and not can_use(tool_id, now):
            return False

        from shared.packet import UseOrientedItem

        packet = UseOrientedItem()
        packet.loop_count = int(getattr(self.server, "loop_count", 0))
        packet.player_id = int(getattr(player, "id", 0))
        packet.tool = tool_id
        packet.value = float(fuse)
        packet.position = tuple(float(value) for value in position)
        packet.velocity = tuple(float(value) for value in velocity)
        if self.server.spawn_grenade(player, packet) is False:
            return False
        consume = getattr(player, "consume_oriented_item", None)
        if callable(consume) and not consume(tool_id, now):
            return False
        player.disguised = False
        return True
