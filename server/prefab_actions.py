"""Authoritative prefab placement shared by retail packets and bots."""

from __future__ import annotations

import logging
import math
import struct
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

import shared.constants as C
from shared.packet import (
    BuildPrefabAction,
    Damage,
    ErasePrefabAction,
    PaintBlockPacket,
    PrefabComplete,
)

from server import prefabs
from server.audio import SND_PREFAB_BUILD, play_sound
from server.game_constants import MAX_BUILD_Z, MIN_BUILD_Z, build_z_is_safe

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player


logger = logging.getLogger(__name__)


# BlockManagerState(38) user-block rows per packet.  Seven bytes each; the
# default keeps one packet near the 1 KB MapSync slice size.
BLOCK_STATE_DEFAULT_ROWS = 128
BLOCK_STATE_MAX_ROWS = 4096
_BLOCK_STATE_ID = 38


def encode_block_manager_state(rows, damaged_rows=()) -> bytes:
    """Encode a retail BlockManagerState(38).

    Wire layout recovered from the stock client's own ``shared.packet``
    (generate/read round trip, live 2026-09-26), little-endian::

        u8  id = 38
        i32 damaged_count, damaged rows: i16 x, i16 y, i16 z,
            u8 remaining_health * 4, u8 b, u8 g, u8 r
        i32 user_count, user rows: i16 x, i16 y, i16 z, u8 health * 4
        i32 occupied_count, occupied rows (always 0 here)

    ``receive_block_manager_state`` MERGES both tables (live 2026-09-26): a
    user row sets ``user_blocks[cell]`` (initial health, unscaled); a damaged
    row sets ``damaged_blocks[cell] = DamagedBlock(health, original_color)``
    (scaled remaining health) and darkens the voxel from ``original_color``
    exactly like live damage.  The server's reconstructed
    ``BlockManagerState``/``ServerBlockItem`` classes do not match this
    layout, hence the explicit encoder.  User health is rounded UP to the
    0.25 wire step so a joiner never breaks a cell before the server;
    remaining health is already on the 0.25 grid (retail damage quanta).
    """

    rows = tuple(rows)
    damaged_rows = tuple(damaged_rows)
    out = bytearray(struct.pack("<Bi", _BLOCK_STATE_ID, len(damaged_rows)))
    for x, y, z, health, color in damaged_rows:
        quarters = max(1, min(255, int(math.floor(float(health) * 4.0 + 0.5))))
        r, g, b = (int(value) & 0xFF for value in tuple(color)[:3])
        out += struct.pack("<hhhBBBB", int(x), int(y), int(z), quarters, b, g, r)
    out += struct.pack("<i", len(rows))
    for x, y, z, health in rows:
        quarters = max(1, min(255, int(math.ceil(float(health) * 4.0 - 1e-9))))
        out += struct.pack("<hhhB", int(x), int(y), int(z), quarters)
    out += struct.pack("<i", 0)
    return bytes(out)


def block_state_packets(user_rows, damaged_rows=(), batch=BLOCK_STATE_DEFAULT_ROWS):
    """Split BlockManagerState rows into bounded packets (bytes list).

    Every user-row packet precedes every damaged-row packet.  The stock
    client darkens a damaged row's voxel by ``0.125`` per point of damage
    measured against ``get_initial_health`` AT THAT MOMENT (live
    2026-09-26: a 4.0/9 row before its 9.0 user row darkened like 4.0/5), so
    the initial health must already be merged.
    """

    user_rows = list(user_rows)
    damaged_rows = list(damaged_rows)
    batch = max(1, min(BLOCK_STATE_MAX_ROWS, int(batch)))
    packets = [
        encode_block_manager_state(user_rows[start:start + batch])
        for start in range(0, len(user_rows), batch)
    ]
    packets.extend(
        encode_block_manager_state((), damaged_rows[start:start + batch])
        for start in range(0, len(damaged_rows), batch)
    )
    return packets


def block_shade_packets(shade_rows, loop_count: int = 0) -> list[bytes]:
    """PaintBlock(7) per ``(x, y, z, (r, g, b))`` live damage shade.

    Sent AFTER the damaged rows: the stock ``add_damage`` compounds
    ``dim`` on the current colour per hit while a damaged row darkens its
    original colour once by the total (IDA 2026-09-26), so multi-hit or
    painted-after-damage cells need the live shade restated.  PaintBlock's
    ``color_block`` only sets the voxel colour; the DamagedBlock health and
    original colour the row installed are untouched.
    """

    out = []
    for x, y, z, color in shade_rows:
        paint = PaintBlockPacket()
        paint.loop_count = max(0, int(loop_count))
        paint.x, paint.y, paint.z = int(x), int(y), int(z)
        paint.color = tuple(int(value) & 0xFF for value in tuple(color)[:3])
        out.append(bytes(paint.generate()))
    return out


