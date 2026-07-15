"""Compatibility imports for the isolated bot runtime.

New code should import from :mod:`server.bot_ai`.  ``BotManager`` remains as
an alias so older plugins fail neither import nor construction while migrating
to the asynchronous :class:`~server.bot_ai.director.BotDirector` lifecycle.
"""

from server.bot_ai.director import BotDirector, _BotConnection

BotManager = BotDirector

__all__ = ["BotDirector", "BotManager", "_BotConnection"]
