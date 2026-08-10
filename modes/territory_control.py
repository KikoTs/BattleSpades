"""Retail-style Territory Control objective mode."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL

from .base_mode import BaseMode
from .objective_zones import ObjectiveZone, around, from_map_zone, minimap_zone_packet


logger = logging.getLogger(__name__)

_PLAYABLE_TEAMS = (TEAM1, TEAM2)
_NEUTRAL_COLOR = (255, 255, 255)
_BASE_CAPTURE_PER_SECOND = 0.05
_FALLBACK_RADIUS = 15.0


def _configured_rule(server, key: str, rule: str, fallback):
    resolver = getattr(getattr(server, "config", None), "mode_rule", None)
    if callable(resolver):
        try:
            value = resolver("tc", key, rule)
            if value is not None and value is not False:
                return value
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    overlay = getattr(getattr(server, "config", None), "mode_settings", {}).get(
        "tc", {}
    )
    return overlay.get(key, fallback)


@dataclass(slots=True)
class Territory:
    """One active native TC base and its continuous capture state."""

    zone: ObjectiveZone
    owner: int = TEAM_NEUTRAL
    attacker: int = TEAM_NEUTRAL
    progress: float = 0.5
    active: bool = True
    contested: bool = False
    occupants: dict[int, set[int]] = field(
        default_factory=lambda: {TEAM1: set(), TEAM2: set()}
    )
    last_non_neutral_owner: int = TEAM_NEUTRAL


class TerritoryControlMode(BaseMode):
    """Capture a line of territories until one team owns every active base.

    Packet 106 is the native TC HUD state machine.  Progress is continuous:
    ``0`` is Blue, ``0.5`` neutral, and ``1`` Green.  Packet 43 supplies the
    matching lettered minimap/billboard zones.
    """

    name = "Territory Control"
    description = "Capture and hold all active territories."
    mode_code = "tc"

    def __init__(self, server) -> None:
        super().__init__(server)
        data = mode_data.get(self.mode_code)
        self.score_limit = int(data.default_score_limit)
        resolve_time = getattr(getattr(server, "config", None), "configured_time_limit", None)
        self.time_limit = (
            float(resolve_time(self.mode_code, data.default_time_limit))
            if callable(resolve_time)
            else float(data.default_time_limit)
        )
        self.max_active_bases = max(2, min(5, int(_configured_rule(
            server,
            "max_active_bases",
            "RULE_TC_MAX_ACTIVE_BASES",
            CG.TC_DEFAULT_BASE_COUNT_TO_USE,
        ))))
        self.capture_multiplier = max(0.1, float(_configured_rule(
            server, "capture_rate", "RULE_CAPTURE_RATE", 1.0
        )))
        self.territories: list[Territory] = []
        self._next_capture_at = 0.0
        self._next_personal_score_at = 0.0

    async def on_mode_start(self) -> None:
        await super().on_mode_start()
        for team in self.server.teams.values():
            team.reset()
        zones = self._select_active_zones(self._build_zones())
        self.territories = self._initialise_territories(zones)
        now = time.time()
        self._next_capture_at = now
        self._next_personal_score_at = now + float(CG.TC_SCORE_OCCUPY_INTERVAL)
        for territory in self.territories:
            self._send_zone(territory)
            self._send_state(territory, int(C.TC_INITIAL_INFO))
            self._send_state(territory, int(C.TC_BASE_ACTIVATE))
        self._refresh_team_scores()
        logger.info(
            "Territory Control started with %d active bases at %.2fx capture rate",
            len(self.territories),
            self.capture_multiplier,
        )

    async def deactivate(self) -> None:
        for territory in self.territories:
            self._send_state(territory, int(C.TC_BASE_DEACTIVATE))
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        await super().on_tick(tick)
        if self.ended:
            return
        now = time.time()
        interval = float(CG.TC_CAPTURE_TICK_RATE)
        if now >= self._next_capture_at:
            elapsed = max(interval, now - self._next_capture_at + interval)
            self._next_capture_at = now + interval
            await self._capture_tick(min(elapsed, interval * 3.0))
        if not self.ended and now >= self._next_personal_score_at:
            periods = max(1, int(
                (now - self._next_personal_score_at)
                / float(CG.TC_SCORE_OCCUPY_INTERVAL)
            ) + 1)
            self._next_personal_score_at += (
                periods * float(CG.TC_SCORE_OCCUPY_INTERVAL)
            )
            self._award_presence_scores(periods)

    def reveal_to(self, connection) -> None:
        for territory in self.territories:
            self._send_zone(territory, connection=connection)
            self._send_state(
                territory, int(C.TC_INITIAL_INFO), connection=connection
            )
            if territory.active:
                self._send_state(
                    territory, int(C.TC_BASE_ACTIVATE), connection=connection
                )

    async def on_player_kill(self, killer, victim, kill_type: int) -> None:
        if (
            self.ended
            or killer is victim
            or int(getattr(killer, "team", -1)) not in _PLAYABLE_TEAMS
            or int(getattr(victim, "team", -1)) not in _PLAYABLE_TEAMS
            or int(killer.team) == int(victim.team)
        ):
            return
        killer_zone = self._territory_at(killer)
        victim_zone = self._territory_at(victim)
        if killer_zone is not None and killer_zone.owner == int(killer.team):
            self._award_player(
                killer,
                int(CG.TC_SCORE_KILL_KILLERINHILL),
                int(C.SCORE_REASON.TC_DEFEND_SCORE_REASON),
            )
        elif victim_zone is not None and victim_zone.owner != int(killer.team):
            self._award_player(
                killer,
                int(CG.TC_SCORE_KILL_VICTIMINHILL),
                int(C.SCORE_REASON.TC_ASSAULT_SCORE_REASON),
            )

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
            return self._sort_along_team_axis(zones)

        first, second = self._team_anchors()
        dry = getattr(wm, "dry_ground_anchor", None)
        zones = []
        for index, fraction in enumerate((0.15, 0.325, 0.5, 0.675, 0.85)):
            x = first[0] + (second[0] - first[0]) * fraction
            y = first[1] + (second[1] - first[1]) * fraction
            center = dry(x, y, 48) if callable(dry) else (x, y, 58.0)
            zones.append(around(
                index,
                center,
                radius_xy=_FALLBACK_RADIUS,
                height_above=8.0,
                depth_below=10.0,
            ))
        logger.warning(
            "Map %s has no complete TC objective sidecar; using dry corridor bases",
            getattr(wm, "map_name", "<unknown>"),
        )
        return zones

    def _team_anchors(self):
        wm = getattr(self.server, "world_manager", None)
        reader = getattr(wm, "team_base_anchor", None)
        if callable(reader):
            return (
                tuple(float(value) for value in reader(TEAM1)),
                tuple(float(value) for value in reader(TEAM2)),
            )
        return (64.0, 256.0, 58.0), (448.0, 256.0, 58.0)

    def _sort_along_team_axis(self, zones: list[ObjectiveZone]) -> list[ObjectiveZone]:
        first, second = self._team_anchors()
        dx, dy = second[0] - first[0], second[1] - first[1]
        if abs(dx) + abs(dy) < 1e-6:
            return sorted(zones, key=lambda zone: zone.index)
        return sorted(
            zones,
            key=lambda zone: (
                (zone.center[0] - first[0]) * dx
                + (zone.center[1] - first[1]) * dy,
                zone.index,
            ),
        )

    def _select_active_zones(self, zones: list[ObjectiveZone]) -> list[ObjectiveZone]:
        if len(zones) <= self.max_active_bases:
            selected = zones
        else:
            count = self.max_active_bases
            indexes = [
                int(round(index * (len(zones) - 1) / float(count - 1)))
                for index in range(count)
            ]
            selected = [zones[index] for index in indexes]
        return [
            ObjectiveZone(index, zone.bounds, zone.center)
            for index, zone in enumerate(selected[:10])
        ]

    def _initialise_territories(
        self, zones: list[ObjectiveZone]
    ) -> list[Territory]:
        count = len(zones)
        result = []
        for index, zone in enumerate(zones):
            if index < count // 2:
                owner, progress = TEAM1, 0.0
            elif index > (count - 1) // 2:
                owner, progress = TEAM2, 1.0
            else:
                owner, progress = TEAM_NEUTRAL, 0.5
            result.append(Territory(
                zone=zone,
                owner=owner,
                progress=progress,
                last_non_neutral_owner=owner,
            ))
        return result

    async def _capture_tick(self, elapsed: float) -> None:
        changed_score = False
        for territory in self.territories:
            occupants = self._occupants(territory.zone)
            self._send_presence_transitions(territory, occupants)
            blue = len(occupants[TEAM1])
            green = len(occupants[TEAM2])
            contested = blue > 0 and green > 0
            if contested != territory.contested:
                territory.contested = contested
                self._send_state(
                    territory,
                    int(C.TC_BASE_CONTENDED if contested else C.TC_BASE_UNCONTENDED),
                )
            net = green - blue
            territory.attacker = (
                TEAM2 if net > 0 else TEAM1 if net < 0 else TEAM_NEUTRAL
            )
            if net == 0:
                continue

            previous_progress = territory.progress
            previous_owner = territory.owner
            territory.progress = min(1.0, max(
                0.0,
                territory.progress
                + net * _BASE_CAPTURE_PER_SECOND * self.capture_multiplier * elapsed,
            ))
            if territory.progress <= 0.0:
                territory.owner = TEAM1
            elif territory.progress >= 1.0:
                territory.owner = TEAM2
            elif (
                previous_owner in _PLAYABLE_TEAMS
                and (previous_progress - 0.5) * (territory.progress - 0.5) <= 0.0
            ):
                territory.last_non_neutral_owner = previous_owner
                territory.owner = TEAM_NEUTRAL

            if territory.owner != previous_owner:
                changed_score = True
                self._send_zone(territory)
                if territory.owner in _PLAYABLE_TEAMS:
                    decisive = min(
                        occupants[territory.owner],
                        key=lambda player: int(getattr(player, "id", 0)),
                    )
                    was_enemy = (
                        territory.last_non_neutral_owner in _PLAYABLE_TEAMS
                        and territory.last_non_neutral_owner != territory.owner
                    )
                    self._award_player(
                        decisive,
                        int(CG.TC_SCORE_CONTROL if was_enemy else CG.TC_SCORE_CLAIM),
                        int(
                            C.SCORE_REASON.TC_CONTROL_SCORE_REASON
                            if was_enemy
                            else C.SCORE_REASON.TC_CLAIM_SCORE_REASON
                        ),
                    )
                    territory.last_non_neutral_owner = territory.owner
            self._send_state(territory, int(C.TC_BASE_CAPTURE_UPDATE))

        if changed_score:
            self._refresh_team_scores()
            for team_id in _PLAYABLE_TEAMS:
                if self.server.teams[team_id].score >= len(self.territories):
                    await self._end_by_score(team_id)
                    break

    def _occupants(self, zone: ObjectiveZone) -> dict[int, list]:
        result = {TEAM1: [], TEAM2: []}
        for player in tuple(getattr(self.server, "players", {}).values()):
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
                result[team].append(player)
        return result

    def _send_presence_transitions(self, territory: Territory, occupants) -> None:
        for team in _PLAYABLE_TEAMS:
            current = {int(player.id) for player in occupants[team]}
            entered = current - territory.occupants[team]
            left = territory.occupants[team] - current
            for player_id in entered:
                player = getattr(self.server, "players", {}).get(player_id)
                if player is not None:
                    self._send_state(
                        territory, int(C.TC_BASE_ENTERING), connection=player
                    )
            for player_id in left:
                player = getattr(self.server, "players", {}).get(player_id)
                if player is not None:
                    self._send_state(
                        territory, int(C.TC_BASE_LEAVING), connection=player
                    )
            territory.occupants[team] = current

    def _award_presence_scores(self, periods: int) -> None:
        for territory in self.territories:
            if territory.contested:
                for team in _PLAYABLE_TEAMS:
                    for player_id in territory.occupants[team]:
                        player = getattr(self.server, "players", {}).get(player_id)
                        if player is not None:
                            self._award_player(
                                player,
                                periods * int(CG.TC_SCORE_CONTEND_HILL),
                                int(C.SCORE_REASON.TC_CONTEND_SCORE_REASON),
                            )
                continue
            if territory.owner not in _PLAYABLE_TEAMS:
                continue
            for player_id in territory.occupants[territory.owner]:
                player = getattr(self.server, "players", {}).get(player_id)
                if player is not None:
                    self._award_player(
                        player,
                        periods * int(CG.TC_SCORE_OCCUPY_PERHILL),
                        int(C.SCORE_REASON.TC_OCCUPY_SCORE_REASON),
                    )

    def _refresh_team_scores(self) -> None:
        for team_id in _PLAYABLE_TEAMS:
            team = self.server.teams[team_id]
            team.score = sum(
                1 for territory in self.territories if territory.owner == team_id
            )
            try:
                self.server.broadcast_set_score(
                    team, reason=int(C.SCORE_REASON.TC_CONTROL_SCORE_REASON)
                )
            except TypeError:
                self.server.broadcast_set_score(team)

    def _send_zone(self, territory: Territory, connection=None) -> None:
        color = (
            self.server.teams[territory.owner].color
            if territory.owner in _PLAYABLE_TEAMS
            else _NEUTRAL_COLOR
        )
        packet = minimap_zone_packet(
            territory.zone,
            color=color,
            icon_id=int(CG.ZONE_ICON_TERRITORY_A) + territory.zone.index,
            visible_team=TEAM_NEUTRAL,
        )
        self._send_packet(packet, connection)

    def _send_state(self, territory: Territory, action: int, connection=None) -> None:
        from shared.packet import TerritoryBaseState

        packet = TerritoryBaseState()
        packet.base_index = int(territory.zone.index)
        packet.action = int(action)
        packet.controlled_by = int(territory.owner)
        packet.attacked_by = int(territory.attacker)
        packet.capture_amount = float(territory.progress)
        self._send_packet(packet, connection)

    def _send_packet(self, packet, connection=None) -> None:
        data = bytes(packet.generate())
        if connection is None:
            self.server.broadcast(data, reliable=True)
        else:
            send = getattr(connection, "send", None)
            if callable(send):
                send(data, reliable=True)

    def _territory_at(self, player) -> Territory | None:
        position = getattr(player, "position", None)
        if position is None:
            position = (player.x, player.y, player.z)
        return next(
            (territory for territory in self.territories if territory.zone.contains(position)),
            None,
        )

    def _award_player(self, player, points: int, reason: int) -> None:
        from server.scoreboard import send_player_score

        player.score = int(getattr(player, "score", 0)) + int(points)
        send_player_score(self.server, player, reason=int(reason))


__all__ = ["Territory", "TerritoryControlMode"]
