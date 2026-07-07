"""Example plugin — copy this to start your own.

Any *.py in plugins/ that defines a BasePlugin subclass is auto-discovered and
loaded at server startup (server/main.py::_load_plugins). Override the hooks you
care about; the rest are no-ops. Hooks fire from the server's mode-event
dispatch: on_player_spawn / on_player_kill / on_player_join / on_player_leave and
on_tick (per 60 Hz frame — keep it cheap).
"""
import logging

from plugins.base_plugin import BasePlugin

logger = logging.getLogger(__name__)


class ExamplePlugin(BasePlugin):
    name = "Example"
    version = "1.0.0"
    author = "BattleSpades"
    description = "Template plugin — logs on load and announces kill streaks."

    async def on_load(self):
        self._streaks = {}
        logger.info("[Example plugin] loaded — override hooks in plugins/example_plugin.py")

    async def on_player_kill(self, killer, victim, kill_type):
        if killer is None or killer is victim:
            return
        n = self._streaks.get(killer.id, 0) + 1
        self._streaks[killer.id] = n
        self._streaks[victim.id] = 0
        if n in (3, 5, 10):
            label = {3: "on a spree", 5: "dominating", 10: "unstoppable"}[n]
            await self.server.broadcast_message(f"{killer.name} is {label}! ({n} kills)")
