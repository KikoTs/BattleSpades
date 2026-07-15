"""
Authoritative combat and block-damage helpers.
"""

from __future__ import annotations

import logging
import math
import random
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
from shared.packet import (
    BlockBuild,
    BlockBuildColored,
    BlockLine,
    HitEntity,
    ShootFeedbackPacket,
    ShootPacket,
    ShootResponse,
)
from server.world_mutations import PendingWorldMutation

logger = logging.getLogger(__name__)

SHOT_ORIGIN_TOLERANCE = 8.0
SHOT_ORIENTATION_DOT_TOLERANCE = 0.25
HITBOX_SCALE = 0.05
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


DIG_SINGLE = "single"
DIG_COLUMN = "column"
DIG_CUBE = "cube"
DIG_MACHETE = "machete_vertical_pair"


def _build_melee_profiles():
    """Per-tool dig behavior. (damage_type, block_damage_per_hit, pattern).

    - damage_type: the client BlockManager self-expands + credits the wallet by
      this int. LIVE-MEASURED 2026-07-09 cells removed per hit: SPADE_DAMAGE(2)
      = a 3-tall column (z-1,z,z+1); PICKAXE(0)/KNIFE(1)/CROWBAR(26)/WEAPON(6)
      = exactly 1 cell. All melee types credit the digger's block wallet.
    - block_damage_per_hit: how fast blocks break (DEFAULT_BLOCK_HEALTH=5.0).
      spade 5 (1 hit), pickaxe 9 (1 hit, fast miner), knife 1 (5 hits, weak),
      crowbar 5, superspade 7.5. This is the user-visible "different damage".
    - pattern: pickaxes/knives damage one cell, ordinary spades remove the
      centered z column, the Machete damages (z,z+1), and the Miner Super
      Spade removes a centered 3x3x3 cube. The latter is
      orientation-independent: the retail handler
      subtracts one from all three hit coordinates and passes extent 3 to its
      block-distance helper.
    """
    import shared.constants as C

    def T(name, default):
        return int(getattr(C, name, default))

    return {
        T("SPADE_TOOL", 2):        (T("SPADE_DAMAGE", 2),      5.0, DIG_COLUMN),
        T("CLASSIC_SPADE_TOOL", 4):(T("SPADE_DAMAGE", 2),      3.0, DIG_COLUMN),
        T("SUPERSPADE_TOOL", 3):   (T("SUPERSPADE_DAMAGE", 3), 7.5, DIG_CUBE),
        # IDA: BlockManager.handle_zombie_damage at gameScene.pyd
        # 0x10081340 is structurally identical to handle_superspade_damage at
        # 0x10082C90: both subtract one from x/y/z and invoke the native 3x3x3
        # area handler.  Zombie hands differ only in damage type/amount.
        T("ZOMBIEHAND_TOOL", 24):   (
            T("ZOMBIE_DAMAGE", 17),
            float(getattr(C, "ZOMBIEHAND_DAMAGE_AMOUNT", 2.0)),
            DIG_CUBE,
        ),
        T("PICKAXE_TOOL", 0):      (T("PICKAXE_DAMAGE", 0),    9.0, DIG_SINGLE),
        T("KNIFE_TOOL", 1):        (T("KNIFE_DAMAGE", 1),      1.0, DIG_SINGLE),
        T("CROWBAR_TOOL", 34):     (T("CROWBAR_DAMAGE", 26),   5.0, DIG_SINGLE),
        # Native BlockManager.handle_machete_damage applies this one packet to
        # the hit voxel and the next voxel in VXL z (z and z+1).
        T("MACHETE_TOOL", 50):     (T("MACHETE_DAMAGE", 35),   2.0, DIG_MACHETE),
        T("UGC_PICKAXE_TOOL", 44): (T("UGC_PICKAXE_DAMAGE", 28), 9.0, DIG_SINGLE),
        T("UGC_SUPERSPADE_TOOL", 45): (
            T("UGC_SUPERSPADE_DAMAGE", 29), 7.5, DIG_CUBE
        ),
    }


MELEE_DIG_PROFILES = _build_melee_profiles()
DEFAULT_MELEE_PROFILE = (2, 5.0, DIG_COLUMN)   # spade fallback

