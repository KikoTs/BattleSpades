"""Authoritative prefab placement shared by retail packets and bots."""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

import shared.constants as C
from shared.packet import BlockBuild, BlockBuildColored, PrefabComplete

from server import prefabs

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _PendingPrefab:
    """One validated prefab drained in bounded per-tick cell batches."""

    player: object
    name: str
    anchor: tuple[int, int, int]
    yaw: int
    action_loop: int
    cells: deque
    total_cells: int
    reservation: int | None
    placed: int = 0


class PrefabActionService:
    """Validate, expand, charge, commit, and replicate one prefab action.

    Thread/tick context: called synchronously on the gameplay thread after a
    packet or bot intent has been framed.  KV6 models are registry-cached after
    their first load.  Failures are atomic before VXL mutation; an unexpected
    per-cell VXL rejection is skipped without charging that cell.
    """

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server
        self._pending: deque[_PendingPrefab] = deque()
        # Lightweight domain tests without SimulationRuntime retain immediate
        # behavior; production always drains through ``tick``.
        self._deferred = hasattr(server, "simulation_runtime")

    @property
    def pending_count(self) -> int:
        """Return queued prefab actions, not individual cells."""

        return len(self._pending)

    def place_packet(self, player: "Player", packet) -> bool:
        """Translate ``BuildPrefabAction(30)`` into the public action API."""

        return self.place(
            player,
            name=str(getattr(packet, "prefab_name", "") or ""),
            position=getattr(packet, "position", None),
            yaw=int(getattr(packet, "prefab_yaw", 0)),
            pitch=int(getattr(packet, "prefab_pitch", 0)),
            roll=int(getattr(packet, "prefab_roll", 0)),
            color=getattr(packet, "color", None),
            loop_count=int(getattr(packet, "loop_count", self.server.loop_count)),
        )

    def place(
        self,
        player: "Player",
        *,
        name: str,
        position,
        yaw: int = 0,
        pitch: int = 0,
        roll: int = 0,
        color=None,
        loop_count: int | None = None,
        snap_to_surface: bool = False,
    ) -> bool:
        """Place one selected prefab through stock packet replication.

        ``snap_to_surface`` is reserved for server-owned bots, whose worker
        cannot know the KV6 footprint height.  Human packet coordinates remain
        byte-for-byte authoritative and are never adjusted.
        """

        if not self._authorized(player, name) or position is None:
            return False
        model = prefabs.get_registry().get(name)
        if model is None:
            return False
        try:
            anchor = tuple(int(round(float(value))) for value in position[:3])
        except (IndexError, TypeError, ValueError):
            return False
        if len(anchor) != 3 or not all(math.isfinite(float(value)) for value in anchor):
            return False

        yaw, pitch, roll = int(yaw) & 3, int(pitch) & 3, int(roll) & 3
        if snap_to_surface:
            anchor = self._surface_anchor(model, anchor, yaw, pitch, roll)
            if anchor is None:
                return False

        base_color = self._base_color(player, color)
        cells = prefabs.expand_prefab(
            model,
            anchor,
            yaw,
            pitch,
            roll,
            base_color=base_color,
        )
        if not cells:
            return False

        world = self.server.world_manager
        in_world = [
            ((int(x), int(y), int(z)), tuple(int(component) & 0xFF for component in rgb))
            for (x, y, z), rgb in cells
            if 0 <= int(x) < 512 and 0 <= int(y) < 512 and 0 <= int(z) <= 238
        ]
        if not in_world or not prefabs.touches_world(world, in_world):
            return False
        if prefabs.collides_with_player(in_world, self.server.players.values()):
            return False

        infinite = bool(
            getattr(self.server.teams.get(player.team), "infinite_blocks", False)
        )
        if not infinite and len(in_world) > int(getattr(player, "blocks", 0)):
            return False

        footprint = tuple(position for position, _rgb in in_world)
        construction = getattr(self.server, "construction", None)
        reservation = None
        if construction is not None:
            reservation, reason = construction.reserve_construction(
                int(player.id), int(player.team), footprint
            )
            if reservation is None:
                logger.debug(
                    "Prefab rejected by construction safety: %s player=%s reason=%s",
                    name,
                    getattr(player, "name", player.id),
                    reason,
                )
                return False

        action_loop = max(
            0,
            int(self.server.loop_count if loop_count is None else loop_count),
        )
        if self._deferred:
            return self._enqueue(
                player,
                name=name,
                anchor=anchor,
                yaw=yaw,
                cells=in_world,
                action_loop=action_loop,
                reservation=reservation,
                infinite=infinite,
            )
        try:
            placed, new_cells = self._commit(
                player,
                in_world,
                action_loop=action_loop,
            )
        finally:
            if construction is not None:
                construction.release(reservation)

        if new_cells and not infinite:
            player.blocks = max(0, int(player.blocks) - new_cells)

        complete = PrefabComplete()
        player.send(bytes(complete.generate()), reliable=True)
        logger.info(
            "PREFAB %s by %s at %s yaw=%d: placed %d/%d blocks",
            name,
            getattr(player, "name", player.id),
            anchor,
            yaw,
            placed,
            len(in_world),
        )
        return placed > 0

    def tick(self) -> int:
        """Commit a bounded number of queued cells after player physics."""

        if not self._pending:
            return 0
        budget = max(
            1,
            min(
                128,
                int(getattr(self.server.config, "prefab_cell_batch_limit", 16)),
            ),
        )
        committed = 0
        while self._pending and committed < budget:
            pending = self._pending[0]
            player = pending.player
            current = self.server.players.get(int(player.id))
            if current is not player:
                self._pending.popleft()
                self._cancel(pending)
                continue
            coordinate, color, charged = pending.cells.popleft()
            was_solid = bool(self.server.world_manager.get_solid(*coordinate))
            if self._commit_cell(
                player,
                coordinate,
                color,
                action_loop=pending.action_loop,
            ):
                pending.placed += 1
                if charged and was_solid:
                    player.blocks += 1
            elif charged:
                player.blocks += 1
            committed += 1
            if not pending.cells:
                self._pending.popleft()
                self._finish(pending)
        return committed

    def cancel_owner(self, owner_id: int) -> int:
        """Cancel queued work before a compact player id can be reused."""

        kept: deque[_PendingPrefab] = deque()
        cancelled = 0
        while self._pending:
            pending = self._pending.popleft()
            if int(pending.player.id) == int(owner_id):
                self._cancel(pending)
                cancelled += 1
            else:
                kept.append(pending)
        self._pending = kept
        return cancelled

    def cancel_all(self) -> None:
        """Cancel every queued prefab during round/map teardown."""

        while self._pending:
            self._cancel(self._pending.popleft())

    def _enqueue(
        self,
        player: "Player",
        *,
        name: str,
        anchor: tuple[int, int, int],
        yaw: int,
        cells,
        action_loop: int,
        reservation: int | None,
        infinite: bool,
    ) -> bool:
        limit = max(
            1,
            min(128, int(getattr(self.server.config, "prefab_queue_limit", 32))),
        )
        if len(self._pending) >= limit:
            construction = getattr(self.server, "construction", None)
            if construction is not None:
                construction.release(reservation)
            return False
        queued_cells = deque()
        reserved_blocks = 0
        world = self.server.world_manager
        for coordinate, color in cells:
            charged = not infinite and not world.get_solid(*coordinate)
            queued_cells.append((coordinate, color, charged))
            reserved_blocks += int(charged)
        if reserved_blocks > int(player.blocks):
            construction = getattr(self.server, "construction", None)
            if construction is not None:
                construction.release(reservation)
            return False
        player.blocks -= reserved_blocks
        self._pending.append(
            _PendingPrefab(
                player=player,
                name=name,
                anchor=anchor,
                yaw=yaw,
                action_loop=action_loop,
                cells=queued_cells,
                total_cells=len(queued_cells),
                reservation=reservation,
            )
        )
        return True

    def _cancel(self, pending: _PendingPrefab) -> None:
        refund = sum(1 for _coordinate, _color, charged in pending.cells if charged)
        if refund:
            pending.player.blocks += refund
        construction = getattr(self.server, "construction", None)
        if construction is not None:
            construction.release(pending.reservation)

    def _finish(self, pending: _PendingPrefab) -> None:
        complete = PrefabComplete()
        pending.player.send(bytes(complete.generate()), reliable=True)
        construction = getattr(self.server, "construction", None)
        if construction is not None:
            construction.release(pending.reservation)
        logger.info(
            "PREFAB %s by %s at %s yaw=%d: placed %d/%d blocks",
            pending.name,
            getattr(pending.player, "name", pending.player.id),
            pending.anchor,
            pending.yaw,
            pending.placed,
            pending.total_cells,
        )

    def _authorized(self, player: "Player", name: str) -> bool:
        """Require alive state, a native prefab tool, and selected geometry.

        BuildPrefabAction(30) is shared by ordinary tool 23, Zombie tool 28,
        and the UGC prefab tools.  The held raw tool still has to match the
        committed loadout; accepting the family here does not weaken the
        active-life authorization boundary.
        """

        if (
            not name
            or not bool(getattr(player, "alive", False))
            or not bool(getattr(player, "spawned", False))
        ):
            return False
        loadout = {int(value) for value in (getattr(player, "loadout", ()) or ())}
        tool = int(getattr(player, "tool", -1))
        prefab_tools = {int(value) for value in C.PREFAB_TOOLS}
        if tool not in prefab_tools or tool not in loadout:
            return False
        if not bool(getattr(player, "tool_is_raw", False)):
            return False
        return bool(prefabs.prefab_allowed(player, name))

    def _base_color(self, player: "Player", color) -> tuple[int, int, int]:
        try:
            values = tuple(int(component) & 0xFF for component in color[:3])
        except (TypeError, ValueError):
            values = ()
        if len(values) == 3:
            return values
        team = self.server.teams.get(player.team)
        return tuple(int(value) & 0xFF for value in getattr(team, "color", (128, 128, 128)))

    def _surface_anchor(
        self,
        model,
        anchor: tuple[int, int, int],
        yaw: int,
        pitch: int,
        roll: int,
    ) -> tuple[int, int, int] | None:
        """Move a bot prefab so its lowest rotated voxel rests on terrain."""

        try:
            offsets = [
                prefabs.rotate_point(x, y, z, yaw, pitch, roll)
                for x, y, z, _r, _g, _b in model.get_points()
            ]
            max_z = max(point[2] for point in offsets)
            surface_z = int(self.server.world_manager.get_height(anchor[0], anchor[1]))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None
        z = surface_z - int(max_z) - 1
        if not 0 <= z <= 238:
            return None
        return anchor[0], anchor[1], z

    def _commit(
        self,
        player: "Player",
        cells,
        *,
        action_loop: int,
    ) -> tuple[int, int]:
        """Commit validated cells and emit the two proven observer paths."""

        placed = 0
        new_cells = 0
        world = self.server.world_manager
        for (x, y, z), color in cells:
            was_solid = bool(world.get_solid(x, y, z))
            if not self._commit_cell(
                player, (x, y, z), color, action_loop=action_loop
            ):
                continue
            if not was_solid:
                new_cells += 1
            placed += 1
        return placed, new_cells

    def _commit_cell(
        self,
        player: "Player",
        coordinate: tuple[int, int, int],
        color: tuple[int, int, int],
        *,
        action_loop: int,
    ) -> bool:
        """Commit and replicate one cell from an already validated footprint."""

        x, y, z = coordinate
        try:
            if not self.server.world_manager.set_block(
                x, y, z, solid=True, color=color
            ):
                return False
        except (AttributeError, RuntimeError, TypeError, ValueError):
            logger.exception("Prefab VXL commit failed at %s", coordinate)
            return False

        observer = BlockBuildColored()
        observer.loop_count = action_loop
        observer.player_id = int(player.id)
        observer.x, observer.y, observer.z = x, y, z
        observer.color = (
            (int(color[0]) << 16) | (int(color[1]) << 8) | int(color[2])
        )
        self.server.broadcast(
            bytes(observer.generate()), reliable=True, exclude=player
        )

        # Native builders debit/finalize only their ordinary BlockBuild echo.
        # Colored packet 33 is the stable remote/rejoin path.
        owner = BlockBuild()
        owner.loop_count = action_loop
        owner.player_id = int(player.id)
        owner.x, owner.y, owner.z = x, y, z
        owner.block_type = 0
        player.send(bytes(owner.generate()), reliable=True)
        return True
