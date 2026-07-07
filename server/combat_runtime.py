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
from shared.packet import BlockBuild, ShootPacket

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

        # Use the CLIENT's reported shot origin + direction for hit resolution
        # (already sanity-validated). The server's own eye/orientation lags the
        # client by the reconciliation delay, so raycasting from it destroys a
        # DIFFERENT cell than the player's crosshair (blocks vanishing offset /
        # underground) and draws tracers in the wrong place. The client knows
        # exactly where it fired from.
        origin = (float(packet.x), float(packet.y), float(packet.z))
        direction = self._normalize((packet.ori_x, packet.ori_y, packet.ori_z))
        if direction is None:
            direction = player.orientation
            origin = player.eye

        if player.is_spade_tool():
            # MEASURED: the 1.x client digs terrain with the spade/pick by
            # sending a ShootPacket (id 6), NOT BlockLiberate. The spade shot
            # must destroy the block it points at, not only melee-hit players.
            self._resolve_spade_dig(player, origin, direction, packet)
            return self._resolve_melee_hit(player, origin, direction)

        profile = player.get_weapon_profile()
        if profile.pellet_count <= 1:
            return self._resolve_hitscan(player, direction, origin)
        return self._resolve_shotgun(player, packet.seed, origin)

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

    def handle_block_line(self, player, packet) -> bool:
        """BlockLine (id 40) — how the 1.x client actually PLACES blocks (it
        never emits BlockBuild/id 32). Builds every cell on the line from
        (x1,y1,z1) to (x2,y2,z2); a single tap has equal endpoints -> 1 cell.
        """
        if not player.alive or not player.spawned or not player.is_block_tool():
            return False

        cells = self._block_line_cells(
            (packet.x1, packet.y1, packet.z1),
            (packet.x2, packet.y2, packet.z2),
        )
        built = []
        for (x, y, z) in cells:
            if not self.server.world_manager.can_build(x, y, z):
                continue
            if not player.remove_block():
                break  # out of blocks
            self.server.world_manager.set_block(x, y, z, True, player.block_color)
            built.append((x, y, z))

        if not built:
            return False
        for pos in built:
            self._broadcast_block_mutation(player, pos, BLOCK_ACTION_BUILD)
        return True

    def _block_line_cells(self, a, b, max_len: int = 64):
        """Integer cells along the segment a..b inclusive (deduped, capped)."""
        x1, y1, z1 = a
        x2, y2, z2 = b
        n = max(abs(x2 - x1), abs(y2 - y1), abs(z2 - z1))
        if n == 0:
            return [(x1, y1, z1)]
        n = min(n, max_len)
        seen = set()
        out = []
        for i in range(n + 1):
            t = i / float(n)
            cell = (
                int(round(x1 + (x2 - x1) * t)),
                int(round(y1 + (y2 - y1) * t)),
                int(round(z1 + (z2 - z1) * t)),
            )
            if cell not in seen:
                seen.add(cell)
                out.append(cell)
        return out

    def _resolve_spade_dig(self, player, origin, direction, packet) -> bool:
        """Raycast terrain from the CLIENT's reported origin/direction and
        remove the targeted block(s). Secondary (right-click) digs a 3-tall
        column, primary digs one cell.
        """
        if direction is None:
            return False
        block_pos = self.server.world_manager.raycast(
            origin[0],
            origin[1],
            origin[2],
            direction[0],
            direction[1],
            direction[2],
            MELEE_RANGE,
        )
        if block_pos is None:
            return False
        if getattr(packet, "secondary", 0):
            positions = [
                (block_pos[0], block_pos[1], block_pos[2] - 1),
                (block_pos[0], block_pos[1], block_pos[2]),
                (block_pos[0], block_pos[1], block_pos[2] + 1),
            ]
        else:
            positions = [block_pos]
        destroyed = self.server.world_manager.destroy_blocks(positions)
        if not destroyed:
            return False
        self._broadcast_block_destroy(player, destroyed)
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

    def _resolve_melee_hit(self, attacker, origin=None, direction=None) -> bool:
        if origin is None:
            origin = attacker.eye
        if direction is None:
            direction = attacker.orientation
        hit = self._find_first_player_hit(attacker, origin, direction, MELEE_RANGE)
        if hit is None:
            return False

        target, _, _, _ = hit
        damage = self._calculate_damage(attacker, attacker.get_weapon_profile(), headshot=False)
        target.damage(damage, source=attacker, kill_type=attacker.get_weapon_profile().kill_type)
        return True

    def _resolve_hitscan(self, attacker, direction, origin=None) -> bool:
        if origin is None:
            origin = attacker.eye
        hit = self._trace_player_hit(attacker, origin, direction, attacker.get_weapon_profile().max_range)
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

    def _resolve_shotgun(self, attacker, seed: int, origin=None) -> bool:
        if origin is None:
            origin = attacker.eye
        profile = attacker.get_weapon_profile()
        rng = random.Random(seed)
        damages = defaultdict(float)
        headshots = {}
        hit_anything = False

        for _ in range(profile.pellet_count):
            direction = self._spread_direction(attacker, profile.spread, rng)
            hit = self._trace_player_hit(attacker, origin, direction, profile.max_range)
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
        if destroyed:
            # Kill-damage guarantees client removal even if the client's own
            # ledger drifted from ours (its per-hit crack progression came
            # from the earlier hit broadcasts).
            self._broadcast_block_destroy(attacker, [block_pos])
        elif total > 0.0:
            self._broadcast_block_hit(attacker, block_pos, damage)
        return total > 0.0

    # Block damage/removal wire contract (DECOMPILED from gameScene.pyd,
    # 2026-07-07, adversarially verified): this client has NO destroy packet.
    # BlockBuild(32) is ADD-ONLY — its block_type is a MATERIAL selector
    # (0=prefab/normal, 1=snow; 2/3 KeyError-crash). Our old "destroy" as
    # type 1 made clients BUILD a snow block and fire the green snow-ring
    # particle (the reported wrong-colored effect) while the real block
    # stayed solid. ServerBlockAction(39) is a no-op stub client-side;
    # BlockOccupy(34)/BlockLiberate(35) are ownership bookkeeping. World
    # geometry changes ONLY via Damage(37): BlockManager.handle_damage ->
    # damage_handlers[type] -> crack tint -> remove_block + BLOCK-COLORED
    # debris at 0 health (DEFAULT_BLOCK_HEALTH=5, 0.25 quantization).
    # Damage.damage is a 1-byte fixed-point float — keep <= 31.75 (byte 127)
    # for signedness safety; still >6x block health.
    _BLOCK_KILL_DAMAGE = 31.75

    def _damage_type_for(self, player) -> int:
        # ALWAYS WEAPON_DAMAGE(6). DECOMPILED (BlockManager.handle_weapon_damage
        # sub_10083C80): type 6 removes EXACTLY int(position) — no expansion.
        # SPADE_DAMAGE(2) makes the CLIENT self-expand to a 3-tall column
        # (z-1, z, z+1 = the "underground" cell), and since our server already
        # picks the exact cells to destroy, type 2 double-expanded them (dig
        # one block -> client removed a whole vertical set). GRENADE_DAMAGE(7)
        # self-expands to a radius too. The server owns the exact cell list, so
        # type 6 keeps the client VXL byte-identical to ours.
        import shared.constants as C
        return int(C.WEAPON_DAMAGE)

    def _broadcast_block_damage(self, player, block_pos, damage: float,
                                damage_type: int = None, seed: int = 0):
        from shared.packet import Damage
        packet = Damage()
        packet.player_id = player.id if player is not None else -1
        packet.type = self._damage_type_for(player) if damage_type is None else int(damage_type)
        packet.damage = min(float(damage), self._BLOCK_KILL_DAMAGE)
        packet.face = 0
        # chunk_check=1 tells the client to flood-fill connectivity after the
        # removal and ANIMATE any now-disconnected chunk falling (the classic
        # collapse). Our server-side find_unsupported_chunks mirrors the same
        # removal so both VXLs agree; the client owns the visual.
        packet.chunk_check = 1
        packet.seed = int(seed) & 0xFF
        # causer_id is an ENTITY id the client reads UNSIGNED (measured: -1
        # decodes to 65535 -> entities[65535] lookup aborts the whole damage
        # handler). The reference server sends the shooter's id here; use a
        # small in-range id (0 for no player) so the client's entity lookup
        # resolves safely instead of exploding.
        packet.causer_id = player.id if player is not None else 0
        # Send the INTEGER cell coords, NOT the block center. MEASURED live
        # (real shot path, 2026-07-07): the client resolves the damaged cell as
        # int(position + 0.5) (round-half-up), so a block-center (cell+0.5)
        # rounded to cell+1 — every dug/shot block broke one cell too far on
        # +x/+y/+z (the "offset / underground" the user saw). Sending the exact
        # cell coordinate makes int(cell + 0.5) == cell.
        packet.position = (
            float(block_pos[0]),
            float(block_pos[1]),
            float(block_pos[2]),
        )
        self.server.broadcast(bytes(packet.generate()))

    def _broadcast_block_hit(self, player, block_pos, damage: float = None):
        """Per-hit crack progression on all clients (their ledger mirrors
        ours and removes the block itself when it reaches 0)."""
        if damage is None:
            damage = player.get_weapon_profile().block_damage if player is not None else 1.0
        self._broadcast_block_damage(player, block_pos, damage)

    def _broadcast_block_destroy(self, player, positions, damage_type: int = None):
        """Guaranteed removal on all clients: kill-damage Damage(37) per cell
        (a no-op for cells the client already removed on its own)."""
        for pos in positions:
            self._broadcast_block_damage(
                player, pos, self._BLOCK_KILL_DAMAGE, damage_type=damage_type
            )
        self._collapse_unsupported(player, positions)

    def _collapse_unsupported(self, player, removed_positions):
        """Floating-structure collapse: any solid chunk left disconnected from
        the base plane by these removals falls too (cascading until stable).
        The classic AoS behavior — without it a streetlamp whose base is dug
        out levitates forever."""
        wm = self.server.world_manager
        frontier = list(removed_positions)
        guard = 0
        while frontier and guard < 8:
            guard += 1
            chunks = wm.find_unsupported_chunks(frontier)
            if not chunks:
                return
            frontier = []
            for chunk in chunks:
                fell = wm.destroy_blocks(chunk)
                for pos in fell:
                    self._broadcast_block_damage(
                        player, pos, self._BLOCK_KILL_DAMAGE
                    )
                frontier.extend(fell)

    def _broadcast_block_mutation(self, player, position, block_type: int):
        """BUILD announcements only — BlockBuild(32) is add-only on the wire;
        destroys route through _broadcast_block_destroy (Damage 37)."""
        if block_type != BLOCK_ACTION_BUILD:
            self._broadcast_block_destroy(player, [position])
            return
        packet = BlockBuild()
        packet.loop_count = self.server.loop_count
        packet.player_id = player.id
        packet.x = position[0]
        packet.y = position[1]
        packet.z = position[2]
        # Material selector on this client: 0=prefab (normal build).
        packet.block_type = 0
        self.server.broadcast(bytes(packet.generate()))

    def _build_sanitized_shoot_packet(self, player, packet) -> ShootPacket:
        profile = player.get_weapon_profile()
        sanitized = ShootPacket()
        sanitized.loop_count = getattr(self.server, "loop_count", 0)
        sanitized.shooter_id = player.id
        sanitized.shot_on_world_update = getattr(packet, "shot_on_world_update", 0)
        # Relay the CLIENT's own shot origin + direction (already validated)
        # so other clients draw the tracer/muzzle from the exact spot the
        # shooter fired. Overwriting with the server's lagged eye/orientation
        # put every relayed shot in the wrong place.
        sanitized.x, sanitized.y, sanitized.z = packet.x, packet.y, packet.z
        sanitized.ori_x, sanitized.ori_y, sanitized.ori_z = packet.ori_x, packet.ori_y, packet.ori_z
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

    def _trace_player_hit(self, attacker, origin, direction, max_range: float):
        block_pos = self.server.world_manager.raycast(
            origin[0],
            origin[1],
            origin[2],
            direction[0],
            direction[1],
            direction[2],
            max_range,
        )
        max_distance = max_range
        if block_pos is not None:
            max_distance = min(max_distance, self._distance(origin, self._block_center(block_pos)))

        hit = self._find_first_player_hit(attacker, origin, direction, max_distance)
        if hit is not None:
            target, headshot, distance, position = hit
            return target, headshot, position, block_pos
        return None, False, None, block_pos

    def _find_first_player_hit(self, attacker, origin, direction, max_distance: float):
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

            hit = self._ray_hits_target(origin, direction, max_distance, target)
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
