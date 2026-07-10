"""
Authoritative combat and block-damage helpers.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Optional

from aoslib.world import cube_line
import shared.constants as C
from server.game_constants import (
    BLOCK_ACTION_BUILD,
    BLOCK_ACTION_DESTROY,
    DEFAULT_BLOCK_HEALTH,
    KILL_HEADSHOT,
    MELEE_RANGE,
)
from shared.packet import BlockBuild, BlockLine, ShootPacket

logger = logging.getLogger(__name__)

SHOT_ORIGIN_TOLERANCE = 8.0
SHOT_ORIENTATION_DOT_TOLERANCE = 0.25
HITBOX_SCALE = 0.05
PELLET_GROUP_TIMEOUT = 0.25
ASSAULT_BURST_SIZE = 3
ASSAULT_BURST_WINDOW = 0.30
ASSAULT_BURST_LOOP_INTERVAL = 6
MINIGUN_INTERVAL_INITIAL = 0.30
MINIGUN_INTERVAL_MIN = 0.10
MINIGUN_INTERVAL_RAMP_PER_SECOND = 0.15

# Stock KV6 collision-model bounding boxes. Values are
# ((size_x, size_y, size_z), (effective_pivot_x, pivot_y, pivot_z)); effective
# pivots include CLASS_BODY_PARTS_OFFSETS. The client tests these oriented
# boxes, not a generic player cylinder/AABB and not occupied KV6 voxels.
_COMMON_ARMS = ((24, 20, 12), (12, 2, 1))
_ZOMBIE_ARMS = ((24, 20, 12), (12, 10, 6))
_CROUCH_TORSO = ((16, 16, 14), (8, 14, 2))
_CROUCH_LEG = ((6, 14, 16), (3, 7, 3))


def _part(size, pivot):
    return (size, pivot)


_CLASS_HITBOXES = {
    C.CLASS_SOLDIER: (_part((14, 16, 15), (7, 8, 13)), _part((16, 9, 19), (7, 6, .5)), _COMMON_ARMS, _part((6, 11, 24), (3, 5.5, 0)), _part((6, 11, 24), (3, 5.5, 0))),
    C.CLASS_SCOUT: (_part((16, 16, 12), (8, 8, 11.5)), _part((16, 8, 18), (8, 5.5, 0)), _COMMON_ARMS, _part((6, 11, 24), (3, 5.5, 0)), _part((6, 11, 24), (3, 5.5, 0))),
    C.CLASS_ROCKETEER: (_part((16, 14, 12), (8, 7, 11.5)), _part((16, 10, 18), (8, 5, 0)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_MINER: (_part((16, 17, 12), (8, 8.5, 11.5)), _part((16, 11, 18), (8, 5.5, 0)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_ZOMBIE: (_part((11, 12, 12), (5.5, 6, 11.5)), _part((16, 8, 18), (8, 4, 0)), _ZOMBIE_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_CLASSIC_SOLDIER: (_part((14, 14, 12), (7, 8, 11.5)), _part((18, 12, 20), (8, 7.5, 1)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_GANGSTER_1: (_part((16, 19, 16), (8, 8.5, 13)), _part((16, 8, 19), (7.5, 4.5, 1)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_GANGSTER_2: (_part((12, 16, 12), (6, 6.5, 11.5)), _part((16, 8, 19), (7.5, 4.5, 1)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_GANGSTER_3: (_part((14, 16, 11), (7, 7.5, 10.5)), _part((16, 8, 19), (7.5, 4.5, 1)), _COMMON_ARMS, _part((6, 9, 24), (3, 4, 0)), _part((6, 9, 24), (3, 4, 0))),
    C.CLASS_GANGSTER_4: (_part((20, 21, 13), (10, 10.5, 12)), _part((16, 8, 19), (7.5, 4.5, 1)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_GANGSTER_VIP_1: (_part((12, 15, 12), (6, 6.5, 11.5)), _part((18, 11, 26), (8.5, 6.5, 1)), _COMMON_ARMS, _part((6, 9, 24), (3, 4, 0)), _part((6, 9, 24), (3, 4, 0))),
    C.CLASS_GANGSTER_VIP_2: (_part((14, 15, 11), (7, 6.5, 10.5)), _part((16, 8, 19), (7.5, 4.5, 1)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_ENGINEER: (_part((16, 13, 12), (8, 6.5, 11.5)), _part((16, 11, 18), (8, 5.5, 0)), _COMMON_ARMS, _part((6, 11, 24), (3, 5.5, 0)), _part((6, 11, 24), (3, 5.5, 0))),
    C.CLASS_UGCBUILDER: (_part((16, 17, 12), (8, 8.5, 11.5)), _part((16, 11, 18), (8, 5.5, 0)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_SPECIALIST: (_part((14, 15, 13), (7, 8, 12.5)), _part((16, 10, 18), (8, 6.5, -1)), _COMMON_ARMS, _part((8, 11, 24), (4, 5.5, 0)), _part((8, 11, 24), (4, 5.5, 0))),
    C.CLASS_MEDIC: (_part((14, 13, 13), (7, 7, 11.5)), _part((16, 11, 17), (7, 7, -.5)), _COMMON_ARMS, _part((8, 12, 24), (4, 6, 0)), _part((8, 12, 24), (2, 6, 0))),
}
_CLASS_HITBOXES[C.CLASS_FAST_ZOMBIE] = _CLASS_HITBOXES[C.CLASS_ZOMBIE]
_CLASS_HITBOXES[C.CLASS_JUMP_ZOMBIE] = _CLASS_HITBOXES[C.CLASS_ZOMBIE]


def _build_melee_profiles():
    """Per-tool dig behavior. (damage_type, block_damage_per_hit, is_column).

    - damage_type: the client BlockManager self-expands + credits the wallet by
      this int. LIVE-MEASURED 2026-07-09 cells removed per hit: SPADE_DAMAGE(2)
      = a 3-tall column (z-1,z,z+1); PICKAXE(0)/KNIFE(1)/CROWBAR(26)/WEAPON(6)
      = exactly 1 cell. All melee types credit the digger's block wallet.
    - block_damage_per_hit: how fast blocks break (DEFAULT_BLOCK_HEALTH=5.0).
      spade 5 (1 hit), pickaxe 9 (1 hit, fast miner), knife 1 (5 hits, weak),
      crowbar 5, superspade 7.5. This is the user-visible "different damage".
    - is_column: True = classic instant 3-tall spade dig; False = single cell.
    """
    import shared.constants as C

    def T(name, default):
        return int(getattr(C, name, default))

    return {
        T("SPADE_TOOL", 2):        (T("SPADE_DAMAGE", 2),      5.0, True),
        T("CLASSIC_SPADE_TOOL", 4):(T("SPADE_DAMAGE", 2),      3.0, True),
        T("SUPERSPADE_TOOL", 3):   (T("SUPERSPADE_DAMAGE", 3), 7.5, True),
        T("PICKAXE_TOOL", 0):      (T("PICKAXE_DAMAGE", 0),    9.0, False),
        T("KNIFE_TOOL", 1):        (T("KNIFE_DAMAGE", 1),      1.0, False),
        T("CROWBAR_TOOL", 34):     (T("CROWBAR_DAMAGE", 26),   5.0, False),
        T("MACHETE_TOOL", 50):     (T("MACHETE_DAMAGE", 35),   2.0, False),
    }


MELEE_DIG_PROFILES = _build_melee_profiles()
DEFAULT_MELEE_PROFILE = (2, 5.0, True)   # spade fallback


def get_combat_system(server):
    combat = getattr(server, "combat", None)
    if combat is None:
        combat = CombatSystem(server)
        server.combat = combat
    return combat


class CombatSystem:
    def __init__(self, server):
        self.server = server
        self._pellet_groups = {}
        self._assault_bursts = {}
        self._minigun_runs = {}

    def handle_shot(self, player, packet) -> bool:
        if not player.alive or not player.spawned:
            return False
        if not (player.is_weapon_tool() or player.is_spade_tool()):
            return False
        if not self._validate_shot_packet(player, packet):
            return False

        profile = player.get_weapon_profile()
        now = time.monotonic()
        if int(player.tool) == int(getattr(C, "ASSAULT_RIFLE_TOOL", 60)):
            if not self._accept_assault_burst_packet(player, packet, now):
                return False
        elif int(player.tool) == int(getattr(C, "MINIGUN_TOOL", 8)):
            if not self._accept_minigun_packet(player, now):
                return False
        elif profile.pellet_count > 1:
            if not self._accept_pellet_packet(player, packet, profile, now):
                return False
        elif not player.consume_shot(now):
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

        if profile.pellet_count <= 1:
            return self._resolve_hitscan(player, direction, origin)
        # The stock client transmits one ShootPacket for every pellet, already
        # carrying that pellet's final spread direction. Resolve exactly this
        # ray once; never expand the trigger seed into a second server-side
        # pellet cloud.
        return self._resolve_hitscan(player, direction, origin)

    def _accept_assault_burst_packet(self, player, packet, now: float) -> bool:
        """Accept the stock three-round burst at 0.1s internal spacing."""
        loop_count = int(getattr(packet, "loop_count", 0))
        burst = self._assault_bursts.get(player.id)
        if (
            burst is not None
            and burst["count"] < ASSAULT_BURST_SIZE
            and loop_count - burst["last_loop"] >= ASSAULT_BURST_LOOP_INTERVAL
            and now - burst["started_at"] <= ASSAULT_BURST_WINDOW
        ):
            if player.ammo_clip <= 0 or player.reloading:
                return False
            player.ammo_clip -= 1
            burst["count"] += 1
            burst["last_loop"] = loop_count
            return True

        if not player.consume_shot(now):
            return False
        self._assault_bursts[player.id] = {
            "count": 1,
            "last_loop": loop_count,
            "started_at": now,
        }
        return True

    def _accept_minigun_packet(self, player, now: float) -> bool:
        """Mirror the stock 0.30s -> 0.10s active-fire cadence ramp."""
        run = self._minigun_runs.get(player.id)
        if run is None or now - run["last_packet_at"] > MINIGUN_INTERVAL_INITIAL * 2:
            run = {"started_at": now, "last_packet_at": now}
            self._minigun_runs[player.id] = run
        elapsed = max(0.0, now - run["started_at"])
        interval = max(
            MINIGUN_INTERVAL_MIN,
            MINIGUN_INTERVAL_INITIAL - MINIGUN_INTERVAL_RAMP_PER_SECOND * elapsed,
        )
        if not player.consume_shot(now, fire_interval=interval):
            return False
        run["last_packet_at"] = now
        return True

    def _accept_pellet_packet(self, player, packet, profile, now: float) -> bool:
        """Gate one stock-client pellet group while consuming one round.

        Every packet in a trigger shares loop/seed/tool and arrives as a tight
        burst. Cadence and ammunition apply to the trigger's first packet;
        subsequent distinct rays are admitted up to the recovered pellet cap.
        """
        key = (
            int(getattr(packet, "loop_count", 0)),
            int(getattr(packet, "seed", 0)) & 0xFF,
            int(player.tool),
            int(getattr(packet, "shot_on_world_update", 0)),
        )
        direction_signature = (
            float(getattr(packet, "ori_x", 0.0)),
            float(getattr(packet, "ori_y", 0.0)),
            float(getattr(packet, "ori_z", 0.0)),
        )
        group = self._pellet_groups.get(player.id)
        if group is not None and now > group["expires_at"]:
            group = None

        if group is None or group["key"] != key:
            if not player.consume_shot(now):
                return False
            self._pellet_groups[player.id] = {
                "key": key,
                "count": 1,
                "directions": {direction_signature},
                "expires_at": now + PELLET_GROUP_TIMEOUT,
            }
            return True

        if group["count"] >= profile.pellet_count:
            return False
        if direction_signature in group["directions"]:
            return False
        group["count"] += 1
        group["directions"].add(direction_signature)
        return True

    def handle_weapon_reload(self, player) -> bool:
        if not player.start_reload():
            return False
        player._broadcast_reload_state(False)
        return True

    # The CLIENT refuses to place a block that touches nothing: its gate is
    # `map.has_neighbors(x, y, z, 1)` — FACE adjacency (the 6 axis neighbours),
    # not diagonal — plus `get_max_modifiable_z() == 238`. Live-measured on the
    # real client 2026-07-10: directly above the surface -> True, any gap -> False.
    #
    # Our world_manager.can_build only checks bounds/solidity, so the server used
    # to accept FLOATING placements the client silently drops. The server then
    # held blocks no client had: builds "didn't appear", the builder lost
    # inventory for nothing, and the server carried collision where every client
    # saw air (a server-side "invisible wall"). Mirror the client's rule exactly.
    _NEIGHBOR_OFFSETS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
    MAX_MODIFIABLE_Z = 238

    def _block_supported(self, x: int, y: int, z: int, pending=()) -> bool:
        """Client-parity placement gate: the cell must FACE-touch an existing
        solid, or a cell committed earlier in this same line (the client's
        add_block loop walks the generated points in order, so a later cell may
        rest on an earlier one)."""
        if not (0 <= z <= self.MAX_MODIFIABLE_Z):
            return False
        wm = self.server.world_manager
        for dx, dy, dz in self._NEIGHBOR_OFFSETS:
            neighbor = (x + dx, y + dy, z + dz)
            if neighbor in pending or wm.get_solid(*neighbor):
                return True
        return False

    def handle_block_build(self, player, packet) -> bool:
        if not player.alive or not player.spawned or not player.is_block_tool():
            return False

        x, y, z = packet.x, packet.y, packet.z
        if not self.server.world_manager.can_build(x, y, z):
            return False
        if not self._block_supported(x, y, z):
            return False
        if not player.remove_block():
            return False

        self.server.world_manager.set_block(x, y, z, True, player.block_color)
        self._broadcast_block_mutation(player, (x, y, z), BLOCK_ACTION_BUILD)
        return True

    # Longest line the server will accept. The client regenerates the cells
    # from the echoed ENDPOINTS with its own generator, so the server must
    # never truncate/sparsify a line (server cells != client cells); overlong
    # lines are rejected whole instead.
    BLOCK_LINE_MAX_CELLS = 64

    def handle_block_line(self, player, packet) -> bool:
        """BlockLine (id 40) — how the 1.x client actually PLACES blocks (it
        never emits BlockBuild/id 32). ATOMIC: the whole line builds or none
        of it does, and the announcement to other clients is ONE sanitized
        BlockLine(40) echo.

        IDA ground truth (docs/REPLICATION_IDA_FINDINGS.md): the client's
        native remote-placement path is process_packet_block_line
        (sub_1018D690) — it regenerates the cells from the packet's endpoints
        and add_block()s each one. Our old per-cell BlockBuild(32) conversion
        reached every client but did not render (measured live 2026-07-09);
        the original server likewise echoes a line as one packet 40. Atomicity
        is what makes the single echo safe: the client rebuilds the FULL line
        from the endpoints, so the server must commit exactly those cells.
        """
        if not player.alive or not player.spawned or not player.is_block_tool():
            return False

        x1, y1, z1 = packet.x1, packet.y1, packet.z1
        x2, y2, z2 = packet.x2, packet.y2, packet.z2

        # The remote client regenerates this packet through world.cube_line,
        # whose face-connected path is different from VXL.block_line's rounded
        # max-axis interpolation. A single tap has equal endpoints -> 1 cell.
        cells = self._block_line_cells((x1, y1, z1), (x2, y2, z2))
        if not cells or len(cells) > self.BLOCK_LINE_MAX_CELLS:
            return False

        # Validate the COMPLETE line before mutating anything. Support is
        # checked PROGRESSIVELY (a cell may rest on an earlier cell of the same
        # line), mirroring the client's in-order add_block walk; if any cell is
        # unsupported the client would drop it, so the server rejects the whole
        # line rather than commit blocks no client will ever render.
        if player.blocks < len(cells):
            return False
        pending: set[tuple[int, int, int]] = set()
        for cell in cells:
            if not self.server.world_manager.can_build(*cell):
                return False
            if not self._block_supported(*cell, pending=pending):
                return False
            pending.add(cell)

        # Commit: consume the full cost, build every cell.
        player.blocks -= len(cells)
        for (x, y, z) in cells:
            self.server.world_manager.set_block(x, y, z, True, player.block_color)

        echo = BlockLine()
        echo.loop_count = self.server.loop_count
        echo.player_id = player.id
        echo.x1, echo.y1, echo.z1 = x1, y1, z1
        echo.x2, echo.y2, echo.z2 = x2, y2, z2
        self.server.broadcast(bytes(echo.generate()))
        return True

    def _block_line_cells(self, a, b):
        """Stock face-connected traversal used by remote BlockLine handling."""
        return list(cube_line(*a, *b))

    def _resolve_spade_dig(self, player, origin, direction, packet) -> bool:
        """Raycast terrain from the CLIENT's reported origin/direction and dig
        per the player's CURRENT tool (MELEE_DIG_PROFILES).

        - Spade family (is_column): the classic instant 3-tall dig — one hit
          removes the (z-1, z, z+1) column; the broadcast SPADE_DAMAGE(2) at the
          center makes the client self-expand to the same column.
        - Pickaxe / knife / crowbar (single cell): accumulate the tool's
          per-hit block damage on the one hit cell (knife 1 -> 5 hits/block,
          pickaxe 9 -> 1 hit); the block breaks when it reaches
          DEFAULT_BLOCK_HEALTH on both sides. Each tool broadcasts its OWN
          damage type so the client shows the right particles + credits the
          wallet (all melee types are block-granting, measured single-cell).
        """
        if direction is None:
            return False
        block_pos = self.server.world_manager.raycast(
            origin[0], origin[1], origin[2],
            direction[0], direction[1], direction[2],
            MELEE_RANGE,
        )
        if block_pos is None:
            return False

        dmg_type, block_dmg, is_column = MELEE_DIG_PROFILES.get(
            getattr(player, "tool", None), DEFAULT_MELEE_PROFILE)
        x, y, z = block_pos
        wm = self.server.world_manager

        if is_column:
            # Instant 3-tall column dig (one hit clears the column).
            positions = [(x, y, z - 1), (x, y, z), (x, y, z + 1)]
            destroyed = wm.destroy_blocks(positions)
            if not destroyed:
                return False
            wm.clear_block_damage(x, y, z)
            player.add_blocks(len(destroyed))
            self._broadcast_block_damage(
                player, block_pos, self._BLOCK_KILL_DAMAGE, damage_type=dmg_type)
            self._collapse_unsupported(player, destroyed)
            return True

        # Single-cell tool: accumulate damage until the block breaks.
        total, destroyed = wm.apply_block_damage(
            x, y, z, block_dmg, threshold=DEFAULT_BLOCK_HEALTH)
        if destroyed:
            player.add_blocks(1)
            self._broadcast_block_damage(
                player, block_pos, self._BLOCK_KILL_DAMAGE, damage_type=dmg_type)
            self._collapse_unsupported(player, [block_pos])
            return True
        if total > 0.0:
            # Partial crack — the client accumulates the same per-hit amount.
            self._broadcast_block_damage(
                player, block_pos, block_dmg, damage_type=dmg_type)
            return True
        return False

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

    def _apply_block_damage(self, attacker, block_pos, damage: float,
                            damage_type: int = None,
                            causer_id: int = None) -> bool:
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
            self._broadcast_block_destroy(
                attacker, [block_pos], damage_type=damage_type,
                causer_id=causer_id,
            )
        elif total > 0.0:
            self._broadcast_block_damage(
                attacker, block_pos, damage, damage_type=damage_type,
                causer_id=causer_id,
            )
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
                                damage_type: int = None, seed: int = 0,
                                causer_id: int = None):
        from shared.packet import Damage
        packet = Damage()
        packet.player_id = player.id if player is not None else -1
        packet.type = self._damage_type_for(player) if damage_type is None else int(damage_type)
        packet.damage = min(float(damage), self._BLOCK_KILL_DAMAGE)
        packet.face = 0
        # Checked removal makes the stock client run its native 18-neighbor
        # collapse and falling-block animation. The server mirrors the exact
        # same topology/work-budget rules in find_unsupported_chunks, so both
        # authorities remove the same component without per-fallen-cell floods.
        packet.chunk_check = 1
        packet.seed = int(seed) & 0xFF
        # causer_id is an ENTITY id the client reads UNSIGNED (measured: -1
        # decodes to 65535 -> entities[65535] lookup aborts the whole damage
        # handler). The reference server sends the shooter's id here; use a
        # small in-range id (0 for no player) so the client's entity lookup
        # resolves safely instead of exploding.
        packet.causer_id = (
            int(causer_id) if causer_id is not None
            else (player.id if player is not None else 0)
        )
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

    def _broadcast_block_destroy(self, player, positions, damage_type: int = None,
                                 causer_id: int = None):
        """Guaranteed removal on all clients: kill-damage Damage(37) per cell
        (a no-op for cells the client already removed on its own)."""
        for pos in positions:
            self._broadcast_block_damage(
                player, pos, self._BLOCK_KILL_DAMAGE,
                damage_type=damage_type, causer_id=causer_id,
            )
        self._collapse_unsupported(player, positions)

    def _collapse_unsupported(self, player, removed_positions):
        """Floating-structure collapse: any solid chunk left disconnected from
        the base plane by these removals falls too (cascading until stable).
        The classic AoS behavior — without it a streetlamp whose base is dug
        out levitates forever."""
        wm = self.server.world_manager
        chunks = wm.find_unsupported_chunks(list(removed_positions))
        for chunk in chunks:
            # The triggering Damage(chunk_check=1) makes every client remove
            # and animate this whole component natively. Mirror it server-side
            # only; broadcasting one reliable Damage per falling voxel causes
            # effect noise, channel floods, and duplicate collapse work.
            wm.destroy_blocks(chunk)

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
        sanitized.affect_shooter = getattr(packet, "affect_shooter", 0)
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
        """Match stock hitscan_player against oriented KV6 model bounds."""
        profile = _CLASS_HITBOXES.get(target.class_id, _CLASS_HITBOXES[C.CLASS_SOLDIER])
        if target.input.crouch:
            parts = (
                (C.PART_TORSO, _CROUCH_TORSO, (0.0, 0.0, 0.3)),
                (C.PART_HEAD, profile[C.PART_HEAD], (0.0, 0.0, 0.3)),
                (C.PART_ARMS, profile[C.PART_ARMS], (0.0, 0.0, C.BODY_PART_ARMS_CROUCH_Z)),
                (C.PART_LEFT_LEG, _CROUCH_LEG, (0.25, C.BODY_PART_LEG_CROUCH_Y, 0.7)),
                (C.PART_RIGHT_LEG, _CROUCH_LEG, (-0.25, C.BODY_PART_LEG_CROUCH_Y, 0.7)),
            )
        else:
            parts = (
                (C.PART_TORSO, profile[C.PART_TORSO], (0.0, 0.0, 0.3)),
                (C.PART_HEAD, profile[C.PART_HEAD], (0.0, 0.0, 0.3)),
                (C.PART_ARMS, profile[C.PART_ARMS], (0.0, 0.0, 0.5)),
                (C.PART_LEFT_LEG, profile[C.PART_LEFT_LEG], (0.25, 0.0, 1.1)),
                (C.PART_RIGHT_LEG, profile[C.PART_RIGHT_LEG], (-0.25, 0.0, 1.1)),
            )

        for part_id, model_bounds, model_offset in parts:
            hit = self._ray_hits_model_bounds(
                origin, direction, max_distance, target, model_offset, model_bounds)
            if hit is not None:
                distance, position = hit
                return distance, position, part_id == C.PART_HEAD
        return None

    def _ray_hits_model_bounds(
        self, origin, direction, max_distance, target, model_offset, model_bounds
    ):
        """Ray/slab intersection using the stock hitscan_model transform."""
        yaw = math.atan2(target.orientation[0], target.orientation[1])
        cosine = math.cos(yaw)
        sine = math.sin(yaw)
        model_x, model_y, model_z = model_offset
        model_position = (
            target.x - model_x * cosine + model_y * sine,
            target.y + model_x * sine - model_y * cosine,
            target.z + model_z,
        )
        axes = (
            (-cosine * HITBOX_SCALE, sine * HITBOX_SCALE, 0.0),
            (sine * HITBOX_SCALE, cosine * HITBOX_SCALE, 0.0),
            (0.0, 0.0, HITBOX_SCALE),
        )
        sizes, pivots = model_bounds
        enter = 0.0
        leave = max_distance
        relative = (
            origin[0] - model_position[0],
            origin[1] - model_position[1],
            origin[2] - model_position[2],
        )

        for axis, size, pivot in zip(axes, sizes, pivots):
            axis_length_sq = sum(component * component for component in axis)
            coordinate = sum(relative[i] * axis[i] for i in range(3)) / axis_length_sq + pivot
            rate = sum(direction[i] * axis[i] for i in range(3)) / axis_length_sq
            if abs(rate) < 1e-9:
                if coordinate < 0.0 or coordinate > size:
                    return None
                continue
            first = -coordinate / rate
            second = (size - coordinate) / rate
            if first > second:
                first, second = second, first
            enter = max(enter, first)
            leave = min(leave, second)
            if enter > leave:
                return None

        position = tuple(origin[i] + direction[i] * enter for i in range(3))
        return enter, position

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
