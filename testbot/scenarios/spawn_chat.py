# -*- coding: utf-8 -*-
"""spawn_chat — verify ChatMessage round-trip after spawn.

Sends a ChatMessage(49). Expects to receive the same message echoed back
via the server's broadcast (with our player_id stamped on it).
"""

import shared.packet as P

NAME = 'spawn_chat'
TIMEOUT = 30.0

CHAT_TEXT = u'hello from testbot'


def script(c):
    c.do_full_handshake()

    msg = P.ChatMessage()
    msg.player_id = c.our_player_id
    msg.chat_type = 0  # all
    msg.value = CHAT_TEXT

    after_idx = len(c.received_log)
    c.send(msg)

    # Wait for our chat to come back
    echo = c.wait_for(
        'ChatMessage',
        predicate=lambda p: getattr(p, 'value', u'') == CHAT_TEXT,
        timeout=3.0,
        after_idx=after_idx,
    )
    c.log.emit('chat_echo_received',
               from_player=int(getattr(echo, 'player_id', -1)),
               chat_type=int(getattr(echo, 'chat_type', -1)),
               value=getattr(echo, 'value', None))

    c.disconnect()
