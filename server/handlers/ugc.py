"""Wire-safe handlers for the isolated retail UGC Map Creator protocol."""

from __future__ import annotations

import logging

import shared.constants as C

from protocol.handler_registry import register_handler
from server.class_selection import active_tool_authorized


logger = logging.getLogger(__name__)


def _ugc_mode(server):
    """Return the active isolated mode or ``None`` on ordinary servers."""

    if not bool(getattr(server.config, "ugc_runtime", False)):
        return None
    mode = getattr(server, "mode", None)
    return mode if callable(getattr(mode, "send_initial_batch", None)) else None


@register_handler(12)  # SetUGCEditMode
async def handle_set_ugc_edit_mode(server, player, packet):
    """Let only the editor host change the project validation context."""

    mode = _ugc_mode(server)
    if mode is None:
        return
    if not mode.set_target_mode(player, int(getattr(packet, "mode", -1))):
        logger.debug("Rejected UGC mode change from %s", getattr(player, "name", "?"))


@register_handler(51)  # SkyboxData: host setter -> server -> editor guests
async def handle_ugc_skybox(server, player, packet):
    """Relay only a safe skydome basename selected by the active host."""

    mode = _ugc_mode(server)
    if mode is not None:
        mode.set_skybox(player, str(getattr(packet, "value", "")))


@register_handler(118)  # SetGroundColors
async def handle_ugc_ground_colors(server, player, packet):
    """Apply the complete terrain/water palette selected in UGC Settings."""

    mode = _ugc_mode(server)
    if mode is not None:
        mode.set_ground_colors(player, getattr(packet, "ground_colors", ()))


@register_handler(102)  # UGCMapInfo
async def handle_ugc_map_info(server, player, packet):
    """Accept the host's optional PNG preview without gameplay-thread I/O."""

    mode = _ugc_mode(server)
    if mode is not None:
        await mode.receive_preview(player, bytes(getattr(packet, "png_data", b"")))


@register_handler(97)  # PlaceUGC
async def handle_place_ugc(server, player, packet):
    """Validate and commit a raw-voxel editor object placement/removal."""

    mode = _ugc_mode(server)
    if mode is None or not mode.is_host(player):
        return
    if (
        not active_tool_authorized(player, int(C.UGC_TOOL))
        or not bool(getattr(player, "tool_is_raw", False))
    ):
        return
    try:
        x = int(packet.x)
        y = int(packet.y)
        z = int(packet.z)
        item = int(packet.ugc_item_id)
        placing = bool(int(packet.placing))
    except (AttributeError, TypeError, ValueError):
        return
    if not (0 <= x < 512 and 0 <= y < 512 and 0 <= z < 256 and 0 <= item < 19):
        return
    # The recovered client limits its ghost to ten blocks.  Two extra blocks
    # cover one delayed movement sample without allowing remote map edits.
    dx = float(getattr(player, "x", 0.0)) - x
    dy = float(getattr(player, "y", 0.0)) - y
    dz = float(getattr(player, "z", 0.0)) - z
    if dx * dx + dy * dy + dz * dz > 12.0 * 12.0:
        return
    mode.place_object(player, x, y, z, item, placing)


@register_handler(99)  # ReqestUGCEntities (retail spelling)
async def handle_request_ugc_entities(server, player, packet):
    """Replay packet-98 objects and validation after a client-side refresh."""

    mode = _ugc_mode(server)
    connection = getattr(player, "connection", None)
    if mode is None or connection is None or not bool(getattr(connection, "in_game", False)):
        return
    mode.send_initial_batch(connection)
    mode.send_objectives(connection)


@register_handler(100)  # UGCMessage
async def handle_ugc_message(server, player, packet):
    """Handle the five recovered editor control requests without unsafe echoes."""

    mode = _ugc_mode(server)
    connection = getattr(player, "connection", None)
    if mode is None or connection is None:
        return
    message = int(getattr(packet, "message_id", -1))
    if message == int(C.UGC_REQUEST_MAPINFO):
        mode.send_preview(connection)
        return
    if message in (int(C.UGC_REQUEST_VXL), int(C.UGC_NO_VXL_USE_BASEPLATE)):
        # These are lobby-stage guest requests.  The dedicated launcher
        # answers them proactively with the exact packet 54/56/58 zlib stream
        # before MapDataValidation.  Replaying packet 101 or editor entities
        # after GameScene exists is both semantically wrong and crash-prone.
        logger.debug(
            "Ignored late UGC source-map request %d from %s",
            message,
            getattr(player, "name", "?"),
        )
        return
    if message in (
        int(C.UGC_REQUEST_MAP_VALIDATION),
        int(C.UGC_CONVERT_TO_GAME),
    ) and mode.is_host(player):
        mode.send_objectives(connection)
        mode.request_checkpoint()


__all__ = [
    "handle_ugc_ground_colors",
    "handle_ugc_map_info",
    "handle_ugc_skybox",
    "handle_place_ugc",
    "handle_request_ugc_entities",
    "handle_set_ugc_edit_mode",
    "handle_ugc_message",
]
