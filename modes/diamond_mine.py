"""Retail-style Diamond Mine objective mode."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL

from .base_mode import BaseMode
from .objective_zones import (
    ObjectiveZone,
    around,
    from_map_zone,
    minimap_zone_clear_packet,
    minimap_zone_packet,
)


logger = logging.getLogger(__name__)

_PLAYABLE_TEAMS = (TEAM1, TEAM2)
_NEUTRAL_COLOR = (255, 255, 255)
_FALLBACK_RADIUS = 12.0


def _configured_rule(server, key: str, rule: str, fallback):
    resolver = getattr(getattr(server, "config", None), "mode_rule", None)
    if callable(resolver):
        try:
            value = resolver("dia", key, rule)
            if value is not None and value is not False:
                return value
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    overlay = getattr(getattr(server, "config", None), "mode_settings", {}).get(
        "dia", {}
    )
    return overlay.get(key, fallback)


@dataclass(slots=True)
class DiamondDropoff:
    """One authored cash-in volume with its remaining retail capacity."""

    zone: ObjectiveZone
    team: int = TEAM_NEUTRAL
    capacity: int = 1
    remaining: int = 1
    active: bool = False


@dataclass(slots=True)
class GroundDiamond:
    """One authoritative diamond entity awaiting pickup."""

    entity_id: int
    serial: int
    position: tuple[float, float, float]
    spawned_at: float
    expires_at: float
    pickup_after: float


class DiamondMineMode(BaseMode):
    """Mine hidden diamonds, carry one as the native tool, and cash it in."""

    name = "Diamond Mine"
    description = "Mine to find diamonds, then cash them in at drop-off points!"
    mode_code = "dia"

    def __init__(self, server) -> None:
        super().__init__(server)
        data = mode_data.get(self.mode_code)
        resolve_time = getattr(getattr(server, "config", None), "configured_time_limit", None)
        self.time_limit = (
            float(resolve_time(self.mode_code, data.default_time_limit))
            if callable(resolve_time)
            else float(data.default_time_limit)
        )
        self.score_limit = max(1, int(_configured_rule(
            server, "score_limit", "RULE_DIA_SCORE_TARGET",
            CG.DIA_DIAMONDS_TO_GET_FOR_MAP_ROTATION,
        )))
        self.max_active_bases = max(1, min(5, int(_configured_rule(
            server, "max_active_bases", "RULE_DIAMOND_MAX_ACTIVE_BASES",
            CG.DIA_DEFAULT_ACTIVE_BASES_AT_ONCE,
        ))))
        self.max_active_diamonds = max(1, min(5, int(_configured_rule(
            server, "max_active_diamonds", "RULE_MAX_ACTIVE_DIAMONDS",
            CG.DIA_DEFAULT_MAX_ACTIVE_DIAMONDS,
        ))))
        self.diamond_lifetime = max(1.0, float(_configured_rule(
            server, "diamond_lifetime", "RULE_DIAMOND_LIFETIME", 60.0
        )))
        self.dropoffs: list[DiamondDropoff] = []
        self.active_dropoffs: list[DiamondDropoff] = []
        self.ground_diamonds: dict[int, GroundDiamond] = {}
        self.carriers: dict[int, int] = {}
        self._serial = 1
        self._rotation_cursor = 0
        self._next_discovery_at = 0.0
        self._next_carry_score_at = 0.0
        self._rng = random.Random()

    async def on_mode_start(self) -> None:
        await super().on_mode_start()
        self._clear_runtime_entities()
        for team in self.server.teams.values():
            team.reset()
        self.dropoffs = self._build_dropoffs()
        self.active_dropoffs = []
        self.carriers.clear()
        self._rotation_cursor = 0
        self._activate_next_dropoffs()
        now = time.time()
        self._next_discovery_at = now
        self._next_carry_score_at = now + float(CG.DIA_SCORE_CARRY_INTERVAL)
        logger.info(
            "Diamond Mine started with %d drop-offs (%d active), max %d diamonds",
            len(self.dropoffs),
            len(self.active_dropoffs),
            self.max_active_diamonds,
        )

    async def deactivate(self) -> None:
        for dropoff in tuple(self.active_dropoffs):
            self._clear_dropoff(dropoff)
        self._clear_runtime_entities()
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        await super().on_tick(tick)
        if self.ended:
            return
        now = time.time()
        for diamond in tuple(self.ground_diamonds.values()):
            if now >= diamond.expires_at:
                self._remove_ground_diamond(diamond.entity_id)

        for player in tuple(getattr(self.server, "players", {}).values()):
            if not self._active_player(player):
                continue
            if int(player.id) not in self.carriers:
                diamond = self._nearest_ground_diamond(player)
                if diamond is not None and now >= diamond.pickup_after:
                    self._pickup_diamond(player, diamond)
            if int(player.id) in self.carriers:
                dropoff = self._cashable_dropoff(player)
                if dropoff is not None:
                    await self._cash_in(player, dropoff)
                    if self.ended:
                        return

        if now >= self._next_carry_score_at:
            periods = max(1, int(
                (now - self._next_carry_score_at)
                / float(CG.DIA_SCORE_CARRY_INTERVAL)
            ) + 1)
            self._next_carry_score_at += periods * float(CG.DIA_SCORE_CARRY_INTERVAL)
            self._award_carry_and_escort(periods)

    async def on_blocks_destroyed(
        self,
        player,
        positions: tuple[tuple[int, int, int], ...],
        mined: bool,
    ) -> None:
        """Roll once per mined voxel batch after the server commits terrain."""

        if (
            self.ended
            or not mined
            or not self._active_player(player)
            or not positions
            or self._active_diamond_count() >= self.max_active_diamonds
        ):
            return
        now = time.time()
        if now < self._next_discovery_at:
            return
        active_ratio = self._active_diamond_count() / float(
            max(1, self.max_active_diamonds)
        )
        chance = (
            float(CG.DIA_HIGHEST_DIAMOND_CHANCE)
            + (float(CG.DIA_LOWEST_DIAMOND_CHANCE)
               - float(CG.DIA_HIGHEST_DIAMOND_CHANCE)) * active_ratio
        )
        # A bulk spade/prefab removal still represents multiple independently
        # mined blocks.  This exact complement calculation preserves per-voxel
        # chance while spawning at most one diamond from one server event.
        event_chance = 1.0 - (1.0 - chance) ** len(positions)
        if self._rng.random() > event_chance:
            return
        position = positions[self._rng.randrange(len(positions))]
        self._spawn_diamond((
            float(position[0]) + 0.5,
            float(position[1]) + 0.5,
            float(position[2]) + 0.5,
        ), now=now, uncovered_by=player)

    async def on_player_death(self, player, killer, kill_type: int) -> None:
        await self._drop_carried_diamond(player)

    async def on_player_leave(self, player) -> None:
        await self._drop_carried_diamond(player)

    async def on_player_team_change(self, player, old_team: int, new_team: int) -> None:
        await self._drop_carried_diamond(player)

    async def handle_drop_pickup(self, player, position, velocity) -> bool:
        if int(getattr(player, "id", -1)) not in self.carriers:
            return False
        await self._drop_carried_diamond(player, position=position, velocity=velocity)
        return True

    def reveal_to(self, connection) -> None:
        for dropoff in self.active_dropoffs:
            self._send_dropoff(dropoff, connection=connection)
        from server.entities.registry import send_create_entity_to

        for diamond in self.ground_diamonds.values():
            entity = self.server.entity_registry.get(diamond.entity_id)
            if entity is not None:
                send_create_entity_to(connection, entity)
        self.send_localised_message_to(
            connection,
            "DIAMOND_START",
            override_previous=True,
        )

    def _build_dropoffs(self) -> list[DiamondDropoff]:
        wm = getattr(self.server, "world_manager", None)
        metadata = getattr(wm, "map_metadata", None)
        authored = list(getattr(metadata, "diamond_base_zones", ()) or ())
        capacities = list(
            getattr(metadata, "diamond_base_capacities", ()) or ()
        )
        shift = int(getattr(getattr(wm, "map", None), "source_z_shift", 0))
        result = []
        for index, zone in enumerate(authored[:10]):
            capacity = capacities[index] if index < len(capacities) else 1
            team = int(getattr(zone, "team", TEAM_NEUTRAL))
            if team not in (*_PLAYABLE_TEAMS, TEAM_NEUTRAL):
                team = TEAM_NEUTRAL
            result.append(DiamondDropoff(
                zone=from_map_zone(index, zone, z_shift=shift),
                team=team,
                capacity=max(1, int(capacity)),
                remaining=max(1, int(capacity)),
            ))
        if result:
            return result

        first, second = self._team_anchors()
        wm = getattr(self.server, "world_manager", None)
        dry = getattr(wm, "dry_ground_anchor", None)
        result = []
        for index, fraction in enumerate((0.25, 0.5, 0.75)):
            x = first[0] + (second[0] - first[0]) * fraction
            y = first[1] + (second[1] - first[1]) * fraction
            center = dry(x, y, 48) if callable(dry) else (x, y, 58.0)
            result.append(DiamondDropoff(
                zone=around(index, center, radius_xy=_FALLBACK_RADIUS),
            ))
        logger.warning(
            "Map %s has no Diamond Mine sidecar; using dry corridor drop-offs",
            getattr(wm, "map_name", "<unknown>"),
        )
        return result

    def _team_anchors(self):
        wm = getattr(self.server, "world_manager", None)
        reader = getattr(wm, "team_base_anchor", None)
        if callable(reader):
            return (
                tuple(float(value) for value in reader(TEAM1)),
                tuple(float(value) for value in reader(TEAM2)),
            )
        return (64.0, 256.0, 58.0), (448.0, 256.0, 58.0)

    def _activate_next_dropoffs(self) -> None:
        if not self.dropoffs:
            return
        for old in tuple(self.active_dropoffs):
            old.active = False
            self._clear_dropoff(old)
        count = min(self.max_active_bases, len(self.dropoffs))
        selected = [
            self.dropoffs[(self._rotation_cursor + offset) % len(self.dropoffs)]
            for offset in range(count)
        ]
        self._rotation_cursor = (self._rotation_cursor + count) % len(self.dropoffs)
        for dropoff in selected:
            dropoff.active = True
            dropoff.remaining = dropoff.capacity
            self._send_dropoff(dropoff)
        self.active_dropoffs = selected

    def _send_dropoff(self, dropoff: DiamondDropoff, connection=None) -> None:
        color = (
            self.server.teams[dropoff.team].color
            if dropoff.team in _PLAYABLE_TEAMS
            else _NEUTRAL_COLOR
        )
        packet = minimap_zone_packet(
            dropoff.zone,
            color=color,
            icon_id=int(CG.ZONE_ICON_DIAMONDMINE),
            visible_team=TEAM_NEUTRAL,
        )
        self._send_packet(packet, connection)

    def _clear_dropoff(self, dropoff: DiamondDropoff) -> None:
        self.server.broadcast(
            bytes(minimap_zone_clear_packet(dropoff.zone).generate()), reliable=True
        )

    def _spawn_diamond(
        self,
        position,
        *,
        now: float,
        uncovered_by=None,
        pickup_delay: float = 0.0,
    ) -> GroundDiamond:
        entity = self.server.entity_registry.place(
            int(C.DIAMOND_PICKUP),
            *position,
            state=TEAM_NEUTRAL,
            kind="diamond",
            radius=0.5,
        )
        self.server.broadcast_create_entity(entity)
        diamond = GroundDiamond(
            entity_id=int(entity.entity_id),
            serial=self._serial,
            position=tuple(float(value) for value in position),
            spawned_at=float(now),
            expires_at=float(now) + self.diamond_lifetime,
            pickup_after=float(now) + max(0.0, float(pickup_delay)),
        )
        self._serial += 1
        self.ground_diamonds[diamond.entity_id] = diamond
        self._next_discovery_at = float(now) + float(CG.DIA_TIME_BETWEEN_DIAMOND_SPAWN)
        if uncovered_by is not None:
            self._award_player(
                uncovered_by,
                int(CG.DIA_INDIVIDUAL_SCORE_FOR_MINED_DIAMOND),
                int(C.SCORE_REASON.DIA_UNCOVER_SCORE_REASON),
            )
        return diamond

    def _pickup_diamond(self, player, diamond: GroundDiamond) -> None:
        from server.pickups import broadcast_pickup

        if not broadcast_pickup(
            self.server,
            player,
            int(C.DIAMOND_PICKUP),
            burdensome=True,
            state=diamond.serial,
        ):
            return
        self.carriers[int(player.id)] = diamond.serial
        self._remove_ground_diamond(diamond.entity_id)

    async def _cash_in(self, player, dropoff: DiamondDropoff) -> None:
        from server.pickups import broadcast_drop

        if broadcast_drop(
            self.server,
            player,
            (player.x, player.y, player.z),
            (0.0, 0.0, 0.0),
        ) is None:
            return
        self.carriers.pop(int(player.id), None)
        self._award_player(
            player,
            int(CG.DIA_INDIVIDUAL_SCORE_FOR_CASHED_IN_DIAMOND),
            int(C.SCORE_REASON.DIA_CAPTURE_SCORE_REASON),
        )
        team = self.server.teams[int(player.team)]
        team.add_score(1)
        try:
            self.server.broadcast_set_score(
                team, reason=int(C.SCORE_REASON.DIA_CAPTURE_SCORE_REASON)
            )
        except TypeError:
            self.server.broadcast_set_score(team)
        dropoff.remaining -= 1
        await self.broadcast_message(f"{player.name} cashed in a diamond!")
        if team.score >= self.score_limit:
            await self._end_by_score(int(player.team))
            return
        if dropoff.remaining <= 0:
            self._rotate_dropoff(dropoff)

    async def _drop_carried_diamond(self, player, position=None, velocity=None) -> None:
        serial = self.carriers.get(int(getattr(player, "id", -1)))
        if serial is None:
            return
        from server.pickups import broadcast_drop

        position = position or (player.x, player.y, player.z)
        velocity = velocity or (
            float(getattr(player, "vx", 0.0)),
            float(getattr(player, "vy", 0.0)),
            float(getattr(player, "vz", 0.0)),
        )
        dropped = broadcast_drop(self.server, player, position, velocity)
        if dropped is None:
            return
        self.carriers.pop(int(player.id), None)
        settled = self._surface_anchor(dropped[2][0], dropped[2][1])
        diamond = self._spawn_diamond(
            settled,
            now=time.time(),
            pickup_delay=float(C.NO_PICKUP_AFTER_DROP_TIME),
        )
        diamond.serial = serial

    def _rotate_dropoff(self, exhausted: DiamondDropoff) -> None:
        """Replace one depleted drop-off without disrupting other live bases."""

        exhausted.active = False
        self._clear_dropoff(exhausted)
        self.active_dropoffs = [
            dropoff for dropoff in self.active_dropoffs if dropoff is not exhausted
        ]
        inactive = [dropoff for dropoff in self.dropoffs if not dropoff.active]
        replacement = next(
            (dropoff for dropoff in inactive if dropoff is not exhausted),
            exhausted,
        )
        replacement.active = True
        replacement.remaining = replacement.capacity
        self.active_dropoffs.append(replacement)
        self._send_dropoff(replacement)

    def _surface_anchor(self, x: float, y: float) -> tuple[float, float, float]:
        wm = getattr(self.server, "world_manager", None)
        reader = getattr(wm, "dry_surface_anchor", None)
        if callable(reader):
            return tuple(float(value) for value in reader(x, y))
        return float(x), float(y), 60.0

    def _remove_ground_diamond(self, entity_id: int) -> None:
        diamond = self.ground_diamonds.pop(int(entity_id), None)
        if diamond is None:
            return
        self.server.broadcast_destroy_entity(diamond.entity_id)
        self.server.entity_registry.remove(diamond.entity_id)

    def _clear_runtime_entities(self) -> None:
        from server.pickups import broadcast_drop

        for player_id in tuple(self.carriers):
            player = getattr(self.server, "players", {}).get(player_id)
            if (
                player is not None
                and getattr(player, "pickup_id", None) == int(C.DIAMOND_PICKUP)
            ):
                broadcast_drop(
                    self.server,
                    player,
                    (player.x, player.y, player.z),
                    (0.0, 0.0, 0.0),
                )
        for entity_id in tuple(self.ground_diamonds):
            self._remove_ground_diamond(entity_id)
        self.ground_diamonds.clear()
        self.carriers.clear()

    def _nearest_ground_diamond(self, player) -> GroundDiamond | None:
        radius_sq = float(C.PICKUP_DISTANCE) ** 2
        candidates = [
            diamond
            for diamond in self.ground_diamonds.values()
            if sum((float(value) - float(origin)) ** 2 for value, origin in zip(
                diamond.position, (player.x, player.y, player.z)
            )) <= radius_sq
        ]
        return min(candidates, key=lambda item: item.entity_id) if candidates else None

    def _cashable_dropoff(self, player) -> DiamondDropoff | None:
        return next((
            dropoff
            for dropoff in self.active_dropoffs
            if dropoff.remaining > 0
            and dropoff.team in (TEAM_NEUTRAL, int(player.team))
            and dropoff.zone.contains((player.x, player.y, player.z))
        ), None)

    def _award_carry_and_escort(self, periods: int) -> None:
        players = getattr(self.server, "players", {})
        for player_id in tuple(self.carriers):
            carrier = players.get(player_id)
            if not self._active_player(carrier):
                continue
            self._award_player(
                carrier,
                periods * int(CG.DIA_SCORE_CARRY_SCORE),
                int(C.SCORE_REASON.DIA_CARRY_SCORE_REASON),
            )
            radius_sq = float(CG.DIA_ESCORT_RADIUS) ** 2
            for escort in players.values():
                if (
                    escort is carrier
                    or not self._active_player(escort)
                    or int(escort.team) != int(carrier.team)
                ):
                    continue
                distance_sq = (
                    (escort.x - carrier.x) ** 2
                    + (escort.y - carrier.y) ** 2
                    + (escort.z - carrier.z) ** 2
                )
                if distance_sq <= radius_sq:
                    self._award_player(
                        escort,
                        periods * int(CG.DIA_SCORE_ESCORT_SCORE),
                        int(C.SCORE_REASON.DIA_ESCORT_SCORE_REASON),
                    )

    def _active_diamond_count(self) -> int:
        return len(self.ground_diamonds) + len(self.carriers)

    @staticmethod
    def _active_player(player) -> bool:
        return bool(
            player is not None
            and getattr(player, "alive", False)
            and getattr(player, "spawned", True)
            and int(getattr(player, "team", -1)) in _PLAYABLE_TEAMS
        )

    def _award_player(self, player, points: int, reason: int) -> None:
        if points <= 0:
            return
        from server.scoreboard import send_player_score

        player.score = int(getattr(player, "score", 0)) + int(points)
        send_player_score(self.server, player, reason=int(reason))

    def _send_packet(self, packet, connection=None) -> None:
        data = bytes(packet.generate())
        if connection is None:
            self.server.broadcast(data, reliable=True)
        else:
            connection.send(data, reliable=True)


__all__ = ["DiamondDropoff", "DiamondMineMode", "GroundDiamond"]
