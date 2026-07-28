"""Class and loadout packet handlers.

Packets 13 and 78 may arrive in either order.  Both handlers stage or commit a
complete :class:`ClassSelection`, never independently mutate class and tools.
"""

from __future__ import annotations

import logging

from protocol.handler_registry import register_handler
import shared.constants as C
from server.class_selection import normalize_server_selection
from server.game_rules import get_rules
from server.game_constants import KILL_CLASS_CHANGE
from shared.packet import SetClassLoadout

logger = logging.getLogger(__name__)


def _matches_active_selection(player, selection) -> bool:
    """Return whether ``selection`` already describes the current life."""

    return (
        int(selection.class_id) == int(player.class_id)
        and tuple(selection.loadout) == tuple(getattr(player, "loadout", ()) or ())
        and tuple(selection.prefabs) == tuple(getattr(player, "prefabs", ()) or ())
        and tuple(selection.ugc_tools)
        == tuple(getattr(player, "ugc_tools", ()) or ())
    )


def _allows_live_ugc_selection(server, player, selection) -> bool:
    """Keep live backpack editing isolated to the Map Creator runtime."""

    return (
        bool(getattr(server.config, "ugc_runtime", False))
        and int(player.class_id) == int(C.CLASS_UGCBUILDER)
        and int(selection.class_id) == int(C.CLASS_UGCBUILDER)
    )


def _broadcast_live_selection(server, player, selection) -> None:
    """Publish a normalized live UGC backpack selection to the client."""

    acknowledgement = SetClassLoadout()
    acknowledgement.player_id = int(player.id)
    acknowledgement.class_id = int(selection.class_id)
    acknowledgement.instant = 1
    acknowledgement.loadout = list(selection.loadout)
    acknowledgement.prefabs = list(selection.prefabs)
    acknowledgement.ugc_tools = list(selection.ugc_tools)
    broadcast = getattr(server, "broadcast", None)
    if callable(broadcast):
        broadcast(bytes(acknowledgement.generate()), reliable=True)


@register_handler(13)  # SetClassLoadout
async def handle_set_class_loadout(server, player, packet) -> None:
    """Normalize and atomically stage or commit a client menu selection."""
    selection = normalize_server_selection(
        server.config,
        getattr(packet, "class_id", player.class_id),
        getattr(packet, "loadout", ()) or (),
        getattr(packet, "prefabs", ()) or (),
        getattr(packet, "ugc_tools", ()) or (),
        fallback_class_id=player.class_id,
    )
    if not get_rules(server.config).is_class_enabled(selection.class_id):
        logger.debug("Ignoring disabled class/loadout from %s", player.name)
        return
    mode = getattr(server, "mode", None)
    allows_selection = getattr(mode, "allows_class_selection", None)
    if callable(allows_selection) and not allows_selection(player, selection):
        logger.debug("Ignoring mode-locked loadout change from %s", player.name)
        return
    instant = bool(getattr(packet, "instant", 0))
    if _matches_active_selection(player, selection):
        logger.debug("Ignoring unchanged class/loadout from %s", player.name)
        return
    if _allows_live_ugc_selection(server, player, selection):
        # Map Creator changes its active Constructs backpack without creating a
        # new playable life. Keep that exception isolated from normal matches.
        player.apply_class_selection(selection)
        player.pending_selection = None
        player.pending_class_id = None
        player.pending_loadout = None
        _broadcast_live_selection(server, player, selection)
    else:
        # A same-class equipment swap still needs a new life. Committing it on
        # the live Character gives the replacement tool zero charges because
        # its CreatePlayer/restock path never ran.
        player.stage_class_selection(selection)
        if player.alive:
            player.die(kill_type=KILL_CLASS_CHANGE)
    logger.info(
        "LOADOUT %s -> class=%d loadout=%s instant=%s",
        player.name,
        selection.class_id,
        list(selection.loadout),
        instant,
    )


@register_handler(78)  # ChangeClass
async def handle_change_class(server, player, packet) -> None:
    """Stage a class change and end the old life exactly once."""
    requested_class = int(getattr(packet, "class_id", player.class_id))
    pending = getattr(player, "pending_selection", None)
    if pending is not None and int(pending.class_id) == requested_class:
        selection = pending
    else:
        selection = normalize_server_selection(
            server.config,
            requested_class,
            fallback_class_id=player.class_id,
        )
    if not get_rules(server.config).is_class_enabled(selection.class_id):
        logger.debug("Ignoring disabled class change from %s", player.name)
        return
    mode = getattr(server, "mode", None)
    allows_selection = getattr(mode, "allows_class_selection", None)
    if callable(allows_selection) and not allows_selection(player, selection):
        logger.debug("Ignoring mode-locked class change from %s", player.name)
        return
    player.stage_class_selection(selection)
    if selection.class_id != int(player.class_id) and player.alive:
        player.die(kill_type=KILL_CLASS_CHANGE)
