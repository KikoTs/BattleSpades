"""
Team management for BattleSpades.
"""

from typing import List, Tuple, Optional, TYPE_CHECKING

from .game_constants import TEAM1

if TYPE_CHECKING:
    from .player import Player


class Team:
    """
    Represents a team in the game.
    Manages team members, score, and spawn points.
    """
    
    def __init__(self, id: int, name: str, color: Tuple[int, int, int]):
        self.id = id
        self.name = name
        self.color = color  # RGB tuple
        
        # Team members
        self.players: List['Player'] = []
        
        # Score
        self.score: int = 0
        self.captures: int = 0
        self.kills: int = 0
        
        # Spawn points
        self.spawn_points: List[Tuple[float, float, float]] = []
        self.spawn_index: int = 0
        
        # Intel/flag position (for CTF)
        self.intel_position: Optional[Tuple[float, float, float]] = None
        self.intel_holder: Optional['Player'] = None
    
    @property
    def color_int(self) -> int:
        """Get color as 0xRRGGBB integer."""
        r, g, b = self.color
        return (r << 16) | (g << 8) | b
    
    @property
    def player_count(self) -> int:
        """Get number of players on this team."""
        return len(self.players)
    
    def add_player(self, player: 'Player'):
        """Add a player to this team."""
        if player not in self.players:
            self.players.append(player)
    
    def remove_player(self, player: 'Player'):
        """Remove a player from this team."""
        if player in self.players:
            self.players.remove(player)
    
    def add_spawn_point(self, x: float, y: float, z: float):
        """Add a spawn point for this team."""
        self.spawn_points.append((x, y, z))
    
    def get_spawn_point(self) -> Tuple[float, float, float]:
        """
        Get the next spawn point for this team.
        Cycles through available spawn points.
        """
        if not self.spawn_points:
            # Default spawn based on team
            if self.id == TEAM1:  # Team 1 - West
                return (64.0, 256.0, 58.0)
            return (448.0, 256.0, 58.0)
        
        point = self.spawn_points[self.spawn_index]
        self.spawn_index = (self.spawn_index + 1) % len(self.spawn_points)
        return point
    
    def add_score(self, points: int = 1):
        """Add to team score."""
        self.score += points
    
    def add_capture(self):
        """Record a capture (flag/intel)."""
        self.captures += 1
        self.add_score(10)
    
    def add_kill(self):
        """Record a kill."""
        self.kills += 1
        self.add_score(1)
    
    def reset(self):
        """Reset team state for new round."""
        self.score = 0
        self.captures = 0
        self.kills = 0
        self.spawn_index = 0
        self.intel_holder = None
    
    def set_intel_position(self, x: float, y: float, z: float):
        """Set the intel/flag position."""
        self.intel_position = (x, y, z)
        self.intel_holder = None
    
    def pick_up_intel(self, player: 'Player'):
        """Player picks up this team's intel."""
        self.intel_holder = player
    
    def drop_intel(self, x: float, y: float, z: float):
        """Intel is dropped at position."""
        self.intel_position = (x, y, z)
        self.intel_holder = None
    
    def return_intel(self, base_position: Tuple[float, float, float]):
        """Return intel to base."""
        self.intel_position = base_position
        self.intel_holder = None
    
    def __repr__(self) -> str:
        return f"Team(id={self.id}, name='{self.name}', players={self.player_count})"
