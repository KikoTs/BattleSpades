"""Chat, command, and vote packet handlers."""

from __future__ import annotations

import time

from protocol.handler_registry import register_handler


@register_handler(48)  # InitiateKickMessage
async def handle_initiate_kick(server, player, packet) -> None:
    """Start or cancel a server-owned kick vote."""
    from server.voting import KICK_CANCEL

    reason = int(getattr(packet, "reason", 0))
    if reason == KICK_CANCEL:
        server.vote_manager.cancel()
        return
    target = server.players.get(int(getattr(packet, "target_id", -1)))
    if target is not None:
        server.vote_manager.start_kick(player, target, reason, time.time())


@register_handler(47)  # GenericVoteMessage
async def handle_generic_vote(server, player, packet) -> None:
    """Record one vote for the active server-owned ballot."""
    from server.voting import VOTE_CAST

    if int(getattr(packet, "message_type", -1)) != VOTE_CAST:
        return
    candidates = getattr(packet, "candidates", None) or []
    choice_yes = True
    if candidates and isinstance(candidates[0], dict):
        choice_yes = str(
            candidates[0].get("name", "Yes")
        ).lower().startswith("y")
    server.vote_manager.cast(player, choice_yes)


@register_handler(49)  # ChatMessage
async def handle_chat(server, player, packet) -> None:
    """Dispatch slash commands or broadcast a validated chat message."""
    message = packet.value
    if message.startswith("/"):
        # Muting suppresses public/team chat, not the command channel. Keeping
        # this check first lets a muted admin use /unmute and lets ordinary
        # muted players still reach harmless commands such as /help or /ping.
        from commands import handle_command

        await handle_command(server, player, message[1:])
        return
    if player.muted:
        return
    from shared.packet import ChatMessage

    broadcast_packet = ChatMessage()
    broadcast_packet.player_id = player.id
    broadcast_packet.chat_type = packet.chat_type
    broadcast_packet.value = message
    server.broadcast(bytes(broadcast_packet.generate()))
