"""server.builders — factories that turn server state into wire packets.

These exist so connection.py and elsewhere don't hand-build packets with
hardcoded magic. Each builder takes the server + relevant context and
returns a `shared.packet.<Cls>` instance ready for `.generate()`.

When you find a hardcoded value in a packet field, the right home is here.
"""

from .initial_info import build_initial_info
from .state_data import build_state_data

__all__ = ['build_initial_info', 'build_state_data']
