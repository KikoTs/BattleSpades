"""Mode-neutral map pickups and native static-light replication.

The stock map description owns resource positions and hidden flare markers.
This service rebuilds only that map-owned subset on mode/round start; objective
entities, projectiles, and player deployables remain owned by their domains.
All methods run synchronously on the gameplay event-loop thread and perform no
file I/O or whole-map scans. VXL marker discovery already happened at load time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import shared.constants as C

from server.entities.behaviors import PickupCrateBehavior
from server.game_constants import MAX_HEALTH, TEAM1, TEAM2, TEAM_NEUTRAL

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.map_metadata import MapEntitySpec


logger = logging.getLogger(__name__)

_MANAGED_KINDS = frozenset((
    "map_ammo", "map_health", "map_block", "map_jetpack", "map_flare",
))
_PICKUP_TYPES = frozenset((
    int(C.AMMO_CRATE), int(C.HEALTH_CRATE), int(C.BLOCK_CRATE),
    int(C.JETPACK_CRATE),
))
_MAX_STATIC_LIGHTS = 2048


class MapResourceService:
    """Own map-authored crates and hidden chroma-marker flare entities."""

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server

    def rebuild(self) -> None:
        """Replace map resources while preserving objectives and deployables."""
        server = self.server
        registry = getattr(server, "entity_registry", None)
        world = getattr(server, "world_manager", None)
        if registry is None or world is None or world.map is None:
            return

        self._remove_previous()
        pickup_count = self._place_pickups()
        flare_count = self._place_static_flares()
        logger.info(
            "Map resources rebuilt for %s: %d pickups, %d static flare lights%s",
            getattr(world, "map_name", "unknown"),
            pickup_count,
            flare_count,
            "" if getattr(server.config, "entities_wire_ready", False)
            else " (server-side only; entity wire disabled)",
        )

    def _remove_previous(self) -> None:
        registry = self.server.entity_registry
        for entity in registry.all():
            if entity.kind not in _MANAGED_KINDS:
                continue
            if entity.wire_visible and entity.alive:
                self.server.broadcast_destroy_entity(entity.entity_id)
            registry.remove(entity.entity_id)

    @staticmethod
    def _behaviors() -> dict[int, tuple[str, PickupCrateBehavior]]:
        from server.audio import SND_CRATE, SND_CRATE_BLOCKS, SND_HEALTHCRATE

        return {
            int(C.AMMO_CRATE): ("map_ammo", PickupCrateBehavior(
                # Type zero is a full-life restock in Character.restock.
                lambda player: player.restock_ammo(int(C.AMMO_CRATE)),
                respawn_delay=15.0,
                sound_id=SND_CRATE,
            )),
            int(C.HEALTH_CRATE): ("map_health", PickupCrateBehavior(
                lambda player: player.heal(MAX_HEALTH),
                respawn_delay=15.0,
                sound_id=SND_HEALTHCRATE,
            )),
            int(C.BLOCK_CRATE): ("map_block", PickupCrateBehavior(
                lambda player: player.restock_blocks(),
                respawn_delay=15.0,
                sound_id=SND_CRATE_BLOCKS,
            )),
            int(C.JETPACK_CRATE): ("map_jetpack", PickupCrateBehavior(
                lambda player: player.restock_jetpack(),
                respawn_delay=15.0,
                sound_id=SND_CRATE,
            )),
        }

    def _fallback_pickups(self) -> list[tuple[float, float, int]]:
        world = self.server.world_manager
        spots: list[tuple[float, float]] = []
        for team in (TEAM1, TEAM2):
            base_x, base_y, _base_z = world.team_base_anchor(team)
            spots.extend((
                (base_x + 8.0, base_y),
                (base_x - 8.0, base_y),
                (base_x, base_y + 8.0),
            ))
        spots.extend(((248.0, 256.0), (256.0, 256.0), (264.0, 256.0)))
        types = (int(C.AMMO_CRATE), int(C.HEALTH_CRATE), int(C.BLOCK_CRATE))
        return [
            (x, y, types[index % len(types)])
            for index, (x, y) in enumerate(spots)
        ]

    def _authored_position(self, spec: "MapEntitySpec") -> tuple[float, float, float]:
        """Translate legacy sidecar Z alongside a vertically normalized VXL."""
        shift = int(getattr(self.server.world_manager.map, "source_z_shift", 0))
        return float(spec.x), float(spec.y), float(spec.z) + shift

    def _place_pickups(self) -> int:
        server = self.server
        world = server.world_manager
        registry = server.entity_registry
        behaviors = self._behaviors()
        from server.game_rules import get_rules
        respawn_delay = float(
            get_rules(server.config).get("RULE_CRATES_SPAWN_TIME")
        )
        for _kind, behavior in behaviors.values():
            behavior.respawn_delay = respawn_delay
        authored = [
            spec for spec in world.map_metadata.entities
            if int(spec.entity_type) in _PICKUP_TYPES
        ]

        if authored:
            placements = [
                (*self._authored_position(spec), int(spec.entity_type))
                for spec in authored
            ]
        else:
            placements = []
            for x, y, entity_type in self._fallback_pickups():
                anchor_x, anchor_y, anchor_z = world.dry_surface_anchor(x, y)
                placements.append((anchor_x, anchor_y, anchor_z, entity_type))

        count = 0
        for x, y, z, entity_type in placements:
            if not (0.0 <= x < float(C.MAP_X) and 0.0 <= y < float(C.MAP_Y)
                    and 0.0 <= z < float(C.MAP_Z)):
                logger.warning("Ignoring out-of-world map pickup at %s", (x, y, z))
                continue
            kind, behavior = behaviors[entity_type]
            entity = registry.place(
                entity_type, x, y, z,
                state=TEAM_NEUTRAL,
                kind=kind,
                behavior=behavior,
            )
            if getattr(server.config, "entities_wire_ready", False):
                server.broadcast_create_entity(entity)
            count += 1
        return count

    def _place_static_flares(self) -> int:
        server = self.server
        world = server.world_manager
        registry = server.entity_registry
        metadata = world.map_metadata
        placements: list[tuple[float, float, float, tuple[int, int, int]]] = []
        missing_marker_families: dict[int, int] = {}

        for spec in metadata.entities:
            if int(spec.entity_type) != int(C.FLARE_BLOCK) or spec.color is None:
                continue
            x, y, z = self._authored_position(spec)
            placements.append((x, y, z, spec.color))

        # vxl.pyd removes these chroma voxels from client terrain. Re-create
        # their intended illumination as neutral FlareBlockEntity instances.
        for x, y, z, family in getattr(world.map, "retail_marker_families", ()):
            family = int(family)
            color = metadata.static_light_colors.get(family)
            if color is None:
                missing_marker_families[family] = (
                    missing_marker_families.get(family, 0) + 1
                )
                continue
            placements.append((float(x), float(y), float(z), color))

        if missing_marker_families:
            details = ", ".join(
                f"{family}={count}"
                for family, count in sorted(missing_marker_families.items())
            )
            logger.warning(
                "Skipped %d static-light markers on %s because their map "
                "palette is missing (family counts: %s)",
                sum(missing_marker_families.values()),
                getattr(world, "map_name", "unknown"),
                details,
            )

        if len(placements) > _MAX_STATIC_LIGHTS:
            logger.warning(
                "Map declares %d static lights; truncating to bounded limit %d",
                len(placements), _MAX_STATIC_LIGHTS,
            )
            placements = placements[:_MAX_STATIC_LIGHTS]

        placed_count = 0
        for x, y, z, color in placements:
            # Native FlareBlockEntity adds a coloured user block as well as a
            # point light. Restore the same cell in authoritative collision;
            # otherwise the retail client stops on a block the server calls
            # air and movement reconciliation pulls the player through it.
            restore_block = getattr(world, "restore_static_light_block", None)
            if callable(restore_block) and not restore_block(x, y, z, color):
                logger.warning(
                    "Ignoring out-of-world static light at %s on %s",
                    (x, y, z),
                    getattr(world, "map_name", "unknown"),
                )
                continue
            entity = registry.place(
                int(C.FLARE_BLOCK), x, y, z,
                state=TEAM_NEUTRAL,
                color=color,
                kind="map_flare",
                player_id=0,
                # Static map ownership: player deployable support/damage rules
                # do not delete authored atmosphere during a round.
                behavior=None,
            )
            if getattr(server.config, "entities_wire_ready", False):
                server.broadcast_create_entity(entity)
            placed_count += 1
        return placed_count
