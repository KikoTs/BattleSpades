"""Authoritative action boundary shared by bot intentions and gameplay code."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

import shared.constants as C
from shared.packet import BlockBuild, BlockLine, ShootPacket

from server.game_constants import WEAPON_PROFILES

from .messages import BotAction, BotActionKind

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player


logger = logging.getLogger(__name__)

_ORIENTED_SPEEDS = {
    int(C.GRENADE_TOOL): float(getattr(C, "GRENADE_THROW_SPEED", 50.0)),
    int(getattr(C, "CLASSIC_GRENADE_TOOL", 31)): float(
        getattr(C, "CLASSIC_GRENADE_THROW_SPEED", 35.0)
    ),
    int(getattr(C, "ANTIPERSONNEL_GRENADE_TOOL", 32)): float(
        getattr(C, "ANTIPERSONNEL_GRENADE_THROW_SPEED", 50.0)
    ),
    int(getattr(C, "MOLOTOV_TOOL", 33)): float(
        getattr(C, "MOLOTOV_THROW_SPEED", 40.0)
    ),
    int(C.RPG_TOOL): float(getattr(C, "ROCKET_SPEED", 75.0)),
    int(C.RPG2_TOOL): float(getattr(C, "ROCKET2_SPEED", 150.0)),
    int(C.DRILLGUN_TOOL): float(getattr(C, "DRILL_FLYING_SPEED", 40.0)),
    int(getattr(C, "SNOWBLOWER_TOOL", 29)): float(
        getattr(C, "SNOWBALL_SPEED", 50.0)
    ),
    int(getattr(C, "CHEMICALBOMB_TOOL", 54)): 40.0,
    int(getattr(C, "GRENADE_LAUNCHER_WEAPON_TOOL", 55)): float(
        getattr(C, "GRENADE_LAUNCHER_PROJECTILE_SPEED", 75.0)
    ),
    int(getattr(C, "STICKY_GRENADE_TOOL", 57)): 50.0,
    int(getattr(C, "MINE_LAUNCHER_TOOL", 58)): float(
        getattr(C, "MINE_LAUNCHER_PROJECTILE_SPEED", 75.0)
    ),
}


class BotActionGateway:
    """Validate and execute bot actions on the gameplay thread.

    The gateway intentionally calls public domain operations such as
    ``CombatSystem.handle_shot``.  It never calls private hit resolution or
    directly edits terrain/entities, so bot actions retain normal ammo,
    cadence, LOS, damage, inventory, replication, and mode checks.
    """

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server

    def execute(self, player: "Player", action: BotAction) -> bool:
        """Execute one validated action, returning whether it was accepted."""

        if not bool(getattr(player, "is_bot", False)):
            return False
        if not bool(getattr(player, "alive", False)) or not bool(
            getattr(player, "spawned", False)
        ):
            return False
        if action.kind is BotActionKind.NONE:
            return False
        if action.kind is BotActionKind.FIRE:
            if action.tool_id >= 0 and not self.select_tool(player, action.tool_id):
                return False
            return self.fire(player)
        if action.kind is BotActionKind.RELOAD:
            if action.tool_id >= 0 and not self.select_tool(player, action.tool_id):
                return False
            return bool(self.server.combat.handle_weapon_reload(player))
        if action.kind is BotActionKind.MELEE:
            if not self.select_tool(player, action.tool_id):
                return False
            return self.fire(player)
        if action.kind is BotActionKind.BUILD:
            return self.build(player, action)
        if action.kind is BotActionKind.BUILD_LINE:
            return self.build_line(player, action)
        if action.kind is BotActionKind.PLACE_PREFAB:
            return self.prefab(player, action)
        if action.kind is BotActionKind.DEPLOY:
            return self.deploy(player, action)
        if action.kind is BotActionKind.ORIENTED:
            return self.oriented(player, action)
        # Prefabs remain fail-closed until their packet expansion is extracted
        # into a public domain service as well.
        return False

    def select_tool(self, player: "Player", tool_id: int) -> bool:
        """Select an item only when it belongs to the normalized active loadout."""

        tool_id = int(tool_id)
        loadout = {int(value) for value in (getattr(player, "loadout", ()) or ())}
        from server.game_rules import get_rules
        config = getattr(self.server, "config", None)
        if (
            tool_id not in loadout
            or not get_rules(config).is_tool_enabled(tool_id)
        ):
            return False
        player.set_tool(tool_id, raw=True)
        return int(getattr(player, "tool", -1)) == tool_id

    def build(self, player: "Player", action: BotAction) -> bool:
        """Submit one normal block placement through ``CombatSystem``."""

        if action.position is None or not self.select_tool(player, int(C.BLOCK_TOOL)):
            return False
        try:
            x, y, z = (int(round(value)) for value in action.position)
        except (TypeError, ValueError):
            return False
        construction = getattr(self.server, "construction", None)
        reservation = None
        if construction is not None:
            reservation, _reason = construction.reserve_construction(
                int(player.id), int(player.team), ((x, y, z),)
            )
            if reservation is None:
                return False
        packet = BlockBuild()
        packet.loop_count = int(getattr(self.server, "loop_count", 0))
        packet.player_id = int(player.id)
        packet.x, packet.y, packet.z = x, y, z
        packet.block_type = 0
        accepted = bool(self.server.combat.handle_block_build(player, packet))
        if not accepted and construction is not None:
            construction.release(reservation)
        # Accepted block mutations can commit after physics; the short TTL
        # keeps the cell reserved until the normal mutation service catches up.
        return accepted

    def build_line(self, player: "Player", action: BotAction) -> bool:
        """Submit one atomic native BlockLine through the shared combat path."""

        if (
            action.position is None
            or action.end_position is None
            or not self.select_tool(player, int(C.BLOCK_TOOL))
        ):
            return False
        try:
            start = tuple(int(round(value)) for value in action.position)
            end = tuple(int(round(value)) for value in action.end_position)
        except (TypeError, ValueError):
            return False
        if len(start) != 3 or len(end) != 3:
            return False
        combat = getattr(self.server, "combat", None)
        cell_provider = getattr(combat, "block_line_cells", None)
        if combat is None or not callable(cell_provider):
            return False
        try:
            cells = tuple(
                tuple(int(component) for component in cell)
                for cell in cell_provider(start, end)
            )
        except (TypeError, ValueError):
            return False
        if not cells or len(cells) > int(
            getattr(combat, "BLOCK_LINE_MAX_CELLS", 64)
        ):
            return False

        construction = getattr(self.server, "construction", None)
        reservation = None
        if construction is not None:
            reservation, _reason = construction.reserve_construction(
                int(player.id), int(player.team), cells
            )
            if reservation is None:
                return False

        packet = BlockLine()
        packet.loop_count = int(getattr(self.server, "loop_count", 0))
        packet.player_id = int(player.id)
        packet.x1, packet.y1, packet.z1 = start
        packet.x2, packet.y2, packet.z2 = end
        accepted = bool(combat.handle_block_line(player, packet))
        if not accepted and construction is not None:
            construction.release(reservation)
        return accepted

    def prefab(self, player: "Player", action: BotAction) -> bool:
        """Place one selected prefab through the shared public service.

        The retail client has distinct ordinary, Zombie, and UGC prefab tool
        IDs even though all three produce BuildPrefabAction(30).  Preserve the
        action's normalized held tool instead of silently coercing it to the
        ordinary class tool (23).
        """

        tool = int(action.tool_id)
        if (
            action.position is None
            or not action.argument
            or tool not in {int(value) for value in C.PREFAB_TOOLS}
            or not self.select_tool(player, tool)
        ):
            return False
        quarter_yaw = int(round(float(action.yaw) / (math.pi / 2.0))) & 3
        return bool(
            self.server.prefab_actions.place(
                player,
                name=str(action.argument),
                position=action.position,
                yaw=quarter_yaw,
                color=(
                    (int(getattr(player, "block_color", 0)) >> 16) & 0xFF,
                    (int(getattr(player, "block_color", 0)) >> 8) & 0xFF,
                    int(getattr(player, "block_color", 0)) & 0xFF,
                ),
                loop_count=int(getattr(self.server, "loop_count", 0)),
                snap_to_surface=True,
            )
        )

    def deploy(self, player: "Player", action: BotAction) -> bool:
        """Dispatch a class-authorized placement to the shared deployable service."""

        tool = int(action.tool_id)
        if not self.select_tool(player, tool):
            return False
        service = self.server.deployable_actions
        position = action.position
        if tool == int(C.DISGUISE_TOOL):
            return bool(service.set_disguise(player, active=True))
        if position is None:
            return False
        if tool in {
            int(C.DYNAMITE_TOOL),
            int(C.LANDMINE_TOOL),
            int(C.C4_TOOL),
        } and not self._explosive_deploy_safe(player, position, tool):
            return False
        if tool == int(C.DYNAMITE_TOOL):
            return bool(service.place_dynamite(player, position))
        if tool == int(C.LANDMINE_TOOL):
            return bool(service.place_landmine(player, position))
        if tool == int(C.C4_TOOL):
            return bool(service.place_c4(player, position, face=action.face))
        if tool == int(C.RADAR_STATION_TOOL):
            return bool(service.place_radar(player, position))
        if tool == int(C.MEDPACK_TOOL):
            return bool(service.place_medpack(player, position, face=action.face))
        if tool == int(C.MG_TOOL):
            return bool(service.place_machine_gun(player, position, yaw=action.yaw))
        if tool == int(C.ROCKET_TURRET_TOOL):
            return bool(service.place_rocket_turret(player, position, yaw=action.yaw))
        return False

    def _explosive_deploy_safe(
        self, player: "Player", position, tool: int
    ) -> bool:
        """Fail closed when a bot charge would overlap friends or live blasts.

        The worker performs tactical planning, but this gameplay-thread gate
        closes the race where two bots arm charges from the same perception
        frame.  The owner is exempt because timed dynamite is intentionally
        placed inside its initial blast volume and must then retreat.
        """

        from server.projectiles import PROJECTILE_SPECS

        try:
            target = tuple(float(value) for value in position)
        except (TypeError, ValueError):
            return False
        if len(target) != 3 or not all(math.isfinite(value) for value in target):
            return False
        fallback_radii = {
            int(C.DYNAMITE_TOOL): float(
                getattr(C, "DYNAMITE_EXPLOSION_RADIUS", 5.0)
            ),
            int(C.LANDMINE_TOOL): float(
                getattr(C, "LANDMINE_EXPLOSION_RADIUS", 3.0)
            ),
            int(C.C4_TOOL): float(getattr(C, "C4_EXPLOSION_RADIUS", 8.0)),
        }
        spec = PROJECTILE_SPECS.get(int(tool))
        radius = max(
            fallback_radii.get(int(tool), 0.0),
            float(getattr(spec, "blast_radius", 0.0) or 0.0),
        )
        if radius <= 0.0:
            return False

        for teammate in tuple(getattr(self.server, "players", {}).values()):
            if int(getattr(teammate, "id", -1)) == int(player.id):
                continue
            if int(getattr(teammate, "team", -1)) != int(player.team):
                continue
            if not bool(getattr(teammate, "alive", False)) or not bool(
                getattr(teammate, "spawned", False)
            ):
                continue
            try:
                teammate_position = (
                    float(teammate.x),
                    float(teammate.y),
                    float(teammate.z),
                )
            except (AttributeError, TypeError, ValueError):
                return False
            if sum(
                (teammate_position[index] - target[index]) ** 2
                for index in range(3)
            ) <= (radius + 1.5) ** 2:
                return False

        explosive_types = {
            int(getattr(C, "DYNAMITE_ENTITY", 10)),
            int(getattr(C, "LANDMINE_ENTITY", 9)),
            int(getattr(C, "C4_ENTITY", 38)),
        }
        registry = getattr(self.server, "entity_registry", None)
        for entity in tuple(registry.all() if registry is not None else ()):
            if not bool(getattr(entity, "alive", True)):
                continue
            entity_type = int(
                getattr(entity, "entity_type", getattr(entity, "type", -1))
            )
            behavior = getattr(entity, "behavior", None)
            other_radius = float(
                getattr(behavior, "blast_radius", 0.0) or 0.0
            )
            if entity_type not in explosive_types or other_radius <= 0.0:
                continue
            try:
                entity_position = (
                    float(entity.x),
                    float(entity.y),
                    float(entity.z),
                )
            except (AttributeError, TypeError, ValueError):
                return False
            if sum(
                (entity_position[index] - target[index]) ** 2
                for index in range(3)
            ) <= (radius + other_radius + 1.0) ** 2:
                return False
        return True

    def _oriented_launch_safe(self, player: "Player", direction, spec) -> bool:
        """Revalidate the live muzzle lane before launching an explosive."""

        eye = tuple(float(value) for value in player.eye)
        radius = max(0.0, float(getattr(spec, "blast_radius", 0.0) or 0.0))
        world = getattr(self.server, "world_manager", None)
        raycast = getattr(world, "raycast", None)
        if callable(raycast):
            try:
                hit = raycast(
                    eye[0],
                    eye[1],
                    eye[2],
                    direction[0],
                    direction[1],
                    direction[2],
                    radius + 3.0,
                )
            except (TypeError, ValueError):
                return False
            if hit is not None:
                return False

        for teammate in tuple(getattr(self.server, "players", {}).values()):
            if int(getattr(teammate, "id", -1)) == int(player.id):
                continue
            if int(getattr(teammate, "team", -1)) != int(player.team):
                continue
            if not bool(getattr(teammate, "alive", False)) or not bool(
                getattr(teammate, "spawned", False)
            ):
                continue
            try:
                teammate_eye = tuple(float(value) for value in teammate.eye)
            except (AttributeError, TypeError, ValueError):
                return False
            relative = tuple(
                teammate_eye[index] - eye[index] for index in range(3)
            )
            along = sum(
                relative[index] * direction[index] for index in range(3)
            )
            if not 0.75 < along < 96.0:
                continue
            closest = tuple(
                eye[index] + direction[index] * along for index in range(3)
            )
            lateral = math.sqrt(
                sum(
                    (teammate_eye[index] - closest[index]) ** 2
                    for index in range(3)
                )
            )
            if lateral <= 1.75:
                return False
        return True

    def oriented(self, player: "Player", action: BotAction) -> bool:
        """Launch an equipped oriented weapon through its shared action service."""

        tool = int(action.tool_id)
        if tool not in _ORIENTED_SPEEDS:
            return False
        direction = self._normalized(getattr(player, "orientation", ()))
        if direction is None:
            return False
        from server.projectiles import PROJECTILE_SPECS

        spec = PROJECTILE_SPECS.get(tool)
        if (
            spec is None
            or not self._oriented_launch_safe(player, direction, spec)
            or not self.select_tool(player, tool)
        ):
            return False
        speed = _ORIENTED_SPEEDS[tool]
        eye = tuple(float(value) for value in player.eye)
        position = tuple(eye[index] + direction[index] * 0.6 for index in range(3))
        player_velocity = (
            float(getattr(player, "vx", 0.0)),
            float(getattr(player, "vy", 0.0)),
            float(getattr(player, "vz", 0.0)),
        )
        velocity = tuple(
            direction[index] * speed + player_velocity[index] * 0.35
            for index in range(3)
        )
        if spec.behavior in ("contact", "deploy"):
            fuse = 0.0
        elif spec.behavior == "stick":
            fuse = 0.0
        else:
            fuse = 2.5
        return bool(
            self.server.oriented_actions.use(
                player,
                tool_id=tool,
                position=position,
                velocity=velocity,
                fuse=fuse,
            )
        )

    def fire(self, player: "Player") -> bool:
        """Submit one normal ShootPacket-equivalent through combat authority."""

        if (
            int(getattr(player, "tool", -1)) not in WEAPON_PROFILES
            and not bool(player.is_spade_tool())
        ):
            return False
        direction = self._normalized(getattr(player, "orientation", ()))
        if direction is None:
            return False
        eye = tuple(float(value) for value in player.eye)
        packet = ShootPacket()
        packet.loop_count = int(getattr(self.server, "loop_count", 0))
        packet.shooter_id = int(player.id)
        packet.shot_on_world_update = 0
        packet.x, packet.y, packet.z = eye
        packet.ori_x, packet.ori_y, packet.ori_z = direction
        packet.damage = 0.0
        packet.penetration = 0
        packet.affect_shooter = 0
        # Bots use the same right-button/zoom state as a retail player.  This
        # is required for remote sniper presentation (including the beam) and
        # keeps packet semantics aligned with the replicated action flags.
        packet.secondary = int(
            bool(getattr(getattr(player, "input", None), "secondary_fire", False))
        )
        # Stable seed keeps shotgun pellet expansion deterministic for a fixed
        # server loop and bot identity while still varying successive shots.
        packet.seed = (
            int(getattr(self.server, "loop_count", 0)) * 1103515245
            + int(player.id) * 12345
        ) & 0x7FFFFFFF
        try:
            return bool(self.server.combat.handle_shot(player, packet))
        except (AttributeError, TypeError, ValueError):
            logger.exception("Rejected malformed bot fire action for id=%s", player.id)
            return False

    @staticmethod
    def _normalized(values) -> tuple[float, float, float] | None:
        try:
            x, y, z = (float(value) for value in values)
        except (TypeError, ValueError):
            return None
        length = math.sqrt(x * x + y * y + z * z)
        if not math.isfinite(length) or length <= 1e-6:
            return None
        return x / length, y / length, z / length
