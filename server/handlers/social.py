"""Chat, command, and vote packet handlers."""

from __future__ import annotations

import time

import shared.constants as C
from protocol.handler_registry import register_handler

CHAT_ALL = int(getattr(C, "CHAT_ALL", 0))
CHAT_TEAM = int(getattr(C, "CHAT_TEAM", 1))


_LOCAL_UGC_TITLE_COMMAND = "/__local_ugc_title"


def _consume_local_ugc_title(server, player, message: str) -> bool:
    """Consume the private local-editor title bridge when addressed exactly.

    The stock UGC settings menu stores its title in Steam-lobby state and has
    no dedicated game packet.  The maintained local client therefore sends a
    private ChatMessage command.  It is always swallowed (including forged or
    unauthorized attempts) and is never exposed to plugins, admin commands,
    or public chat.
    """

    if not (
        message == _LOCAL_UGC_TITLE_COMMAND
        or message.startswith(_LOCAL_UGC_TITLE_COMMAND + " ")
    ):
        return False
    config = getattr(server, "config", None)
    mode = getattr(server, "mode", None)
    if not bool(getattr(config, "ugc_runtime", False)):
        return True
    is_host = getattr(mode, "is_host", None)
    set_title = getattr(mode, "set_title", None)
    if not callable(is_host) or not is_host(player) or not callable(set_title):
        return True
    title = message[len(_LOCAL_UGC_TITLE_COMMAND):]
    if title.startswith(" "):
        title = title[1:]
    set_title(player, title)
    return True


@register_handler(48)  # InitiateKickMessage
async def handle_initiate_kick(server, player, packet) -> None:
    """Start or cancel a server-owned kick vote."""
    from server.voting import KICK_CANCEL

    reason = int(getattr(packet, "reason", 0))
    if reason == KICK_CANCEL:
        vote_manager = server.vote_manager
        if (
            getattr(vote_manager, "kind", None) == "kick"
            and getattr(vote_manager, "starter_id", None) == int(player.id)
        ):
            vote_manager.cancel(by_starter=True)
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
    if not candidates or not isinstance(candidates[0], dict):
        return
    # GameScene sends the selected candidate record, including the literal
    # localization token advertised by the server. Treat it as opaque: exact
    # wire-token matching supports kick and map ballots without evaluating
    # untrusted client text.
    selected = candidates[0].get("name", "")
    server.vote_manager.cast_wire_candidate(player, selected)


# The retail client's chat entry is bounded by MAX_CHAT_MESSAGE_LENGTH (200;
# shared.constants A1, consumed by the client hud/steam chat widgets).
# Anything longer came from a modified client and is truncated to what a
# stock client can send. MAX_CHAT_SIZE (A994 = 90) is NOT a display width: no
# stock client pyc or pyd reads it, so it was a retail SERVER rule of the
# pyspades-heritage block (RAPID_*, TIMER_*, RUBBERBAND_DISTANCE). Whether it
# truncated, rejected or only bounded something else is unrecovered, and
# cutting stock 200-character lines to 90 is not verified, so it stays unused
# (rules audit 2026-09-27 #30).
CHAT_MAX_LENGTH = int(getattr(C, "MAX_CHAT_MESSAGE_LENGTH", 200))
# Per-player token bucket: a burst of CHAT_BURST lines, then one line per
# 1/CHAT_REFILL_PER_SECOND seconds. Excess lines are dropped quietly.
CHAT_BURST = 5.0
CHAT_REFILL_PER_SECOND = 1.0


def _chat_allowed(player, now: float) -> bool:
    """Consume one token from ``player``'s chat bucket if available."""

    bucket = getattr(player, "_chat_bucket", None)
    if bucket is None:
        tokens, last = CHAT_BURST, now
    else:
        tokens, last = bucket
        tokens = min(
            CHAT_BURST,
            tokens + max(0.0, now - last) * CHAT_REFILL_PER_SECOND,
        )
    allowed = tokens >= 1.0
    if allowed:
        tokens -= 1.0
    try:
        player._chat_bucket = (tokens, now)
    except AttributeError:
        return True
    return allowed


# Slash commands bypass the chat bucket above (muted players must still reach
# /help and admins /unmute), so they get their own per-player bucket: a burst
# of COMMAND_BURST commands, then one per 1/COMMAND_REFILL_PER_SECOND s. This
# bounds /admin password guessing, /pm spam and every command's server work.
# A logged-in admin (valid /admin session) is exempt.
COMMAND_BURST = 5.0
COMMAND_REFILL_PER_SECOND = 1.0
# At most one "slow down" notice per this many seconds per player.
COMMAND_NOTICE_INTERVAL_SECONDS = 5.0


