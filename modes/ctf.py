"""
Capture the Flag game mode.
Two teams fight to capture the enemy's intel and return it to their base.
"""

import time
import logging
from typing import Optional, Tuple, TYPE_CHECKING

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.game_constants import (
    PLAYER_STANDING_POS_ABOVE_GROUND,
    TEAM1,
    TEAM2,
)

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player

logger = logging.getLogger(__name__)

_FALLBACK_BASE_RADIUS = float(CG.CLASSIC_CTF_BASE_CAPTURE_DISTANCE)


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
    mode_code = "ctf"
    intel_auto_return_default = True
    shoot_with_intel_default = False
    intel_offset_from_base = 12.0
    
    def __init__(self, server):
        super().__init__(server)

        data = mode_data.get(self.mode_code)
        overlay = getattr(server.config, "mode_settings", {}).get(
            self.mode_code, {}
        )
        from server.game_rules import get_rules
        rules = get_rules(server.config)
        self.score_limit = int(overlay.get(
            "score_limit", rules.get("RULE_CTF_SCORE_TARGET")
        ))
        resolve_time = getattr(server.config, "configured_time_limit", None)
        self.time_limit = (
            resolve_time(self.mode_code, data.default_time_limit)
            if callable(resolve_time)
            else float(overlay.get("time_limit", data.default_time_limit))
        )
        explicit = getattr(rules, "explicit", set())
        self.intel_auto_return = bool(overlay.get(
            "intel_auto_return",
            rules.get("RULE_CTF_ENABLE_INTEL_AUTO_RETURN")
            if "RULE_CTF_ENABLE_INTEL_AUTO_RETURN" in explicit
            else self.intel_auto_return_default,
        ))
        self.intel_return_on_touch = bool(overlay.get(
            "intel_return_on_touch",
            rules.get("RULE_CTF_ENABLE_INTEL_RETURN_ON_TOUCH"),
        ))
        self.intel_in_own_base_to_score = bool(overlay.get(
            "intel_in_own_base_to_score",
            rules.get("RULE_CTF_ENABLE_INTEL_IN_OWN_BASE_TO_SCORE"),
        ))
        self.shoot_with_intel = bool(overlay.get(
            "shoot_with_intel",
            self.shoot_with_intel_default
            if "RULE_CTF_ENABLE_SHOOT_WITH_INTEL" not in explicit
            else rules.get("RULE_CTF_ENABLE_SHOOT_WITH_INTEL"),
        ))
        
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
        self.pickup_cooldown = float(C.NO_PICKUP_AFTER_DROP_TIME)
        self.intel_home_positions = dict(self.intel_positions)
        self._intel_entities = {TEAM1: None, TEAM2: None}
        self._base_entities = {TEAM1: None, TEAM2: None}
        self.base_bounds = {
            TEAM1: (0, 0, 0, 0, 0, 0),
            TEAM2: (0, 0, 0, 0, 0, 0),
        }
    
    async def on_mode_start(self):
        """Initialize intel and base positions."""
        await super().on_mode_start()
        # A same-scene round restart preserves native Player objects. Clear a
        # previous carrier marker before replacing the authoritative holders.
        old_holders = []
        for holder in self.intel_holder.values():
            if holder is not None and all(holder is not old for old in old_holders):
                old_holders.append(holder)
        for holder in old_holders:
            self._set_carrier_visibility(holder, False)
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
        offset = float(self.intel_offset_from_base)
        self.intel_positions[TEAM1] = _intel_near(
            self.server, self.base_positions[TEAM1], +offset
        )
        self.intel_positions[TEAM2] = _intel_near(
            self.server, self.base_positions[TEAM2], -offset
        )
        self.intel_home_positions = dict(self.intel_positions)
        self.base_bounds = {
            TEAM1: self._base_zone_bounds(TEAM1),
            TEAM2: self._base_zone_bounds(TEAM2),
        }
        
        # Update team objects
        for team_id, pos in self.intel_positions.items():
            self.server.teams[team_id].set_intel_position(*pos)

        self._place_objective_entities()
        self._send_base_zones()
        
        logger.info("CTF mode started")

    def _place_objective_entities(self):
        """Create CTF objective markers without unsafe legacy entity packets.

        The retail ``GameScene.ENTITIES`` mapping has ``INTEL_PICKUP`` (16),
        but not the legacy ``BASE`` type (1).  A BASE sent through packet 21
        crashes/freeze-loops the client during CTF join.  We retain a private
        base marker for authoritative capture logic and expose only the intel;
        the base itself is represented by the map's authored base/tent area.
        """
        reg = getattr(self.server, "entity_registry", None)
        wm = getattr(self.server, "world_manager", None)
        if reg is None or wm is None:
            return

        # BaseMode has already rebuilt the map-owned crates/lights.  Remove
        # only stale CTF markers here: clearing the registry used to erase all
        # shared resources in CTF.  Do not trust the remembered ids alone;
        # RoundLifecycle resets the allocator and a new crate can legitimately
        # reuse an old intel id before this method runs.
        for ent in reg.all():
            if getattr(ent, "kind", "") not in ("base", "intel"):
                continue
            removed = reg.remove(ent.entity_id)
            if (
                removed is not None
                and removed.alive
                and getattr(removed, "wire_visible", True)
            ):
                self.server.broadcast_destroy_entity(removed.entity_id)
        self._base_entities = {TEAM1: None, TEAM2: None}
        self._intel_entities = {TEAM1: None, TEAM2: None}

        for team in (TEAM1, TEAM2):
            bx, by, _bz = self.base_positions[team]
            x, y, z = wm.dry_surface_anchor(bx, by)
            base = reg.place(
                int(C.BASE), x, y, z, state=team, kind="base",
                wire_visible=False,
            )
            self._base_entities[team] = base.entity_id

            ix, iy, _iz = self.intel_positions[team]
            x, y, z = wm.dry_surface_anchor(ix, iy)
            flag = reg.place(int(C.INTEL_PICKUP), x, y, z, state=team, kind="intel")
            self._intel_entities[team] = flag.entity_id

            if getattr(self.server.config, "entities_wire_ready", False):
                self.server.broadcast_create_entity(flag)

    def _set_intel_entity(self, team: int, visible: bool, *, broadcast: bool = True):
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
        flag = reg.place(int(C.INTEL_PICKUP), x, y, z, state=team, kind="intel")
        self._intel_entities[team] = flag.entity_id
        if broadcast and getattr(self.server.config, "entities_wire_ready", False):
            self.server.broadcast_create_entity(flag)

    def _base_zone_bounds(self, team: int) -> tuple[int, int, int, int, int, int]:
        """Return the native minimap/capture bounds for one team's base.

        Authored UGC bounds are retained when present. Voxel-only stock maps
        receive the retail classic five-block capture box around the stable
        terrain base anchor. Values are raw voxel coordinates, not fixed-point
        packet values; packet 43 writes these six fields as signed shorts.
        """
        wm = getattr(self.server, "world_manager", None)
        metadata = getattr(wm, "map_metadata", None)
        authored = [] if metadata is None else metadata.base_zones.get(team, [])
        if authored:
            zone = authored[0]
            x0, x1, y0, y1, z0, z1 = zone.extents
            shift = int(getattr(getattr(wm, "map", None), "source_z_shift", 0))
            bounds = (
                zone.x + x0, zone.x + x1,
                zone.y + y0, zone.y + y1,
                zone.z + z0 + shift, zone.z + z1 + shift,
            )
        else:
            x, y, z = self.base_positions[team]
            radius = _FALLBACK_BASE_RADIUS
            bounds = (x - radius, x + radius, y - radius, y + radius, z - 3, z + 6)

        x0, x1, y0, y1, z0, z1 = bounds
        return (
            max(0, min(int(C.MAP_X) - 1, int(round(x0)))),
            max(0, min(int(C.MAP_X) - 1, int(round(x1)))),
            max(0, min(int(C.MAP_Y) - 1, int(round(y0)))),
            max(0, min(int(C.MAP_Y) - 1, int(round(y1)))),
            max(0, min(int(C.MAP_Z) - 1, int(round(z0)))),
            max(0, min(int(C.MAP_Z) - 1, int(round(z1)))),
        )

    def _base_zone_packet(self, team: int):
        """Build the retail packet-43 base zone and its CTF icon billboard."""
        from shared.packet import MinimapZone

        x0, x1, y0, y1, z0, z1 = self.base_bounds[team]
        packet = MinimapZone()
        # The native HUD stores this byte as ``visible_team``. Sending both
        # team-owned zones lets the same snapshot survive a later team switch.
        packet.key = int(team)
        packet.color = tuple(int(value) for value in self.server.teams[team].color)
        packet.A2018, packet.A2019 = x0, x1
        packet.A2020, packet.A2021 = y0, y1
        packet.A2022, packet.A2023 = z0, z1
        packet.icon_scale = 1.0
        packet.icon_id = int(CG.ZONE_ICON_CTF)
        packet.locked_in_zone = 0
        return packet

    def _send_base_zones(self, connection=None) -> None:
        """Send both native base zones to all clients or one joining client."""
        for team in (TEAM1, TEAM2):
            data = bytes(self._base_zone_packet(team).generate())
            if connection is None:
                self.server.broadcast(data, reliable=True)
            else:
                connection.send(data, reliable=True)

    def _set_carrier_visibility(self, player, visible: bool, connection=None) -> None:
        """Expose or clear an intel carrier through ChangePlayer action 8.

        Ground intel owns its native type-16 minimap icon. While carried there
        is no ground entity, so the retail high-visibility player marker keeps
        the objective trackable by both teams until it is dropped or captured.
        """
        from shared.packet import ChangePlayer

        player_id = getattr(player, "id", None)
        if player_id is None:
            return
        packet = ChangePlayer()
        packet.player_id = int(player_id)
        packet.type = int(C.SET_HIGH_MINIMAP_VISIBILITY)
        packet.high_minimap_visibility = int(bool(visible))
        data = bytes(packet.generate())
        if connection is None:
            self.server.broadcast(data, reliable=True)
        else:
            connection.send(data, reliable=True)

    def reveal_to(self, connection) -> None:
        """Send CTF-only minimap state after a late joiner's world reveal."""
        self._send_base_zones(connection)
        for holder in self.intel_holder.values():
            if holder is not None:
                self._set_carrier_visibility(holder, True, connection)
    
    async def on_tick(self, tick: int):
        """Check for intel pickups and captures."""
        await super().on_tick(tick)
        
        current_time = time.time()

        if self.intel_auto_return:
            for team in (TEAM1, TEAM2):
                dropped_at = self.intel_drop_time[team]
                if (
                    self.intel_holder[team] is None
                    and dropped_at > 0.0
                    and current_time - dropped_at
                    >= float(CG.CTF_INTEL_RETURN_TIME)
                ):
                    await self._return_intel(team)
        
        for player in list(self.server.players.values()):
            if not player.alive:
                continue
            
            if player.team not in (TEAM1, TEAM2):
                continue

            if (
                self.intel_return_on_touch
                and self.intel_holder[player.team] is None
                and self.intel_drop_time[player.team] > 0.0
                and self._is_near(
                    player,
                    self.intel_positions[player.team],
                    radius=float(C.PICKUP_DISTANCE),
                )
            ):
                await self._return_intel(player.team, returned_by=player)
            
            # Check intel pickup
            enemy_team = TEAM2 if player.team == TEAM1 else TEAM1
            if self.intel_holder[enemy_team] is None:
                # Intel is on ground
                intel_pos = self.intel_positions[enemy_team]
                if self._is_near(player, intel_pos, radius=float(C.PICKUP_DISTANCE)):
                    # Check cooldown
                    if current_time - self.intel_drop_time[enemy_team] > self.pickup_cooldown:
                        await self._pickup_intel(player, enemy_team)
            
            # Check intel capture
            if self.intel_holder[enemy_team] == player:
                # Player is holding enemy intel
                own_intel_home = (
                    self.intel_holder[player.team] is None
                    and self.intel_drop_time[player.team] <= 0.0
                )
                if self._is_at_base(player, player.team) and (
                    not self.intel_in_own_base_to_score or own_intel_home
                ):
                    await self._capture_intel(player, enemy_team)

    def configure_initial_info(self, packet) -> None:
        """Keep the client carrier weapon gate equal to server authority."""

        packet.allow_shooting_holding_intel = int(self.shoot_with_intel)
    
    async def _pickup_intel(self, player: 'Player', intel_team: int):
        """Player picks up intel."""
        from server.pickups import broadcast_pickup
        if not broadcast_pickup(
            self.server, player, int(C.INTEL_PICKUP),
            burdensome=True, state=intel_team,
        ):
            return
        self.intel_holder[intel_team] = player
        self.intel_drop_time[intel_team] = 0.0
        self.server.teams[intel_team].pick_up_intel(player)
        self._set_intel_entity(intel_team, False)
        self._set_carrier_visibility(player, True)
        
        team_name = self.server.teams[intel_team].name
        await self.broadcast_message(f"{player.name} has the {team_name} intel!")
        
        logger.info(f"{player.name} picked up {team_name} intel")
    
    async def _capture_intel(self, player: 'Player', intel_team: int):
        """Player captures intel."""
        from server.pickups import broadcast_drop
        broadcast_drop(
            self.server, player,
            (player.x, player.y, player.z), (0.0, 0.0, 0.0),
        )
        self._set_carrier_visibility(player, False)
        self.intel_holder[intel_team] = None
        
        # Reset intel to base
        home_pos = self.intel_home_positions[intel_team]
        self.intel_positions[intel_team] = home_pos
        self.intel_drop_time[intel_team] = 0.0
        self.server.teams[intel_team].return_intel(home_pos)
        self._set_intel_entity(intel_team, True)
        
        # Add score
        player.captures += 1
        self._award_player_score(
            player,
            int(CG.CTF_INDIVIDUAL_SCORE_FOR_CAPTURED_INTEL),
            int(C.SCORE_REASON.CTF_CAPTURE_SCORE_REASON),
        )
        capturing_team = self.server.teams[player.team]
        capturing_team.add_capture()
        # Push the new team score to the HUD (CTF never did this, so the
        # score bar stayed frozen at its spawn value).
        try:
            self.server.broadcast_set_score(
                capturing_team,
                reason=int(C.SCORE_REASON.CTF_CAPTURE_SCORE_REASON),
            )
        except TypeError:
            # Compatibility with plugin/test facades predating score reasons.
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

    async def on_player_kill(
        self,
        killer: 'Player',
        victim: 'Player',
        kill_type: int,
    ) -> None:
        """Award the stock one-point personal score for an enemy kill.

        The recovered reference ``aosmodes.GameMode.on_player_kill`` performs
        this independently of intel capture/return bonuses. Classic CTF
        inherits this hook, restoring its missing per-kill points without
        changing the team capture score.
        """
        if self.ended or killer is victim:
            return
        if (
            int(getattr(killer, "team", -1)) not in (TEAM1, TEAM2)
            or int(getattr(victim, "team", -1)) not in (TEAM1, TEAM2)
            or int(killer.team) == int(victim.team)
        ):
            return
        self._award_player_score(
            killer,
            1,
            int(C.SCORE_REASON.KILL_SCORE_REASON),
        )
    
    async def _drop_intel(self, player: 'Player', intel_team: int,
                          position=None, velocity=None):
        """Player drops intel."""
        from server.pickups import broadcast_drop
        if position is None:
            position = (player.x, player.y, player.z)
        if velocity is None:
            velocity = (
                float(getattr(player, "vx", 0.0)),
                float(getattr(player, "vy", 0.0)),
                float(getattr(player, "vz", 0.0)),
            )
        dropped = broadcast_drop(self.server, player, position, velocity)
        if dropped is None:
            return
        self._set_carrier_visibility(player, False)
        self.intel_holder[intel_team] = None
        
        # DropPickup removes the carried native tool but does not create a
        # persistent entity. Settle the authoritative type-16 objective on the
        # nearest dry surface and explicitly CreateEntity it for every client.
        drop_pos = _ground_anchor(self.server, dropped[2][0], dropped[2][1])
        self.intel_positions[intel_team] = drop_pos
        self.intel_drop_time[intel_team] = time.time()
        
        self.server.teams[intel_team].drop_intel(*drop_pos)
        self._set_intel_entity(intel_team, True, broadcast=True)
        
        team_name = self.server.teams[intel_team].name
        await self.broadcast_message(f"{player.name} dropped the {team_name} intel!")
        
        logger.info(f"{player.name} dropped {team_name} intel at {drop_pos}")

    async def _return_intel(self, intel_team: int, returned_by=None) -> None:
        """Return ground intel and award the recovered touch-return point."""
        home_pos = self.intel_home_positions[intel_team]
        self.intel_positions[intel_team] = home_pos
        self.intel_drop_time[intel_team] = 0.0
        self.server.teams[intel_team].return_intel(home_pos)
        self._set_intel_entity(intel_team, True)
        if returned_by is not None:
            self._award_player_score(
                returned_by,
                int(CG.CTF_INDIVIDUAL_SCORE_FOR_RETURNING_INTEL),
                int(C.SCORE_REASON.CTF_CLAIM_SCORE_REASON),
            )
        team_name = self.server.teams[intel_team].name
        await self.broadcast_message(f"The {team_name} intel returned to base!")
        logger.info("%s intel auto-returned", team_name)

    def _award_player_score(self, player, amount: int, reason: int) -> None:
        """Apply and replicate one native CTF personal-score event."""

        if amount <= 0:
            return
        player.score = int(getattr(player, "score", 0)) + int(amount)
        from server.scoreboard import send_player_score

        send_player_score(self.server, player, reason=int(reason))

    async def handle_drop_pickup(self, player, position, velocity) -> bool:
        """Packet-71 mode hook; only the actual enemy-intel holder may drop."""
        for team_id in (TEAM1, TEAM2):
            if self.intel_holder[team_id] is player:
                await self._drop_intel(player, team_id, position, velocity)
                return True
        return False
    
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

    def _is_at_base(self, player: 'Player', team: int) -> bool:
        """Check the same visible base box used by the packet-43 HUD zone."""
        x0, x1, y0, y1, _z0, _z1 = self.base_bounds[team]
        base_z = self.base_positions[team][2]
        return (
            x0 <= float(player.x) <= x1
            and y0 <= float(player.y) <= y1
            and abs(float(player.z) - float(base_z)) <= 6.0
        )
