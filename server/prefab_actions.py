"""Authoritative prefab placement shared by retail packets and bots."""

from __future__ import annotations

import logging
import math
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

import shared.constants as C
from shared.packet import (
    BlockBuild,
    BlockBuildColored,
    BuildPrefabAction,
    ErasePrefabAction,
    PrefabComplete,
)

from server import prefabs
from server.audio import SND_PREFAB_BUILD, play_sound

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
    editor_native: bool = False
    erase: bool = False
    placed: int = 0


@dataclass(frozen=True, slots=True)
class _EditorPacketSnapshot:
    """Immutable packet-30/31 fields retained while a KV6 is prepared."""

    prefab_name: str
    position: tuple[int, int, int]
    prefab_yaw: int
    prefab_pitch: int
    prefab_roll: int
    color: tuple[int, int, int]
    loop_count: int
    erase: bool = False


@dataclass(slots=True)
class _PreparingEditorPrefab:
    """One native editor request executing outside the gameplay thread."""

    player: object
    snapshot: _EditorPacketSnapshot
    future: Future


@dataclass(slots=True)
class _ValidatingEditorPrefab:
    """Prepared cells awaiting bounded live-world contact validation."""

    player: object
    snapshot: _EditorPacketSnapshot
    cells: deque
    model_block_count: int
    iterator: object


