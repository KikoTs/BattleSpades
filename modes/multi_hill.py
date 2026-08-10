"""Retail-style Multi-Hill objective mode."""

from __future__ import annotations

import logging
import math
import time

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL

from .airstrike import trigger_airstrike
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
_FALLBACK_RADIUS = 16.0


def _configured_rule(server, key: str, rule: str, fallback):
    resolver = getattr(getattr(server, "config", None), "mode_rule", None)
    if callable(resolver):
        try:
            value = resolver("mh", key, rule)
            if value is not None and value is not False:
                return value
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    overlay = getattr(getattr(server, "config", None), "mode_settings", {}).get(
        "mh", {}
    )
    return overlay.get(key, fallback)


class MultiHillMode(BaseMode):
    """Fight over rotating shared hill volumes.

    The native HUD represents a hill with packet 43/icon 2.  Ownership is the
    zone colour, not its visibility key, so every hill remains TEAM_NEUTRAL
    (shared) while blue/green ownership changes are resent in place.
    """

    name = "Multi-Hill"
    description = (
        "Both teams fight to control changing Hill points. "
        "Watch out for airstrikes!"
    )
    mode_code = "mh"

    def __init__(self, server) -> None:
        super().__init__(server)
        data = mode_data.get(self.mode_code)
        self.score_limit = max(1, int(_configured_rule(
            server, "score_limit", "RULE_MH_SCORE_TARGET",
            data.default_score_limit,
        )))
        resolve_time = getattr(getattr(server, "config", None), "configured_time_limit", None)
        self.time_limit = (
            float(resolve_time(self.mode_code, data.default_time_limit))
            if callable(resolve_time)
            else float(data.default_time_limit)
        )
        self.max_active_bases = max(1, int(_configured_rule(
            server,
            "max_active_bases",
            "RULE_MULTIHILL_MAX_ACTIVE_BASES",
            CG.MH_DEFAULT_NUMBER_OF_BASE_TO_ACTIVATE_AT_ONCE,
        )))
        self.base_active_time = max(1.0, float(_configured_rule(
            server,
            "base_active_time",
            "RULE_BASE_ACTIVE_TIME",
            CG.MH_DEFAULT_BASE_AUTO_TIMEOUT,
        )))

        self.zones: list[ObjectiveZone] = []
        self.active_zones: list[ObjectiveZone] = []
        self.zone_owner: dict[int, int | None] = {}
        self.zone_contested: dict[int, bool] = {}
        self.phase = "waiting"
        self._rotation_cursor = 0
        self._next_rotation_at = 0.0
        self._next_activation_at = 0.0
        self._last_score_at = 0.0

    async def on_mode_start(self) -> None:
        await super().on_mode_start()
        for team in self.server.teams.values():
            team.reset()
        self.zones = self._build_zones()
        self.active_zones = []
        self.zone_owner = {zone.index: None for zone in self.zones}
        self.zone_contested = {zone.index: False for zone in self.zones}
        self._rotation_cursor = 0
        now = time.time()
        self._last_score_at = now
        self._activate_next(now)
        logger.info(
            "Multi-Hill started with %d zones (%d active, %.0fs rotation)",
            len(self.zones),
            len(self.active_zones),
            self.base_active_time,
        )

    async def deactivate(self) -> None:
        self._clear_active_zones()
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        await super().on_tick(tick)
        if self.ended:
            return
        now = time.time()

        if self.phase == "intermission":
            if now >= self._next_activation_at:
                self._activate_next(now)
            return

        self._update_control()
        await self._award_team_ticks(now)
        if self.ended:
            return
        if now >= self._next_rotation_at:
            expired = tuple(self.active_zones)
            for zone in expired:
                trigger_airstrike(self.server, zone.center)
            self._clear_active_zones()
            self.phase = "intermission"
            self._next_activation_at = now + float(CG.MH_TIME_BETWEEN_BASE_ACTIVATIONS)

    def reveal_to(self, connection) -> None:
        for zone in self.active_zones:
            self._send_zone(zone, connection=connection)

    def _build_zones(self) -> list[ObjectiveZone]:
        wm = getattr(self.server, "world_manager", None)
        metadata = getattr(wm, "map_metadata", None)
        authored = list(getattr(metadata, "neutral_base_zones", ()) or ())
        shift = int(getattr(getattr(wm, "map", None), "source_z_shift", 0))
        zones = [
            from_map_zone(index, zone, z_shift=shift)
            for index, zone in enumerate(authored[:10])
        ]
        if len(zones) >= 2:
            return zones

        # Stock clients shipped the VXLs but not the official server's zone
        # sidecars.  Build deterministic dry objectives along and beside the
        # two team anchors, never arbitrary world coordinates or water beds.
        anchor_reader = getattr(wm, "team_base_anchor", None)
        if callable(anchor_reader):
            first = tuple(float(v) for v in anchor_reader(TEAM1))
            second = tuple(float(v) for v in anchor_reader(TEAM2))
        else:
            first, second = (64.0, 256.0, 58.0), (448.0, 256.0, 58.0)
        dx, dy = second[0] - first[0], second[1] - first[1]
        distance = max(1.0, math.hypot(dx, dy))
        px, py = -dy / distance, dx / distance
        lateral = min(80.0, distance * 0.20)
        candidates = [
            (first[0] + dx * 0.35, first[1] + dy * 0.35),
            (first[0] + dx * 0.50, first[1] + dy * 0.50),
            (first[0] + dx * 0.65, first[1] + dy * 0.65),
            (first[0] + dx * 0.50 + px * lateral,
             first[1] + dy * 0.50 + py * lateral),
            (first[0] + dx * 0.50 - px * lateral,
             first[1] + dy * 0.50 - py * lateral),
        ]
        ground = getattr(wm, "dry_ground_anchor", None)
        seen: set[tuple[int, int]] = set()
        zones = []
        for x, y in candidates:
            center = ground(x, y, 48) if callable(ground) else (x, y, 58.0)
            key = (int(center[0]), int(center[1]))
            if key in seen:
                continue
            seen.add(key)
            zones.append(around(
                len(zones), center, radius_xy=_FALLBACK_RADIUS,
                height_above=7.0, depth_below=10.0,
            ))
        if len(zones) < 2:
            zones = [
                around(0, first, radius_xy=_FALLBACK_RADIUS),
                around(1, second, radius_xy=_FALLBACK_RADIUS),
            ]
        logger.warning(
            "Map %s has no complete Multi-Hill sidecar; using %d dry fallback zones",
            getattr(wm, "map_name", "<unknown>"),
            len(zones),
        )
        return zones

    def _activate_next(self, now: float) -> None:
        if not self.zones:
            return
        count = min(len(self.zones), self.max_active_bases)
        selected = []
        for offset in range(count):
            selected.append(self.zones[(self._rotation_cursor + offset) % len(self.zones)])
        self._rotation_cursor = (self._rotation_cursor + count) % len(self.zones)
        self.active_zones = selected
        for zone in selected:
            self.zone_owner[zone.index] = None
            self.zone_contested[zone.index] = False
            self._send_zone(zone)
        self.phase = "active"
        self._last_score_at = now
        self._next_rotation_at = now + self.base_active_time

    def _clear_active_zones(self) -> None:
        for zone in tuple(self.active_zones):
            data = bytes(minimap_zone_clear_packet(zone).generate())
            self.server.broadcast(data, reliable=True)
        self.active_zones = []

    def _send_zone(self, zone: ObjectiveZone, connection=None) -> None:
        owner = self.zone_owner.get(zone.index)
        color = (
            self.server.teams[owner].color
            if owner in _PLAYABLE_TEAMS
            else _NEUTRAL_COLOR
        )
        packet = minimap_zone_packet(
            zone,
            color=color,
            icon_id=int(CG.ZONE_ICON_MULTIHILL),
            visible_team=TEAM_NEUTRAL,
        )
        data = bytes(packet.generate())
        if connection is None:
            self.server.broadcast(data, reliable=True)
        else:
            connection.send(data, reliable=True)

    def _update_control(self) -> None:
        players = tuple(getattr(self.server, "players", {}).values())
        for zone in self.active_zones:
            occupants = {TEAM1: [], TEAM2: []}
            for player in players:
                team = int(getattr(player, "team", -1))
                if (
                    team not in _PLAYABLE_TEAMS
                    or not bool(getattr(player, "alive", False))
                    or not bool(getattr(player, "spawned", True))
                ):
                    continue
                position = getattr(player, "position", None)
                if position is None:
                    position = (player.x, player.y, player.z)
                if zone.contains(position):
                    occupants[team].append(player)

            blue, green = len(occupants[TEAM1]), len(occupants[TEAM2])
            was_contested = self.zone_contested.get(zone.index, False)
            contested = blue > 0 and green > 0
            self.zone_contested[zone.index] = contested
            if contested and not was_contested:
                self._broadcast_localised("MULTIHILL_CONTESTED")
            if blue == green:
                continue
            claimant = TEAM1 if blue > green else TEAM2
            old_owner = self.zone_owner.get(zone.index)
            if claimant == old_owner:
                continue
            self.zone_owner[zone.index] = claimant
            self._send_zone(zone)

            # Recovered personal scoring distinguishes the first arrival from
            # later neutral claims.  Award the decisive participant only;
            # ordinary hold scoring remains the team's one-point clock.
            decisive = min(occupants[claimant], key=lambda p: int(getattr(p, "id", 0)))
            if old_owner is None:
                reason = int(C.SCORE_REASON.MH_FIRST_SCORE_REASON)
                points = int(CG.MH_SCORE_FIRST)
            else:
                reason = int(C.SCORE_REASON.MH_CLAIM_SCORE_REASON)
                points = int(CG.MH_SCORE_CLAIM)
            self._award_player_score(decisive, points, reason)
            self._announce_claim(decisive, claimant, old_owner)

    async def _award_team_ticks(self, now: float) -> None:
        interval = float(CG.MH_TEAM_SCORE_TICK_RATE)
        elapsed = max(0.0, now - self._last_score_at)
        ticks = int(elapsed / interval)
        if ticks <= 0:
            return
        self._last_score_at += ticks * interval
        changed = set()
        for zone in self.active_zones:
            owner = self.zone_owner.get(zone.index)
            if owner not in _PLAYABLE_TEAMS or self.zone_contested.get(zone.index):
                continue
            team = self.server.teams[owner]
            team.add_score(ticks * int(CG.MH_TEAM_SCORE_PER_TICK))
            changed.add(owner)
        for team_id in changed:
            team = self.server.teams[team_id]
            try:
                self.server.broadcast_set_score(
                    team,
                    reason=int(C.SCORE_REASON.MH_CONTROL_SCORE_REASON),
                )
            except TypeError:
                self.server.broadcast_set_score(team)
            if team.score >= self.score_limit:
                await self._end_by_score(team_id)
                break

    def _award_player_score(self, player, points: int, reason: int) -> None:
        from server.scoreboard import send_player_score

        player.score = int(getattr(player, "score", 0)) + int(points)
        send_player_score(self.server, player, reason=int(reason))

    def _broadcast_localised(self, string_id: str, parameters=()) -> None:
        from server.announcements import build_localised_overlay

        self.server.broadcast(build_localised_overlay(string_id, parameters))

    def _announce_claim(self, claimant, team: int, old_owner) -> None:
        from server.announcements import build_localised_overlay

        name = str(getattr(claimant, "name", f"Player {claimant.id}"))
        rows = {
            "you": build_localised_overlay("MULTIHILL_OCCUPIED_YOU", (name,)),
            "friendly": build_localised_overlay(
                "MULTIHILL_OCCUPIED_FRIENDLY", (name,)
            ),
            "enemy": build_localised_overlay("MULTIHILL_OCCUPIED_ENEMY", (name,)),
            "lost": build_localised_overlay("MULTIHILL_LOST"),
        }
        for player in tuple(getattr(self.server, "players", {}).values()):
            send = getattr(player, "send", None)
            if not callable(send):
                continue
            if player is claimant:
                send(rows["you"], reliable=True)
            elif int(getattr(player, "team", -1)) == int(team):
                send(rows["friendly"], reliable=True)
            elif old_owner in _PLAYABLE_TEAMS and int(
                getattr(player, "team", -1)
            ) == int(old_owner):
                send(rows["lost"], reliable=True)
            else:
                send(rows["enemy"], reliable=True)


__all__ = ["MultiHillMode"]
