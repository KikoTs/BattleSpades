"""
Capture the Flag game mode.
Two teams fight to capture the enemy's intel and return it to their base.
"""

import time
import logging
from typing import Optional, Tuple, TYPE_CHECKING

import shared.constants as C

from server.game_constants import PLAYER_STANDING_POS_ABOVE_GROUND, TEAM1, TEAM2

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player

logger = logging.getLogger(__name__)


def _ground_anchor(
    server,
    x: float,
    y: float,
    fallback_z: float = 62.0 - PLAYER_STANDING_POS_ABOVE_GROUND,
) -> tuple[float, float, float]:
    world_manager = getattr(server, "world_manager", None)
    if world_manager is None:
        return (x, y, fallback_z)
    try:
        # Anchor on the nearest DRY column so a base/intel whose nominal spot is
        # over water snaps to the shoreline instead of the seabed.
        return world_manager.dry_ground_anchor(x, y)
    except Exception:
        return (x, y, fallback_z)


def _intel_near(server, base_pos, dx: float) -> tuple[float, float, float]:
    """Place the intel `dx` blocks along +x from the base, re-anchored to dry
    ground (keeps it out of the water near shoreline bases)."""
    return _ground_anchor(server, base_pos[0] + dx, base_pos[1])


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
            TEAM1: (0.0, 0.0, 0.0),
            TEAM2: (0.0, 0.0, 0.0),
        }
        
        # Base positions (tent locations)
        self.base_positions = {
            TEAM1: (0.0, 0.0, 0.0),
            TEAM2: (0.0, 0.0, 0.0),
        }
        
        # Intel state
        self.intel_holder = {
            TEAM1: None,
            TEAM2: None,
        }
        
        # Pickup cooldown (to prevent instant re-grab)
        self.intel_drop_time = {TEAM1: 0.0, TEAM2: 0.0}
        self.pickup_cooldown = 2.0  # Seconds
        self.intel_home_positions = dict(self.intel_positions)
        self._intel_entities = {TEAM1: None, TEAM2: None}
        self._base_entities = {TEAM1: None, TEAM2: None}
    
    async def on_mode_start(self):
        """Initialize intel and base positions."""
        await super().on_mode_start()
        self.intel_holder = {TEAM1: None, TEAM2: None}
        self.intel_drop_time = {TEAM1: 0.0, TEAM2: 0.0}
        
        # Prefer authored sidecar base/spawn zones, falling back to validated
        # dry terrain in the legacy west/east team regions on voxel-only maps.
        wm = getattr(self.server, "world_manager", None)
        if wm is not None and hasattr(wm, "team_base_anchor"):
            self.base_positions[TEAM1] = wm.team_base_anchor(TEAM1)
            self.base_positions[TEAM2] = wm.team_base_anchor(TEAM2)
        else:
            self.base_positions[TEAM1] = _ground_anchor(self.server, 64.0, 256.0)
            self.base_positions[TEAM2] = _ground_anchor(self.server, 448.0, 256.0)

        # Intel sits a few blocks toward midfield from each base, re-anchored to
        # dry ground so it never floats over water.
        self.intel_positions[TEAM1] = _intel_near(self.server, self.base_positions[TEAM1], +12.0)
        self.intel_positions[TEAM2] = _intel_near(self.server, self.base_positions[TEAM2], -12.0)
        self.intel_home_positions = dict(self.intel_positions)
        
        # Update team objects
        for team_id, pos in self.intel_positions.items():
            self.server.teams[team_id].set_intel_position(*pos)

        self._place_objective_entities()
        
        logger.info("CTF mode started")

    def _place_objective_entities(self):
        """Create the retail base tent and team flag models on dry surfaces."""
        reg = getattr(self.server, "entity_registry", None)
        wm = getattr(self.server, "world_manager", None)
        if reg is None or wm is None:
            return
        for ent in reg.all():
            if getattr(ent, "kind", "") != "projectile":
                self.server.broadcast_destroy_entity(ent.entity_id)
        reg.clear()
        for team in (TEAM1, TEAM2):
            bx, by, _bz = self.base_positions[team]
            x, y, z = wm.dry_surface_anchor(bx, by)
            base = reg.place(int(C.BASE), x, y, z, state=team, kind="base")
            self._base_entities[team] = base.entity_id

            ix, iy, _iz = self.intel_positions[team]
            x, y, z = wm.dry_surface_anchor(ix, iy)
            flag = reg.place(int(C.FLAG), x, y, z, state=team, kind="flag")
            self._intel_entities[team] = flag.entity_id

            if getattr(self.server.config, "entities_wire_ready", False):
                self.server.broadcast_create_entity(base)
                self.server.broadcast_create_entity(flag)

    def _set_intel_entity(self, team: int, visible: bool):
        reg = getattr(self.server, "entity_registry", None)
        wm = getattr(self.server, "world_manager", None)
        if reg is None or wm is None:
            return
        old_id = self._intel_entities.get(team)
        if old_id is not None:
            if reg.remove(old_id) is not None:
                self.server.broadcast_destroy_entity(old_id)
            self._intel_entities[team] = None
        if not visible:
            return
        px, py, _pz = self.intel_positions[team]
        x, y, z = wm.dry_surface_anchor(px, py)
        flag = reg.place(int(C.FLAG), x, y, z, state=team, kind="flag")
        self._intel_entities[team] = flag.entity_id
        if getattr(self.server.config, "entities_wire_ready", False):
            self.server.broadcast_create_entity(flag)
    
    async def on_tick(self, tick: int):
        """Check for intel pickups and captures."""
        await super().on_tick(tick)
        
        current_time = time.time()
        
        for player in list(self.server.players.values()):
            if not player.alive:
                continue
            
            if player.team not in (TEAM1, TEAM2):
                continue
            
            # Check intel pickup
            enemy_team = TEAM2 if player.team == TEAM1 else TEAM1
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
        self._set_intel_entity(intel_team, False)
        
        team_name = self.server.teams[intel_team].name
        await self.broadcast_message(f"{player.name} has the {team_name} intel!")
        
        logger.info(f"{player.name} picked up {team_name} intel")
    
    async def _capture_intel(self, player: 'Player', intel_team: int):
        """Player captures intel."""
        self.intel_holder[intel_team] = None
        
        # Reset intel to base
        home_pos = self.intel_home_positions[intel_team]
        self.intel_positions[intel_team] = home_pos
        self.server.teams[intel_team].return_intel(home_pos)
        self._set_intel_entity(intel_team, True)
        
        # Add score
        player.captures += 1
        capturing_team = self.server.teams[player.team]
        capturing_team.add_capture()
        # Push the new team score to the HUD (CTF never did this, so the
        # score bar stayed frozen at its spawn value).
        self.server.broadcast_set_score(capturing_team)

        # Check for win
        winning = capturing_team.score >= self.score_limit
        
        team_name = self.server.teams[intel_team].name
        await self.broadcast_message(f"{player.name} captured the {team_name} intel!")
        
        logger.info(f"{player.name} captured {team_name} intel")
        
        if winning:
            await self._end_by_score(player.team)
    
    async def on_player_death(self, player: 'Player', killer: Optional['Player'], kill_type: int):
        """Drop intel if player was holding it."""
        for team_id in (TEAM1, TEAM2):
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
        self._set_intel_entity(intel_team, True)
        
        team_name = self.server.teams[intel_team].name
        await self.broadcast_message(f"{player.name} dropped the {team_name} intel!")
        
        logger.info(f"{player.name} dropped {team_name} intel at {drop_pos}")
    
    async def on_player_leave(self, player: 'Player'):
        """Handle player leaving with intel."""
        for team_id in (TEAM1, TEAM2):
            if self.intel_holder[team_id] == player:
                await self._drop_intel(player, team_id)
                break
    
    async def on_player_team_change(self, player: 'Player', old_team: int, new_team: int):
        """Handle player changing team while holding intel."""
        enemy_team = TEAM2 if old_team == TEAM1 else TEAM1
        if self.intel_holder[enemy_team] == player:
            await self._drop_intel(player, enemy_team)
    
    def _is_near(self, player: 'Player', pos: Tuple[float, float, float], radius: float) -> bool:
        """Check if player is within radius of a position."""
        dx = player.x - pos[0]
        dy = player.y - pos[1]
        dz = player.z - pos[2]
        dist_sq = dx*dx + dy*dy + dz*dz
        return dist_sq <= radius * radius
