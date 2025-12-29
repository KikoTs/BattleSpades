"""
Team Deathmatch game mode.
Two teams fight for kills until score or time limit is reached.
"""

import logging
from typing import Optional, TYPE_CHECKING

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player

logger = logging.getLogger(__name__)


class TDMMode(BaseMode):
    """
    Team Deathmatch mode.
    
    Rules:
    - Teams score points for kills
    - First team to reach score limit wins
    - Or team with most kills when time expires wins
    """
    
    name = "Team Deathmatch"
    description = "Eliminate the enemy team to score points!"
    
    score_limit = 100
    time_limit = 900  # 15 minutes
    
    # Points
    kill_points = 1
    headshot_bonus = 1
    
    def __init__(self, server):
        super().__init__(server)
    
    async def on_mode_start(self):
        """Start TDM mode."""
        await super().on_mode_start()
        
        # Reset team scores
        for team in self.server.teams.values():
            team.reset()
        
        logger.info("TDM mode started")
    
    async def on_player_kill(self, killer: 'Player', victim: 'Player', kill_type: int):
        """Award points for kills."""
        from aoslib.constants import KILL_HEADSHOT
        
        # Award team points
        points = self.kill_points
        if kill_type == KILL_HEADSHOT:
            points += self.headshot_bonus
        
        self.server.teams[killer.team].add_score(points)
        
        # Broadcast updated scores
        await self._broadcast_scores()
        
        # Check win condition
        if self.server.teams[killer.team].score >= self.score_limit:
            await self._end_by_score(killer.team)
    
    async def on_player_death(self, player: 'Player', killer: Optional['Player'], kill_type: int):
        """Handle player death."""
        pass  # Kill event handled in on_player_kill
    
    async def _broadcast_scores(self):
        """Broadcast current team scores."""
        blue_score = self.server.teams[0].score
        green_score = self.server.teams[1].score
        
        message = f"Score - Blue: {blue_score} | Green: {green_score}"
        # Could send as system message for HUD update
    
    async def on_tick(self, tick: int):
        """Periodic updates."""
        await super().on_tick(tick)
        
        # Every 60 seconds, announce scores
        if tick % (60 * self.server.tick_rate) == 0:
            blue_score = self.server.teams[0].score
            green_score = self.server.teams[1].score
            
            if blue_score != green_score:
                leader = 0 if blue_score > green_score else 1
                team_name = self.server.teams[leader].name
                diff = abs(blue_score - green_score)
                await self.broadcast_message(f"{team_name} leads by {diff} points!")
            else:
                await self.broadcast_message("Teams are tied!")
