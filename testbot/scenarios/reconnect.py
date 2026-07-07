# -*- coding: utf-8 -*-
"""reconnect — disconnect cleanly then connect again as a fresh bot.

Surfaces:
  - server frees player_id slot on disconnect
  - server broadcasts PlayerLeft cleanly
  - second connection completes handshake without state leakage
  - same map sync flow works on second join (no caching bug)

This catches the classic class of bugs where the first connection works but
subsequent ones fail because something is held onto.
"""

NAME = 'reconnect'
TIMEOUT = 30.0


def script(c):
    # First join
    c.do_full_handshake()
    first_pid = c.our_player_id
    c.log.emit('first_join_done', player_id=first_pid)
    c.disconnect()

    # Wait for server to fully process the disconnect — small grace period.
    # (We're using a single Client instance and disconnect() nukes the host;
    #  but the server-side cleanup is what we're testing here.)
    import time as _t
    _t.sleep(0.3)

    # Second join — fresh state on the bot side
    c.connected = False
    c.disconnected = False
    c.received_log = []
    c.steam_key = None
    c.last_initial_info = None
    c.last_state_data = None
    c.last_create_player = None
    c.our_player_id = None
    c.spawn_xyz = None

    c.do_full_handshake()
    second_pid = c.our_player_id
    c.log.emit('second_join_done', player_id=second_pid)

    # Idle a bit
    c.pump(1.0)
    if c.disconnected:
        raise RuntimeError('server disconnected us during second-join idle')

    c.disconnect()
