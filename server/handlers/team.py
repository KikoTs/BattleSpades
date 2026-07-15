"""Team, palette, and menu-state packet handlers."""

from __future__ import annotations

import logging

from protocol.handler_registry import register_handler
from server.game_constants import KILL_TEAM_CHANGE, PALETTE_TOOL_IDS

logger = logging.getLogger(__name__)


@register_handler(77)  # ChangeTeam
async def handle_change_team(server, player, packet) -> None:
    """Move a player to a playable team and end the old life."""
    from server.connection import wire_team_to_internal

    wire_team = packet.team
    new_team = wire_team_to_internal(wire_team)
    if new_team is None:
        logger.debug(
            "Ignoring ChangeTeam from %s for wire team %s",
            player.name,
            wire_team,
        )
        return
    if new_team == player.team:
        return
    mode = getattr(server, "mode", None)
    allows_team_change = getattr(mode, "allows_team_change", None)
    if callable(allows_team_change) and not allows_team_change(player, new_team):
        logger.debug("Ignoring mode-locked team change from %s", player.name)
        return
    old_team = player.team
    if player.team in server.teams:
        server.teams[player.team].remove_player(player)
    player.team = new_team
    if new_team in server.teams:
        server.teams[new_team].add_player(player)
    if player.alive:
        player.die(kill_type=KILL_TEAM_CHANGE)
    server.queue_mode_event("on_player_team_change", player, old_team, new_team)


@register_handler(11)  # SetColor
async def handle_set_color(server, player, packet) -> None:
    """Commit a live palette-tool colour and announce it to other players.

    The retail client already applies its palette choice locally.  Echoing the
    packet to its sender races self WorldUpdate processing and visibly flickers
    the held block, while dead/non-palette-tool updates are invalid state.
    """
    if (
        not player.alive
        or not player.spawned
        # FlareBlockTool (22), BlockTool (5), and both Block Cannons (29/48)
        # share Character.block_color. The retail Snowblower.on_set explicitly
        # activates the same palette, so its SetColor is gameplay state too.
        or int(player.tool) not in PALETTE_TOOL_IDS
    ):
        return
    value = int(packet.value) & 0xFFFFFF
    player.set_color(value)
    from shared.packet import SetColor

    broadcast_packet = SetColor()
    broadcast_packet.player_id = player.id
    broadcast_packet.value = value
    # The sender has already committed this colour in its palette UI.
    server.broadcast(bytes(broadcast_packet.generate()), exclude=player)


@register_handler(110)  # ClientInMenu
async def handle_client_in_menu(server, player, packet) -> None:
    """Track menu state used by safe round and class transitions."""
    if player.connection:
        player.connection.in_menu = bool(packet.in_menu)
