"""Bounded canonical terrain repair for clients already inside GameScene.

Normal block packets are reliable and remain the primary replication path.
The regular lane repairs rejected client prediction. A separate, faster
collapse-confirmation lane handles the one successful mutation that cannot be
represented exactly by its original packet: the server removes an entire
unsupported component while the checked Damage packet asks each client to
derive that component from its local BlockManager state. Exact confirmations
remove rare stale geometry without replaying native collapse work.

The service never serializes the whole map on the gameplay thread.
"""

from __future__ import annotations

from collections import OrderedDict, deque
import logging
from typing import Iterable, TYPE_CHECKING

import shared.constants as C
from shared.packet import BlockBuildColored, Damage

if TYPE_CHECKING:
    from .main import BattleSpadesServer

logger = logging.getLogger(__name__)

Cell = tuple[int, int, int]


class TerrainRepairService:
    """Reassert recent canonical voxels with bounded, proven native packets.

    ``record_cells`` is called explicitly by a gameplay validator for a
    rejected/predicted footprint. ``tick`` runs on the gameplay thread after
    committed world mutations. Cells are de-duplicated and read from the
    authoritative VXL only when sent, so a later edit can never be overwritten
    by stale queued packet data.
    """

    _BLOCK_KILL_DAMAGE = 31.75

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server
        self._pending: OrderedDict[Cell, int] = OrderedDict()
        self._collapse_pending: OrderedDict[Cell, int] = OrderedDict()

    @property
    def pending_count(self) -> int:
        """Return the number of unique canonical cells awaiting repair."""

        return len(self._pending) + len(self._collapse_pending)

    def reset(self) -> None:
        """Discard delayed work when a world is replaced or the server stops."""

        self._pending.clear()
        self._collapse_pending.clear()

    @staticmethod
    def _surface_first(cells: Iterable[Cell]) -> tuple[Cell, ...]:
        """Order one or more collapsed components outside-in.

        The queue is intentionally finite. Clearing the visible shell first
        hides a partial confirmation while the remaining interior cells drain.
        A six-neighbour breadth-first walk then peels successive shells in
        deterministic coordinate order. This helper performs only O(n) voxel
        membership work on the gameplay thread; no VXL raycasts are involved.
        """

        unique: set[Cell] = set()
        for raw_cell in cells:
            try:
                if len(raw_cell) != 3:
                    continue
                unique.add(tuple(int(value) for value in raw_cell))
            except (TypeError, ValueError):
                continue
        if not unique:
            return ()

        neighbours = (
            (-1, 0, 0),
            (1, 0, 0),
            (0, -1, 0),
            (0, 1, 0),
            (0, 0, -1),
            (0, 0, 1),
        )
        boundary = sorted(
            cell
            for cell in unique
            if any(
                (cell[0] + dx, cell[1] + dy, cell[2] + dz) not in unique
                for dx, dy, dz in neighbours
            )
        )
        queue = deque(boundary)
        seen = set(boundary)
        ordered: list[Cell] = []
        while queue:
            cell = queue.popleft()
            ordered.append(cell)
            for dx, dy, dz in neighbours:
                neighbour = (
                    cell[0] + dx,
                    cell[1] + dy,
                    cell[2] + dz,
                )
                if neighbour in unique and neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        return tuple(ordered)

    def _enforce_limit(self) -> None:
        """Bound both lanes while preserving collapse surface confirmations."""

        limit = max(
            64,
            int(getattr(self.server.config, "terrain_repair_queue_limit", 8192)),
        )
        while self.pending_count > limit:
            if self._pending:
                self._pending.popitem(last=False)
            else:
                # Collapse cells are inserted surface-first. Drop the deepest
                # unsent interior before any visible-shell confirmation.
                self._collapse_pending.popitem(last=True)
            self.server.metrics.dropped_terrain_repairs += 1

    def record_collapse_cells(self, cells: Iterable[Cell]) -> None:
        """Queue exact-air confirmations for a committed native collapse.

        This method runs after :class:`WorldManager` has removed the component.
        It does not replace the original checked Damage packet: clients that
        derived the collapse keep their normal falling animation, while a
        client with divergent local topology later receives exact type-6,
        ``chunk_check=0`` removals. Rebuilt cells are discarded at send time
        because their ordinary reliable build packet is authoritative.
        """

        config = self.server.config
        if not bool(getattr(config, "terrain_repair_enabled", True)):
            return

        delay = max(
            1,
            int(getattr(config, "terrain_collapse_repair_delay_ticks", 18)),
        )
        due_tick = int(self.server.loop_count) + delay
        world = self.server.world_manager
        for cell in self._surface_first(cells):
            if not world._valid_block_position(*cell):
                continue
            self._pending.pop(cell, None)
            self._collapse_pending.pop(cell, None)
            self._collapse_pending[cell] = due_tick
            self._enforce_limit()

        self.server.metrics.terrain_repair_queue_peak = max(
            self.server.metrics.terrain_repair_queue_peak,
            self.pending_count,
        )

    def record_cells(self, cells: Iterable[Cell]) -> None:
        """Queue changed cells without retaining stale solid/color state.

        Invalid positions are ignored.  Repeated edits move a cell to the end
        and restart its quiet-period delay.  On overflow, the oldest repair is
        dropped; the original reliable gameplay packet remains authoritative.
        """

        config = self.server.config
        if not bool(getattr(config, "terrain_repair_enabled", True)):
            return

        delay = max(1, int(getattr(config, "terrain_repair_delay_ticks", 120)))
        due_tick = int(self.server.loop_count) + delay
        world = self.server.world_manager

        for raw_cell in cells:
            if len(raw_cell) != 3:
                continue
            cell = tuple(int(value) for value in raw_cell)
            if not world._valid_block_position(*cell):
                continue
            if cell in self._collapse_pending:
                if not world.get_solid(*cell):
                    # The faster exact-air confirmation already covers this
                    # rejected prediction without a duplicate regular replay.
                    continue
                del self._collapse_pending[cell]
            if cell in self._pending:
                del self._pending[cell]
            self._pending[cell] = due_tick
            self._enforce_limit()

        self.server.metrics.terrain_repair_queue_peak = max(
            self.server.metrics.terrain_repair_queue_peak,
            self.pending_count,
        )

    def tick(self) -> int:
        """Send one bounded repair batch and return its canonical cell count.

        Only fully joined clients are eligible.  Joiners receive the full VXL
        snapshot plus the mutation journal instead, because gameplay packets
        during native GameScene construction are crash-sensitive.
        """

        if not self._pending and not self._collapse_pending:
            return 0
        config = self.server.config
        if not bool(getattr(config, "terrain_repair_enabled", True)):
            self._pending.clear()
            self._collapse_pending.clear()
            return 0

        interval = max(
            1,
            int(getattr(config, "terrain_repair_interval_ticks", 3)),
        )
        if int(self.server.loop_count) % interval:
            return 0

        recipients = [
            connection
            for connection in tuple(self.server.connections.values())
            if getattr(connection, "in_game", False)
            and getattr(connection, "player", None) is not None
        ]
        if not recipients:
            # With no settled GameScene there is nobody to repair. A future
            # join receives the current canonical map rather than this queue.
            self._pending.clear()
            self._collapse_pending.clear()
            return 0

        now = int(self.server.loop_count)
        regular_batch_limit = max(
            1,
            int(getattr(config, "terrain_repair_batch_limit", 8)),
        )
        collapse_batch_limit = max(
            1,
            int(getattr(config, "terrain_collapse_repair_batch_limit", 8)),
        )
        cells: list[Cell] = []
        collapse_cells = 0
        for cell, due_tick in tuple(self._collapse_pending.items()):
            if due_tick > now:
                break
            del self._collapse_pending[cell]
            # A reliable build after the collapse has already restored this
            # coordinate. Do not replay its placement callback/particles.
            if self.server.world_manager.get_solid(*cell):
                continue
            cells.append(cell)
            collapse_cells += 1
            if collapse_cells >= collapse_batch_limit:
                break

        regular_cells = 0
        for cell, due_tick in tuple(self._pending.items()):
            if due_tick > now:
                break
            del self._pending[cell]
            cells.append(cell)
            regular_cells += 1
            if regular_cells >= regular_batch_limit:
                break
        if not cells:
            return 0

        # Packet 33 indexes the recipient's player roster. Use that
        # connection's own id, which is guaranteed to exist after admission,
        # rather than borrowing an arbitrary first recipient under roster
        # skew. Damage type 6 remains the verified exact-cell removal path.
        sends = 0
        for connection in recipients:
            actor_id = int(connection.player.id)
            packets = [
                self.canonical_packet(cell, actor_id) for cell in cells
            ]
            for data in packets:
                try:
                    connection.send(data, reliable=True)
                    sends += 1
                except Exception:
                    # A dead peer must not break the fixed-step gameplay tick.
                    logger.debug("terrain repair send failed", exc_info=True)
                    self.server.metrics.failed_terrain_repair_sends += 1

        self.server.metrics.terrain_repair_cells += len(cells)
        self.server.metrics.terrain_repair_sends += sends
        return len(cells)

    def canonical_packet(self, cell: Cell, actor_id: int) -> bytes:
        """Encode the VXL's current state for one exact cell at send time.

        Terrain catch-up uses the same crash-safe representation as delayed
        settled-client repair: explicit RGB for solid cells and an exact,
        non-collapsing removal for air. Reading the VXL here prevents an old
        queued color or solidity value from overwriting a later edit.
        """

        x, y, z = cell
        world = self.server.world_manager
        if world.get_solid(x, y, z):
            packet = BlockBuildColored()
            packet.loop_count = int(self.server.loop_count)
            packet.player_id = actor_id
            packet.x, packet.y, packet.z = x, y, z
            packet.color = int(world.get_color(x, y, z)) & 0xFFFFFF
            return bytes(packet.generate())

        packet = Damage()
        packet.player_id = actor_id
        packet.type = int(C.WEAPON_DAMAGE)
        packet.damage = self._BLOCK_KILL_DAMAGE
        packet.face = 0
        # Repair one voxel only. Native collapse already ran on the original
        # checked Damage; repeating it here would replay effects/work.
        packet.chunk_check = 0
        packet.seed = 0
        packet.causer_id = actor_id
        packet.position = (float(x), float(y), float(z))
        return bytes(packet.generate())
