"""Class and loadout packet handlers.

Packets 13 and 78 may arrive in either order.  Both handlers stage or commit a
complete :class:`ClassSelection`, never independently mutate class and tools.
"""

from __future__ import annotations

import logging

from protocol.handler_registry import register_handler
from server.class_selection import normalize_server_selection
from server.game_rules import get_rules
from server.game_constants import KILL_CLASS_CHANGE

logger = logging.getLogger(__name__)


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
    same_live_class = (
        player.alive and selection.class_id == int(player.class_id)
    )
    if instant or same_live_class:
        # Committing all fields synchronously prevents the Miner/Medic
        # split-brain that used to authorize packet 90 after switching class.
        player.apply_class_selection(selection)
        player.pending_selection = None
        player.pending_class_id = None
        player.pending_loadout = None
    else:
        player.stage_class_selection(selection)
        if selection.class_id != int(player.class_id) and player.alive:
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
