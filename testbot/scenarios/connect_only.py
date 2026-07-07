# -*- coding: utf-8 -*-
"""connect_only — minimal handshake proof.

Sequence:
  1. ENet CONNECT to server with PROTOCOL_VERSION
  2. Send SteamSessionTicket(105) with empty ticket (offline mode)
  3. Expect InitialInfo(114) within 5s
  4. Disconnect cleanly

This is the smallest scenario that proves: ENet works, server accepts the
protocol byte, the steam-ticket gating works, the InitialInfo encoder on the
server produces bytes the original packet decoder can parse.
"""

NAME = 'connect_only'
TIMEOUT = 15.0


def script(c):
    c.connect()
    c.send_steam_session_ticket(ticket=b'')
    info = c.expect('InitialInfo', timeout=5.0)
    c.log.emit('initial_info_parsed',
               server_name=getattr(info, 'server_name', None),
               map_name=getattr(info, 'map_name', None),
               mode_key=getattr(info, 'mode_key', None),
               friendly_fire=getattr(info, 'friendly_fire', None))
    c.disconnect()
