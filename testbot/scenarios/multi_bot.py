# -*- coding: utf-8 -*-
"""multi_bot — two clients in one py2 process, observe each other.

Bot A and Bot B both join, both should appear in each other's WorldUpdate.
Then Bot A walks while Bot B watches, Bot B chats while Bot A watches.

Surfaces:
  - ExistingPlayer broadcast on second join
  - WorldUpdate items dict carries multiple players
  - chat broadcast reaches the OTHER player (not just the sender)
  - per-bot player_id allocation increments correctly
"""

import time as _t

import shared.packet as P

from testbot.client import Client, EventLog

NAME = 'multi_bot'
TIMEOUT = 45.0

CHAT_TEXT = u'multi_bot test message'


def _pump_both(a, b, seconds):
    """Pump both clients for `seconds` wall-clock total, time-sliced."""
    deadline = _t.time() + seconds
    while _t.time() < deadline:
        a.pump(0.005)
        b.pump(0.005)


def script(_unused_c):
    """We ignore the harness-supplied Client and create our own pair."""
    log = _unused_c.log
    host = _unused_c.host_addr
    port = _unused_c.port
    # Tear down the harness-created client (we never use it).
    try:
        _unused_c.host = None
    except Exception:
        pass

    a = Client(host=host, port=port, name='AlphaBot', log=log)
    b = Client(host=host, port=port, name='BravoBot', log=log)

    # Join sequentially so server has time to stabilize between them.
    a.do_full_handshake()
    log.emit('alpha_joined', player_id=a.our_player_id, spawn=a.spawn_xyz)

    _t.sleep(0.2)

    b.do_full_handshake()
    log.emit('bravo_joined', player_id=b.our_player_id, spawn=b.spawn_xyz)

    if a.our_player_id == b.our_player_id:
        raise RuntimeError(
            'both bots got the same player_id={} — server is not allocating uniquely'
            .format(a.our_player_id))

    # Pump both for a moment so server-side state settles.
    _pump_both(a, b, 0.5)

    # ----- Test 1: Bot A walks, Bot B should see A move in WorldUpdate -----
    a_idx = len(a.received_log)
    b_idx = len(b.received_log)
    for tick in range(60):  # 1 second of input
        cd = a.make_client_data(loop_count=tick, tool_id=2,
                                orientation=(0.0, 1.0, 0.0),
                                up=True, sprint=True)
        a.send(cd)
        # Pump both so neither side starves.
        a.pump(1.0/120)
        b.pump(1.0/120)

    # Find the latest WorldUpdate B received that contains A's snapshot
    last_a_seen_by_b = None
    for ts, pid, parsed in b.received_log[b_idx:]:
        if parsed is not None and type(parsed).__name__ == 'WorldUpdate':
            items = getattr(parsed, 'items', None) or {}
            if a.our_player_id in items:
                last_a_seen_by_b = items[a.our_player_id]

    if last_a_seen_by_b is None:
        raise RuntimeError(
            "Bot B never saw Bot A (id {}) in any WorldUpdate during walk"
            .format(a.our_player_id))

    a_pos_seen = last_a_seen_by_b[0]
    log.emit('alpha_seen_by_bravo',
             alpha_pos=tuple(float(v) for v in a_pos_seen))

    dy = float(a_pos_seen[1]) - a.spawn_xyz[1]
    if abs(dy) < 0.5:
        raise RuntimeError(
            'Bot A did not visibly move from B\'s perspective: dy={:.3f}'.format(dy))

    # ----- Test 2: Bot B chats, Bot A should see the broadcast -----
    msg = P.ChatMessage()
    msg.player_id = b.our_player_id
    msg.chat_type = 0
    msg.value = CHAT_TEXT
    a_idx = len(a.received_log)
    b.send(msg)

    deadline = _t.time() + 3.0
    found = None
    while _t.time() < deadline and found is None:
        a.pump(0.05)
        b.pump(0.05)
        for ts, pid, parsed in a.received_log[a_idx:]:
            if parsed is not None and type(parsed).__name__ == 'ChatMessage':
                if getattr(parsed, 'value', u'') == CHAT_TEXT:
                    found = parsed
                    break

    if found is None:
        raise RuntimeError(
            "Bot A did not receive B's chat broadcast within 3s")
    log.emit('bravo_chat_seen_by_alpha',
             from_pid=int(getattr(found, 'player_id', -1)),
             value=getattr(found, 'value', None))

    a.disconnect()
    _t.sleep(0.1)
    b.disconnect()
