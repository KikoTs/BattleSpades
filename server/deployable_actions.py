"""Authoritative deployable actions shared by packets and server-owned bots.

The methods in :class:`DeployableActionService` run on the gameplay thread.
They accept normalized primitive arguments, enforce the committed class,
loadout, held tool, placement range, stock, and entity limits, then use the
normal entity replication paths.  Packet handlers only decode wire fields;
bot code only submits intentions through :class:`server.bot_ai.gateway.BotActionGateway`.
"""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

import shared.constants as C

from server.class_selection import deployable_authorized
from server.connection import internal_team_to_wire
from server.entities.behaviors import (
    MedpackBehavior,
    ProximityMineBehavior,
    RadarStationBehavior,
    RemoteChargeBehavior,
    TimedExplosiveBehavior,
)
from server.entities.machine_gun import MachineGunBehavior
from server.game_constants import KILL_TYPES

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player


logger = logging.getLogger(__name__)
Vector3 = tuple[float, float, float]


class DeployableActionService:
    """Own class-safe placement and activation of replicated deployables.

    All methods fail closed and return ``False`` without partial mutation when
    authorization or primitive validation fails.  They must be called on the
    authoritative gameplay thread; none performs blocking I/O.
    """

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server

    @staticmethod
    def validate_position(
        player: "Player", position: Vector3, *, max_distance: float
    ) -> Vector3 | None:
        """Normalize a finite placement within ``max_distance`` of ``player``."""

        try:
            x, y, z = (float(value) for value in position)
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(value) and abs(value) <= 1e6 for value in (x, y, z)):
            return None
        dx, dy, dz = x - player.x, y - player.y, z - player.z
        if dx * dx + dy * dy + dz * dz > float(max_distance) ** 2:
            return None
        return x, y, z

    def place_medpack(
        self, player: "Player", position: Vector3, *, face: int = 4
    ) -> bool:
        """Place the visible Medic pack with stock healing behavior."""

        if not deployable_authorized(player, C.MEDPACK_TOOL):
            return False
        pos = self.validate_position(
            player,
            position,
            max_distance=float(getattr(C, "MEDPACK_FAR_RADIUS", 5.0)),
        )
        if pos is None or not 0 <= int(face) <= 5:
            return False
        entity = self.server.entity_registry.place(
            int(getattr(C, "MEDPACK_ENTITY", 30)),
            *pos,
            state=internal_team_to_wire(player.team),
            kind="medpack",
            player_id=player.id,
            face=int(face),
            behavior=MedpackBehavior(
                team=player.team,
                heal_amount=int(getattr(C, "MEDPACK_HEAL_AMOUNT", 25)),
                uses=int(getattr(C, "MEDPACK_USES", 3)),
                health=float(getattr(C, "MEDPACK_HEALTH", 1.0)),
            ),
        )
        self.server.broadcast_create_entity(entity)
        logger.info("MEDPACK id=%d placed by %s at %s", entity.entity_id, player.name, pos)
        return True

    def place_dynamite(
        self,
        player: "Player",
        position: Vector3,
        *,
        face: int = 4,
    ) -> bool:
        """Attach one timed Miner charge to the client-selected voxel face."""

        if (
            not deployable_authorized(player, C.DYNAMITE_TOOL)
            or not 0 <= int(face) <= 5
        ):
            return False
        pos = self.validate_position(
            player,
            position,
            max_distance=float(getattr(C, "DYNAMITE_FAR_RADIUS", 5.0)),
        )
        if pos is None:
            return False
        behavior = TimedExplosiveBehavior(
            player.id,
            fuse=float(getattr(C, "DYNAMITE_EXPLOSION_FUSE", 7.0)),
            damage=float(getattr(C, "DYNAMITE_EXPLOSION_DAMAGE", 300.0)),
            block_damage=float(getattr(C, "DYNAMITE_EXPLOSION_BLOCK_DAMAGE", 7.0)),
            crater_radius=2,
            kill_type=KILL_TYPES.get("DYNAMITE_KILL", 15),
            blast_radius=float(getattr(C, "DYNAMITE_EXPLOSION_RADIUS", 5.0)),
            force_destroy=True,
        )
        entity = self.server.entity_registry.place(
            int(getattr(C, "DYNAMITE_ENTITY", 10)),
            *pos,
            state=internal_team_to_wire(player.team),
            kind="deployable",
            player_id=player.id,
            face=int(face),
            behavior=behavior,
        )
        self.server.broadcast_create_entity(entity)
        logger.info("DYNAMITE id=%d placed by %s at %s", entity.entity_id, player.name, pos)
        return True

    def place_landmine(self, player: "Player", position: Vector3) -> bool:
        """Place one arming proximity mine for the active Scout."""

        if not deployable_authorized(player, C.LANDMINE_TOOL):
            return False
        pos = self.validate_position(
            player,
            position,
            max_distance=float(getattr(C, "LANDMINE_FAR_RADIUS", 5.0)),
        )
        if pos is None:
            return False
        behavior = ProximityMineBehavior(
            player.id,
            player.team,
            damage=float(getattr(C, "LANDMINE_EXPLOSION_DAMAGE", 100.0)),
            block_damage=float(getattr(C, "LANDMINE_EXPLOSION_BLOCK_DAMAGE", 15.0)),
            crater_radius=1,
            kill_type=KILL_TYPES.get("LANDMINE_KILL", 14),
            trigger_radius=float(getattr(C, "LANDMINE_DETECTION_RANGE", 2.5)),
            arm_delay=float(getattr(C, "LANDMINE_ACTIVATION_TIMER", 4.0)),
            blast_radius=float(getattr(C, "LANDMINE_EXPLOSION_RADIUS", 3.0)),
            force_destroy=False,
            detection_layers=int(getattr(C, "LANDMINE_DETECTION_LAYERS", 3)),
        )
        entity = self.server.entity_registry.place(
            int(getattr(C, "LANDMINE_ENTITY", 9)),
            *pos,
            state=internal_team_to_wire(player.team),
            kind="deployable",
            player_id=player.id,
            behavior=behavior,
        )
        self.server.broadcast_create_entity(entity)
        logger.info("LANDMINE id=%d placed by %s at %s", entity.entity_id, player.name, pos)
        return True

    def place_c4(
        self, player: "Player", position: Vector3, *, face: int
    ) -> bool:
        """Attach one stock-limited remote charge to a valid face."""

        if not deployable_authorized(player, C.C4_TOOL) or not 0 <= int(face) <= 5:
            return False
        pos = self.validate_position(
            player,
            position,
            max_distance=float(getattr(C, "C4_FAR_RADIUS", 5.0)),
        )
        if pos is None:
            return False
        live_ids: list[int] = []
        for entity_id in list(getattr(player, "_c4_entity_ids", ()) or ()):
            entity = self.server.entity_registry.get(entity_id)
            if entity is not None and entity.alive:
                live_ids.append(int(entity_id))
        if len(live_ids) >= int(getattr(C, "C4_STOCK", 2)):
            return False
        behavior = RemoteChargeBehavior(
            thrower_id=player.id,
            damage=float(getattr(C, "C4_EXPLOSION_DAMAGE", 300.0)),
            block_damage=float(getattr(C, "C4_EXPLOSION_BLOCK_DAMAGE", 7.0)),
            crater_radius=2,
            kill_type=int(getattr(C.KILL, "C4_KILL", 36)),
            blast_radius=float(getattr(C, "C4_EXPLOSION_RADIUS", 8.0)),
            health=float(getattr(C, "C4_HEALTH", 1.0)),
        )
        entity = self.server.entity_registry.place(
            int(getattr(C, "C4_ENTITY", 38)),
            *pos,
            state=internal_team_to_wire(player.team),
            kind="deployable",
            player_id=player.id,
            face=int(face),
            behavior=behavior,
        )
        live_ids.append(entity.entity_id)
        player._c4_entity_ids = live_ids
        self.server.broadcast_create_entity(entity)
        logger.info(
            "C4 id=%d placed by %s at %s face=%d",
            entity.entity_id,
            player.name,
            pos,
            face,
        )
        return True

    def detonate_c4(self, player: "Player") -> bool:
        """Detonate all live charges still owned by the active Miner."""

        if not deployable_authorized(player, C.C4_TOOL):
            return False
        detonated = False
        context = self.server._build_entity_ctx()
        for entity_id in list(getattr(player, "_c4_entity_ids", ()) or ()):
            entity = self.server.entity_registry.get(entity_id)
            if (
                entity is None
                or not entity.alive
                or entity.player_id != player.id
                or not isinstance(entity.behavior, RemoteChargeBehavior)
            ):
                continue
            entity.behavior.detonate(entity, context)
            detonated = True
        player._c4_entity_ids = []
        return detonated

    def place_radar(self, player: "Player", position: Vector3) -> bool:
        """Place the one-live-station Scout radar and enable visibility."""

        if not deployable_authorized(player, C.RADAR_STATION_TOOL):
            return False
        old = self.server.entity_registry.get(getattr(player, "_radar_entity_id", -1))
        if old is not None and old.alive:
            return False
        pos = self.validate_position(
            player,
            position,
            max_distance=float(getattr(C, "RADAR_STATION_FAR_RADIUS", 10.0)),
        )
        if pos is None:
            return False
        lifetime = float(
            getattr(
                self.server.config,
                "radar_station_lifetime_seconds",
                35.0,
            )
        )
        entity = self.server.entity_registry.place(
            int(getattr(C, "RADAR_STATION_ENTITY", 36)),
            *pos,
            state=internal_team_to_wire(player.team),
            kind="deployable",
            player_id=player.id,
            # RadarStationEntity.set_fuse enables the retail countdown. A zero
            # fuse leaves the model alive forever on the client even when the
            # authoritative behavior later removes it.
            fuse=lifetime,
            behavior=RadarStationBehavior(
                player.team,
                lifetime=lifetime,
                health=float(getattr(C, "RADAR_STATION_HEALTH", 45.0)),
            ),
        )
        player._radar_entity_id = entity.entity_id
        self.server._radar_station_added(player.team)
        self.server.broadcast_create_entity(entity)
        logger.info("RADAR id=%d placed by %s at %s", entity.entity_id, player.name, pos)
        return True

    def place_machine_gun(
        self, player: "Player", position: Vector3, *, yaw: float
    ) -> bool:
        """Place the one-per-owner durable mounted machine gun."""

        if not deployable_authorized(player, C.MG_TOOL) or not math.isfinite(float(yaw)):
            return False
        pos = self.validate_position(
            player,
            position,
            max_distance=float(getattr(C, "MG_FAR_RADIUS", 5.0)),
        )
        if pos is None:
            return False
        if any(
            entity.alive
            and isinstance(entity.behavior, MachineGunBehavior)
            and entity.behavior.owner_id == player.id
            for entity in self.server.entity_registry.all()
        ):
            return False
        entity = self.server.entity_registry.place(
            int(C.MACHINE_GUN),
            *pos,
            yaw=float(yaw),
            state=internal_team_to_wire(player.team),
            kind="machine_gun",
            player_id=0xFF,
            behavior=MachineGunBehavior(player.id, player.team),
        )
        self.server.broadcast_create_entity(entity)
        logger.info(
            "MACHINE GUN id=%d placed by %s at %s yaw=%.2f",
            entity.entity_id,
            player.name,
            pos,
            yaw,
        )
        return True

    def place_rocket_turret(
        self, player: "Player", position: Vector3, *, yaw: float
    ) -> bool:
        """Place a controller-owned Engineer/Rocketeer rocket turret."""

        if not deployable_authorized(player, C.ROCKET_TURRET_TOOL):
            return False
        if not math.isfinite(float(yaw)):
            return False
        pos = self.validate_position(
            player,
            position,
            max_distance=float(getattr(C, "ROCKET_TURRET_FAR_RADIUS", 10.0)),
        )
        if pos is None:
            return False
        turret = self.server.rocket_turret_controller.place(
            player, pos, float(yaw), now=time.monotonic()
        )
        if turret is None:
            return False
        logger.info(
            "ROCKET TURRET id=%d placed by %s at %s",
            turret.entity_id,
            player.name,
            pos,
        )
        return True

    def set_disguise(self, player: "Player", *, active: bool) -> bool:
        """Activate/deactivate the stock two-use Engineer disguise."""

        if not active:
            changed = bool(getattr(player, "disguised", False))
            player.disguised = False
            return changed
        if not deployable_authorized(player, C.DISGUISE_TOOL):
            return False
        if bool(getattr(player, "disguised", False)):
            return False
        now = time.monotonic()
        if now < float(getattr(player, "_disguise_next_use", 0.0)):
            return False
        stock = int(getattr(player, "disguise_stock", 0))
        if stock <= 0:
            return False
        player.disguise_stock = stock - 1
        player._disguise_next_use = now + float(
            getattr(C, "DISGUISE_SHOOT_INTERVAL", 0.5)
        )
        player.disguised = True
        logger.info("DISGUISE %s activated (%d remaining)", player.name, player.disguise_stock)
        return True
