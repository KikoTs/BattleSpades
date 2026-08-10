"""Retail-style asymmetric Occupation bomb mode."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL

from .base_mode import BaseMode
from .objective_zones import ObjectiveZone, around, from_map_zone, minimap_zone_packet


logger = logging.getLogger(__name__)

_PLAYABLE_TEAMS = (TEAM1, TEAM2)
_TARGET_RADIUS = 16.0


def _configured_rule(server, key: str, rule: str, fallback, *, allow_false=False):
    resolver = getattr(getattr(server, "config", None), "mode_rule", None)
    if callable(resolver):
        try:
            value = resolver("oc", key, rule)
            if value is not None and (allow_false or value is not False):
                return value
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    overlay = getattr(getattr(server, "config", None), "mode_settings", {}).get(
        "oc", {}
    )
    return overlay.get(key, fallback)


@dataclass(slots=True)
class OccupationBomb:
    """One bomb across ground, carried, armed, and exploding states."""

    serial: int
    entity_id: int | None
    position: tuple[float, float, float]
    carrier_id: int | None = None
    armed: bool = False
    explode_at: float = 0.0
    pickup_after: float = 0.0
    last_carrier_id: int | None = None
    last_carrier_team: int = TEAM_NEUTRAL
    intercepted: bool = False


class OccupationMode(BaseMode):
    """Blue retrieves bombs and detonates them in Green's defended base.

    A bomb is carried as the native non-swappable BombTool.  Dropping it
    lights the configured fuse; defenders can kill the carrier, pick up the
    live bomb, and carry it out so its blast counts as a disposal instead of a
    successful occupation strike.
    """

    name = "Occupation"
    description = "Green controls the base. Blue must bomb the base!"
    mode_code = "oc"

    def __init__(self, server) -> None:
        super().__init__(server)
        data = mode_data.get(self.mode_code)
        resolve_time = getattr(getattr(server, "config", None), "configured_time_limit", None)
        self.time_limit = (
            float(resolve_time(self.mode_code, data.default_time_limit))
            if callable(resolve_time)
            else float(data.default_time_limit)
        )
        score = _configured_rule(
            server,
            "score_limit",
            "RULE_OCC_SCORE_TARGET",
            30,
            allow_false=True,
        )
        self.score_limit = 0 if score is False else max(0, int(score))
        self.max_active_bombs = max(1, min(3, int(_configured_rule(
            server, "max_active_bombs", "RULE_MAX_ACTIVE_BOMBS", 1
        ))))
        self.bomb_fuse_time = max(1.0, float(_configured_rule(
            server, "bomb_fuse_time", "RULE_BOMB_FUSE_TIME",
            C.BOMB_EXPLOSION_FUSE,
        )))
        self.target_zone: ObjectiveZone | None = None
        self.bomb_spawn_points: list[tuple[float, float, float]] = []
        self.bombs: dict[int, OccupationBomb] = {}
        self.entity_to_bomb: dict[int, int] = {}
        self.carriers: dict[int, int] = {}
        self._pending_spawns: list[float] = []
        self._serial = 1
        self._spawn_cursor = 0
        self._next_personal_score_at = 0.0

    async def on_mode_start(self) -> None:
        await super().on_mode_start()
        self._clear_bombs()
        for team in self.server.teams.values():
            team.reset()
        self.target_zone = self._build_target_zone()
        self.bomb_spawn_points = self._build_bomb_spawn_points()
        self._pending_spawns.clear()
        self._spawn_cursor = 0
        self._send_target_zone()
        now = time.time()
        for _ in range(self.max_active_bombs):
            self._spawn_bomb(now)
        self._next_personal_score_at = now + float(CG.OC_SCORE_CARRY_INTERVAL)
        logger.info(
            "Occupation started with %d bombs, %.1fs fuse, score target %s",
            len(self.bombs),
            self.bomb_fuse_time,
            self.score_limit or "disabled",
        )

    async def deactivate(self) -> None:
        self._clear_bombs()
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        await super().on_tick(tick)
        if self.ended:
            return
        now = time.time()
        due = [when for when in self._pending_spawns if when <= now]
        self._pending_spawns = [when for when in self._pending_spawns if when > now]
        for _ in due:
            if len(self.bombs) < self.max_active_bombs:
                self._spawn_bomb(now)

        for player in tuple(getattr(self.server, "players", {}).values()):
            if not self._active_player(player) or int(player.id) in self.carriers:
                continue
            bomb = self._nearest_ground_bomb(player, now)
            if bomb is not None:
                self._pickup_bomb(player, bomb)

        # Bots cannot synthesize a client DropPickup packet.  Once an attacker
        # reaches the exact native target volume, perform the same validated
        # drop that a human triggers with BombTool primary fire.
        for player_id, serial in tuple(self.carriers.items()):
            player = getattr(self.server, "players", {}).get(player_id)
            bomb = self.bombs.get(serial)
            if (
                bomb is not None
                and not bomb.armed
                and player is not None
                and bool(getattr(player, "is_bot", False))
                and int(getattr(player, "team", -1)) == TEAM1
                and self._inside_target(player)
            ):
                await self._drop_bomb(player)

        for bomb in tuple(self.bombs.values()):
            if bomb.armed and now >= bomb.explode_at:
                await self._detonate_bomb(bomb)
                if self.ended:
                    return

        interval = float(CG.OC_SCORE_CARRY_INTERVAL)
        if now >= self._next_personal_score_at:
            periods = max(1, int((now - self._next_personal_score_at) / interval) + 1)
            self._next_personal_score_at += periods * interval
            self._award_periodic_scores(periods)

    async def on_player_death(self, player, killer, kill_type: int) -> None:
        if int(getattr(player, "id", -1)) not in self.carriers:
            return
        if (
            killer is not None
            and killer is not player
            and int(getattr(killer, "team", -1)) in _PLAYABLE_TEAMS
            and int(getattr(killer, "team", -1)) != int(getattr(player, "team", -1))
        ):
            team = self.server.teams[int(killer.team)]
            team.add_score(int(CG.OC_TEAM_SCORE_FOR_KILLING_CARRIER))
            self._award_player(
                killer,
                int(CG.OC_SCORE_INTERCEPT),
                int(C.SCORE_REASON.OCC_INTERCEPT_SCORE_REASON),
            )
            self._broadcast_team_score(team, C.SCORE_REASON.OCC_INTERCEPT_SCORE_REASON)
            if self.score_limit > 0 and team.score >= self.score_limit:
                await self._end_by_score(int(killer.team))
        await self._drop_bomb(player)

    async def on_player_leave(self, player) -> None:
        await self._drop_bomb(player)

    async def on_player_team_change(self, player, old_team: int, new_team: int) -> None:
        await self._drop_bomb(player)

    async def handle_drop_pickup(self, player, position, velocity) -> bool:
        if int(getattr(player, "id", -1)) not in self.carriers:
            return False
        await self._drop_bomb(player, position=position, velocity=velocity)
        return True

    def reveal_to(self, connection) -> None:
        self._send_target_zone(connection=connection)
        from server.entities.registry import send_create_entity_to

        for bomb in self.bombs.values():
            if bomb.entity_id is None:
                continue
            entity = self.server.entity_registry.get(bomb.entity_id)
            if entity is not None:
                send_create_entity_to(connection, entity)

        player = getattr(connection, "player", None)
        team = int(getattr(player if player is not None else connection, "team", -1))
        if team == TEAM1:
            self.send_localised_message_to(
                connection,
                "OCCUPATION_START_ATTACK",
                override_previous=True,
            )
        elif team == TEAM2:
            self.send_localised_message_to(
                connection,
                "OCCUPATION_START_DEFEND",
                override_previous=True,
            )

    def _build_target_zone(self) -> ObjectiveZone:
        wm = getattr(self.server, "world_manager", None)
        metadata = getattr(wm, "map_metadata", None)
        authored = getattr(metadata, "occupation_base_zone", None)
        shift = int(getattr(getattr(wm, "map", None), "source_z_shift", 0))
        if authored is not None:
            return from_map_zone(0, authored, z_shift=shift)
        team_bases = getattr(metadata, "base_zones", {}).get(TEAM2, []) if metadata else []
        if team_bases:
            return from_map_zone(0, team_bases[0], z_shift=shift)
        reader = getattr(wm, "team_base_anchor", None)
        center = reader(TEAM2) if callable(reader) else (448.0, 256.0, 58.0)
        logger.warning(
            "Map %s has no Occupation base; using Green's dry base anchor",
            getattr(wm, "map_name", "<unknown>"),
        )
        return around(
            0,
            center,
            radius_xy=_TARGET_RADIUS,
            height_above=12.0,
            depth_below=16.0,
        )

    def _build_bomb_spawn_points(self) -> list[tuple[float, float, float]]:
        wm = getattr(self.server, "world_manager", None)
        metadata = getattr(wm, "map_metadata", None)
        authored = list(getattr(metadata, "occupation_bomb_points", ()) or ())
        shift = int(getattr(getattr(wm, "map", None), "source_z_shift", 0))
        if authored:
            return [
                (float(x), float(y), float(z) + shift) for x, y, z in authored[:5]
            ]
        anchors = getattr(wm, "team_base_anchor", None)
        if callable(anchors):
            blue = anchors(TEAM1)
            green = anchors(TEAM2)
        else:
            blue, green = (64.0, 256.0, 58.0), (448.0, 256.0, 58.0)
        dry = getattr(wm, "dry_surface_anchor", None)
        points = []
        for fraction in (0.35, 0.45, 0.55):
            x = blue[0] + (green[0] - blue[0]) * fraction
            y = blue[1] + (green[1] - blue[1]) * fraction
            point = dry(x, y, 48) if callable(dry) else (x, y, 60.0)
            points.append(tuple(float(value) for value in point))
        logger.warning(
            "Map %s has no Occupation bomb points; using dry corridor spawns",
            getattr(wm, "map_name", "<unknown>"),
        )
        return points

    def _spawn_bomb(self, now: float) -> OccupationBomb | None:
        if not self.bomb_spawn_points:
            return None
        position = self.bomb_spawn_points[self._spawn_cursor % len(self.bomb_spawn_points)]
        self._spawn_cursor += 1
        entity = self.server.entity_registry.place(
            int(C.BOMB_PICKUP),
            *position,
            state=TEAM_NEUTRAL,
            kind="occupation_bomb",
            radius=0.5,
            fuse=0.0,
        )
        self.server.broadcast_create_entity(entity)
        bomb = OccupationBomb(
            serial=self._serial,
            entity_id=int(entity.entity_id),
            position=position,
            pickup_after=float(now),
        )
        self._serial += 1
        self.bombs[bomb.serial] = bomb
        self.entity_to_bomb[int(entity.entity_id)] = bomb.serial
        return bomb

    def _pickup_bomb(self, player, bomb: OccupationBomb) -> None:
        from server.pickups import broadcast_pickup

        if not broadcast_pickup(
            self.server,
            player,
            int(C.BOMB_PICKUP),
            burdensome=True,
            state=bomb.serial,
        ):
            return
        if bomb.entity_id is not None:
            self.server.broadcast_destroy_entity(bomb.entity_id)
            self.server.entity_registry.remove(bomb.entity_id)
            self.entity_to_bomb.pop(bomb.entity_id, None)
        bomb.entity_id = None
        bomb.carrier_id = int(player.id)
        bomb.last_carrier_id = int(player.id)
        bomb.last_carrier_team = int(player.team)
        if bomb.armed and int(player.team) == TEAM2:
            bomb.intercepted = True
        self.carriers[int(player.id)] = bomb.serial

    async def _drop_bomb(self, player, position=None, velocity=None) -> None:
        serial = self.carriers.get(int(getattr(player, "id", -1)))
        bomb = self.bombs.get(serial) if serial is not None else None
        if bomb is None:
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
        now = time.time()
        self.carriers.pop(int(player.id), None)
        bomb.carrier_id = None
        bomb.last_carrier_id = int(player.id)
        bomb.last_carrier_team = int(getattr(player, "team", TEAM_NEUTRAL))
        bomb.position = self._surface_anchor(dropped[2][0], dropped[2][1])
        if not bomb.armed:
            bomb.armed = True
            bomb.explode_at = now + self.bomb_fuse_time
        remaining = max(0.05, bomb.explode_at - now)
        entity = self.server.entity_registry.place(
            int(C.BOMB_PICKUP),
            *bomb.position,
            state=TEAM_NEUTRAL,
            kind="occupation_bomb_armed",
            radius=0.5,
            fuse=remaining,
        )
        self.server.broadcast_create_entity(entity)
        bomb.entity_id = int(entity.entity_id)
        bomb.pickup_after = now + float(C.NO_PICKUP_AFTER_DROP_TIME)
        self.entity_to_bomb[bomb.entity_id] = bomb.serial

    async def _detonate_bomb(self, bomb: OccupationBomb) -> None:
        thrower = None
        if bomb.carrier_id is not None:
            thrower = getattr(self.server, "players", {}).get(bomb.carrier_id)
            if thrower is not None:
                from server.pickups import broadcast_drop

                broadcast_drop(
                    self.server,
                    thrower,
                    (thrower.x, thrower.y, thrower.z),
                    (0.0, 0.0, 0.0),
                )
                bomb.position = self._surface_anchor(thrower.x, thrower.y)
            self.carriers.pop(bomb.carrier_id, None)
            bomb.carrier_id = None

        if bomb.entity_id is None:
            entity = self.server.entity_registry.place(
                int(C.BOMB_PICKUP),
                *bomb.position,
                state=TEAM_NEUTRAL,
                kind="occupation_bomb_exploding",
                radius=0.5,
                fuse=0.05,
            )
            self.server.broadcast_create_entity(entity)
            bomb.entity_id = int(entity.entity_id)
            self.entity_to_bomb[bomb.entity_id] = bomb.serial

        inside = self._bomb_inside_target(bomb.position)
        scorer = getattr(self.server, "players", {}).get(bomb.last_carrier_id)
        if inside:
            team = self.server.teams[TEAM1]
            team.add_score(int(CG.OC_TEAM_SCORE_FOR_BOMB_EXPLOSION_IN_BASE))
            if scorer is not None:
                self._award_player(
                    scorer,
                    int(CG.OC_SCORE_FOR_BOMB_EXPLOSION_IN_BASE),
                    int(C.SCORE_REASON.OCC_BOOM_SCORE_REASON),
                )
            self._broadcast_team_score(team, C.SCORE_REASON.OCC_BOOM_SCORE_REASON)
            await self.broadcast_message("Bomb detonated in Green base!")
        else:
            if scorer is not None and bomb.last_carrier_team == TEAM2:
                self._award_player(
                    scorer,
                    int(
                        CG.OC_SCORE_FOR_DISPOSAL_INTERCEPT
                        if bomb.intercepted
                        else CG.OC_SCORE_FOR_DISPOSAL
                    ),
                    int(
                        C.SCORE_REASON.OCC_INTERCEPT_DISPOSAL_SCORE_REASON
                        if bomb.intercepted
                        else C.SCORE_REASON.OCC_DISPOSAL_SCORE_REASON
                    ),
                )
            await self.broadcast_message("Bomb failed to reach Green base!")

        entity_id = int(bomb.entity_id)
        self.server._apply_blast(
            bomb.position[0],
            bomb.position[1],
            bomb.position[2],
            float(C.BOMB_EXPLOSION_DAMAGE),
            float(C.BOMB_EXPLOSION_BLOCK_DAMAGE),
            int(C.KILL.BOMB_KILL),
            thrower,
            crater_radius=1,
            force_destroy=True,
            blast_radius=float(C.BOMB_EXPLOSION_RADIUS),
            knockback_min=float(C.BOMB_EXPLOSION_KNOCKBACK_MIN),
            knockback_max=float(C.BOMB_EXPLOSION_KNOCKBACK_MAX),
            native_damage_type=int(C.BOMB_DAMAGE),
            causer_entity_id=entity_id,
        )
        self.server.broadcast_destroy_entity(entity_id)
        self.server.entity_registry.remove(entity_id)
        self.entity_to_bomb.pop(entity_id, None)
        self.bombs.pop(bomb.serial, None)
        self._pending_spawns.append(
            time.time() + float(CG.OC_BOMB_RESPAWN_TIME_ON_EXPLOSION)
        )
        if inside and self.score_limit > 0:
            if self.server.teams[TEAM1].score >= self.score_limit:
                await self._end_by_score(TEAM1)

    def _nearest_ground_bomb(self, player, now: float) -> OccupationBomb | None:
        radius_sq = float(C.PICKUP_DISTANCE) ** 2
        candidates = []
        for bomb in self.bombs.values():
            if bomb.entity_id is None or bomb.carrier_id is not None or now < bomb.pickup_after:
                continue
            distance_sq = sum(
                (float(value) - float(origin)) ** 2
                for value, origin in zip(bomb.position, (player.x, player.y, player.z))
            )
            if distance_sq <= radius_sq:
                candidates.append(bomb)
        return min(candidates, key=lambda item: item.serial) if candidates else None

    def _award_periodic_scores(self, periods: int) -> None:
        players = getattr(self.server, "players", {})
        for player_id in tuple(self.carriers):
            player = players.get(player_id)
            if self._active_player(player):
                self._award_player(
                    player,
                    periods * int(CG.OC_SCORE_CARRY_SCORE),
                    int(C.SCORE_REASON.OCC_CARRY_SCORE_REASON),
                )
        for player in players.values():
            if (
                self._active_player(player)
                and int(player.team) == TEAM1
                and self._inside_target(player)
            ):
                self._award_player(
                    player,
                    periods * int(CG.OC_SCORE_OCCUPY_SCORE),
                    int(C.SCORE_REASON.OCC_OCCUPY_SCORE_REASON),
                )

    def _send_target_zone(self, connection=None) -> None:
        if self.target_zone is None:
            return
        packet = minimap_zone_packet(
            self.target_zone,
            color=self.server.teams[TEAM2].color,
            icon_id=int(CG.ZONE_ICON_OCCUPATION),
            visible_team=TEAM_NEUTRAL,
        )
        data = bytes(packet.generate())
        if connection is None:
            self.server.broadcast(data, reliable=True)
        else:
            connection.send(data, reliable=True)

    def _inside_target(self, player) -> bool:
        return bool(
            self.target_zone
            and self.target_zone.contains((player.x, player.y, player.z))
        )

    def _bomb_inside_target(self, position) -> bool:
        """Use the authored horizontal objective footprint for a floor bomb.

        Players occupy the volume at their feet anchor, while a dropped entity
        is serialized on the supporting voxel surface (about 2.25 blocks
        lower in VXL coordinates).  Small UGC zones end two blocks below their
        center, so applying the player Z bounds to the bomb itself rejects a
        visually valid floor placement by a quarter block.
        """

        if self.target_zone is None:
            return False
        x0, x1, y0, y1, _z0, _z1 = self.target_zone.bounds
        return (
            x0 <= float(position[0]) <= x1
            and y0 <= float(position[1]) <= y1
        )

    def _surface_anchor(self, x: float, y: float) -> tuple[float, float, float]:
        wm = getattr(self.server, "world_manager", None)
        reader = getattr(wm, "dry_surface_anchor", None)
        if callable(reader):
            return tuple(float(value) for value in reader(x, y))
        return float(x), float(y), 60.0

    def _clear_bombs(self) -> None:
        from server.pickups import broadcast_drop

        for player_id in tuple(self.carriers):
            player = getattr(self.server, "players", {}).get(player_id)
            if (
                player is not None
                and getattr(player, "pickup_id", None) == int(C.BOMB_PICKUP)
            ):
                broadcast_drop(
                    self.server,
                    player,
                    (player.x, player.y, player.z),
                    (0.0, 0.0, 0.0),
                )
        for bomb in tuple(self.bombs.values()):
            if bomb.entity_id is not None:
                self.server.broadcast_destroy_entity(bomb.entity_id)
                self.server.entity_registry.remove(bomb.entity_id)
        self.bombs.clear()
        self.entity_to_bomb.clear()
        self.carriers.clear()
        self._pending_spawns.clear()

    def _broadcast_team_score(self, team, reason) -> None:
        try:
            self.server.broadcast_set_score(team, reason=int(reason))
        except TypeError:
            self.server.broadcast_set_score(team)

    def _award_player(self, player, points: int, reason: int) -> None:
        if points <= 0:
            return
        from server.scoreboard import send_player_score

        player.score = int(getattr(player, "score", 0)) + int(points)
        send_player_score(self.server, player, reason=int(reason))

    @staticmethod
    def _active_player(player) -> bool:
        return bool(
            player is not None
            and getattr(player, "alive", False)
            and getattr(player, "spawned", True)
            and int(getattr(player, "team", -1)) in _PLAYABLE_TEAMS
        )


__all__ = ["OccupationBomb", "OccupationMode"]