def block_hit_replay_packets(replays, actor_id: int) -> list[bytes]:
    """Single-cell Damage(37) per recorded hit of a client-coloured cell.

    ``replays`` rows are ``(x, y, z, (amount, ...))`` from
    :meth:`WorldManager.block_hit_replays`.  Type 6 (WEAPON_DAMAGE) applies
    the packet amount to exactly the centre cell, the same ``add_damage``
    the live hit made, so the joiner reproduces the health and the per-hit
    darkening of a colour only the client knows.  ``chunk_check=0``: no hit
    breaks the cell, and no collapse work is replayed.  Only for a joiner
    whose client holds no damage for these cells (after its user rows).
    """

    out = []
    for x, y, z, amounts in replays:
        for amount in amounts:
            packet = Damage()
            packet.player_id = int(actor_id)
            packet.type = int(C.WEAPON_DAMAGE)
            packet.damage = float(amount)
            packet.face = 0
            packet.chunk_check = 0
            packet.seed = 0
            packet.causer_id = int(actor_id)
            packet.position = (float(x), float(y), float(z))
            out.append(bytes(packet.generate()))
    return out


@dataclass(frozen=True, slots=True)
class _BotPrefabOwner:
    """The concrete bot life and committed selection that paid for a job."""

    generation: int
    deaths: int
    life: int
    team: int
    class_id: int
    tool: int
    loadout: tuple[int, ...]
    prefabs: tuple[str, ...]

    @classmethod
    def capture(cls, player: object) -> "_BotPrefabOwner":
        return cls(
            int(getattr(player, "bot_generation", 0)),
            int(getattr(player, "deaths", 0)),
            int(getattr(player, "replication_generation", 0)),
            int(getattr(player, "team", -1)),
            int(getattr(player, "class_id", -1)),
            int(getattr(player, "tool", -1)),
            tuple(int(tool) for tool in (getattr(player, "loadout", ()) or ())),
            tuple(str(name) for name in (getattr(player, "prefabs", ()) or ())),
        )

    def owns_inventory(self, player: object) -> bool:
        """Refund only the original living wallet, never a respawn's stock."""

        return (
            bool(getattr(player, "alive", False))
            and bool(getattr(player, "spawned", False))
            and self.generation == int(getattr(player, "bot_generation", 0))
            and self.deaths == int(getattr(player, "deaths", 0))
            and self.life == int(getattr(player, "replication_generation", 0))
        )


