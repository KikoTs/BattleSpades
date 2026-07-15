"""Bounded construction safety and team reservation service.

The service owns *intent-level* reservations only.  It never mutates VXL and
therefore cannot bypass :class:`CombatSystem` or :class:`PrefabActionService`.
Human packet handlers and bot actions use those same authoritative domain
operations after this inexpensive conflict/safety gate succeeds.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Iterable, TYPE_CHECKING

import shared.constants as C

from server.game_constants import TEAM1, TEAM2

if TYPE_CHECKING:
    from server.main import BattleSpadesServer


Cell = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class ConstructionReservation:
    """One expiring construction footprint or friendly movement corridor."""

    token: int
    owner_id: int
    team: int
    cells: frozenset[Cell]
    expires_at: float
    kind: str


class ConstructionSafetyService:
    """Protect spawns/objectives and coordinate bounded team construction.

    Thread/tick context: all methods run synchronously on the gameplay thread.
    The service performs only bounded set intersection and a few live voxel
    probes.  It owns no timers or background work; expired records are removed
    lazily whenever the API is used.
    """

    MAX_RESERVATIONS = 256
    MAX_CELLS_PER_RESERVATION = 2048
    DEFAULT_CONSTRUCTION_TTL = 1.5
    DEFAULT_PATH_TTL = 0.6

    def __init__(
        self,
        server: "BattleSpadesServer",
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.server = server
        self._clock = clock
        self._next_token = 1
        self._reservations: dict[int, ConstructionReservation] = {}
        self._path_token_by_owner: dict[int, int] = {}

    @property
    def active_count(self) -> int:
        """Return the number of non-expired reservations."""

        self._expire()
        return len(self._reservations)

    def clear(self) -> None:
        """Discard all transient reservations during map/round teardown."""

        self._reservations.clear()
        self._path_token_by_owner.clear()

    def release(self, token: int | None) -> None:
        """Release a previously returned reservation token, if it exists."""

        if token is None:
            return
        reservation = self._reservations.pop(int(token), None)
        if reservation is not None and reservation.kind == "path":
            if self._path_token_by_owner.get(reservation.owner_id) == reservation.token:
                self._path_token_by_owner.pop(reservation.owner_id, None)

    def reserve_path(
        self,
        owner_id: int,
        team: int,
        cells: Iterable[Cell],
        *,
        ttl: float = DEFAULT_PATH_TTL,
    ) -> int | None:
        """Replace one bot's short-lived friendly movement corridor.

        Corridors are advisory: enemy construction is not blocked by private
        friendly route knowledge, while same-team builders may not overlap a
        teammate's immediate route.  Replacing by owner keeps the collection
        bounded even when motors update at 60 Hz.
        """

        old = self._path_token_by_owner.pop(int(owner_id), None)
        self.release(old)
        normalized = self._normalize_cells(cells)
        if not normalized:
            return None
        token = self._store(
            int(owner_id), int(team), normalized, max(0.05, float(ttl)), "path"
        )
        if token is not None:
            self._path_token_by_owner[int(owner_id)] = token
        return token

    def reserve_construction(
        self,
        owner_id: int,
        team: int,
        cells: Iterable[Cell],
        *,
        ttl: float = DEFAULT_CONSTRUCTION_TTL,
    ) -> tuple[int | None, str]:
        """Validate and reserve a proposed build footprint.

        Returns ``(token, "")`` on success and ``(None, reason)`` on failure.
        A successful token expires automatically if the downstream gameplay
        operation is queued or abandoned.
        """

        normalized = self._normalize_cells(cells)
        if not normalized:
            return None, "empty or oversized footprint"
        reason = self.rejection_reason(int(owner_id), int(team), normalized)
        if reason:
            return None, reason
        token = self._store(
            int(owner_id),
            int(team),
            normalized,
            max(0.05, float(ttl)),
            "construction",
        )
        if token is None:
            return None, "reservation capacity reached"
        return token, ""

    def rejection_reason(
        self,
        owner_id: int,
        team: int,
        cells: Iterable[Cell],
    ) -> str:
        """Return a stable reason string when ``cells`` are unsafe."""

        proposed = self._normalize_cells(cells)
        if not proposed:
            return "empty or oversized footprint"
        self._expire()
        if self._overlaps_living_player(proposed):
            return "player body overlap"
        if any(self._protected(cell) for cell in proposed):
            return "spawn or objective zone"
        for reservation in self._reservations.values():
            if reservation.owner_id == int(owner_id):
                continue
            if reservation.kind == "path" and reservation.team != int(team):
                continue
            if not proposed.isdisjoint(reservation.cells):
                return "reserved construction or friendly path"
        director = getattr(self.server, "bots", None)
        path_provider = getattr(director, "friendly_path_cells", None)
        if callable(path_provider):
            friendly_paths = path_provider(int(team), exclude_owner=int(owner_id))
            if not proposed.isdisjoint(friendly_paths):
                return "reserved construction or friendly path"
        if self._seals_friendly_exit(int(team), proposed):
            return "sole friendly exit"
        return ""

    def _overlaps_living_player(self, proposed: frozenset[Cell]) -> bool:
        """Reject voxels intersecting any authoritative living body volume."""

        for player in tuple(getattr(self.server, "players", {}).values()):
            if not bool(getattr(player, "alive", False)) or not bool(
                getattr(player, "spawned", False)
            ):
                continue
            try:
                player_x = float(player.x)
                player_y = float(player.y)
                body_z = int(math.floor(float(player.z)))
            except (AttributeError, TypeError, ValueError):
                # Incomplete player state must not authorize construction.
                return True
            x_cells = range(
                int(math.floor(player_x - 0.45)),
                int(math.floor(player_x + 0.45)) + 1,
            )
            y_cells = range(
                int(math.floor(player_y - 0.45)),
                int(math.floor(player_y + 0.45)) + 1,
            )
            if any(
                (x, y, z) in proposed
                for x in x_cells
                for y in y_cells
                for z in (body_z, body_z + 1)
            ):
                return True
        return False

    def _store(
        self,
        owner_id: int,
        team: int,
        cells: frozenset[Cell],
        ttl: float,
        kind: str,
    ) -> int | None:
        self._expire()
        if len(self._reservations) >= self.MAX_RESERVATIONS:
            return None
        token = self._next_token
        self._next_token += 1
        self._reservations[token] = ConstructionReservation(
            token,
            owner_id,
            team,
            cells,
            self._clock() + ttl,
            kind,
        )
        return token

    def _expire(self) -> None:
        now = self._clock()
        expired = [
            token
            for token, reservation in self._reservations.items()
            if reservation.expires_at <= now
        ]
        for token in expired:
            self.release(token)

    def _normalize_cells(self, cells: Iterable[Cell]) -> frozenset[Cell]:
        result: set[Cell] = set()
        try:
            for raw in cells:
                x, y, z = (int(value) for value in raw)
                if not (
                    0 <= x < int(C.MAP_X)
                    and 0 <= y < int(C.MAP_Y)
                    and 0 <= z <= 238
                ):
                    return frozenset()
                result.add((x, y, z))
                if len(result) > self.MAX_CELLS_PER_RESERVATION:
                    return frozenset()
        except (TypeError, ValueError):
            return frozenset()
        return frozenset(result)

    def _protected(self, cell: Cell) -> bool:
        x, y, _z = cell
        mode = getattr(self.server, "mode", None)
        bounds_by_team = getattr(mode, "base_bounds", None)
        if isinstance(bounds_by_team, dict):
            for bounds in bounds_by_team.values():
                if bounds is None or len(bounds) < 4:
                    continue
                x0, x1, y0, y1 = (int(value) for value in bounds[:4])
                if x0 <= x <= x1 and y0 <= y <= y1:
                    return True

        world = getattr(self.server, "world_manager", None)
        metadata = getattr(world, "map_metadata", None)
        if metadata is not None:
            for zones in (
                getattr(metadata, "spawn_zones", {}).values(),
                getattr(metadata, "base_zones", {}).values(),
            ):
                for team_zones in zones:
                    for zone in team_zones:
                        x0, x1, y0, y1 = zone.xy_bounds()
                        if x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1:
                            return True

        for attribute in ("base_positions", "intel_positions"):
            positions = getattr(mode, attribute, None)
            if not isinstance(positions, dict):
                continue
            radius = 5 if attribute == "base_positions" else 2
            for position in positions.values():
                if position is not None and self._within_xy(x, y, position, radius):
                    return True
        return False

    @staticmethod
    def _within_xy(x: int, y: int, position, radius: int) -> bool:
        try:
            dx = float(x) + 0.5 - float(position[0])
            dy = float(y) + 0.5 - float(position[1])
        except (IndexError, TypeError, ValueError):
            return False
        return dx * dx + dy * dy <= float(radius * radius)

    def _seals_friendly_exit(self, team: int, proposed: frozenset[Cell]) -> bool:
        """Reject the final body-height wall around a living teammate."""

        world = getattr(self.server, "world_manager", None)
        players = getattr(self.server, "players", {})
        if world is None:
            return True
        min_x = min(cell[0] for cell in proposed) - 2
        max_x = max(cell[0] for cell in proposed) + 2
        min_y = min(cell[1] for cell in proposed) - 2
        max_y = max(cell[1] for cell in proposed) + 2
        for player in tuple(players.values()):
            if (
                int(getattr(player, "team", -1)) != team
                or not bool(getattr(player, "alive", False))
                or not bool(getattr(player, "spawned", False))
            ):
                continue
            px, py = int(math.floor(float(player.x))), int(math.floor(float(player.y)))
            if not (min_x <= px <= max_x and min_y <= py <= max_y):
                continue
            body_z = int(math.floor(float(player.z)))
            blocked_sides = 0
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = px + dx, py + dy
                if any(
                    (nx, ny, body_z + dz) in proposed
                    or bool(world.get_solid(nx, ny, body_z + dz))
                    for dz in (0, 1)
                ):
                    blocked_sides += 1
            if blocked_sides == 4:
                return True
        return False