def _command_allowed(player, now: float) -> bool:
    """Consume one token from ``player``'s slash-command bucket."""

    if bool(getattr(player, "admin", False)):
        return True
    bucket = getattr(player, "_command_bucket", None)
    if bucket is None:
        tokens, last = COMMAND_BURST, now
    else:
        tokens, last = bucket
        tokens = min(
            COMMAND_BURST,
            tokens + max(0.0, now - last) * COMMAND_REFILL_PER_SECOND,
        )
    allowed = tokens >= 1.0
    if allowed:
        tokens -= 1.0
    try:
        player._command_bucket = (tokens, now)
    except AttributeError:
        return True
    if allowed:
        return True
    from server import anticheat

    anticheat.report(None, player, "command_rate_limited")
    last_notice = getattr(player, "_command_notice_at", None)
    if last_notice is None or now - float(last_notice) >= COMMAND_NOTICE_INTERVAL_SECONDS:
        player._command_notice_at = now
        send = getattr(player, "send", None)
        if callable(send):
            from shared.packet import ChatMessage

            notice = ChatMessage()
            notice.player_id = 255
            notice.chat_type = int(getattr(C, "CHAT_SYSTEM", 2))
            notice.value = "You're sending commands too fast; slow down."
            send(bytes(notice.generate()))
    return False


def _relay_chat(server, player, data: bytes, team_only: bool) -> None:
    """Deliver a player chat line to in-game peers that know the sender.

    ChatMessage carries the sender's player id and the retail HUD resolves it
    through the scene roster; a peer that never received CreatePlayer for the
    sender (e.g. a dead joiner still waiting for its first life) must not get
    the line. Team chat reaches only the sender's team (retail
    ``team.broadcast_chat_message``).
    """

    connections = getattr(server, "connections", None)
    if not isinstance(connections, dict):
        # Lightweight embedders without per-connection state.
        if team_only:
            send_team = getattr(server, "broadcast_team", None)
            if callable(send_team):
                send_team(int(player.team), data)
            return
        server.broadcast(data)
        return
    if getattr(server, "_stopping", False):
        return
    sender_id = int(player.id)
    sender_team = int(getattr(player, "team", -1))
    for connection in tuple(connections.values()):
        if not bool(getattr(connection, "in_game", False)):
            continue
        recipient = getattr(connection, "player", None)
        if recipient is None:
            continue
        if team_only and int(getattr(recipient, "team", -2)) != sender_team:
            continue
        known = getattr(connection, "known_player_lives", None)
        # The sender's own client shows its line only from this echo; the
        # known-peer roster does not list the connection's own player.
        if recipient is not player and known is not None and sender_id not in known:
            continue
        connection.send(data, reliable=True)


@register_handler(49)  # ChatMessage
async def handle_chat(server, player, packet) -> None:
    """Dispatch slash commands or relay a validated chat message."""
    message = str(packet.value or "")
    if _consume_local_ugc_title(server, player, message):
        return
    if message.startswith("/"):
        # Muting suppresses public/team chat, not the command channel. Keeping
        # this check first lets a muted admin use /unmute and lets ordinary
        # muted players still reach harmless commands such as /help or /ping.
        from commands import handle_command

        if not _command_allowed(player, time.monotonic()):
            return
        await handle_command(server, player, message[1:])
        return
    if player.muted:
        return
    message = message[:CHAT_MAX_LENGTH]
    if not message.strip():
        return
    if not _chat_allowed(player, time.monotonic()):
        return
    # Chatting is activity: a player talking in chat is not AFK.
    from server import conduct

    conduct.note_activity(player)
    from shared.packet import ChatMessage

    # Players may only speak on the ALL(0) and TEAM(1) lanes. SYSTEM(2) and
    # BIG(3) are server announcement styles; echoing a client-chosen type let
    # anyone forge a centre-screen server announcement.
    team_only = int(getattr(packet, "chat_type", CHAT_ALL)) == CHAT_TEAM
    broadcast_packet = ChatMessage()
    broadcast_packet.player_id = player.id
    broadcast_packet.chat_type = CHAT_TEAM if team_only else CHAT_ALL
    broadcast_packet.value = message
    _relay_chat(server, player, bytes(broadcast_packet.generate()), team_only)
