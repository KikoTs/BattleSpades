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
    
    # =========================================================================
    # Lifecycle Events
    # =========================================================================
    
    async def on_mode_start(self):
        """Called when the mode starts."""
        self.started = True
        import time
        self.start_time = time.time()
    
    async def on_mode_end(self, winner: Optional[int] = None):
        """Called when the mode ends — pop up the end-of-round stats widget."""
        from server.scoreboard import broadcast_game_stats
        broadcast_game_stats(self.server, winner)
        self.ended = True
        self.winner = winner
    
    async def on_tick(self, tick: int):
        """Called every game tick."""
        if self.ended:
            return
        import time
        self.elapsed_time = time.time() - self.start_time

        # Last-minute tension music (the original's 61s "game_ending" track).
        if self.time_limit > 0 and not self._timeout_music_played:
            from server.audio import TIMEOUT_MUSIC_SECONDS, play_timeout_music
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
        self.ended = True
        self.winner = winner
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
        self.ended = True
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

        self.winner = winner
        await self.on_mode_end(winner)
