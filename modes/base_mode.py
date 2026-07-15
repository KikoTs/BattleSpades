"""
Base game mode class.
Provides hooks for game events that modes can override.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional, List, Tuple

from server.game_constants import CHAT_SYSTEM
from shared.packet import ChatMessage

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player


class BaseMode(ABC):
    """
    Abstract base class for game modes.
    Override the event methods to implement custom game logic.
    """
    
    # Mode metadata
    name: str = "Base Mode"
    description: str = "Base game mode"
    
    # Scoring
    score_limit: int = 10
    time_limit: int = 0  # Seconds, 0 = unlimited
    
    def __init__(self, server: 'BattleSpadesServer'):
        self.server = server
        self.started = False
        self.ended = False
        self.winner: Optional[int] = None  # Winning team ID

        # Round timing
        self.start_time: float = 0.0
        self.elapsed_time: float = 0.0
        # Timeout music fires exactly once, TIMEOUT_MUSIC_SECONDS before the end.
        self._timeout_music_played = False
        # Gameplay music bed is re-sent on a cadence so a finite track never
        # leaves the round silent; this stamps the last (re)start time.
        self._last_music_at: float = 0.0
        # Guards the async end sequence so it runs exactly once.
        self._end_sequence_running = False
        self._end_task = None
    
    # =========================================================================
    # Lifecycle Events
    # =========================================================================
    
    async def on_mode_start(self):
        """Called when the mode starts (also on every round restart)."""
        import time
        self.started = True
        # Reset the full end-of-round state so a RESTART after a natural end
        # actually revives the mode (previously ended/winner stuck True and the
        # timer + win checks never ran again).
        self.ended = False
        self.winner = None
        self._timeout_music_played = False
        self._end_sequence_running = False
        self.start_time = time.time()
        self.elapsed_time = 0.0

        # Kick off the in-game music bed so the round is never silent.
        from server.audio import play_gameplay_music
        play_gameplay_music(self.server)
        self._last_music_at = self.start_time

    async def on_mode_end(self, winner: Optional[int] = None):
        """Called when the mode ends — run the full end-of-round sequence
        (victory audio → stats screen → restart), not just a chat line."""
        self.ended = True
        self.winner = winner
        await self._run_end_sequence(winner)

    async def cancel_end_sequence(self):
        """Cancel a delayed victory restart before an admin transition.

        This runs on the gameplay event loop and emits no packets. Its only job
        is to ensure an old mode cannot wake later and reset a replacement map
        or mode underneath connected clients.
        """
        import asyncio

        task = self._end_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._end_task = None
        self._end_sequence_running = False

    async def deactivate(self):
        """Retire this mode without starting the normal victory sequence."""
        self.started = False
        self.ended = True

    async def on_tick(self, tick: int):
        """Called every game tick."""
        if self.ended:
            return
        import time
        now = time.time()
        self.elapsed_time = now - self.start_time

        # The gameplay bed (started in on_mode_start) alure-loops forever, so no
        # re-send is needed. Swap to the last-minute "game_ending" track once,
        # 61s before the clock runs out.
        from server.audio import TIMEOUT_MUSIC_SECONDS, play_timeout_music
        if self.time_limit > 0 and not self._timeout_music_played:
            if self.time_limit - self.elapsed_time <= TIMEOUT_MUSIC_SECONDS:
                self._timeout_music_played = True
                play_timeout_music(self.server)

        # Check time limit
        if self.time_limit > 0 and self.elapsed_time >= self.time_limit:
            await self._end_by_time()
    
    async def on_round_start(self):
        """Called when a new round starts."""
        pass
    
    async def on_round_end(self, winner: Optional[int] = None):
        """Called when a round ends."""
        pass
    
    # =========================================================================
    # Player Events
    # =========================================================================
    
    async def on_player_join(self, player: 'Player'):
        """Called when a player joins the game."""
        pass
    
    async def on_player_leave(self, player: 'Player'):
        """Called when a player leaves the game."""
        pass
    
    async def on_player_spawn(self, player: 'Player'):
        """Called when a player spawns."""
        pass
    
    async def on_player_kill(self, killer: 'Player', victim: 'Player', kill_type: int):
        """Called when a player kills another player."""
        pass
    
    async def on_player_death(self, player: 'Player', killer: Optional['Player'], kill_type: int):
        """Called when a player dies."""
        pass
    
    async def on_player_team_change(self, player: 'Player', old_team: int, new_team: int):
        """Called when a player changes team."""
        pass
    
    # =========================================================================
    # Block Events
    # =========================================================================
    
    async def on_block_build(self, player: 'Player', x: int, y: int, z: int):
        """Called when a player places a block."""
        pass
    
    async def on_block_destroy(self, player: 'Player', x: int, y: int, z: int):
        """Called when a player destroys a block."""
        pass
    
    async def on_block_line(self, player: 'Player', x1: int, y1: int, z1: int, 
                            x2: int, y2: int, z2: int):
        """Called when a player builds a line of blocks."""
        pass
    
    # =========================================================================
    # Combat Events
    # =========================================================================
    
    async def on_grenade_explode(self, player: 'Player', x: float, y: float, z: float):
        """Called when a grenade explodes."""
        pass
    
    async def on_player_damage(self, player: 'Player', attacker: Optional['Player'], 
                               damage: int, kill_type: int) -> int:
        """
        Called when a player takes damage.
        Return modified damage value (can reduce/increase).
        """
        return damage
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    def get_spawn_point(self, player: 'Player') -> Tuple[float, float, float]:
        """
        Get spawn point for a player.
        Override to customize spawn logic.
        """
        return self.server.world_manager.get_spawn_point(player.team)
    
    async def broadcast_message(self, message: str):
        """Broadcast a system message to all players."""
        packet = ChatMessage()
        packet.player_id = 255  # System message
        packet.chat_type = CHAT_SYSTEM
        packet.value = message
        self.server.broadcast(bytes(packet.generate()))
    
    async def check_win_condition(self) -> Optional[int]:
        """
        Check if a team has won.
        Returns winning team ID or None.
        """
        for team_id, team in self.server.teams.items():
            if team.score >= self.score_limit:
                return team_id
        return None
    
    async def _end_by_score(self, winner: int):
        """End game due to score limit reached (fires exactly once)."""
        if self.ended:
            return
        team = self.server.teams[winner]
        await self.broadcast_message(f"{team.name} wins!")
        await self.on_mode_end(winner)

    async def _end_by_time(self):
        """End game due to time limit (fires exactly once).

        Without the `ended` guard this re-fires EVERY TICK once the timer
        expires, flooding ChatMessage on the single reliable ENet channel
        and starving every other gameplay packet (blocks, kills, entities,
        score) — which reads in-game as "nothing works".
        """
        if self.ended:
            return
        # Determine winner by score
        scores = [(t.id, t.score) for t in self.server.teams.values()]
        scores.sort(key=lambda x: x[1], reverse=True)

        if scores[0][1] > scores[1][1]:
            winner = scores[0][0]
            team = self.server.teams[winner]
            await self.broadcast_message(f"Time's up! {team.name} wins!")
        else:
            await self.broadcast_message("Time's up! It's a draw!")
            winner = None

        await self.on_mode_end(winner)

    # =========================================================================
    # End-of-round sequence  (win message already sent by the caller)
    # =========================================================================

    # How long the client holds the full-screen scores/credits widget before
    # the server restarts the round. TIME_AFTER_WIN_BEFORE_SCORES (5.0) is the
    # measured gap between the win and the scores screen (constants_gamemode).
    SCORES_SCREEN_SECONDS = 12.0

    async def _run_end_sequence(self, winner: Optional[int]):
        """Victory audio → (5s) stats/credits screen → (hold) → restart.

        Runs once per end. The whole thing is fire-and-forget on the event
        loop so the caller (a mode hook on the game thread) isn't blocked."""
        if self._end_sequence_running:
            return
        self._end_sequence_running = True
        import asyncio
        # HOLD A REFERENCE: asyncio keeps only a weak ref to a bare task, so a
        # GC during the ~17s of awaits below can collect it mid-flight and the
        # round then never restarts.
        self._end_task = asyncio.ensure_future(self._end_sequence_task(winner))

    async def _end_sequence_task(self, winner: Optional[int]):
        import asyncio
        from server.audio import play_ending_music, TIME_AFTER_WIN_BEFORE_SCORES
        from server.scoreboard import broadcast_game_stats
        try:
            # 1) Victory sting immediately on the win.
            play_ending_music(self.server)

            # 2) Send final leaderboard data without a terminal UI trigger.
            # ShowGameStats, MapEnded, and ForceShowScores all destroy the
            # retail client's GameScene, which makes a same-map restart unsafe.
            await asyncio.sleep(TIME_AFTER_WIN_BEFORE_SCORES)
            broadcast_game_stats(self.server, winner)

            # 3) Hold the final scores, then rebuild the same live scene.
            await asyncio.sleep(self.SCORES_SCREEN_SECONDS)
            transition = getattr(self.server, "match_transition", None)
            if transition is None:
                await self._restart_round()
            else:
                result = await transition.restart_round()
                if not result.ok:
                    raise RuntimeError(result.message)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("end sequence failed")
            self._end_sequence_running = False

    async def _restart_round(self):
        """Reset scores + respawn everyone + revive the mode for a new round.
        Mirrors the reference server's reset(): stop → start → respawn all →
        reset teams (aosmodes/__init__.py reset())."""
        # GameScene remains alive. Remove its old transient entities before the
        # mode re-creates crates/objectives and reuses registry ids.
        reset_runtime = getattr(self.server, "reset_round_runtime", None)
        if reset_runtime is not None:
            reset_runtime()

        # Reset team scores and re-broadcast the zeroed bars.
        from server.scoreboard import send_team_score
        for team in self.server.teams.values():
            team.reset()
        # on_mode_start clears ended/winner/timeout flags and restarts music.
        await self.on_mode_start()
        for team in self.server.teams.values():
            send_team_score(self.server, team)
        # Respawn every connected player through the ordinary CreatePlayer and
        # restock path while the client is still in its original GameScene.
        for player in list(self.server.players.values()):
            try:
                if getattr(player, "connection", None) is not None:
                    self.server.respawn_player(player)
            except Exception:
                import logging
                logging.getLogger(__name__).debug(
                    "restart respawn failed for %s", getattr(player, "id", "?"),
                    exc_info=True)