# PlaySound(23) is needed for remote observers. The acting retail client plays
# its own melee impact immediately, but Damage(37) alone does not consistently
# produce the matching sample for another player (and bots have no local
# client at all). Values are the live constants_audio SOUND_MAP ids.
_BLOCK_HIT_SOUND_BY_DAMAGE = {
    int(getattr(C, "PICKAXE_DAMAGE", 0)): 36,
    int(getattr(C, "KNIFE_DAMAGE", 1)): 35,
    int(getattr(C, "SPADE_DAMAGE", 2)): 33,
    int(getattr(C, "SUPERSPADE_DAMAGE", 3)): 37,
    int(getattr(C, "CLASSIC_SPADE_DAMAGE", 4)): 33,
    int(getattr(C, "ZOMBIE_DAMAGE", 17)): 38,
    int(getattr(C, "CROWBAR_DAMAGE", 26)): 34,
    int(getattr(C, "UGC_PICKAXE_DAMAGE", 28)): 36,
    int(getattr(C, "UGC_SUPERSPADE_DAMAGE", 29)): 37,
    # Machete has no dedicated entry in this client's 61-id SOUND_MAP. Knife
    # is the closest safe stock cue; arbitrary sound filenames cannot travel
    # in PlaySound(23).
    int(getattr(C, "MACHETE_DAMAGE", 35)): 35,
}


def _melee_dig_positions(block_pos, pattern):
    """Return the exact retail voxel footprint centered on ``block_pos``."""
    x, y, z = (int(value) for value in block_pos)
    if pattern == DIG_CUBE:
        return [
            (x + dx, y + dy, z + dz)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for dz in (-1, 0, 1)
        ]
    if pattern == DIG_COLUMN:
        return [(x, y, z - 1), (x, y, z), (x, y, z + 1)]
    if pattern == DIG_MACHETE:
        return [(x, y, z), (x, y, z + 1)]
    return [(x, y, z)]


def get_combat_system(server):
    combat = getattr(server, "combat", None)
    if combat is None:
        combat = CombatSystem(server)
        server.combat = combat
    return combat


