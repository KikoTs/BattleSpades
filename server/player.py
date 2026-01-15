"""
Player entity for BattleSpades.
Represents a connected player with position, health, inventory, etc.
"""

import time
import logging
from typing import Optional, Tuple, TYPE_CHECKING
from dataclasses import dataclass, field

from aoslib.constants import (
    MAX_HEALTH, MAX_BLOCKS, MAX_GRENADES,
    WEAPON_RIFLE, TOOL_WEAPON,
    TEAM_SPECTATOR,
)

if TYPE_CHECKING:
    from .connection import Connection

logger = logging.getLogger(__name__)


@dataclass
class InputState:
    """Current input state from client."""
    up: bool = False
    down: bool = False
    left: bool = False
    right: bool = False
    jump: bool = False
    crouch: bool = False
    sneak: bool = False
    sprint: bool = False
    
    primary_fire: bool = False
    secondary_fire: bool = False


class Player:
    """
    Represents a player in the game.
    Handles position, health, weapons, and input processing.
    """
    
    def __init__(
        self,
        id: int,
        name: str,
        team: int,
        weapon: int,
        connection: Optional['Connection'] = None,
    ):
        self.id = id
        self.name = name
        self.team = team
        self.weapon = weapon
        self.connection = connection
        
        # Position and orientation (Z-down coordinate system)
        self.x: float = 0.0
        self.y: float = 0.0
        self.z: float = 0.0
        
        self.yaw: float = 0.0    # Horizontal angle (radians)
        self.pitch: float = 0.0  # Vertical angle (radians)
        
        # 3D orientation vector (for WorldUpdate)
        self.o_x: float = 1.0
        self.o_y: float = 0.0
        self.o_z: float = 0.0
        
        # Velocity
        self.vx: float = 0.0
        self.vy: float = 0.0
        self.vz: float = 0.0
        
        # State
        self.health: int = MAX_HEALTH
        self.blocks: int = MAX_BLOCKS
        self.grenades: int = MAX_GRENADES
        
        self.tool: int = TOOL_WEAPON
        self.block_color: int = 0x00FFFF  # Default cyan
        
        # Ammo (clip, reserve)
        self.ammo_clip: int = 10
        self.ammo_reserve: int = 50
        
        # Flags
        self.spawned: bool = False
        self.alive: bool = False
        self.admin: bool = False
        self.muted: bool = False
        
        # Respawn
        self.respawn_time: float = 0.0
        self.death_time: float = 0.0
        
        # Input
        self.input = InputState()
        
        # Last update time
        self.last_update: float = time.time()
        self.last_position_update: float = 0.0
        
        # Stats
        self.kills: int = 0
        self.deaths: int = 0
        self.captures: int = 0
    
    @property
    def position(self) -> Tuple[float, float, float]:
        """Get position as tuple."""
        return (self.x, self.y, self.z)
    
    @position.setter
    def position(self, value: Tuple[float, float, float]):
        """Set position from tuple."""
        self.x, self.y, self.z = value
    
    @property
    def orientation(self) -> Tuple[float, float, float]:
        """Get orientation as (o_x, o_y, o_z) tuple."""
        return (self.o_x, self.o_y, self.o_z)
    
    @orientation.setter
    def orientation(self, value: Tuple[float, float, float]):
        """Set orientation from tuple."""
        self.o_x, self.o_y, self.o_z = value
    
    @property
    def velocity(self) -> Tuple[float, float, float]:
        """Get velocity as tuple."""
        return (self.vx, self.vy, self.vz)
    
    @velocity.setter
    def velocity(self, value: Tuple[float, float, float]):
        """Set velocity from tuple."""
        self.vx, self.vy, self.vz = value
    
    def set_position(self, x: float, y: float, z: float):
        """Set player position."""
        self.x = x
        self.y = y
        self.z = z
    
    def set_orientation(self, yaw: float, pitch: float):
        """Set player orientation."""
        self.yaw = yaw
        self.pitch = pitch
    
    def spawn(self, x: float, y: float, z: float):
        """Spawn the player at the given position."""
        self.set_position(x, y, z)
        self.velocity = (0.0, 0.0, 0.0)
        
        self.health = MAX_HEALTH
        self.alive = True
        self.spawned = True
        
        # Reset inventory
        self.blocks = MAX_BLOCKS
        self.grenades = MAX_GRENADES
        self._reset_ammo()
        
        logger.debug(f"Player {self.name} spawned at ({x:.1f}, {y:.1f}, {z:.1f})")
    
    def _reset_ammo(self):
        """Reset ammo based on weapon type."""
        from aoslib.constants import WEAPON_RIFLE, WEAPON_SMG, WEAPON_SHOTGUN
        
        if self.weapon == WEAPON_RIFLE:
            self.ammo_clip = 10
            self.ammo_reserve = 50
        elif self.weapon == WEAPON_SMG:
            self.ammo_clip = 30
            self.ammo_reserve = 120
        elif self.weapon == WEAPON_SHOTGUN:
            self.ammo_clip = 6
            self.ammo_reserve = 48
    
    def damage(self, amount: int, source: Optional['Player'] = None, kill_type: int = 0) -> bool:
        """
        Apply damage to the player.
        Returns True if the player died.
        """
        if not self.alive:
            return False
        
        self.health = max(0, self.health - amount)
        
        if self.health <= 0:
            self.die(killer=source, kill_type=kill_type)
            return True
        
        return False
    
    def die(self, killer: Optional['Player'] = None, kill_type: int = 0):
        """Handle player death."""
        self.alive = False
        self.spawned = False
        self.death_time = time.time()
        self.deaths += 1
        
        if killer and killer != self:
            killer.kills += 1
        
        logger.debug(f"Player {self.name} died (killer: {killer.name if killer else 'none'})")
    
    def heal(self, amount: int):
        """Heal the player."""
        if self.alive:
            self.health = min(MAX_HEALTH, self.health + amount)
    
    def add_blocks(self, count: int = 1):
        """Add blocks to inventory."""
        self.blocks = min(MAX_BLOCKS, self.blocks + count)
    
    def remove_block(self) -> bool:
        """Remove a block from inventory. Returns False if none left."""
        if self.blocks > 0:
            self.blocks -= 1
            return True
        return False
    
    def set_tool(self, tool: int):
        """Set the current tool."""
        self.tool = tool
    
    def set_color(self, color: int):
        """Set the block color."""
        self.block_color = color
    
    async def update(self, dt: float):
        """Update player state for this tick."""
        if not self.alive or not self.spawned:
            return
        
        self.last_update = time.time()
        
        # Process input and update position
        # (Server-side movement validation would go here)
    
    def update_input(self, up: bool, down: bool, left: bool, right: bool,
                     jump: bool, crouch: bool, sneak: bool, sprint: bool):
        """Update input state from client."""
        self.input.up = up
        self.input.down = down
        self.input.left = left
        self.input.right = right
        self.input.jump = jump
        self.input.crouch = crouch
        self.input.sneak = sneak
        self.input.sprint = sprint
    
    def update_weapon_input(self, primary: bool, secondary: bool):
        """Update weapon input state."""
        self.input.primary_fire = primary
        self.input.secondary_fire = secondary
    
    def get_input_byte(self) -> int:
        """Pack input state into a single byte for WorldUpdate."""
        byte = 0
        if self.input.up:
            byte |= 0x01
        if self.input.down:
            byte |= 0x02
        if self.input.left:
            byte |= 0x04
        if self.input.right:
            byte |= 0x08
        if self.input.jump:
            byte |= 0x10
        if self.input.crouch:
            byte |= 0x20
        if self.input.sneak:
            byte |= 0x40
        if self.input.sprint:
            byte |= 0x80
        return byte
    
    def send(self, data: bytes, reliable: bool = True):
        """Send data to this player's connection."""
        if self.connection:
            self.connection.send(data, reliable)
    
    def send_packet(self, packet, reliable: bool = True):
        """Send a packet to this player."""
        if self.connection:
            self.connection.send_packet(packet, reliable)
    
    def disconnect(self, reason: int = 0):
        """Disconnect this player."""
        if self.connection:
            self.connection.disconnect(reason)
    
    def __repr__(self) -> str:
        return f"Player(id={self.id}, name='{self.name}', team={self.team})"
