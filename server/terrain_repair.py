"""Bounded canonical terrain repair for clients already inside GameScene.

Normal block packets are reliable and remain the primary replication path.
This service is a delayed safety net only for a retail client whose local
prediction was rejected or covered a larger footprint than the server. It is
never enrolled for a successful canonical mutation: replaying an accepted
BlockBuild/Damage packet makes native callbacks and particles run twice. It
never serializes the whole map on the gameplay thread.
"""

from __future__ import annotations

from collections import OrderedDict
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

    @property
    def pending_count(self) -> int:
        """Return the number of unique canonical cells awaiting repair."""

        return len(self._pending)

    def reset(self) -> None:
        """Discard delayed work when a world is replaced or the server stops."""

        self._pending.clear()

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
        limit = max(64, int(getattr(config, "terrain_repair_queue_limit", 8192)))
        world = self.server.world_manager

        for raw_cell in cells:
            if len(raw_cell) != 3:
                continue
            cell = tuple(int(value) for value in raw_cell)
            if not world._valid_block_position(*cell):
                continue
            if cell in self._pending:
                del self._pending[cell]
            self._pending[cell] = due_tick
            while len(self._pending) > limit:
                self._pending.popitem(last=False)
                self.server.metrics.dropped_terrain_repairs += 1

        self.server.metrics.terrain_repair_queue_peak = max(
            self.server.metrics.terrain_repair_queue_peak,
            len(self._pending),
        )

    def tick(self) -> int:
        """Send one bounded repair batch and return its canonical cell count.

        Only fully joined clients are eligible.  Joiners receive the full VXL
        snapshot plus the mutation journal instead, because gameplay packets
        during native GameScene construction are crash-sensitive.
        """

        if not self._pending:
            return 0
        config = self.server.config
        if not bool(getattr(config, "terrain_repair_enabled", True)):
            self._pending.clear()
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
            return 0

        now = int(self.server.loop_count)
        batch_limit = max(
            1,
            int(getattr(config, "terrain_repair_batch_limit", 8)),
        )
        cells: list[Cell] = []
        for cell, due_tick in tuple(self._pending.items()):
            if due_tick > now:
                break
            del self._pending[cell]
            cells.append(cell)
            if len(cells) >= batch_limit:
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
                self._canonical_packet(cell, actor_id) for cell in cells
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

    def _canonical_packet(self, cell: Cell, actor_id: int) -> bytes:
        """Encode the VXL's current state for one cell at send time."""

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