class CombatSystem:
    def __init__(self, server):
        self.server = server
        self._pellet_spread = {}
        self._assault_bursts = {}
        self._minigun_runs = {}

    def forget_player(self, player_id: int) -> None:
        """Discard cadence/group state before a wire player id is reused."""

        player_id = int(player_id)
        self._pellet_spread.pop(player_id, None)
        self._assault_bursts.pop(player_id, None)
        self._minigun_runs.pop(player_id, None)

    def _queue_canonical_terrain_repair(self, positions) -> None:
        """Schedule bounded repair for a client-predicted edit footprint.

        Successful mutations already have one reliable gameplay packet and
        must never be enrolled here. Recording only rejected/cancelled
        footprints corrects local prediction without replaying native visual
        callbacks for accepted block placement.
        """

        repair = getattr(self.server, "terrain_repair", None)
        if repair is not None:
            repair.record_cells(positions)

    def _cancel_reserved_block_build(self, player, positions) -> None:
        """Refund a deferred build and repair only after it is cancelled.

        A prediction repair must not race a still-pending world mutation. The
        old pre-queue could reassert air at tick 120 and then let the valid
        build commit at tick 180, producing an avoidable air/solid topology
        flip while the owner was moving.
        """

        positions = tuple(positions)
        player.add_blocks(len(positions))
        self._queue_canonical_terrain_repair(positions)

    def handle_shot(self, player, packet) -> bool:
        if not player.alive or not player.spawned:
            return False
        from server.game_rules import get_rules
        if not get_rules(self.server.config).is_tool_enabled(
            int(getattr(player, "tool", -1))
        ):
            return False
        if bool(getattr(player, "pickup_burdensome", False)) and not bool(
            getattr(getattr(self.server, "mode", None), "shoot_with_intel", False)
        ):
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
        elif (
            int(player.tool) == int(getattr(C, "MG_TOOL", 15))
            and bool(getattr(player.input, "is_weapon_deployed", False))
        ):
            if not player.consume_shot(
                now,
                fire_interval=float(getattr(C, "MG_DEPLOYED_SHOOT_INTERVAL", 0.1)),
            ):
                return False
        elif not player.consume_shot(now):
            return False

        if not player.is_spade_tool():
            feedback = self._build_shoot_feedback_packet(player, packet)
            # Packet 6 is the client -> server request. Retail clients
            # reproduce another firearm's shot (sound, muzzle flash and
            # tracer) from packet 8. The firing retail client already
            # predicted it locally, so feedback excludes that owner.
            #
            # Never send packet 8 for digging/melee tools: its native handler
            # calls Character.shoot(), while SpadeTool/MacheteTool implement
            # use_primary() and have no shoot() method. Their remote swing and
            # sound are driven by WorldUpdate primary-action bit 0x01 instead.
            self.server.broadcast(bytes(feedback.generate()), exclude=player)

        stimuli = getattr(self.server, "bot_stimuli", None)
        if stimuli is not None:
            from server.bot_ai.messages import StimulusKind

            stimuli.publish(
                StimulusKind.SHOT,
                (float(packet.x), float(packet.y), float(packet.z)),
                source_id=int(player.id),
                team=int(player.team),
                radius=72.0,
                lifetime=1.25,
            )

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
            dug_terrain = self._resolve_spade_dig(
                player, origin, direction, packet
            )
            hit_player = self._resolve_melee_hit(player, origin, direction)
            return dug_terrain or hit_player

        if profile.pellet_count <= 1:
            return self._resolve_hitscan(player, direction, origin)

        # Character.shoot sends ONE central ShootPacket for the trigger.  It
        # seeds Python's RNG from packet.seed and expands all pellets locally;
        # observers repeat that expansion from the relayed packet.  Resolve
        # the same cloud here so authoritative damage is not a rifle-like
        # single ray while both clients render a shotgun blast.
        hit_any = False
        for pellet_direction in self._seeded_pellet_directions(
            player, direction, profile, packet, now
        ):
            if self._resolve_hitscan(player, pellet_direction, origin):
                hit_any = True
        return hit_any

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

    def _seeded_pellet_directions(
        self, player, direction, profile, packet, now: float
    ):
        """Reproduce retail ``Character.shoot`` pellet expansion.

        IDA recovery of ``character.pyd:sub_10049DB0`` shows three RNG draws
        per pellet.  Hip fire adds ``(random()*4-2)*accuracy`` to each axis;
        zoom adds ``(random()*2-1)*accuracy``.  Stock shotguns share the same
        variable-accuracy curve: range 3, +0.5 per shot, -1.0 per second.
        The compact level below tracks that curve without coupling combat to
        client render objects.
        """
        state = self._pellet_spread.get(player.id)
        tool = int(player.tool)
        if state is None or state["tool"] != tool:
            level = 0.0
        else:
            elapsed = max(0.0, now - state["last_at"])
            level = max(0.0, float(state["level"]) - elapsed / 3.0)

        # accuracy_max - accuracy_min equals accuracy_min for every stock
        # shotgun in this build, so level 0..1 maps directly to min..max.
        accuracy = float(profile.spread) * (1.0 + level)
        self._pellet_spread[player.id] = {
            "tool": tool,
            "level": min(1.0, level + (0.5 / 3.0)),
            "last_at": now,
        }

        zoomed = bool(getattr(getattr(player, "input", None), "zoom", False))
        scale = 2.0 if zoomed else 4.0
        center = 1.0 if zoomed else 2.0
        rng = random.Random(int(getattr(packet, "seed", 0)) & 0xFF)
        pellets = []
        for _ in range(int(profile.pellet_count)):
            pellet = self._normalize((
                direction[0] + (rng.random() * scale - center) * accuracy,
                direction[1] + (rng.random() * scale - center) * accuracy,
                direction[2] + (rng.random() * scale - center) * accuracy,
            ))
            if pellet is not None:
                pellets.append(pellet)
        return pellets

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

    @staticmethod
    def _ordinary_block_tool_selected(player) -> bool:
        """Return whether the retail normal block tool (id 5) is held.

        Normal and flare blocks share palette/UI behavior, so
        ``Player.is_block_tool()`` intentionally recognizes both ids.  Their
        action packets are not interchangeable, however: tool 5 sends
        BlockBuild/BlockLine while flare tool 22 sends PlaceFlareBlock(104).
        Keep the action gate exact so a delayed or forged packet cannot spend
        the wrong inventory amount or create the wrong world representation.
        """

        return (
            bool(getattr(player, "tool_is_raw", False))
            and int(getattr(player, "tool", -1)) == int(C.BLOCK_TOOL)
        )

    def handle_block_build(self, player, packet) -> bool:
        from server.game_rules import get_rules
        if (
            not player.alive
            or not player.spawned
            or not self._ordinary_block_tool_selected(player)
            or not get_rules(self.server.config).enabled("RULE_ENABLE_BLOCKS")
        ):
            return False

        x, y, z = packet.x, packet.y, packet.z
        position = (int(x), int(y), int(z))
        if not self.server.world_manager.can_build(x, y, z):
            self._queue_canonical_terrain_repair((position,))
            return False
        if not self._block_supported(x, y, z):
            self._queue_canonical_terrain_repair((position,))
            return False
        if not player.remove_block():
            self._queue_canonical_terrain_repair((position,))
            return False

        action_loop = max(0, int(packet.loop_count))
        color = int(player.block_color) & 0xFFFFFF
        service = getattr(self.server, "world_mutations", None)
        if service is not None:
            mutation = PendingWorldMutation(
                owner_id=int(player.id),
                action_loop=action_loop,
                enqueued_tick=int(self.server.loop_count),
                kind="block_build",
                cell_count=1,
                apply=lambda: self._commit_block_build(
                    player, action_loop, position, color
                ),
                cancel=lambda: self._cancel_reserved_block_build(
                    player, (position,)
                ),
            )
            return bool(service.enqueue(mutation))

        self._commit_block_build(player, action_loop, position, color)
        return True

    def _commit_block_build(self, player, action_loop, position, color) -> None:
        """Commit a reserved single-block build after its movement frame."""

        x, y, z = position
        wm = self.server.world_manager
        # Another ready mutation can win the cell before this one. Never charge
        # inventory for a build the authoritative map did not accept.
        if not wm.can_build(x, y, z) or not self._block_supported(x, y, z):
            player.add_blocks(1)
            self._queue_canonical_terrain_repair((position,))
            return
        if not wm.set_block(x, y, z, True, color):
            player.add_blocks(1)
            self._queue_canonical_terrain_repair((position,))
            return
        self._broadcast_block_mutation(
            player, position, BLOCK_ACTION_BUILD, loop_count=action_loop
        )

    # Longest line the server will accept. The client regenerates the cells
    # from the echoed ENDPOINTS with its own generator, so the server must
    # never truncate/sparsify a line (server cells != client cells); overlong
    # lines are rejected whole instead.
    BLOCK_LINE_MAX_CELLS = 64

    def handle_block_line(self, player, packet) -> bool:
        """BlockLine (id 40) — how the 1.x client actually PLACES blocks (it
        never emits BlockBuild/id 32). ATOMIC: the whole line builds or none
        of it does. Accepted cells are announced with explicit RGB so remote
        rendering cannot depend on mutable character palette state.

        IDA ground truth (docs/REPLICATION_IDA_FINDINGS.md): the client's
        native remote-placement path is process_packet_block_line
        (sub_1018D690) — it regenerates the cells from the packet's endpoints
        and add_block()s each one. Plain BlockBuild(32) did not render remotely
        in live testing, while BlockBuildColored(33) is the proven prefab path.
        Explicit colored cells also avoid server SetColor packets changing the
        local player's held-block selection.
        """
        from server.game_rules import get_rules
        if (
            not player.alive
            or not player.spawned
            or not self._ordinary_block_tool_selected(player)
            or not get_rules(self.server.config).enabled("RULE_ENABLE_BLOCKS")
        ):
            return False

        x1, y1, z1 = packet.x1, packet.y1, packet.z1
        x2, y2, z2 = packet.x2, packet.y2, packet.z2

        # The remote client regenerates this packet through world.cube_line,
        # whose face-connected path is different from VXL.block_line's rounded
        # max-axis interpolation. A single tap has equal endpoints -> 1 cell.
        cells = self.block_line_cells((x1, y1, z1), (x2, y2, z2))
        if not cells or len(cells) > self.BLOCK_LINE_MAX_CELLS:
            return False
        # The stock client removes already-solid cells from its preview/cost,
        # but sends only the original endpoints. Filter them identically;
        # rejecting the whole line loses valid player placements whenever a
        # drag crosses terrain or another just-built voxel.
        build_cells = [cell for cell in cells if not self.server.world_manager.get_solid(*cell)]
        if not build_cells or player.blocks < len(build_cells):
            if build_cells:
                self._queue_canonical_terrain_repair(build_cells)
            return False
        pending: set[tuple[int, int, int]] = set()
        for cell in build_cells:
            if not self.server.world_manager.can_build(*cell):
                self._queue_canonical_terrain_repair(build_cells)
                return False
            if not self._block_supported(*cell, pending=pending):
                self._queue_canonical_terrain_repair(build_cells)
                return False
            pending.add(cell)

        # Reserve inventory now, but do not mutate collision geometry during
        # packet draining.  The retail client recorded movement through
        # packet.loop_count before its echoed BlockLine can commit the ghost
        # voxels.  Production therefore commits after authoritative physics
        # consumes that same loop; otherwise build -> run/jump replays the old
        # movement frame against a newer map and visibly rolls the player back.
        cost = len(build_cells)
        player.blocks -= cost
        action_loop = max(0, int(packet.loop_count))
        cells_snapshot = tuple(build_cells)
        endpoints = (x1, y1, z1, x2, y2, z2)
        color = int(player.block_color) & 0xFFFFFF
        service = getattr(self.server, "world_mutations", None)
        if service is not None:
            mutation = PendingWorldMutation(
                owner_id=int(player.id),
                action_loop=action_loop,
                enqueued_tick=int(self.server.loop_count),
                kind="block_line",
                cell_count=cost,
                apply=lambda: self._commit_block_line(
                    player,
                    action_loop,
                    endpoints,
                    cells_snapshot,
                    color,
                ),
                cancel=lambda: self._cancel_reserved_block_build(
                    player, cells_snapshot
                ),
            )
            return bool(service.enqueue(mutation))

        # Compatibility path for focused domain tests and embedders that do
        # not construct the production service composition root.
        self._commit_block_line(
            player,
            action_loop,
            endpoints,
            cells_snapshot,
            color,
        )
        return True

    def _commit_block_line(
        self,
        player,
        action_loop: int,
        endpoints: tuple[int, int, int, int, int, int],
        build_cells: tuple[tuple[int, int, int], ...],
        color: int,
    ) -> None:
        """Commit one validated BlockLine on the post-physics tick boundary."""

        failed_cells = []
        for x, y, z in build_cells:
            if not self.server.world_manager.set_block(x, y, z, True, color):
                failed_cells.append((x, y, z))
        if failed_cells:
            player.add_blocks(len(failed_cells))
            self._queue_canonical_terrain_repair(failed_cells)

        # The builder does NOT commit its ghost blocks locally; it needs the
        # native BlockLine echo to finalize the drag and wallet update. Other
        # clients get explicit-color cells so their rendering is independent
        # of mutable palette state.  Preserve the originating action loop: it
        # is the only timeline label known to exist in the retail history.
        x1, y1, z1, x2, y2, z2 = endpoints
        own_echo = BlockLine()
        own_echo.loop_count = action_loop
        own_echo.player_id = player.id
        own_echo.x1, own_echo.y1, own_echo.z1 = x1, y1, z1
        own_echo.x2, own_echo.y2, own_echo.z2 = x2, y2, z2
        player.send(bytes(own_echo.generate()), reliable=True)

        for x, y, z in build_cells:
            echo = BlockBuildColored()
            echo.loop_count = action_loop
            echo.player_id = player.id
            echo.x, echo.y, echo.z = x, y, z
            echo.color = color
            self.server.broadcast(bytes(echo.generate()), exclude=player)

    def block_line_cells(self, a, b):
        """Return the stock face-connected cells for public action validation."""

        return list(cube_line(*a, *b))

    def _block_line_cells(self, a, b):
        """Compatibility alias retained for reverse-engineering regressions."""

        return self.block_line_cells(a, b)

    def _resolve_spade_dig(self, player, origin, direction, packet) -> bool:
        """Raycast terrain from the CLIENT's reported origin/direction and dig
        per the player's CURRENT tool (MELEE_DIG_PROFILES).

        - Ordinary spades remove the classic (z-1, z, z+1) column. The Miner
          and UGC Super Spades remove the retail centered 3x3x3 cube. One
          matching area-damage packet makes each client self-expand once.
        - Pickaxe / knife / crowbar (single cell) and Machete (z,z+1):
          accumulate the tool's per-hit block damage (knife 1 -> 5
          hits/block, Machete 2 -> 3 hits, pickaxe 9 -> 1 hit); each block
          breaks when it reaches
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

        dmg_type, block_dmg, pattern = MELEE_DIG_PROFILES.get(
            getattr(player, "tool", None), DEFAULT_MELEE_PROFILE)
        x, y, z = block_pos
        wm = self.server.world_manager
        positions = _melee_dig_positions(block_pos, pattern)
        if block_dmg <= 0.0:
            # Non-digging melee (Riot tools) can still produce a
            # client-side predicted crack.  Reassert the canonical voxel, but
            # never mutate or credit inventory for that visual prediction.
            self._queue_canonical_terrain_repair(positions)
            return False
        if pattern == DIG_MACHETE:
            return self._apply_accumulating_melee_footprint(
                player,
                block_pos,
                positions,
                damage_type=dmg_type,
                block_damage=block_dmg,
            )
        if pattern != DIG_SINGLE:
            # Commit the full footprint in one map operation. One matching
            # area-damage packet then makes every native client expand once;
            # sending per-cell Super Spade packets would expand each cell.
            destroyed = wm.destroy_blocks(positions)
            if not destroyed:
                self._queue_canonical_terrain_repair(positions)
                return False
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

    def _apply_accumulating_melee_footprint(
        self,
        player,
        block_pos,
        positions,
        *,
        damage_type: int,
        block_damage: float,
    ) -> bool:
        """Apply one native self-expanding melee packet to canonical cells.

        Retail's Machete handler expands one type-35 Damage packet to ``z``
        and ``z+1`` and applies 2 damage to each. Sending a packet per cell
        would expand twice and touch neighboring voxels the server never hit.
        This method runs on the gameplay thread after shot validation.
        """
        wm = self.server.world_manager
        affected = False
        destroyed_positions = []
        for position in positions:
            total, destroyed = wm.apply_block_damage(
                *position,
                block_damage,
                threshold=DEFAULT_BLOCK_HEALTH,
            )
            if total > 0.0 or destroyed:
                affected = True
            if destroyed:
                destroyed_positions.append(position)

        if not affected:
            self._queue_canonical_terrain_repair(positions)
            return False

        player.add_blocks(len(destroyed_positions))
        # Broadcast the real per-hit amount even on the strike that crosses
        # the threshold. Every client maintains the same 2+2+2 ledger.
        self._broadcast_block_damage(
            player,
            block_pos,
            block_damage,
            damage_type=damage_type,
        )
        if destroyed_positions:
            self._collapse_unsupported(player, destroyed_positions)
        return True

    def handle_block_destroy(self, player, packet) -> bool:
        if not player.alive or not player.spawned:
            return False
        from server.game_rules import get_rules
        if not get_rules(self.server.config).is_tool_enabled(
            int(getattr(player, "tool", -1))
        ):
            return False

        if player.is_block_tool():
            position = (int(packet.x), int(packet.y), int(packet.z))
            if not self.server.world_manager.get_solid(*position):
                return False
            service = getattr(self.server, "world_mutations", None)
            if service is not None:
                mutation = PendingWorldMutation(
                    owner_id=int(player.id),
                    action_loop=max(0, int(packet.loop_count)),
                    enqueued_tick=int(self.server.loop_count),
                    kind="block_destroy",
                    cell_count=1,
                    apply=lambda: self._commit_block_tool_destroy(
                        player, position
                    ),
                    cancel=lambda: self._queue_canonical_terrain_repair(
                        (position,)
                    ),
                )
                return bool(service.enqueue(mutation))
            self._commit_block_tool_destroy(player, position)
            return True

        if player.is_spade_tool():
            damage_type, block_damage, pattern = MELEE_DIG_PROFILES.get(
                getattr(player, "tool", None), DEFAULT_MELEE_PROFILE
            )
            positions = _melee_dig_positions(
                (packet.x, packet.y, packet.z), pattern
            )
            if pattern == DIG_MACHETE:
                # Retail MacheteTool uses ShootPacket. Accepting legacy
                # BlockLiberate as well would apply one swing twice; repair a
                # forged/old predicted liberation instead of instant-killing.
                self._queue_canonical_terrain_repair(positions)
                return False
            if block_damage <= 0.0:
                self._queue_canonical_terrain_repair(positions)
                return False
            now = time.monotonic()
            if not player.consume_shot(now):
                self._queue_canonical_terrain_repair(positions)
                return False
            destroyed = self.server.world_manager.destroy_blocks(positions)
            if not destroyed:
                self._queue_canonical_terrain_repair(positions)
                return False
            player.add_blocks(len(destroyed))
            self._broadcast_block_damage(
                player,
                (packet.x, packet.y, packet.z),
                self._BLOCK_KILL_DAMAGE,
                damage_type=damage_type,
            )
            self._collapse_unsupported(player, destroyed)
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

        target, _, _, position = hit
        damage = self._calculate_damage(attacker, attacker.get_weapon_profile(), headshot=False)
        damage = self._apply_riot_shield_mitigation(target, attacker, damage)
        if int(getattr(attacker, "tool", -1)) == int(C.RIOTSHIELD_TOOL):
            self._apply_riot_shield_knockback(attacker, target)
        health_before = target.health
        target.damage(damage, source=attacker, kill_type=attacker.get_weapon_profile().kill_type)
        if target.health < health_before:
            self._broadcast_player_hit_feedback(attacker, position)
        return True

    def _resolve_hitscan(self, attacker, direction, origin=None) -> bool:
        if origin is None:
            origin = attacker.eye
        hit = self._trace_authoritative_hit(
            attacker, origin, direction, attacker.get_weapon_profile().max_range
        )
        if hit is None:
            return False

        kind, target, headshot, position = hit
        if kind == "player":
            damage = self._calculate_damage(attacker, attacker.get_weapon_profile(), headshot=headshot)
            damage = self._apply_riot_shield_mitigation(target, attacker, damage)
            kill_type = KILL_HEADSHOT if headshot else attacker.get_weapon_profile().kill_type
            health_before = target.health
            target.damage(damage, source=attacker, kill_type=kill_type)
            if target.health < health_before:
                self._broadcast_player_hit_feedback(attacker, position)
            return True

        if kind == "entity":
            self._broadcast_entity_hit(target, position)
            damage = self._calculate_damage(
                attacker, attacker.get_weapon_profile(), headshot=False
            )
            self.server.entity_registry.damage_entity(
                target.entity_id, damage, attacker, self.server._build_entity_ctx()
            )
            return True

        if kind == "block":
            return self._apply_block_damage(
                attacker, target, attacker.get_weapon_profile().block_damage
            )
        return False

    def _commit_block_tool_destroy(self, player, position) -> None:
        """Commit one block-tool removal at the post-physics boundary."""

        destroyed = self.server.world_manager.destroy_blocks([position])
        if not destroyed:
            self._queue_canonical_terrain_repair((position,))
            return
        player.add_blocks(len(destroyed))
        self._broadcast_block_destroy(player, destroyed)
        from server.audio import SND_DIG_HIT_BLOCK, play_sound
        play_sound(
            self.server,
            SND_DIG_HIT_BLOCK,
            position=position,
            exclude=player,
        )

    def _broadcast_entity_hit(self, entity, position) -> None:
        """Drive the compiled client's per-entity bullet impact path.

        Native ``process_packet_hit_entity`` resolves ``scene.entities[id]``
        and invokes the entity hit callback with this position/type.  It is a
        server-to-client effect packet; health remains server-only.
        """
        packet = HitEntity()
        packet.entity_id = int(entity.entity_id)
        packet.x, packet.y, packet.z = (float(value) for value in position)
        packet.type = int(getattr(C, "PART_ENTITY1", 7))
        self.server.broadcast(bytes(packet.generate()))

    def _broadcast_player_hit_feedback(self, attacker, position) -> None:
        """Publish the retail client's blood and shooter hit-confirm event.

        Native ``process_packet_shoot_response`` draws blood for every
        recipient, but plays the hit sound and changes the crosshair only when
        ``damage_by`` equals the local player id.  Broadcast this only after
        authoritative health decreases so protected or mode-rejected hits do
        not produce false confirmations.
        """

        packet = ShootResponse()
        packet.damage_by = int(attacker.id)
        packet.damaged = 1
        packet.blood = 1
        packet.position_x, packet.position_y, packet.position_z = (
            float(value) for value in position
        )
        self.server.broadcast(bytes(packet.generate()))

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

    def _build_block_damage_packet(self, player, block_pos, damage: float,
                                   damage_type: int = None, seed: int = 0,
                                   causer_id: int = None):
        """Build one exact or native-expanding terrain-damage packet."""

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
        return packet

    def _broadcast_block_damage(self, player, block_pos, damage: float,
                                damage_type: int = None, seed: int = 0,
                                causer_id: int = None):
        packet = self._build_block_damage_packet(
            player,
            block_pos,
            damage,
            damage_type=damage_type,
            seed=seed,
            causer_id=causer_id,
        )
        self.server.broadcast(bytes(packet.generate()))
        sound_id = _BLOCK_HIT_SOUND_BY_DAMAGE.get(int(packet.type))
        if sound_id is not None:
            from server.audio import play_sound
            play_sound(
                self.server,
                sound_id,
                position=block_pos,
                exclude=player,
            )

    def record_exact_block_destroy_catchup(self, player, positions,
                                           causer_id: int = None) -> None:
        """Journal crash-safe exact removals without flooding live clients.

        Native Drill damage is compact because one packet expands to an
        81-cell footprint, but it requires a live projectile entity.  A join
        catch-up can run after that entity has expired, so its journal must
        contain stable type-6 cells instead.  This method runs on the gameplay
        thread immediately after the canonical VXL mutation.
        """

        for position in positions:
            packet = self._build_block_damage_packet(
                player,
                position,
                self._BLOCK_KILL_DAMAGE,
                damage_type=int(C.WEAPON_DAMAGE),
                causer_id=causer_id,
            )
            self.server._record_map_mutation(bytes(packet.generate()))

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

    def _broadcast_block_mutation(
        self, player, position, block_type: int, loop_count: int = None
    ):
        """BUILD announcements only — BlockBuild(32) is add-only on the wire;
        destroys route through _broadcast_block_destroy (Damage 37)."""
        if block_type != BLOCK_ACTION_BUILD:
            self._broadcast_block_destroy(player, [position])
            return
        packet = BlockBuild()
        packet.loop_count = (
            self.server.loop_count if loop_count is None else int(loop_count)
        )
        packet.player_id = player.id
        packet.x = position[0]
        packet.y = position[1]
        packet.z = position[2]
        # Material selector on this client: 0=prefab (normal build).
        packet.block_type = 0
        self.server.broadcast(bytes(packet.generate()))

    def _build_shoot_feedback_packet(self, player, packet) -> ShootFeedbackPacket:
        """Build the native server-to-client remote weapon-action event.

        ``GameScene.process_packet_shoot_feedback`` looks up ``shooter_id``,
        verifies that the visible character still has ``tool_id`` equipped,
        then calls ``character.shoot(seed)``. That client call owns firearm
        audio/muzzle effects. It is crash-unsafe for digging tools, which have
        ``use_primary`` but no ``shoot`` method and replicate through the
        WorldUpdate primary-action bit instead. Packet 6 must never be used
        for the server-to-client direction.
        """

        feedback = ShootFeedbackPacket()
        feedback.loop_count = int(getattr(self.server, "loop_count", 0))
        feedback.shooter_id = int(player.id)
        feedback.tool_id = int(player.tool)
        feedback.shot_on_world_update = int(
            getattr(packet, "shot_on_world_update", 0)
        )
        feedback.seed = int(getattr(packet, "seed", 0)) & 0xFF
        return feedback

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

    def _trace_authoritative_hit(self, attacker, origin, direction, max_range: float):
        """Return the nearest player, damageable entity, or terrain impact.

        Terrain caps the ray before player/entity tests, while players and
        entities are compared by their actual entry distance.  This prevents a
        deployable behind a wall (or behind a nearer player) from absorbing the
        shot merely because it exists on the same ray.
        """
        block_pos = self.server.world_manager.raycast(
            origin[0], origin[1], origin[2],
            direction[0], direction[1], direction[2], max_range,
        )
        max_distance = float(max_range)
        if block_pos is not None:
            max_distance = min(
                max_distance,
                self._distance(origin, self._block_center(block_pos)),
            )

        player_hit = self._find_first_player_hit(
            attacker, origin, direction, max_distance
        )
        entity_hit = self._find_first_entity_hit(
            origin, direction, max_distance
        )

        if player_hit is not None and (
            entity_hit is None or player_hit[2] <= entity_hit[1]
        ):
            target, headshot, _, position = player_hit
            return "player", target, headshot, position
        if entity_hit is not None:
            entity, _, position = entity_hit
            return "entity", entity, False, position
        if block_pos is not None:
            return "block", block_pos, False, self._block_center(block_pos)
        return None

    def _find_first_entity_hit(self, origin, direction, max_distance: float):
        registry = getattr(self.server, "entity_registry", None)
        if registry is None:
            return None

        closest = None
        for entity in registry.all():
            behavior = getattr(entity, "behavior", None)
            radius = float(getattr(behavior, "hit_radius", 0.0) or 0.0)
            if (
                not entity.alive
                or behavior is None
                or not getattr(behavior, "takes_damage", False)
                or radius <= 0.0
            ):
                continue
            center = behavior.get_hit_center(entity)
            hit = self._ray_sphere_entry(
                origin, direction, max_distance, center, radius
            )
            if hit is None:
                continue
            distance, position = hit
            if closest is None or distance < closest[1]:
                closest = (entity, distance, position)
        return closest

    @staticmethod
    def _ray_sphere_entry(origin, direction, max_distance, center, radius):
        """Nearest entry point for a normalized ray and finite sphere."""
        relative = tuple(center[index] - origin[index] for index in range(3))
        projection = sum(relative[index] * direction[index] for index in range(3))
        radius_sq = float(radius) * float(radius)
        closest_sq = sum(component * component for component in relative) - projection ** 2
        if closest_sq > radius_sq:
            return None
        half_chord = math.sqrt(max(0.0, radius_sq - closest_sq))
        entry = projection - half_chord
        exit_distance = projection + half_chord
        if exit_distance < 0.0:
            return None
        entry = max(0.0, entry)
        if entry > float(max_distance):
            return None
        position = tuple(
            origin[index] + direction[index] * entry for index in range(3)
        )
        return entry, position

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

    def _apply_riot_shield_mitigation(self, target, attacker, damage: int) -> int:
        """Apply the retail shield's 50% absorption to frontal direct hits.

        The shield has no activation packet: it is held whenever tool 52 is
        equipped and the ordinary WorldUpdate display bit is set.  A positive
        facing dot means the source lies in the shield bearer's front
        hemisphere. Explosions and status damage do not route through this
        helper because their impact origin is not the attacking character.
        """
        if (
            int(getattr(target, "tool", -1)) != int(C.RIOTSHIELD_TOOL)
            or not bool(getattr(getattr(target, "input", None),
                                "can_display_weapon", False))
        ):
            return int(damage)

        to_source = (
            float(attacker.x) - float(target.x),
            float(attacker.y) - float(target.y),
            float(attacker.z) - float(target.z),
        )
        source_direction = self._normalize(to_source)
        facing = self._normalize(target.orientation)
        if source_direction is None or facing is None:
            return int(damage)
        dot = sum(facing[index] * source_direction[index] for index in range(3))
        if dot <= 0.0:
            return int(damage)

        absorption = max(
            0.0,
            min(
                1.0,
                float(getattr(C, "RIOTSHIELD_DAMAGE_ABSORPTION_PERCENT", 50.0))
                / 100.0,
            ),
        )
        return max(0, int(round(float(damage) * (1.0 - absorption))))

    @staticmethod
    def _apply_riot_shield_knockback(attacker, target) -> None:
        """Push a shield-bashed enemy horizontally by the recovered 0.5."""
        dx = float(target.x) - float(attacker.x)
        dy = float(target.y) - float(attacker.y)
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            dx = float(attacker.orientation[0])
            dy = float(attacker.orientation[1])
            length = math.hypot(dx, dy)
        if length <= 1e-6:
            return
        strength = float(getattr(C, "RIOTSHIELD_KNOCKBACK", 0.5))
        vx, vy, vz = target.velocity
        target.velocity = (
            float(vx) + dx / length * strength,
            float(vy) + dy / length * strength,
            float(vz),
        )

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
