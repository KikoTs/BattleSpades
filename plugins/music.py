"""Per-player gameplay music controls.

Commands:
    /music play
    /music stop

This is the opt-in alternative to ``[audio] mode_start_music``.  For fully
personal control, set ``mode_start_music = false`` so mode-wide broadcasts do
not start a gameplay bed for everybody.  A player's ``play`` choice survives
ordinary round restarts until that player uses ``stop`` or disconnects.
"""

from __future__ import annotations

import logging
import random

from commands.command_handler import get_command, register_command, send_message
from plugins.base_plugin import BasePlugin
from server.audio import GAMEPLAY_TRACKS, play_music_to, stop_music_to


logger = logging.getLogger(__name__)


class MusicPlugin(BasePlugin):
    name = "Music"
    version = "1.0.0"
    author = "BattleSpades community"
    description = "Personal gameplay music with /music play and /music stop."

    def __init__(self, server):
        super().__init__(server)
        self._command_handler = None
        self._listeners: dict[int, object] = {}
        self._listener_round_keys: dict[int, object] = {}

    async def on_load(self):
        if get_command("music") is not None:
            raise RuntimeError("/music is already registered by another plugin")

        async def music_command(ctx):
            await self._handle_command(ctx)

        register_command(
            "music",
            usage="/music <play|stop>",
            description="Start or stop your personal gameplay music.",
        )(music_command)
        self._command_handler = music_command
        logger.info("Music plugin loaded: /music play | /music stop")

    async def on_unload(self):
        self._stop_all()
        self._unregister_command()

    async def on_disable(self):
        self._stop_all()

    async def on_player_leave(self, player):
        player_id = self._player_id(player)
        if player_id is not None and self._listeners.get(player_id) is player:
            self._listeners.pop(player_id, None)
            self._listener_round_keys.pop(player_id, None)

    async def on_tick(self, tick):
        """Restore opt-in beds after a mode-wide stop at a new round."""
        current = self._current_round_key()
        if current is None or self._global_gameplay_music_enabled():
            return

        for player_id, player in tuple(self._listeners.items()):
            if not self._owns_slot(player_id, player):
                self._listeners.pop(player_id, None)
                self._listener_round_keys.pop(player_id, None)
                continue
            if self._listener_round_keys.get(player_id) == current:
                continue
            connection = self._settled_connection(player)
            if connection is not None:
                play_music_to(connection, random.choice(GAMEPLAY_TRACKS))
                self._listener_round_keys[player_id] = current

    async def _handle_command(self, ctx):
        player = ctx.player
        if not self.enabled:
            await send_message(ctx.server, player, "Music plugin is disabled.")
            return
        if len(ctx.args) != 1:
            await self._usage(ctx)
            return

        action = str(ctx.args[0]).casefold()
        if action not in ("play", "stop"):
            await self._usage(ctx)
            return

        connection = self._settled_connection(player)
        if connection is None:
            await send_message(
                ctx.server, player, "Music is available after entering the game."
            )
            return

        player_id = self._player_id(player)
        if player_id is None:
            await send_message(ctx.server, player, "Music is unavailable.")
            return

        if action == "play":
            play_music_to(connection, random.choice(GAMEPLAY_TRACKS))
            self._listeners[player_id] = player
            # Mark this round for this player only. Other listeners may still
            # need restoration after the mode-wide round-start StopMusic.
            self._listener_round_keys[player_id] = self._current_round_key()
            await send_message(
                ctx.server, player, "Music started. Use /music stop to turn it off."
            )
            return

        stop_music_to(connection)
        if self._listeners.get(player_id) is player:
            self._listeners.pop(player_id, None)
            self._listener_round_keys.pop(player_id, None)
        await send_message(ctx.server, player, "Music stopped.")

    async def _usage(self, ctx):
        await send_message(ctx.server, ctx.player, "Usage: /music <play|stop>")

    def _stop_all(self):
        for player_id, player in tuple(self._listeners.items()):
            if self._owns_slot(player_id, player):
                connection = self._settled_connection(player)
                if connection is not None:
                    stop_music_to(connection)
        self._listeners.clear()
        self._listener_round_keys.clear()

    def _unregister_command(self):
        from commands.command_handler import _commands

        command = get_command("music")
        if command is not None and command.handler is self._command_handler:
            for key, value in list(_commands.items()):
                if value is command:
                    _commands.pop(key, None)
        self._command_handler = None

    def _current_round_key(self):
        mode = getattr(self.server, "mode", None)
        if (
            mode is None
            or not bool(getattr(mode, "started", False))
            or bool(getattr(mode, "ended", False))
            or bool(getattr(mode, "retiring", False))
        ):
            return None
        return id(mode), float(getattr(mode, "start_time", 0.0) or 0.0)

    def _global_gameplay_music_enabled(self) -> bool:
        return bool(
            getattr(getattr(self.server, "config", None), "mode_start_music", True)
        )

    def _owns_slot(self, player_id: int, player) -> bool:
        players = getattr(self.server, "players", {})
        return players.get(player_id) is player

    @staticmethod
    def _player_id(player):
        try:
            return int(player.id)
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _settled_connection(player):
        connection = getattr(player, "connection", None)
        if connection is None or not bool(getattr(connection, "in_game", True)):
            return None
        if not callable(getattr(connection, "send", None)):
            return None
        return connection


__all__ = ["MusicPlugin"]
