"""Bounded post-physics world mutation scheduling.

Retail movement history is recorded before a locally requested terrain edit is
committed by the echoed block packet.  Applying that edit during tick-start
packet draining makes the server replay an old movement frame against a newer
collision map.  This service keeps client-origin topology changes ordered by
their client loop and commits them only after the owner has simulated through
that loop.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .main import BattleSpadesServer


@dataclass(frozen=True)
class PendingWorldMutation:
    """One validated client-origin mutation waiting for its physics boundary.

    ``apply`` and ``cancel`` run synchronously on the gameplay thread.  They
    must not perform blocking I/O.  ``cancel`` restores any resources reserved
    while validating the request.
    """

    owner_id: int
    action_loop: int
    enqueued_tick: int
    kind: str
    cell_count: int
    apply: Callable[[], None]
    cancel: Callable[[], None]


class WorldMutationService:
    """Commit bounded terrain changes after player movement simulation.

    The network packet drain validates and enqueues requests.  ``commit_ready``
    runs once per 60 Hz gameplay tick, immediately after all player physics and
    before replication.  A mutation is ready once its owner's authoritative
    simulation has consumed the client loop that emitted the action.
    """

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server
        self._pending: deque[PendingWorldMutation] = deque()

    @property
    def pending_count(self) -> int:
        """Return the current bounded queue depth."""

        return len(self._pending)

    def enqueue(self, mutation: PendingWorldMutation) -> bool:
        """Queue a validated mutation, cancelling it when capacity is full."""

        limit = max(
            1,
            int(getattr(self.server.config, "world_mutation_queue_limit", 2048)),
        )
        if len(self._pending) >= limit:
            mutation.cancel()
            self.server.metrics.rejected_world_mutations += 1
            return False
        self._pending.append(mutation)
        self.server.metrics.world_mutation_queue_peak = max(
            self.server.metrics.world_mutation_queue_peak,
            len(self._pending),
        )
        return True

    def commit_ready(self) -> int:
        """Commit ready mutations within the configured per-tick work budget.

        Returns the number committed.  Unready work remains queued.  Expired or
        disconnected-owner work is cancelled so a malformed future loop cannot
        retain inventory or consume memory indefinitely.
        """

        if not self._pending:
            return 0

        mutation_budget = max(
            1,
            int(getattr(self.server.config, "world_mutation_batch_limit", 256)),
        )
        cell_budget = max(
            1,
            int(getattr(
                self.server.config,
                "world_mutation_cell_budget",
                4096,
            )),
        )
        timeout_ticks = max(
            1,
            int(getattr(
                self.server.config,
                "world_mutation_timeout_ticks",
                180,
            )),
        )

        scan_count = len(self._pending)
        committed = 0
        committed_cells = 0
        for _ in range(scan_count):
            mutation = self._pending.popleft()
            owner = self.server.players.get(mutation.owner_id)
            expired = (
                self.server.loop_count - mutation.enqueued_tick
            ) >= timeout_ticks
            if owner is None or expired:
                mutation.cancel()
                self.server.metrics.expired_world_mutations += 1
                continue

            owner_loop = getattr(owner, "last_applied_input_loop", None)
            # Server-owned bots have no retail ClientData history label. Their
            # public actions are submitted before native bot physics, and this
            # service is called immediately after that physics in the same
            # fixed tick, so the ordering boundary itself makes them ready.
            # Waiting for a client loop that can never exist leaked the
            # reservation until timeout and repaired the bot's valid build.
            ready = bool(getattr(owner, "is_bot", False)) or (
                owner_loop is not None
                and int(owner_loop) >= mutation.action_loop
            )
            over_budget = (
                committed >= mutation_budget
                or committed_cells + mutation.cell_count > cell_budget
            )
            if not ready or over_budget:
                self._pending.append(mutation)
                continue

            mutation.apply()
            committed += 1
            committed_cells += mutation.cell_count
            self.server.metrics.committed_world_mutations += 1

        return committed

    def cancel_all(self) -> None:
        """Cancel all queued work during a round/server teardown."""

        while self._pending:
            self._pending.popleft().cancel()

    def cancel_owner(self, owner_id: int) -> int:
        """Cancel queued mutations belonging to one departing player.

        Player ids are compact wire values and are reused immediately.  A
        mutation closure also captures the original ``Player`` object, so
        merely looking up ``players[owner_id]`` at commit time cannot prove
        ownership after a reconnect.  This method runs synchronously from the
        disconnect path before that id is made available again.

        Returns the number of reservations cancelled.
        """

        owner_id = int(owner_id)
        kept: deque[PendingWorldMutation] = deque()
        cancelled = 0
        while self._pending:
            mutation = self._pending.popleft()
            if mutation.owner_id == owner_id:
                mutation.cancel()
                cancelled += 1
            else:
                kept.append(mutation)
        self._pending = kept
        return cancelled
