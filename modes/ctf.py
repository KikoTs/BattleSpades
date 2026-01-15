"""
Capture the Flag game mode.
Two teams fight to capture the enemy's intel and return it to their base.
"""

import time
import logging
from typing import Optional, Tuple, TYPE_CHECKING

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player

logger = logging.getLogger(__name__)


class CTFMode(BaseMode):
    """
    Capture the Flag mode.
    
    Rules:
    - Each team has an intel (flag) at their base
    - Pick up enemy intel by walking over it
    - Return to your base while holding intel to score
    - Dying while holding intel drops it
    - Score limit or time limit determines winner
    """
    
    name = "Capture the Flag"
    description = "Capture the enemy intel and return it to your base!"
    
    score_limit = 10
    time_limit = 1200  # 20 minutes
    
    def __init__(self, server):
        super().__init__(server)
        
        # Intel positions (set during on_mode_start)
        self.intel_positions = {
            0: (0.0, 0.0, 0.0),  # Blue intel
            1: (0.0, 0.0, 0.0),  # Green intel
        }
        
        # Base positions (tent locations)
        self.base_positions = {
            0: (0.0, 0.0, 0.0),
            1: (0.0, 0.0, 0.0),
        }
        
        # Intel state
        self.intel_holder = {
            0: None,  # Who is holding blue intel
            1: None,  # Who is holding green intel
        }
        
        # Pickup cooldown (to prevent instant re-grab)
        self.intel_drop_time = {0: 0.0, 1: 0.0}
        self.pickup_cooldown = 2.0  # Seconds
    
    async def on_mode_start(self):
        """Initialize intel and base positions."""
        await super().on_mode_start()
        
        # Set default positions based on map
        # Blue team on west side, green on east
        self.base_positions[0] = (64.0, 256.0, 60.0)
        self.base_positions[1] = (448.0, 256.0, 60.0)
        
        # Intel slightly in front of base
        self.intel_positions[0] = (80.0, 256.0, 60.0)
        self.intel_positions[1] = (432.0, 256.0, 60.0)
        
        # Update team objects
        for team_id, pos in self.intel_positions.items():
            self.server.teams[team_id].set_intel_position(*pos)
        
        logger.info("CTF mode started")
    
    async def on_tick(self, tick: int):
        """Check for intel pickups and captures."""
        await super().on_tick(tick)
        
        current_time = time.time()
        
        for player in list(self.server.players.values()):
            if not player.alive:
                continue
            
            # Skip spectators (team must be 0 or 1)
            if player.team not in (0, 1):
                continue
            
            # Check intel pickup
            enemy_team = 1 - player.team
            if self.intel_holder[enemy_team] is None:
                # Intel is on ground
                intel_pos = self.intel_positions[enemy_team]
                if self._is_near(player, intel_pos, radius=2.0):
                    # Check cooldown
                    if current_time - self.intel_drop_time[enemy_team] > self.pickup_cooldown:
                        await self._pickup_intel(player, enemy_team)
            
            # Check intel capture
            if self.intel_holder[enemy_team] == player:
                # Player is holding enemy intel
                base_pos = self.base_positions[player.team]
                if self._is_near(player, base_pos, radius=3.0):
                    await self._capture_intel(player, enemy_team)
    
    async def _pickup_intel(self, player: 'Player', intel_team: int):
        """Player picks up intel."""
        self.intel_holder[intel_team] = player
        self.server.teams[intel_team].pick_up_intel(player)
        
        # Broadcast pickup
        from protocol.packets import IntelPickup
        self.server.broadcast(IntelPickup(player.id).write())
        
        team_name = self.server.teams[intel_team].name
        await self.broadcast_message(f"{player.name} has the {team_name} intel!")
        
        logger.info(f"{player.name} picked up {team_name} intel")
    
    async def _capture_intel(self, player: 'Player', intel_team: int):
        """Player captures intel."""
        self.intel_holder[intel_team] = None
        
        # Reset intel to base
        base_pos = self.base_positions[intel_team]
        self.intel_positions[intel_team] = base_pos
        self.server.teams[intel_team].return_intel(base_pos)
        
        # Add score
        player.captures += 1
        self.server.teams[player.team].add_capture()
        
        # Check for win
        winning = self.server.teams[player.team].score >= self.score_limit
        
        # Broadcast capture
        from protocol.packets import IntelCapture
        self.server.broadcast(IntelCapture(player.id, winning).write())
        
        team_name = self.server.teams[intel_team].name
        await self.broadcast_message(f"{player.name} captured the {team_name} intel!")
        
        logger.info(f"{player.name} captured {team_name} intel")
        
        if winning:
            await self._end_by_score(player.team)
    
    async def on_player_death(self, player: 'Player', killer: Optional['Player'], kill_type: int):
        """Drop intel if player was holding it."""
        for team_id in (0, 1):
            if self.intel_holder[team_id] == player:
                await self._drop_intel(player, team_id)
                break
    
    async def _drop_intel(self, player: 'Player', intel_team: int):
        """Player drops intel."""
        self.intel_holder[intel_team] = None
        
        # Drop at player position
        drop_pos = (player.x, player.y, player.z)
        self.intel_positions[intel_team] = drop_pos
        self.intel_drop_time[intel_team] = time.time()
        
        self.server.teams[intel_team].drop_intel(*drop_pos)
        
        # Broadcast drop
        from protocol.packets import IntelDrop
        self.server.broadcast(IntelDrop(player.id, *drop_pos).write())
        
        team_name = self.server.teams[intel_team].name
        await self.broadcast_message(f"{player.name} dropped the {team_name} intel!")
        
        logger.info(f"{player.name} dropped {team_name} intel at {drop_pos}")
    
    async def on_player_leave(self, player: 'Player'):
        """Handle player leaving with intel."""
        for team_id in (0, 1):
            if self.intel_holder[team_id] == player:
                await self._drop_intel(player, team_id)
                break
    
    async def on_player_team_change(self, player: 'Player', old_team: int, new_team: int):
        """Handle player changing team while holding intel."""
        enemy_team = 1 - old_team
        if self.intel_holder[enemy_team] == player:
            await self._drop_intel(player, enemy_team)
    
    def _is_near(self, player: 'Player', pos: Tuple[float, float, float], radius: float) -> bool:
        """Check if player is within radius of a position."""
        dx = player.x - pos[0]
        dy = player.y - pos[1]
        dz = player.z - pos[2]
        dist_sq = dx*dx + dy*dy + dz*dz
        return dist_sq <= radius * radius