class PrefabActionService:
    """Validate, expand, charge, commit, and replicate one prefab action.

    Thread/tick context: framing and VXL mutation run on the gameplay thread.
    Native UGC KV6 loading/rotation runs on one private preparation thread, and
    completed footprints return through bounded live-world validation in
    :meth:`tick`.  The worker never reads or mutates server/world/player state.
    Failures are atomic before VXL mutation; an unexpected per-cell VXL
    rejection is skipped without charging that cell.
    """

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server
        self._pending: deque[_PendingPrefab] = deque()
        self._preparing: deque[_PreparingEditorPrefab] = deque()
        self._validating: deque[_ValidatingEditorPrefab] = deque()
        self._executor: ThreadPoolExecutor | None = None
        # Lightweight domain tests without SimulationRuntime retain immediate
        # behavior; production always drains through ``tick``.
        self._deferred = hasattr(server, "simulation_runtime")

    @property
    def pending_count(self) -> int:
        """Return queued prefab actions, not individual cells."""

        return len(self._pending) + len(self._preparing) + len(self._validating)

    def place_packet(self, player: "Player", packet) -> bool:
        """Translate ``BuildPrefabAction(30)`` into the public action API."""

        if self._deferred and self._is_ugc_editor(player):
            return self._queue_editor_packet(player, packet, erase=False)

        accepted = self.place(
            player,
            name=str(getattr(packet, "prefab_name", "") or ""),
            position=getattr(packet, "position", None),
            yaw=int(getattr(packet, "prefab_yaw", 0)),
            pitch=int(getattr(packet, "prefab_pitch", 0)),
            roll=int(getattr(packet, "prefab_roll", 0)),
            color=getattr(packet, "color", None),
            loop_count=int(getattr(packet, "loop_count", self.server.loop_count)),
        )
        if accepted and self._is_ugc_editor(player):
            self._broadcast_native_build(player, packet)
        return accepted

    def erase_packet(self, player: "Player", packet) -> bool:
        """Commit one UGC erase in bounded batches and echo native packet 31."""

        if self._deferred and self._is_ugc_editor(player):
            return self._queue_editor_packet(player, packet, erase=True)

        name = str(getattr(packet, "prefab_name", "") or "")
        if not self._is_ugc_editor(player) or not self._authorized(player, name):
            return False
        model = prefabs.get_registry().get(name)
        if model is None:
            return False
        position = getattr(packet, "position", None)
        if position is None:
            return False
        try:
            anchor = tuple(int(round(float(value))) for value in position[:3])
        except (IndexError, TypeError, ValueError):
            return False
        if len(anchor) != 3:
            return False
        yaw = int(getattr(packet, "prefab_yaw", 0)) & 3
        pitch = int(getattr(packet, "prefab_pitch", 0)) & 3
        roll = int(getattr(packet, "prefab_roll", 0)) & 3
        expanded = prefabs.expand_prefab(model, anchor, yaw, pitch, roll)
        targets = [
            (int(x), int(y), int(z))
            for (x, y, z), _color in expanded
            if 0 <= int(x) < 512 and 0 <= int(y) < 512 and 0 <= int(z) <= 238
            and self.server.world_manager.get_solid(int(x), int(y), int(z))
        ]
        if not targets:
            return False
        action_loop = max(0, int(getattr(packet, "loop_count", self.server.loop_count)))
        if self._deferred:
            accepted = self._enqueue_erase(
                player,
                name=name,
                anchor=anchor,
                yaw=yaw,
                targets=targets,
                action_loop=action_loop,
            )
        else:
            removed = sum(self._erase_cell(target) for target in targets)
            accepted = removed > 0
            if accepted:
                complete = PrefabComplete()
                player.send(bytes(complete.generate()), reliable=True)
        if accepted:
            self._broadcast_native_erase(player, packet, anchor)
        return accepted

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

        editor_native = self._is_ugc_editor(player)
        # In UGC the model's authored colors are canonical. Competitive
        # prefabs retain the recovered 50/50 player/model blend.
        base_color = None if editor_native else self._base_color(player, color)
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
        if (
            not editor_native
            and prefabs.collides_with_player(in_world, self.server.players.values())
        ):
            return False

        infinite = bool(
            getattr(self.server.teams.get(player.team), "infinite_blocks", False)
        )
        if not infinite and len(in_world) > int(getattr(player, "blocks", 0)):
            return False

        footprint = tuple(position for position, _rgb in in_world)
        construction = getattr(self.server, "construction", None)
        reservation = None
        if construction is not None and not editor_native:
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
                editor_native=editor_native,
            )
        try:
            placed, new_cells = self._commit(
                player,
                in_world,
                action_loop=action_loop,
                editor_native=editor_native,
            )
        finally:
            if construction is not None:
                construction.release(reservation)

        if new_cells and not infinite:
            player.blocks = max(0, int(player.blocks) - new_cells)

        complete = PrefabComplete()
        player.send(bytes(complete.generate()), reliable=True)
        if editor_native and placed:
            self._relocate_entombed_players()
        logger.info(
            "PREFAB %s by %s at %s yaw=%d: placed %d/%d blocks",
            name,
            getattr(player, "name", player.id),
            anchor,
            yaw,
            placed,
            len(in_world),
        )
        if placed:
            play_sound(
                self.server,
                SND_PREFAB_BUILD,
                position=anchor,
                exclude=player,
            )
        return placed > 0

    def tick(self) -> int:
        """Adopt prepared editor work and commit bounded cells after physics."""

        self._collect_editor_preparations()
        self._validate_editor_preparations()

        if not self._pending:
            return 0
        hard_limit = 2048 if bool(getattr(self.server.config, "ugc_runtime", False)) else 128
        budget = max(
            1,
            min(
                hard_limit,
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
            if pending.erase:
                if self._erase_cell(coordinate):
                    pending.placed += 1
                committed += 1
                if not pending.cells:
                    self._pending.popleft()
                    self._finish(pending)
                continue
            was_solid = bool(self.server.world_manager.get_solid(*coordinate))
            if self._commit_cell(
                player,
                coordinate,
                color,
                action_loop=pending.action_loop,
                editor_native=pending.editor_native,
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
        kept_preparing: deque[_PreparingEditorPrefab] = deque()
        while self._preparing:
            preparing = self._preparing.popleft()
            if int(preparing.player.id) == int(owner_id):
                preparing.future.cancel()
                cancelled += 1
            else:
                kept_preparing.append(preparing)
        self._preparing = kept_preparing
        kept_validating: deque[_ValidatingEditorPrefab] = deque()
        while self._validating:
            validating = self._validating.popleft()
            if int(validating.player.id) == int(owner_id):
                cancelled += 1
            else:
                kept_validating.append(validating)
        self._validating = kept_validating
        return cancelled

    def cancel_all(self) -> None:
        """Cancel every queued prefab during round/map teardown."""

        while self._pending:
            self._cancel(self._pending.popleft())
        while self._preparing:
            self._preparing.popleft().future.cancel()
        self._validating.clear()

    def close(self) -> None:
        """Release the optional editor worker during final server shutdown."""

        self.cancel_all()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def _queue_editor_packet(self, player: "Player", packet, *, erase: bool) -> bool:
        """Snapshot and queue one native UGC KV6 operation without blocking.

        Only immutable packet fields cross the thread boundary.  Authorization
        and queue capacity are checked before submission; world contact is
        intentionally checked later on the authoritative thread because the
        terrain may change while the model is being decoded.
        """

        name = str(getattr(packet, "prefab_name", "") or "")
        if not self._authorized(player, name):
            return False
        position = getattr(packet, "position", None)
        try:
            anchor = tuple(int(round(float(value))) for value in position[:3])
        except (IndexError, TypeError, ValueError):
            return False
        if len(anchor) != 3:
            return False
        limit = max(
            1,
            min(128, int(getattr(self.server.config, "prefab_queue_limit", 32))),
        )
        if self.pending_count >= limit:
            return False
        raw_color = getattr(packet, "color", (0, 0, 0))
        try:
            color = tuple(int(value) & 0xFF for value in raw_color[:3])
        except (IndexError, TypeError, ValueError):
            color = (0, 0, 0)
        if len(color) != 3:
            color = (0, 0, 0)
        snapshot = _EditorPacketSnapshot(
            prefab_name=name,
            position=anchor,
            prefab_yaw=int(getattr(packet, "prefab_yaw", 0)) & 3,
            prefab_pitch=int(getattr(packet, "prefab_pitch", 0)) & 3,
            prefab_roll=int(getattr(packet, "prefab_roll", 0)) & 3,
            color=color,
            loop_count=max(
                0,
                int(getattr(packet, "loop_count", self.server.loop_count)),
            ),
            erase=bool(erase),
        )
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="ugc-prefab-prepare",
            )
        future = self._executor.submit(self._prepare_editor_cells, snapshot)
        self._preparing.append(_PreparingEditorPrefab(player, snapshot, future))
        return True

    @staticmethod
    def _prepare_editor_cells(
        snapshot: _EditorPacketSnapshot,
    ) -> tuple[deque, int]:
        """Load and rotate one KV6 using no mutable server-owned objects.

        The second result is the KV6's original block count.  Native
        ``VXL.place_prefab_in_world`` interprets packet 30's range as
        ``[from_block_index, to_block_index)``.  Keeping the unfiltered model
        count is important when part of a prefab falls outside map bounds:
        the retail client must still walk every authored model index and make
        the same bounds decisions as the server.
        """

        model = prefabs.get_registry().get(snapshot.prefab_name)
        if model is None:
            return deque(), 0
        px, py, pz = snapshot.position
        rows = []
        points = model.get_points()
        model_block_count = len(points)
        for x, y, z, red, green, blue in points:
            rx, ry, rz = prefabs.rotate_point(
                x,
                y,
                z,
                snapshot.prefab_yaw,
                snapshot.prefab_pitch,
                snapshot.prefab_roll,
            )
            coordinate = (int(rx) + px, int(ry) + py, int(rz) + pz)
            if not (
                0 <= coordinate[0] < 512
                and 0 <= coordinate[1] < 512
                and 0 <= coordinate[2] <= 238
            ):
                continue
            color = None if snapshot.erase else (
                int(red) & 0xFF,
                int(green) & 0xFF,
                int(blue) & 0xFF,
            )
            rows.append((coordinate, color, False))
        # z grows downward.  Ground-facing voxels first make a normal placement
        # pass live contact validation immediately without weakening the gate.
        rows.sort(key=lambda row: row[0][2], reverse=True)
        return deque(rows), model_block_count

    def _collect_editor_preparations(self) -> None:
        """Poll completed futures without waiting on the gameplay thread."""

        if not self._preparing:
            return
        retained: deque[_PreparingEditorPrefab] = deque()
        while self._preparing:
            preparing = self._preparing.popleft()
            if not preparing.future.done():
                retained.append(preparing)
                continue
            current = self.server.players.get(int(preparing.player.id))
            if current is not preparing.player:
                continue
            try:
                cells, model_block_count = preparing.future.result()
            except Exception:
                logger.exception(
                    "UGC prefab preparation failed: %s",
                    preparing.snapshot.prefab_name,
                )
                continue
            if not cells or model_block_count <= 0:
                continue
            self._validating.append(
                _ValidatingEditorPrefab(
                    player=preparing.player,
                    snapshot=preparing.snapshot,
                    cells=cells,
                    model_block_count=model_block_count,
                    iterator=iter(cells),
                )
            )
        self._preparing = retained

    def _validate_editor_preparations(self) -> None:
        """Validate world contact/erase targets under a strict tick budget."""

        if not self._validating:
            return
        budget = max(
            64,
            min(
                4096,
                int(getattr(self.server.config, "prefab_validation_batch_limit", 1024)),
            ),
        )
        checked = 0
        while self._validating and checked < budget:
            validating = self._validating[0]
            current = self.server.players.get(int(validating.player.id))
            if current is not validating.player:
                self._validating.popleft()
                continue
            try:
                coordinate, _color, _charged = next(validating.iterator)
            except StopIteration:
                self._validating.popleft()
                logger.debug(
                    "UGC prefab rejected without live world contact: %s at %s",
                    validating.snapshot.prefab_name,
                    validating.snapshot.position,
                )
                continue
            checked += 1
            if validating.snapshot.erase:
                accepted = bool(self.server.world_manager.get_solid(*coordinate))
            else:
                accepted = self._coordinate_touches_world(coordinate)
            if not accepted:
                continue
            self._validating.popleft()
            self._accept_editor_preparation(validating)

    def _coordinate_touches_world(self, coordinate: tuple[int, int, int]) -> bool:
        """Check the recovered six-neighbour prefab support invariant."""

        x, y, z = coordinate
        world = self.server.world_manager
        for neighbour in (
            (x + 1, y, z),
            (x - 1, y, z),
            (x, y + 1, z),
            (x, y - 1, z),
            (x, y, z + 1),
            (x, y, z - 1),
        ):
            try:
                if world.get_solid(*neighbour):
                    return True
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
        return False

    def _accept_editor_preparation(
        self, validating: _ValidatingEditorPrefab
    ) -> None:
        """Move one validated native operation into the bounded commit queue."""

        snapshot = validating.snapshot
        pending = _PendingPrefab(
            player=validating.player,
            name=snapshot.prefab_name,
            anchor=snapshot.position,
            yaw=snapshot.prefab_yaw,
            action_loop=snapshot.loop_count,
            cells=validating.cells,
            total_cells=len(validating.cells),
            reservation=None,
            editor_native=True,
            erase=snapshot.erase,
        )
        self._pending.append(pending)
        if snapshot.erase:
            self._broadcast_native_erase(
                validating.player,
                snapshot,
                snapshot.position,
                model_block_count=validating.model_block_count,
            )
        else:
            self._broadcast_native_build(
                validating.player,
                snapshot,
                model_block_count=validating.model_block_count,
            )

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
        editor_native: bool = False,
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
                editor_native=editor_native,
            )
        )
        return True

    def _enqueue_erase(
        self,
        player: "Player",
        *,
        name: str,
        anchor: tuple[int, int, int],
        yaw: int,
        targets,
        action_loop: int,
    ) -> bool:
        """Queue an editor erase without running a full KV6 mutation in one tick."""

        limit = max(
            1,
            min(128, int(getattr(self.server.config, "prefab_queue_limit", 32))),
        )
        if len(self._pending) >= limit:
            return False
        cells = deque((coordinate, None, False) for coordinate in targets)
        self._pending.append(
            _PendingPrefab(
                player=player,
                name=name,
                anchor=anchor,
                yaw=yaw,
                action_loop=action_loop,
                cells=cells,
                total_cells=len(cells),
                reservation=None,
                editor_native=True,
                erase=True,
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
        if pending.placed and not pending.erase:
            play_sound(
                self.server,
                SND_PREFAB_BUILD,
                position=pending.anchor,
                exclude=pending.player,
            )
        if pending.editor_native and not pending.erase and pending.placed:
            self._relocate_entombed_players()
        construction = getattr(self.server, "construction", None)
        if construction is not None:
            construction.release(pending.reservation)
        logger.info(
            "PREFAB %s %s by %s at %s yaw=%d: changed %d/%d blocks",
            "erase" if pending.erase else "build",
            pending.name,
            getattr(pending.player, "name", pending.player.id),
            pending.anchor,
            pending.yaw,
            pending.placed,
            pending.total_cells,
        )

    def _relocate_entombed_players(self) -> int:
        """Lift players out of a just-committed native UGC prefab.

        The recovered PrefabManager performs this after every class-13 build:
        if any of the three body voxels became solid, it walks upward (negative
        VXL z) until a clear three-voxel column is found and recentres the
        player.  Competitive prefabs still reject player collision before the
        commit and never enter this recovery path.
        """

        world = self.server.world_manager
        moved = 0
        for candidate in tuple(getattr(self.server, "players", {}).values()):
            if not bool(getattr(candidate, "alive", False)) or not bool(
                getattr(candidate, "spawned", False)
            ):
                continue
            try:
                x = int(float(candidate.x))
                y = int(float(candidate.y))
                z = int(float(candidate.z))
            except (AttributeError, TypeError, ValueError):
                continue
            if not any(
                0 <= z + offset <= 238
                and world.get_solid(x, y, z + offset)
                for offset in range(3)
            ):
                continue
            safe_z = z
            while safe_z >= 0 and any(
                0 <= safe_z + offset <= 238
                and world.get_solid(x, y, safe_z + offset)
                for offset in range(3)
            ):
                safe_z -= 1
            if safe_z < 0:
                logger.warning(
                    "UGC prefab entombed player %s without an upward escape",
                    getattr(candidate, "name", getattr(candidate, "id", "?")),
                )
                continue
            set_position = getattr(candidate, "set_position", None)
            if not callable(set_position):
                continue
            # PLAYER_STANDING_POS_ABOVE_GROUND is 2.25; the stock expression
            # is safe_z + 2.0 - 2.25.
            set_position(x + 0.5, y + 0.5, safe_z - 0.25)
            moved += 1
        return moved

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

    def authorized(self, player: "Player", name: str) -> bool:
        """Public framing gate shared by build and erase packet handlers."""

        return self._authorized(player, name)

    def _is_ugc_editor(self, player: "Player") -> bool:
        """Identify the isolated Builder path without affecting normal modes."""

        return (
            bool(getattr(getattr(self.server, "config", None), "ugc_runtime", False))
            and int(getattr(player, "class_id", -1)) == int(C.CLASS_UGCBUILDER)
            and int(getattr(player, "tool", -1)) == int(C.UGC_PREFAB_TOOL)
        )

    @staticmethod
    def _source_model_block_count(source) -> int:
        """Return the authored KV6 block count for a native range packet."""

        model = prefabs.get_registry().get(str(source.prefab_name))
        if model is None:
            return 0
        try:
            return len(model.get_points())
        except (AttributeError, TypeError):
            return 0

    def _broadcast_native_build(
        self,
        player: "Player",
        source,
        *,
        model_block_count: int | None = None,
    ) -> None:
        """Let retail clients render a large editor KV6 without cell floods.

        IDA recovery of ``vxl.pyd:sub_1002E7F0`` proved that the range is
        inclusive/exclusive.  The old ``0..0`` echo therefore asked the
        client to place *zero* cells while the server committed the complete
        prefab, producing an invisible solid structure.
        """

        packet = BuildPrefabAction()
        packet.loop_count = int(self.server.loop_count)
        packet.prefab_name = str(source.prefab_name)
        packet.player_id = int(player.id)
        packet.prefab_yaw = int(getattr(source, "prefab_yaw", 0)) & 3
        packet.prefab_pitch = int(getattr(source, "prefab_pitch", 0)) & 3
        packet.prefab_roll = int(getattr(source, "prefab_roll", 0)) & 3
        packet.from_block_index = 0
        packet.to_block_index = max(
            0,
            int(
                self._source_model_block_count(source)
                if model_block_count is None
                else model_block_count
            ),
        )
        if packet.to_block_index <= packet.from_block_index:
            logger.warning(
                "Cannot replicate empty UGC prefab model: %s",
                packet.prefab_name,
            )
            return
        packet.position = tuple(int(round(float(value))) for value in source.position[:3])
        packet.color = tuple(int(value) & 0xFF for value in source.color[:3])
        packet.add_to_user_blocks = False
        self.server.broadcast(
            bytes(packet.generate()), reliable=True, record_mutation=False
        )

    def _broadcast_native_erase(
        self,
        player: "Player",
        source,
        anchor: tuple[int, int, int],
        *,
        model_block_count: int | None = None,
    ) -> None:
        """Mirror packet 31 using its inclusive/exclusive KV6 index range."""

        packet = ErasePrefabAction()
        packet.loop_count = int(self.server.loop_count)
        packet.prefab_name = str(source.prefab_name)
        packet.player_id = int(player.id)
        packet.prefab_yaw = int(getattr(source, "prefab_yaw", 0)) & 3
        packet.prefab_pitch = int(getattr(source, "prefab_pitch", 0)) & 3
        packet.prefab_roll = int(getattr(source, "prefab_roll", 0)) & 3
        packet.from_block_index = 0
        packet.to_block_index = max(
            0,
            int(
                self._source_model_block_count(source)
                if model_block_count is None
                else model_block_count
            ),
        )
        if packet.to_block_index <= packet.from_block_index:
            logger.warning(
                "Cannot replicate erase for empty UGC prefab model: %s",
                packet.prefab_name,
            )
            return
        packet.position = anchor
        self.server.broadcast(
            bytes(packet.generate()), reliable=True, record_mutation=False
        )

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
        editor_native: bool = False,
    ) -> tuple[int, int]:
        """Commit validated cells and emit the two proven observer paths."""

        placed = 0
        new_cells = 0
        world = self.server.world_manager
        for (x, y, z), color in cells:
            was_solid = bool(world.get_solid(x, y, z))
            if not self._commit_cell(
                player,
                (x, y, z),
                color,
                action_loop=action_loop,
                editor_native=editor_native,
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
        editor_native: bool = False,
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

        if editor_native:
            # Packet 30 already makes every settled retail client expand the
            # exact KV6. Canonical WorldManager mutations still protect a
            # client whose MapSync was in flight during this bounded commit.
            return True

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

    def _erase_cell(self, coordinate: tuple[int, int, int]) -> bool:
        """Remove one canonical editor cell; packet 31 owns live rendering."""

        try:
            return bool(self.server.world_manager.destroy_blocks((coordinate,)))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            logger.exception("Prefab VXL erase failed at %s", coordinate)
            return False
