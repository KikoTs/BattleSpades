"""
Arena game mode.
Round-based elimination mode - last team standing wins the round.
"""

import time
import logging
from typing import Optional, List, TYPE_CHECKING

from server.game_constants import TEAM1, TEAM2
from server.round_lifecycle import resolve_player_spawn

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player

logger = logging.getLogger(__name__)


class ArenaMode(BaseMode):
    """
    Arena mode.
    
    Rules:
    - Round-based elimination
    - No respawning during rounds
    - Last team with players alive wins the round
    - Win majority of rounds to win the match
    """
    
    name = "Arena"
    description = "Eliminate all enemies to win the round!"
    
    rounds_to_win = 5
    round_time_limit = 180  # 3 minutes per round
    round_start_delay = 5  # Seconds before round starts
    round_end_delay = 5    # Seconds after round ends
    
    def __init__(self, server):
        super().__init__(server)
        
        # Round state
        self.current_round = 0
        self.round_wins = {TEAM1: 0, TEAM2: 0}
        
        self.round_started = False
        self.round_ended = False
        self.round_start_time = 0.0
        
        # Pre-round countdown
        self.countdown_started = False
        self.countdown_end_time = 0.0
        
        # Players alive this round
        self.alive_players: List['Player'] = []
    
    async def on_mode_start(self):
        """Start arena mode."""
        await super().on_mode_start()
        self.round_wins = {TEAM1: 0, TEAM2: 0}
        await self._start_new_round()
        logger.info("Arena mode started")
    
    async def _start_new_round(self):
        """Start a new round."""
        self.current_round += 1
        self.round_started = False
        self.round_ended = False
        
        await self.broadcast_message(f"Round {self.current_round} - Get ready!")
        
        # Respawn all players
        for player in self.server.players.values():
            spawn = resolve_player_spawn(self.server, player)
            player.spawn(*spawn)
        
        self.alive_players = list(self.server.players.values())
        
        # Start countdown
        self.countdown_started = True
        self.countdown_end_time = time.time() + self.round_start_delay
    
    async def on_tick(self, tick: int):
        """Check round state."""
        current_time = time.time()
        
        # Handle countdown
        if self.countdown_started and not self.round_started:
            remaining = self.countdown_end_time - current_time
            
            if remaining <= 0:
                await self._begin_round()
            elif tick % self.server.tick_rate == 0:
                seconds = int(remaining)
                if seconds > 0:
                    await self.broadcast_message(f"{seconds}...")
        
        # Check round time limit
        if self.round_started and not self.round_ended:
            round_time = current_time - self.round_start_time
            
            if round_time >= self.round_time_limit:
                # Round timed out - draw or team with more alive wins
                await self._end_round_timeout()
    
    async def _begin_round(self):
        """Actually start the round (after countdown)."""
        self.round_started = True
        self.countdown_started = False
        self.round_start_time = time.time()
        
        await self.broadcast_message("FIGHT!")
        logger.info(f"Round {self.current_round} started")
    
    async def on_player_death(self, player: 'Player', killer: Optional['Player'], kill_type: int):
        """Handle player death - check for round end."""
        if not self.round_started or self.round_ended:
            return
        
        # Remove from alive list
        if player in self.alive_players:
            self.alive_players.remove(player)
        
        # Check if team is eliminated
        team_alive = {TEAM1: 0, TEAM2: 0}
        for p in self.alive_players:
            if p.alive:
                team_alive[p.team] += 1
        
        # Check win condition
        if team_alive[TEAM1] == 0 and team_alive[TEAM2] > 0:
            await self._end_round(winner=TEAM2)
        elif team_alive[TEAM2] == 0 and team_alive[TEAM1] > 0:
            await self._end_round(winner=TEAM1)
        elif team_alive[TEAM1] == 0 and team_alive[TEAM2] == 0:
            await self._end_round(winner=None)  # Draw
    
    async def _end_round(self, winner: Optional[int]):
        """End the current round."""
        self.round_ended = True
        
        if winner is not None:
            self.round_wins[winner] += 1
            team_name = self.server.teams[winner].name
            await self.broadcast_message(f"{team_name} wins the round!")
            logger.info(f"Round {self.current_round} won by team {winner}")
        else:
            await self.broadcast_message("Round draw!")
            logger.info(f"Round {self.current_round} was a draw")
        
        # Show round score
        team1_name = self.server.teams[TEAM1].name
        team2_name = self.server.teams[TEAM2].name
        await self.broadcast_message(
            f"Score - {team1_name}: {self.round_wins[TEAM1]} | {team2_name}: {self.round_wins[TEAM2]}"
        )
        
        # Check for match win
        for team_id, wins in self.round_wins.items():
            if wins >= self.rounds_to_win:
                await self._end_match(team_id)
                return
        
        # Start next round after delay
        await self._schedule_next_round()
    
    async def _end_round_timeout(self):
        """Handle round ending due to time limit."""
        # Team with more alive players wins
        team_alive = {TEAM1: 0, TEAM2: 0}
        for p in self.alive_players:
            if p.alive:
                team_alive[p.team] += 1
        
        if team_alive[TEAM1] > team_alive[TEAM2]:
            winner = TEAM1
        elif team_alive[TEAM2] > team_alive[TEAM1]:
            winner = TEAM2
        else:
            winner = None
        
        await self.broadcast_message("Time's up!")
        await self._end_round(winner)
    
    async def _schedule_next_round(self):
        """Schedule the next round."""
        import asyncio
        await asyncio.sleep(self.round_end_delay)
        
        if not self.ended:
            await self._start_new_round()
    
    async def _end_match(self, winner: int):
        """End the entire match."""
        team_name = self.server.teams[winner].name
        await self.broadcast_message(f"{team_name} wins the match!")
        await self.on_mode_end(winner)
    
    async def on_player_damage(self, player: 'Player', attacker: Optional['Player'],
                               damage: int, kill_type: int) -> int:
        """Disable damage during countdown."""
        if not self.round_started or self.round_ended:
            return 0  # No damage during countdown or after round
        return damage