@dataclass(slots=True)
class _PendingPrefab:
    """One validated prefab awaiting its post-physics commit.

    Competitive placements commit whole in one tick; UGC editor builds and
    erases drain in bounded per-tick cell batches.  ``pitch``, ``roll`` and
    ``base_color`` are the exact BuildPrefabAction(30) fields echoed to every
    client for a competitive commit.
    """

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
    bot_owner: _BotPrefabOwner | None = None
    pitch: int = 0
    roll: int = 0
    base_color: tuple[int, int, int] | None = None


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

    # Competitive prefabs one player may have queued at once.
    PER_PLAYER_PENDING_LIMIT = 4

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
        # Reject rather than clip a prefab that reaches the reserved sky layer.
        # Competitive prefabs would otherwise create the same unstable platform
        # as ordinary blocks; native UGC packet 30 would also make clients expand
        # a z=0 voxel that the authoritative server had silently omitted.
        if any(
            0 <= int(x) < 512
            and 0 <= int(y) < 512
            and int(z) == 0
            for (x, y, z), _rgb in cells
        ):
            return False
        in_world = [
            ((int(x), int(y), int(z)), tuple(int(component) & 0xFF for component in rgb))
            for (x, y, z), rgb in cells
            if 0 <= int(x) < 512
            and 0 <= int(y) < 512
            and build_z_is_safe(int(z))
        ]
        if not editor_native and len(in_world) != len(cells):
            # Retail replication is one BuildPrefabAction(30): every client
            # expands the WHOLE model and debits the owner once per model
            # voxel.  A partially out-of-world footprint would make the
            # server's committed cells and wallet diverge from every client,
            # so it is refused whole (retail's allowed_on_beach_layer gate
            # likewise rejects rather than clips).
            return False
        if not in_world or not prefabs.touches_world(world, in_world):
            return False
        # Server-owned bots are trusted actors (and snap their anchor to the
        # surface); only client packets need the reach bound.
        if (
            not editor_native
            and not bool(getattr(player, "is_bot", False))
            and not self._within_build_reach(player, in_world)
        ):
            return False
        if (
            not editor_native
            and not bool(getattr(player, "is_bot", False))
            and not self._footprint_visible(player, in_world)
        ):
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
                pitch=pitch,
                roll=roll,
                base_color=base_color,
            )
        if not editor_native:
            # Lightweight embedders without SimulationRuntime commit the same
            # whole-model action immediately.
            pending = self._new_pending(
                player,
                name=name,
                anchor=anchor,
                yaw=yaw,
                cells=in_world,
                action_loop=action_loop,
                reservation=reservation,
                infinite=infinite,
                editor_native=False,
                pitch=pitch,
                roll=roll,
                base_color=base_color,
            )
            if pending is None:
                return False
            self._commit_whole(pending)
            self._finish(pending)
            return pending.placed > 0
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
            # Native UGC builders only; competitive prefabs returned above.
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
                reliable=False,
            )
        return placed > 0

    # Hard ceiling for whole competitive prefabs committed in one tick.  The
    # largest stock model (superdome) is 675 cells; a single prefab larger
    # than the budget is still committed alone so it can never starve.
    COMPETITIVE_TICK_CELL_CEILING = 8192

    def tick(self) -> int:
        """Adopt prepared editor work and commit queued prefabs after physics.

        Competitive prefabs commit whole in one tick: every cell of one
        placement is written in the same simulation frame and replicated as
        one retail BuildPrefabAction(30), so every client expands the model
        at once (with its smoke ring).  ``prefab_competitive_cell_budget``
        caps the cells committed
        across all players per tick; whole prefabs beyond it wait for the next
        tick and a prefab is never split.  UGC editor builds and erases keep
        the bounded per-cell ``prefab_cell_batch_limit`` lane.
        """

        self._collect_editor_preparations()
        self._validate_editor_preparations()

        if not self._pending:
            return 0
        config = self.server.config
        hard_limit = 2048 if bool(getattr(config, "ugc_runtime", False)) else 128
        budget = max(
            1,
            min(
                hard_limit,
                int(getattr(config, "prefab_cell_batch_limit", 16)),
            ),
        )
        whole_budget = max(
            1,
            min(
                self.COMPETITIVE_TICK_CELL_CEILING,
                int(getattr(config, "prefab_competitive_cell_budget", 2048)),
            ),
        )
        committed = 0
        cell_lane = 0
        whole_lane = 0
        while self._pending:
            pending = self._pending[0]
            player = pending.player
            current = self.server.players.get(int(player.id))
            if current is not player:
                self._pending.popleft()
                self._cancel(pending)
                continue
            if pending.bot_owner is not None and (
                not pending.bot_owner.owns_inventory(player)
                or not bool(getattr(player, "is_bot", False))
                or pending.bot_owner != _BotPrefabOwner.capture(player)
                or not self._authorized(player, pending.name)
            ):
                self._pending.popleft()
                self._cancel(pending)
                continue
            if self._commits_whole(pending):
                size = len(pending.cells)
                if whole_lane and whole_lane + size > whole_budget:
                    break
                self._pending.popleft()
                construction = getattr(self.server, "construction", None)
                if construction is not None and construction._overlaps_living_player(
                    frozenset(cell[0] for cell in pending.cells)
                ):
                    # A body entered the footprint between admission and this
                    # post-physics commit. Retail validated and built in one
                    # step, so refuse the whole prefab rather than entomb.
                    self._cancel(pending)
                    self._finish(pending)
                    continue
                self._commit_whole(pending)
                whole_lane += size
                committed += size
                self._finish(pending)
                continue
            if cell_lane >= budget:
                break
            coordinate, color, charged = pending.cells.popleft()
            if pending.erase:
                if self._erase_cell(coordinate):
                    pending.placed += 1
                cell_lane += 1
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
                    self._refund(pending, 1)
            elif charged:
                self._refund(pending, 1)
            cell_lane += 1
            committed += 1
            if not pending.cells:
                self._pending.popleft()
                self._finish(pending)
        return committed

    @staticmethod
    def _commits_whole(pending: _PendingPrefab) -> bool:
        """Competitive placements commit atomically; editor work is batched."""

        return not pending.editor_native and not pending.erase

    def _commit_whole(self, pending: _PendingPrefab) -> None:
        """Commit one competitive prefab and replicate it the retail way.

        Retail ``PrefabManager.build_prefab`` adds every model voxel with
        ``add_user_block(..., DEFAULT_PREFAB_HEALTH, replace_solids=True)``:
        existing solids are overwritten, every cell starts at prefab health
        (9), and the owner's ``on_single_block_added`` debits one block per
        model voxel.  The server mirrors that ledger (cells, colours, health,
        wallet) and broadcasts ONE BuildPrefabAction(30) with
        ``add_to_user_blocks=True`` to every in-game client, the owner
        included: the stock client's ``send_build_prefab`` only sends the
        request, so the owner builds and debits on this echo (measured live
        2026-09-26: wallet 500 -> 432 for the 68-voxel superminibunker, all
        cells health 9.0, one smoke ring per top-layer voxel).
        """

        world = self.server.world_manager
        health = float(prefabs.DEFAULT_PREFAB_HEALTH)
        charged = 0
        while pending.cells:
            coordinate, color, paid = pending.cells.popleft()
            charged += int(bool(paid))
            try:
                committed = world.set_block(
                    *coordinate, solid=True, color=color, health=health
                )
            except (AttributeError, RuntimeError, TypeError, ValueError):
                logger.exception("Prefab VXL commit failed at %s", coordinate)
                committed = False
            if committed:
                pending.placed += 1
        if not pending.placed:
            # Nothing reached the canonical map, so no client will build or
            # debit either: return the whole reservation.
            if charged:
                self._refund(pending, charged)
            return
        # A cell the VXL refused after full-footprint admission stays paid:
        # every client expands and debits the complete model regardless.
        self._broadcast_competitive_build(pending)

    def _broadcast_competitive_build(self, pending: _PendingPrefab) -> None:
        """Send the one native packet that builds this prefab on clients."""

        packet = BuildPrefabAction()
        packet.loop_count = int(self.server.loop_count)
        packet.prefab_name = str(pending.name)
        packet.player_id = int(pending.player.id)
        packet.prefab_yaw = int(pending.yaw) & 3
        packet.prefab_pitch = int(pending.pitch) & 3
        packet.prefab_roll = int(pending.roll) & 3
        # Ignored by the add_to_user_blocks expansion; the native index range
        # is only read by the UGC ``place_prefab_in_world`` path.
        packet.from_block_index = 0
        packet.to_block_index = 0
        packet.position = tuple(int(value) for value in pending.anchor)
        # The client blends this colour 50/50 with each model voxel exactly
        # like prefabs.expand_prefab did for the canonical server colours.
        base = pending.base_color or (0, 0, 0)
        packet.color = tuple(int(value) & 0xFF for value in base[:3])
        packet.add_to_user_blocks = True
        self.server.broadcast(
            bytes(packet.generate()), reliable=True, record_mutation=False
        )

    def reveal_to(self, connection) -> int:
        """Give a late joiner every block's health, which MapSync lacks.

        MapSync/VXL voxels carry no block health (client default 5.0) and
        packet-33 join catch-up cells are added at 3.0 (both measured), so a
        joiner would break a player-built cell (9.0) before the server does
        and would not show damage cracks.  One BlockManagerState(38) table
        (merged by the stock client, live 2026-09-26) carries every recorded
        non-default initial health (builds, lines, prefabs, block-cannon
        cells) plus every partially damaged cell's remaining health and
        original colour, so the joiner breaks each cell on the same hit as
        everyone else.  The live shade follows: PaintBlock(7) for cells whose
        per-hit (or painted) shade the rows' one-shot darkening misses, and
        the recorded hits themselves (single-cell Damage) for cells whose
        colour only the client knows (implicit interior / black voxels).
        Runs on the gameplay thread after the terrain catch-up replay and
        before ``in_game`` is set.  Returns the number of rows sent.
        """

        world = getattr(self.server, "world_manager", None)
        rows_of = getattr(world, "block_manager_rows", None)
        shade_of = getattr(world, "block_shade_rows", None)
        replays_of = getattr(world, "block_hit_replays", None)
        exact = callable(shade_of) and callable(replays_of)
        if callable(rows_of):
            user_rows, damaged_rows = (
                rows_of(replay_hits=True) if exact else rows_of()
            )
        else:
            iterate = getattr(world, "iter_block_health_state", None)
            if not callable(iterate):
                return 0
            user_rows, damaged_rows = list(iterate()), []
        batch = int(
            getattr(
                getattr(self.server, "config", None),
                "prefab_health_state_batch",
                BLOCK_STATE_DEFAULT_ROWS,
            )
        )
        for data in block_state_packets(user_rows, damaged_rows, batch):
            connection.send(data, reliable=True)
        if exact and callable(rows_of):
            loop_count = int(getattr(self.server, "loop_count", 0) or 0)
            for data in block_shade_packets(shade_of(), loop_count):
                connection.send(data, reliable=True)
            player = getattr(connection, "player", None)
            actor_id = int(getattr(player, "id", 0) or 0)
            for data in block_hit_replay_packets(replays_of(), actor_id):
                connection.send(data, reliable=True)
        return len(user_rows) + len(damaged_rows)

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
                and 0 <= coordinate[2] <= MAX_BUILD_Z
            ):
                continue
            if not snapshot.erase and coordinate[2] < MIN_BUILD_Z:
                # BuildPrefabAction is echoed as one native model range.  A
                # partial filter would desynchronise every client, so reject
                # the complete build by returning no prepared cells.
                return deque(), model_block_count
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
        pitch: int = 0,
        roll: int = 0,
        base_color: tuple[int, int, int] | None = None,
    ) -> bool:
        limit = max(
            1,
            min(128, int(getattr(self.server.config, "prefab_queue_limit", 32))),
        )
        # The queue is shared by every player; without a per-owner share one
        # client spamming packet 30 could fill it and starve everyone else's
        # (and every bot's) prefab placement.
        owner_id = int(getattr(player, "id", -1))
        owner_pending = sum(
            1 for pending in self._pending
            if int(getattr(pending.player, "id", -2)) == owner_id
        )
        if len(self._pending) >= limit or (
            not editor_native and owner_pending >= self.PER_PLAYER_PENDING_LIMIT
        ):
            construction = getattr(self.server, "construction", None)
            if construction is not None:
                construction.release(reservation)
            return False
        pending = self._new_pending(
            player,
            name=name,
            anchor=anchor,
            yaw=yaw,
            cells=cells,
            action_loop=action_loop,
            reservation=reservation,
            infinite=infinite,
            editor_native=editor_native,
            pitch=pitch,
            roll=roll,
            base_color=base_color,
        )
        if pending is None:
            return False
        self._pending.append(pending)
        return True

    def _new_pending(
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
        editor_native: bool,
        pitch: int,
        roll: int,
        base_color: tuple[int, int, int] | None,
    ) -> _PendingPrefab | None:
        """Reserve the wallet and build one pending action (or refuse it).

        Competitive prefabs charge every model voxel, including voxels that
        replace existing solids: the retail client's ``add_user_block``
        callback debits the owner once per model voxel on packet 30, so the
        server wallet must drop by exactly ``len(model points)``.  Native UGC
        editor builds keep charging only newly solid cells.
        """

        queued_cells = deque()
        reserved_blocks = 0
        world = self.server.world_manager
        for coordinate, color in cells:
            if infinite:
                charged = False
            elif editor_native:
                charged = not world.get_solid(*coordinate)
            else:
                charged = True
            queued_cells.append((coordinate, color, charged))
            reserved_blocks += int(charged)
        if reserved_blocks > int(player.blocks):
            construction = getattr(self.server, "construction", None)
            if construction is not None:
                construction.release(reservation)
            return None
        player.blocks -= reserved_blocks
        return _PendingPrefab(
            player=player,
            name=name,
            anchor=anchor,
            yaw=yaw,
            action_loop=action_loop,
            cells=queued_cells,
            total_cells=len(queued_cells),
            reservation=reservation,
            editor_native=editor_native,
            bot_owner=(
                _BotPrefabOwner.capture(player)
                if bool(getattr(player, "is_bot", False)) and not editor_native
                else None
            ),
            pitch=int(pitch) & 3,
            roll=int(roll) & 3,
            base_color=(
                None if base_color is None
                else tuple(int(value) & 0xFF for value in base_color[:3])
            ),
        )

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
            self._refund(pending, refund)
        construction = getattr(self.server, "construction", None)
        if construction is not None:
            construction.release(pending.reservation)

    def _refund(self, pending: _PendingPrefab, count: int) -> None:
        """Return only uncommitted paid cells to their original bot life."""

        player = pending.player
        if pending.bot_owner is not None:
            if (self.server.players.get(int(player.id)) is not player
                    or not pending.bot_owner.owns_inventory(player)):
                return
            wallet_max = getattr(player, "_block_wallet_max", None)
            if callable(wallet_max):
                # A real block pickup during a queued job may already have
                # filled the wallet. Refunds cannot exceed that same cap.
                count = min(count, max(0, int(wallet_max()) - int(player.blocks)))
        player.blocks += count

    def _finish(self, pending: _PendingPrefab) -> None:
        complete = PrefabComplete()
        pending.player.send(bytes(complete.generate()), reliable=True)
        if pending.placed and not pending.erase:
            from server.profile_stats import add
            from shared.constants import MAP_PREFAB_ADDED_TOTAL
            add(pending.player, MAP_PREFAB_ADDED_TOTAL)
            play_sound(
                self.server,
                SND_PREFAB_BUILD,
                position=pending.anchor,
                exclude=pending.player,
                reliable=False,
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

    @staticmethod
    def _within_build_reach(player: "Player", cells) -> bool:
        """Require the prefab footprint to be within the builder's reach.

        The retail ghost is anchored on the surface the crosshair hits, so
        the nearest footprint voxel is always within the ordinary block reach
        (MAX_BLOCK_DISTANCE plus the shared drift slack). Without this a
        forged packet 30 could build anywhere on the map.
        """

        from server.combat_runtime import BUILD_REACH

        eye = getattr(player, "eye", None)
        if eye is None:
            eye = (
                getattr(player, "x", math.nan),
                getattr(player, "y", math.nan),
                getattr(player, "z", math.nan),
            )
        try:
            ex, ey, ez = (float(value) for value in eye)
        except (TypeError, ValueError):
            return False
        if not all(math.isfinite(value) for value in (ex, ey, ez)):
            return False
        limit = float(BUILD_REACH) ** 2
        for (x, y, z), _rgb in cells:
            dx = x + 0.5 - ex
            dy = y + 0.5 - ey
            dz = z + 0.5 - ez
            if dx * dx + dy * dy + dz * dz <= limit:
                return True
        return False

    # Nearest footprint cells tried for line of sight (bounded work per packet).
    _VISIBILITY_SAMPLE_CELLS = 8

    def _footprint_visible(self, player: "Player", cells) -> bool:
        """Require the prefab ghost's anchor to be visible to the builder.

        The retail ghost sits on the surface the crosshair hits, so some
        footprint voxel near the eye is in line of sight. Reach alone let a
        forged packet 30 build inside sealed rooms behind walls.
        """

        from server.combat_runtime import cell_visible, reference_eyes

        _, eyes = reference_eyes(player)
        if not eyes:
            return False
        eye = eyes[0]
        ordered = sorted(
            (position for position, _rgb in cells),
            key=lambda c: sum((c[i] + 0.5 - eye[i]) ** 2 for i in range(3)),
        )
        world = getattr(self.server, "world_manager", None)
        for cell in ordered[: self._VISIBILITY_SAMPLE_CELLS]:
            if cell_visible(world, eyes, cell):
                return True
        from server import anticheat

        anticheat.report(
            self.server, player, "prefab_occluded",
            cell=ordered[0] if ordered else None,
        )
        return False

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
        if not build_z_is_safe(z):
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
        """Commit validated native UGC cells (non-deferred embedders)."""

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
        """Commit one native UGC editor cell to the canonical VXL.

        Packet 30 already makes every settled retail client expand the exact
        KV6.  Canonical WorldManager mutations still protect a client whose
        MapSync was in flight during this bounded commit.  Competitive
        prefabs never use this per-cell path (see :meth:`_commit_whole`).
        """

        x, y, z = coordinate
        if not build_z_is_safe(z):
            return False
        try:
            return bool(
                self.server.world_manager.set_block(
                    x, y, z, solid=True, color=color
                )
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            logger.exception("Prefab VXL commit failed at %s", coordinate)
            return False

    def _erase_cell(self, coordinate: tuple[int, int, int]) -> bool:
        """Remove one canonical editor cell; packet 31 owns live rendering."""

        try:
            return bool(self.server.world_manager.destroy_blocks((coordinate,)))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            logger.exception("Prefab VXL erase failed at %s", coordinate)
            return False
