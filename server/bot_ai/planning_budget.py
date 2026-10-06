"""Fair, bounded route work shared by one active AI worker.

The worker owns this object. Perception timestamps drive admission, while a
bounded duration sample records actual worker CPU-facing wall time. Deferral
means "try later", never evidence that terrain is impassable.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import math
from typing import Iterable


ObserverKey = tuple[int, int]
MAX_PENDING_OBSERVERS = 128
MAX_BURST_JOBS = 8
REQUESTS_PER_BOT = 6.0
MAX_SEARCHES_PER_JOB = 2
MAX_EXPANSIONS_PER_JOB = 512
MAX_PROFILE_SAMPLES = 256
# A job that found its route in a handful of nodes still paid for its setup.
MIN_JOB_CHARGE = 0.125
# Work one observer may spend in a single decision, in full jobs: a route with
# its two fallbacks, or a handful of an escape's small candidate searches.
MAX_DECISION_JOBS = 3.0


@dataclass(slots=True)
class PlanningJob:
    """One route request, including its ordinary-traversal/breach fallback."""

    searches: int = 0
    expansions: int = 0
    # Set on jobs this budget admitted; their unused work is handed back.
    observer: ObserverKey | None = None

    def begin_search(self, maximum: int) -> int:
        if self.searches >= MAX_SEARCHES_PER_JOB:
            return 0
        remaining = MAX_EXPANSIONS_PER_JOB - self.expansions
        if remaining <= 0:
            return 0
        self.searches += 1
        return min(max(0, int(maximum)), remaining)


class PlanningBudget:
    """Work credits handed out oldest waiter first; independent of bot order.

    One credit is one full job. A request is admitted when the credits cover
    it and every observer that has been waiting longer. The oldest waiter
    therefore owns the next credit and nobody is starved, yet a waiter that is
    not asking this instant no longer blocks everyone queued behind it. A job
    is charged for the search work it really did and the rest of its credit is
    returned, so short routes are cheap and several fit where one long search
    would.

    One decision may chain a route and its fallbacks, up to
    ``MAX_DECISION_JOBS`` of work, but only from credit no waiting observer
    has a claim on. A worker decision batch can spend at most one decision
    interval's credits, so idle time cannot accumulate a large burst. Absent
    observers expire instead of holding credit back from the fleet.
    """

    def __init__(self, requests_per_second: float = 24.0, *, decision_hz: float = 8.0) -> None:
        rate, frequency = float(requests_per_second), float(decision_hz)
        if not math.isfinite(rate) or rate <= 0.0:
            raise ValueError("planning rate must be finite and positive")
        if not math.isfinite(frequency) or frequency <= 0.0:
            raise ValueError("decision frequency must be finite and positive")
        self.configured_rate = rate
        self.decision_hz = frequency
        self.requests_per_second = rate
        self.burst = max(1, min(MAX_BURST_JOBS, math.ceil(rate / frequency)))
        self.stale_seconds = max(1.0, 2.0 / frequency)
        # Waiters in the order they began waiting: [since, last request].
        self._pending: OrderedDict[ObserverKey, list[float]] = OrderedDict()
        # Each observer's current decision: [timestamp, work spent, open job].
        self._turns: dict[ObserverKey, list] = {}
        self._last_time: float | None = None
        self._credits = float(self.burst)
        self._durations: deque[float] = deque(maxlen=MAX_PROFILE_SAMPLES)
        self._waits: deque[float] = deque(maxlen=MAX_PROFILE_SAMPLES)
        self.requested = 0
        self.granted = 0
        self.deferred = 0
        self.expired = 0
        self.queue_full = 0
        self.searches = 0
        self.expansions = 0
        self.longest_wait = 0.0
        self.last_denial = ""

    def reset(self) -> None:
        """Drop map/life scheduling state; retain cumulative profiling totals."""
        self._pending.clear()
        self._turns.clear()
        self._last_time = None
        self._credits = float(self.burst)

    def scale_for(self, bots: int) -> None:
        """Never ration a roster below a few route requests per bot per second.

        One fixed team-wide rate was tuned for a handful of bots. Ten bots on a
        terraced map ask about 28 times a second between them; at 24 more than
        half were deferred, and a bot working through an escape's candidate
        exits stood still for nine seconds waiting its turn. A request costs a
        few milliseconds on the AI worker, so the configured rate is kept as a
        floor and the ceiling follows the number of live bots.
        """
        rate = max(self.configured_rate, REQUESTS_PER_BOT * max(0, int(bots)))
        if rate != self.requests_per_second:
            self.requests_per_second = rate
            self.burst = max(1, min(MAX_BURST_JOBS, math.ceil(rate / self.decision_hz)))

    def refresh_waiters(self, observers: Iterable[ObserverKey], now: float) -> None:
        """Refresh a batch's live waiters before slow work expires their turn."""
        now = float(now)
        if not math.isfinite(now):
            raise ValueError("planning timestamp must be finite")
        for observer in observers:
            waiter = self._pending.get(observer)
            if waiter is not None:
                waiter[1] = max(now, waiter[1])

    def cancel(self, observer: ObserverKey) -> None:
        """A completed non-planning decision no longer needs its pending turn."""
        self._pending.pop(observer, None)

    def _advance(self, now: float) -> float:
        """Accrue credits up to ``now`` and forget observers that stopped asking."""
        if self._last_time is not None:
            now = max(now, self._last_time)
            self._credits = min(float(self.burst), self._credits +
                                (now - self._last_time) * self.requests_per_second)
        self._last_time = now
        cutoff = now - self.stale_seconds
        for key, waiter in tuple(self._pending.items()):
            if waiter[1] < cutoff:
                del self._pending[key]
                self.expired += 1
        for key, turn in tuple(self._turns.items()):
            if turn[0] < cutoff:
                del self._turns[key]
        return now

    def _used_turn(self, observer: ObserverKey, now: float) -> bool | None:
        """``None``: first request of this decision; ``True``: it may not run again."""
        turn = self._turns.get(observer)
        if turn is None or turn[0] != now:
            return None
        return turn[2] is not None or turn[1] > MAX_DECISION_JOBS - 1.0 + 1e-9

    @staticmethod
    def _claims_ahead(observer: ObserverKey, waiters: Iterable[ObserverKey],
                      first: bool) -> int:
        """Credits spoken for before this request can have one."""
        if not first:
            # A second helping never comes out of a waiting observer's share.
            return sum(1 for key in waiters if key != observer)
        ahead = 0
        for key in waiters:
            if key == observer:
                break
            ahead += 1
        return ahead

    def try_acquire(self, observer: ObserverKey, now: float) -> PlanningJob | None:
        now = float(now)
        if not math.isfinite(now):
            raise ValueError("planning timestamp must be finite")
        observer = int(observer[0]), int(observer[1])
        self.requested += 1
        now = self._advance(now)
        used = self._used_turn(observer, now)
        if used:
            self.deferred += 1
            self.last_denial = "decision"
            return None
        first = used is None
        if self._credits + 1e-9 < 1.0 + self._claims_ahead(observer, self._pending, first):
            self.deferred += 1
            self.last_denial = "credit" if self._credits + 1e-9 < 1.0 else "reserved"
            waiter = self._pending.get(observer)
            if waiter is not None:
                # Asking again deliberately does not change the waiting order.
                waiter[1] = now
            elif not first:
                # Served a moment ago: it queues behind the others next decision.
                pass
            elif len(self._pending) >= MAX_PENDING_OBSERVERS:
                self.queue_full += 1
                self.last_denial = "queue_full"
            else:
                self._pending[observer] = [now, now]
            return None
        waiter = self._pending.pop(observer, None)
        waited = now - waiter[0] if waiter is not None else 0.0
        self._waits.append(waited)
        self.longest_wait = max(self.longest_wait, waited)
        self._credits = max(0.0, self._credits - 1.0)
        job = PlanningJob(observer=observer)
        if first:
            self._turns[observer] = [now, 0.0, job]
            if len(self._turns) > MAX_PENDING_OBSERVERS:
                del self._turns[next(iter(self._turns))]
        else:
            self._turns[observer][2] = job
        self.granted += 1
        self.last_denial = ""
        return job

    def _preview(self, now: float) -> tuple[float, float, list[ObserverKey]]:
        """The clock, credits and live waiters ``try_acquire`` would see."""
        now = float(now)
        elapsed = 0.0
        if self._last_time is not None:
            now = max(now, self._last_time)
            elapsed = now - self._last_time
        credits = min(float(self.burst), self._credits + elapsed * self.requests_per_second)
        cutoff = now - self.stale_seconds
        return now, credits, [key for key, waiter in self._pending.items()
                              if waiter[1] >= cutoff]

    def would_grant(self, observer: ObserverKey, now: float) -> bool:
        """Whether ``try_acquire`` would admit this observer now; changes nothing."""
        observer = int(observer[0]), int(observer[1])
        now, credits, waiters = self._preview(now)
        used = self._used_turn(observer, now)
        return (not used and credits + 1e-9
                >= 1.0 + self._claims_ahead(observer, waiters, used is None))

    def spare(self, observer: ObserverKey, now: float) -> bool:
        """Is there credit that no waiting observer needs? Changes nothing.

        Work that can wait (map-wide guidance for a bot still walking, a route
        extension) runs on this. Half of a batch's credit stays free for
        whoever finds itself without a route later in the same batch.
        """
        observer = int(observer[0]), int(observer[1])
        now, credits, waiters = self._preview(now)
        return (not self._used_turn(observer, now) and credits + 1e-9
                >= 1.0 + max(1.0, self.burst / 2.0)
                + self._claims_ahead(observer, waiters, False))

    def finish(self, job: PlanningJob, seconds: float) -> None:
        self.searches += job.searches
        self.expansions += job.expansions
        if math.isfinite(seconds):
            self._durations.append(max(0.0, seconds) * 1000.0)
        observer, job.observer = job.observer, None
        turn = self._turns.get(observer) if observer is not None else None
        if turn is None or turn[2] is not job:
            return
        # Charge the search work actually done and hand the rest back.
        charge = min(1.0, max(MIN_JOB_CHARGE, job.expansions / MAX_EXPANSIONS_PER_JOB))
        turn[1] += charge
        turn[2] = None
        self._credits = min(float(self.burst), self._credits + 1.0 - charge)

    def snapshot(self) -> dict[str, int | float]:
        """Small serializable baseline, with nearest-rank p95 over <=256 jobs."""
        samples = sorted(self._durations)
        p95 = samples[max(0, math.ceil(len(samples) * 0.95) - 1)] if samples else 0.0
        waits = sorted(self._waits)
        wait_p95 = waits[max(0, math.ceil(len(waits) * 0.95) - 1)] if waits else 0.0
        return {
            "requested": self.requested, "granted": self.granted,
            "deferred": self.deferred, "expired": self.expired,
            "queue_full": self.queue_full, "pending": len(self._pending),
            "searches": self.searches, "expansions": self.expansions,
            "samples": len(samples), "work_p95_ms": p95,
            "work_max_ms": max(samples, default=0.0),
            "rate": self.requests_per_second, "burst": self.burst,
            "wait_p95_s": wait_p95, "wait_max_s": self.longest_wait,
        }
