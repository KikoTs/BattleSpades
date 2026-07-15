"""Retail-safe top-screen server announcements.

The stock client routes chat/localised packets by ``chat_type``.  Type 3
(``CHAT_BIG``) is the shared top-of-screen announcement lane; packet 73 is
*not* a free-form text packet and accepts only nine hard-coded message IDs.
All functions here run synchronously on the gameplay thread and only enqueue
one bounded ENet packet through ``server.broadcast``.
"""

from __future__ import annotations

from collections.abc import Iterable
import re

from shared.packet import ChatMessage, LocalisedMessage

from server.game_constants import CHAT_BIG

_MAX_ANNOUNCEMENT_LENGTH = 256
_MAX_LOCALISED_PARAMETERS = 16

# Team names are localization IDs on the wire because StateData lets the
# retail client resolve them for its own UI.  Packet 49 does not run that
# lookup, however, so interpolating ``team.name`` into free-form prose would
# leak identifiers such as "TEAM1_COLOR" onto the HUD.  Keep this fallback at
# the packet-49 boundary so every mode/admin/plugin caller gets safe text.
_FREEFORM_VARIABLES = {
    "TEAM_NEUTRAL": "Neutral",
    "TEAM1_COLOR": "Blue",
    "TEAM2_COLOR": "Green",
}
_FREEFORM_VARIABLE_PATTERN = re.compile(
    r"(?<![A-Z0-9_])(" + "|".join(map(re.escape, _FREEFORM_VARIABLES))
    + r")(?![A-Z0-9_])"
)


def _bounded_text(value: object, *, field: str) -> str:
    """Return one protocol-safe string or raise before packet construction."""

    text = str(value)
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > _MAX_ANNOUNCEMENT_LENGTH:
        raise ValueError(
            f"{field} exceeds {_MAX_ANNOUNCEMENT_LENGTH} characters"
        )
    return text


def resolve_freeform_variables(message: object) -> str:
    """Resolve localization IDs that packet 49 would otherwise show raw.

    This is an English fallback for free-form server prose. Client-language
    localization still belongs in :func:`build_localised_overlay`, whose
    packet-50 parameters are resolved independently by each retail client.
    """

    text = str(message)
    return _FREEFORM_VARIABLE_PATTERN.sub(
        lambda match: _FREEFORM_VARIABLES[match.group(1)], text
    )


def build_overlay_message(message: object) -> bytes:
    """Build one free-form ``ChatMessage(49)`` for the global HUD lane."""

    packet = ChatMessage()
    packet.player_id = 0xFF
    packet.chat_type = CHAT_BIG
    packet.value = _bounded_text(
        resolve_freeform_variables(message), field="message"
    )
    return bytes(packet.generate())


def build_localised_overlay(
    string_id: str,
    parameters: Iterable[object] = (),
    *,
    localise_parameters: bool = False,
    override_previous: bool = False,
) -> bytes:
    """Build a localized top-screen ``LocalisedMessage(50)``.

    ``string_id`` names a retail string-table template.  Parameters fill its
    positional ``{0}``/``{1}``/``{2}`` fields.  When ``localise_parameters``
    is true the client first treats every parameter as another string-table
    ID; unknown IDs pass through unchanged in retail builds, allowing player
    names and localized team IDs in the same message.
    """

    values = [
        _bounded_text(value, field=f"parameter[{index}]")
        for index, value in enumerate(parameters)
    ]
    if len(values) > _MAX_LOCALISED_PARAMETERS:
        raise ValueError(
            "localized announcement exceeds "
            f"{_MAX_LOCALISED_PARAMETERS} parameters"
        )
    packet = LocalisedMessage()
    packet.chat_type = CHAT_BIG
    packet.localise_parameters = int(bool(localise_parameters))
    packet.string_id = _bounded_text(string_id, field="string_id")
    packet.parameters = values
    packet.override_previous_message = int(bool(override_previous))
    return bytes(packet.generate())


def broadcast_overlay(server, message: object) -> None:
    """Broadcast one free-form top-screen message to in-game clients."""

    server.broadcast(build_overlay_message(message))


def broadcast_localised_overlay(
    server,
    string_id: str,
    parameters: Iterable[object] = (),
    *,
    localise_parameters: bool = False,
    override_previous: bool = False,
) -> None:
    """Broadcast one localized top-screen message to in-game clients."""

    server.broadcast(
        build_localised_overlay(
            string_id,
            parameters,
            localise_parameters=localise_parameters,
            override_previous=override_previous,
        )
    )


__all__ = [
    "broadcast_localised_overlay",
    "broadcast_overlay",
    "build_localised_overlay",
    "build_overlay_message",
    "resolve_freeform_variables",
]
