# -*- coding: utf-8 -*-
"""full_handshake — complete join sequence to spawn.

Proves: handshake order, map transfer, spawn flow.
"""

NAME = 'full_handshake'
TIMEOUT = 30.0


def script(c):
    c.do_full_handshake()

    # Idle a bit to confirm the server keeps us alive (no spurious disconnect).
    c.pump(2.0)
    if c.disconnected:
        raise RuntimeError('server disconnected us during idle')

    c.disconnect()
