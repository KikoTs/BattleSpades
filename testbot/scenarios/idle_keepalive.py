# -*- coding: utf-8 -*-
"""idle_keepalive — stay connected for ~10s without input.

Surfaces:
  - server doesn't time us out for being silent
  - server-sent ClockSync are echoed correctly (or absent and fine)
  - no spurious DISCONNECT during long idle

Note: this is a long scenario by harness standards. Bumped TIMEOUT.
"""

NAME = 'idle_keepalive'
TIMEOUT = 25.0
IDLE_SECONDS = 10.0


def script(c):
    c.do_full_handshake()

    # Just pump for IDLE_SECONDS, dispatching whatever the server sends.
    import time as _t
    deadline = _t.time() + IDLE_SECONDS
    while _t.time() < deadline:
        if c.disconnected:
            raise RuntimeError(
                'server disconnected us at t={:.2f}s during idle'.format(
                    _t.time() - (deadline - IDLE_SECONDS)))
        c.pump(0.5)

    if c.disconnected:
        raise RuntimeError('server disconnected us during idle window')

    c.log.emit('idle_complete', seconds=IDLE_SECONDS)
    c.disconnect()
