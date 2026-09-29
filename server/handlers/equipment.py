"""Class and loadout packet handlers.

Packets 13 and 78 may arrive in either order.  Both handlers stage or commit a
complete :class:`ClassSelection`, never independently mutate class and tools.
"""

from __future__ import annotations

import logging
import time

from protocol.handler_registry import register_handler
import shared.constants as C
from server.class_selection import normalize_server_selection
from server.game_rules import get_rules
from server.game_constants import KILL_CLASS_CHANGE
from shared.packet import SetClassLoadout

logger = logging.getLogger(__name__)


def is_class_selectable(server, class_id: int) -> bool:
    """Whether ``class_id`` passes the rules and the mode's class list.

    ``RULE_ENABLE_CLASS_*`` is the operator switch; ``mode_data``'s
    ``allowed_classes`` is what InitialInfo/StateData advertise to the class
    picker (empty = every class). A mode with ``allows_class_selection``
    owns finer (per-team) policy and is consulted by the callers.
    """
    from server import mode_data

    class_id = int(class_id)
    if not get_rules(server.config).is_class_enabled(class_id):
        return False
    mode = getattr(server, "mode", None)
    if callable(getattr(mode, "allows_class_selection", None)):
        return True
    if bool(getattr(server.config, "ugc_runtime", False)):
        # The Map Creator session owns its builder class list.
        return True
    allowed = mode_data.get(
        getattr(server.config, "game_mode", getattr(server.config, "default_mode", ""))
    ).allowed_classes
    return not allowed or class_id in {int(value) for value in allowed}


def fallback_class_id(server) -> int | None:
    """First class a joiner may legally take (mode list order first)."""
    from server import mode_data

    allowed = [
        int(value)
        for value in mode_data.get(
            getattr(server.config, "game_mode", getattr(server.config, "default_mode", ""))
        ).allowed_classes
    ]
    candidates = allowed + [int(C.DEFAULT_CLASS)] + sorted(
        int(value) for value in C.CLASS_ITEMS
    )
    for class_id in dict.fromkeys(candidates):
        if class_id in C.CLASS_ITEMS and is_class_selectable(server, class_id):
            return class_id
    return None


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


# Minimum seconds between two class/loadout changes that END A LIVE
# Character. Picking a class while dead (the retail menu flow between lives)
# never kills anyone and stays instant; inside the window the choice is
# staged for the next respawn instead of killing the player again.
CLASS_CHANGE_DEATH_COOLDOWN_SECONDS = 3.0


def _tell_player(player, message: str) -> None:
    from server.handlers.team import _tell_player as tell

    tell(player, message)


def _end_life_for_class_change(server, player) -> bool:
    """Kill ``player`` for a staged class change unless on cooldown.

    The death goes through :func:`server.handlers.team.end_life_for_transition`
    so a class change right after taking enemy fire is the kill it would
    otherwise deny. Returns whether the Character was retired now.
    """
    from server.handlers.team import end_life_for_transition

    now = time.monotonic()
    last = getattr(player, "_last_class_change_death_at", None)
    if (
        last is not None
        and now - float(last) < CLASS_CHANGE_DEATH_COOLDOWN_SECONDS
    ):
        _tell_player(
            player, "Your new class/loadout applies on your next respawn."
        )
        return False
    try:
        player._last_class_change_death_at = now
    except AttributeError:
        pass
    end_life_for_transition(server, player, KILL_CLASS_CHANGE)
    return True


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
    if not is_class_selectable(server, selection.class_id):
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
            _end_life_for_class_change(server, player)
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
    if not is_class_selectable(server, selection.class_id):
        logger.debug("Ignoring disabled class change from %s", player.name)
        return
    mode = getattr(server, "mode", None)
    allows_selection = getattr(mode, "allows_class_selection", None)
    if callable(allows_selection) and not allows_selection(player, selection):
        logger.debug("Ignoring mode-locked class change from %s", player.name)
        return
    player.stage_class_selection(selection)
    if selection.class_id != int(player.class_id) and player.alive:
        _end_life_for_class_change(server, player)
