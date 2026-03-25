"""
Authoritative combat and block-damage helpers.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import defaultdict
from typing import Optional

from server.game_constants import (
    BLOCK_ACTION_BUILD,
    BLOCK_ACTION_DESTROY,
    DEFAULT_BLOCK_HEALTH,
    KILL_HEADSHOT,
    MELEE_RANGE,
    PLAYER_WIDTH_HALF,
)
from shared.packet import BlockBuild, BlockOccupy, ShootPacket

logger = logging.getLogger(__name__)

SHOT_ORIGIN_TOLERANCE = 8.0
SHOT_ORIENTATION_DOT_TOLERANCE = 0.25
RAYCAST_STEP = 0.25
HEADSHOT_HEIGHT = 0.35
BODY_HEIGHT_PADDING = 1.0


def get_combat_system(server):
    combat = getattr(server, "combat", None)
    if combat is None:
        combat = CombatSystem(server)
        server.combat = combat
    return combat


class CombatSystem:
    def __init__(self, server):
        self.server = server

    def handle_shot(self, player, packet) -> bool:
        if not player.alive or not player.spawned:
            return False
        if not (player.is_weapon_tool() or player.is_spade_tool()):
            return False
        if not self._validate_shot_packet(player, packet):
            return False

        now = time.monotonic()
        if not player.consume_shot(now):
            return False

        sanitized = self._build_sanitized_shoot_packet(player, packet)
        self.server.broadcast(bytes(sanitized.generate()))

        if player.is_spade_tool():
            return self._resolve_melee_hit(player)

        profile = player.get_weapon_profile()
        if profile.pellet_count <= 1:
            return self._resolve_hitscan(player, player.orientation)
        return self._resolve_shotgun(player, packet.seed)

    def handle_weapon_reload(self, player) -> bool:
        if not player.start_reload():
            return False
        player._broadcast_reload_state(False)
        return True

    def handle_block_build(self, player, packet) -> bool:
        if not player.alive or not player.spawned or not player.is_block_tool():
            return False

        x, y, z = packet.x, packet.y, packet.z
        if not self.server.world_manager.can_build(x, y, z):
            return False
        if not player.remove_block():
            return False

        self.server.world_manager.set_block(x, y, z, True, player.block_color)
        self._broadcast_block_mutation(player, (x, y, z), BLOCK_ACTION_BUILD)
        return True

    def handle_block_destroy(self, player, packet) -> bool:
        if not player.alive or not player.spawned:
            return False

        if player.is_block_tool():
            destroyed = self.server.world_manager.destroy_blocks([(packet.x, packet.y, packet.z)])
            if not destroyed:
                return False
            player.add_blocks(len(destroyed))
            self._broadcast_block_destroy(player, destroyed)
            return True

        if player.is_spade_tool():
            now = time.monotonic()
            if not player.consume_shot(now):
                return False
            positions = [
                (packet.x, packet.y, packet.z - 1),
                (packet.x, packet.y, packet.z),
                (packet.x, packet.y, packet.z + 1),
            ]
            destroyed = self.server.world_manager.destroy_blocks(positions)
            if not destroyed:
                return False
            self._broadcast_block_destroy(player, destroyed)
            return True

        return False

    def _resolve_melee_hit(self, attacker) -> bool:
        hit = self._find_first_player_hit(attacker, attacker.orientation, MELEE_RANGE)
        if hit is None:
            return False

        target, _, _, _ = hit
        damage = self._calculate_damage(attacker, attacker.get_weapon_profile(), headshot=False)
        target.damage(damage, source=attacker, kill_type=attacker.get_weapon_profile().kill_type)
        return True

    def _resolve_hitscan(self, attacker, direction) -> bool:
        hit = self._trace_player_hit(attacker, direction, attacker.get_weapon_profile().max_range)
        if hit is None:
            return False

        target, headshot, _, block_pos = hit
        if target is not None:
            damage = self._calculate_damage(attacker, attacker.get_weapon_profile(), headshot=headshot)
            kill_type = KILL_HEADSHOT if headshot else attacker.get_weapon_profile().kill_type
            target.damage(damage, source=attacker, kill_type=kill_type)
            return True

        if block_pos is not None:
            return self._apply_block_damage(attacker, block_pos, attacker.get_weapon_profile().block_damage)
        return False

    def _resolve_shotgun(self, attacker, seed: int) -> bool:
        profile = attacker.get_weapon_profile()
        rng = random.Random(seed)
        damages = defaultdict(float)
        headshots = {}
        hit_anything = False

        for _ in range(profile.pellet_count):
            direction = self._spread_direction(attacker, profile.spread, rng)
            hit = self._trace_player_hit(attacker, direction, profile.max_range)
            if hit is None:
                continue

            target, headshot, _, block_pos = hit
            if target is not None:
                damages[target] += self._calculate_damage(attacker, profile, headshot=headshot)
                headshots[target] = headshots.get(target, False) or headshot
                hit_anything = True
                continue

            if block_pos is not None and self._apply_block_damage(attacker, block_pos, profile.block_damage):
                hit_anything = True

        for target, damage in damages.items():
            kill_type = KILL_HEADSHOT if headshots.get(target, False) else profile.kill_type
            target.damage(int(round(damage)), source=attacker, kill_type=kill_type)

        return hit_anything or bool(damages)

    def _apply_block_damage(self, attacker, block_pos, damage: float) -> bool:
        if not getattr(self.server.config, "build_damage", True):
            return False

        total, destroyed = self.server.world_manager.apply_block_damage(
            block_pos[0],
            block_pos[1],
            block_pos[2],
            damage,
            threshold=DEFAULT_BLOCK_HEALTH,
        )
        if total > 0.0:
            self._broadcast_block_hit(attacker, block_pos)
        if destroyed:
            self._broadcast_block_destroy(attacker, [block_pos])
        return total > 0.0

    def _broadcast_block_hit(self, player, block_pos):
        packet = BlockOccupy()
        packet.loop_count = self.server.loop_count
        packet.player_id = player.id
        packet.x = block_pos[0]
        packet.y = block_pos[1]
        packet.z = block_pos[2]
        self.server.broadcast(bytes(packet.generate()))

    def _broadcast_block_destroy(self, player, positions):
        for x, y, z in positions:
            self._broadcast_block_mutation(player, (x, y, z), BLOCK_ACTION_DESTROY)

    def _broadcast_block_mutation(self, player, position, block_type: int):
        packet = BlockBuild()
        packet.loop_count = self.server.loop_count
        packet.player_id = player.id
        packet.x = position[0]
        packet.y = position[1]
        packet.z = position[2]
        packet.block_type = block_type
        self.server.broadcast(bytes(packet.generate()))

    def _build_sanitized_shoot_packet(self, player, packet) -> ShootPacket:
        profile = player.get_weapon_profile()
        sanitized = ShootPacket()
        sanitized.loop_count = getattr(self.server, "loop_count", 0)
        sanitized.shooter_id = player.id
        sanitized.shot_on_world_update = getattr(packet, "shot_on_world_update", 0)
        sanitized.x, sanitized.y, sanitized.z = player.eye
        sanitized.ori_x, sanitized.ori_y, sanitized.ori_z = player.orientation
        sanitized.damage = int(round(profile.base_damage))
        sanitized.penetration = 0
        sanitized.secondary = getattr(packet, "secondary", 0)
        sanitized.seed = getattr(packet, "seed", 0)
        return sanitized

    def _validate_shot_packet(self, player, packet) -> bool:
        packet_origin = (packet.x, packet.y, packet.z)
        packet_direction = self._normalize((packet.ori_x, packet.ori_y, packet.ori_z))
        if packet_direction is None:
            logger.debug("Rejecting shoot packet from %s with zero orientation", player.name)
            return False

        origin_error = self._distance(packet_origin, player.eye)
        if origin_error > SHOT_ORIGIN_TOLERANCE:
            logger.debug(
                "Rejecting shoot packet from %s due to origin drift %.2f",
                player.name,
                origin_error,
            )
            return False

        server_direction = self._normalize(player.orientation)
        if server_direction is None:
            return False
        dot = (
            packet_direction[0] * server_direction[0]
            + packet_direction[1] * server_direction[1]
            + packet_direction[2] * server_direction[2]
        )
        if dot < SHOT_ORIENTATION_DOT_TOLERANCE:
            logger.debug(
                "Rejecting shoot packet from %s due to direction mismatch %.3f",
                player.name,
                dot,
            )
            return False
        return True

    def _trace_player_hit(self, attacker, direction, max_range: float):
        block_pos = self.server.world_manager.raycast(
            attacker.eye_x,
            attacker.eye_y,
            attacker.eye_z,
            direction[0],
            direction[1],
            direction[2],
            max_range,
        )
        max_distance = max_range
        if block_pos is not None:
            max_distance = min(max_distance, self._distance(attacker.eye, self._block_center(block_pos)))

        hit = self._find_first_player_hit(attacker, direction, max_distance)
        if hit is not None:
            target, headshot, distance, position = hit
            return target, headshot, position, block_pos
        return None, False, None, block_pos

    def _find_first_player_hit(self, attacker, direction, max_distance: float):
        closest_target = None
        closest_headshot = False
        closest_distance = max_distance + 1.0
        closest_position = None

        for target in self.server.players.values():
            if target is attacker or not target.alive or not target.spawned:
                continue
            if (
                not getattr(self.server.config, "friendly_fire", False)
                and target.team == attacker.team
            ):
                continue

            hit = self._ray_hits_target(attacker.eye, direction, max_distance, target)
            if hit is None:
                continue

            distance, position, headshot = hit
            if distance < closest_distance:
                closest_target = target
                closest_headshot = headshot
                closest_distance = distance
                closest_position = position

        if closest_target is None:
            return None
        return closest_target, closest_headshot, closest_distance, closest_position

    def _ray_hits_target(self, origin, direction, max_distance: float, target):
        top_z = min(target.z, target.eye_z) - 0.1
        bottom_z = max(target.z, target.eye_z) + BODY_HEIGHT_PADDING
        head_bottom = top_z + HEADSHOT_HEIGHT
        radius_squared = PLAYER_WIDTH_HALF * PLAYER_WIDTH_HALF

        steps = max(1, int(max_distance / RAYCAST_STEP))
        for index in range(steps + 1):
            distance = index * RAYCAST_STEP
            px = origin[0] + direction[0] * distance
            py = origin[1] + direction[1] * distance
            pz = origin[2] + direction[2] * distance

            dx = px - target.x
            dy = py - target.y
            if dx * dx + dy * dy > radius_squared:
                continue
            if pz < top_z or pz > bottom_z:
                continue

            return distance, (px, py, pz), pz <= head_bottom
        return None

    def _spread_direction(self, attacker, spread: float, rng: random.Random):
        if spread <= 0.0:
            return attacker.orientation

        side_amount = rng.uniform(-spread, spread)
        head_amount = rng.uniform(-spread, spread)
        direction = (
            attacker.o_x + attacker.side_x * side_amount + attacker.head_x * head_amount,
            attacker.o_y + attacker.side_y * side_amount + attacker.head_y * head_amount,
            attacker.o_z + attacker.side_z * side_amount + attacker.head_z * head_amount,
        )
        normalized = self._normalize(direction)
        if normalized is None:
            return attacker.orientation
        return normalized

    def _calculate_damage(self, attacker, profile, headshot: bool) -> int:
        damage = profile.base_damage * attacker.movement_profile.damage_multiplier
        if headshot:
            damage *= attacker.movement_profile.headshot_damage_multiplier
        return max(1, int(round(damage)))

    def _block_center(self, block_pos):
        return (block_pos[0] + 0.5, block_pos[1] + 0.5, block_pos[2] + 0.5)

    def _distance(self, a, b) -> float:
        return math.sqrt(
            (a[0] - b[0]) * (a[0] - b[0])
            + (a[1] - b[1]) * (a[1] - b[1])
            + (a[2] - b[2]) * (a[2] - b[2])
        )

    def _normalize(self, vector) -> Optional[tuple[float, float, float]]:
        magnitude = math.sqrt(
            vector[0] * vector[0] + vector[1] * vector[1] + vector[2] * vector[2]
        )
        if magnitude <= 0.000001:
            return None
        return (
            vector[0] / magnitude,
            vector[1] / magnitude,
            vector[2] / magnitude,
        )
