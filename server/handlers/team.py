"""Team, palette, and menu-state packet handlers."""

from __future__ import annotations

import logging
import time

from protocol.handler_registry import register_handler
from server.game_constants import (
    KILL_TEAM_CHANGE,
    PALETTE_TOOL_IDS,
    TEAM_SPECTATOR,
)

logger = logging.getLogger(__name__)


@register_handler(77)  # ChangeTeam
async def handle_change_team(server, player, packet) -> None:
    """Move a player to a playable team or the native spectator roster."""
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
    if new_team == TEAM_SPECTATOR:
        from server.game_rules import get_rules

        if not get_rules(server.config).enabled("RULE_ENABLE_SPECTATORS"):
            return
    if new_team == player.team:
        return
    mode = getattr(server, "mode", None)
    allows_team_change = getattr(mode, "allows_team_change", None)
    if callable(allows_team_change) and not allows_team_change(player, new_team):
        logger.debug("Ignoring mode-locked team change from %s", player.name)
        return
    old_team = player.team
    # Team-bound deployables snapshot allegiance at placement. Retire them
    # before changing the owner or turrets may acquire their former owner and
    # radar reference counts remain attached to the old team.
    lifecycle = getattr(server, "round_lifecycle", None)
    retire = getattr(lifecycle, "remove_owned_deployables", None)
    if callable(retire):
        retire(player)
    if player.team in server.teams:
        server.teams[player.team].remove_player(player)
    player.team = new_team
    if new_team in server.teams:
        server.teams[new_team].add_player(player)
    if player.alive:
        player.die(kill_type=KILL_TEAM_CHANGE)
    if new_team == TEAM_SPECTATOR:
        # KillAction retires the old Character. CreatePlayer(team=0) then
        # moves every retail roster to its spectator representation. There is
        # no server->client ChangeTeam handler in this build.
        player.death_time = 0.0
        from server.roster import build_create_player, remember_player_life

        data = bytes(build_create_player(player).generate())
        server.broadcast(data)
        for connection in server.connections.values():
            if getattr(connection, "in_game", False):
                remember_player_life(connection, player)
    elif old_team == TEAM_SPECTATOR:
        # A spectator has no death event to schedule. Arm one ordinary
        # respawn so its staged class/loadout is applied at the same boundary
        # as every other team transition.
        player.death_time = time.time()
    server.queue_mode_event("on_player_team_change", player, old_team, new_team)


@register_handler(11)  # SetColor
async def handle_set_color(server, player, packet) -> None:
    """Commit a live palette-tool colour and announce it to other players.

    The retail client already applies its palette choice locally.  Echoing the
    packet to its sender races self WorldUpdate processing and visibly flickers
    the held block, while dead/non-palette-tool updates are invalid state.
    """
    from server.game_rules import get_rules
    config = getattr(server, "config", None)
    if (
        not player.alive
        or not player.spawned
        # FlareBlockTool (22), BlockTool (5), and both Block Cannons (29/48)
        # share Character.block_color. The retail Snowblower.on_set explicitly
        # activates the same palette, so its SetColor is gameplay state too.
        or int(player.tool) not in PALETTE_TOOL_IDS
        or not get_rules(config).enabled("RULE_ENABLE_COLOUR_PICKER")
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
