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
MAX_SEARCHES_PER_JOB = 2
MAX_EXPANSIONS_PER_JOB = 512
MAX_PROFILE_SAMPLES = 256


@dataclass(slots=True)
class PlanningJob:
    """One route request, including its ordinary-traversal/breach fallback."""

    searches: int = 0
    expansions: int = 0

    def begin_search(self, maximum: int) -> int:
        if self.searches >= MAX_SEARCHES_PER_JOB:
            return 0
        remaining = MAX_EXPANSIONS_PER_JOB - self.expansions
        if remaining <= 0:
            return 0
        self.searches += 1
        return min(max(0, int(maximum)), remaining)


class PlanningBudget:
    """Rate credits plus a coalesced FIFO; independent of bot iteration order.

    At most one job is admitted per observer/timestamp. A worker decision
    batch can spend at most one decision interval's credits, so idle time
    cannot accumulate a large burst. Live waiters keep their queue position;
    absent observers expire instead of blocking the entire fleet.
    """

    def __init__(self, requests_per_second: float = 24.0, *, decision_hz: float = 8.0) -> None:
        rate, frequency = float(requests_per_second), float(decision_hz)
        if not math.isfinite(rate) or rate <= 0.0:
            raise ValueError("planning rate must be finite and positive")
        if not math.isfinite(frequency) or frequency <= 0.0:
            raise ValueError("decision frequency must be finite and positive")
        self.requests_per_second = rate
        self.burst = max(1, min(MAX_BURST_JOBS, math.ceil(rate / frequency)))
        self.stale_seconds = max(1.0, 2.0 / frequency)
        self._pending: OrderedDict[ObserverKey, float] = OrderedDict()
        self._last_granted: dict[ObserverKey, float] = {}
        self._last_time: float | None = None
        self._credits = float(self.burst)
        self._durations: deque[float] = deque(maxlen=MAX_PROFILE_SAMPLES)
        self.requested = 0
        self.granted = 0
        self.deferred = 0
        self.expired = 0
        self.queue_full = 0
        self.searches = 0
        self.expansions = 0

    def reset(self) -> None:
        """Drop map/life scheduling state; retain cumulative profiling totals."""
        self._pending.clear()
        self._last_granted.clear()
        self._last_time = None
        self._credits = float(self.burst)

    def refresh_waiters(self, observers: Iterable[ObserverKey], now: float) -> None:
        """Refresh a batch's live waiters before slow work expires their turn."""
        now = float(now)
        if not math.isfinite(now):
            raise ValueError("planning timestamp must be finite")
        for observer in observers:
            if observer in self._pending:
                self._pending[observer] = max(now, self._pending[observer])

    def cancel(self, observer: ObserverKey) -> None:
        """A completed non-planning decision no longer needs its pending turn."""
        self._pending.pop(observer, None)

    def try_acquire(self, observer: ObserverKey, now: float) -> PlanningJob | None:
        now = float(now)
        if not math.isfinite(now):
            raise ValueError("planning timestamp must be finite")
        observer = int(observer[0]), int(observer[1])
        self.requested += 1
        if self._last_time is not None:
            now = max(now, self._last_time)
            self._credits = min(float(self.burst), self._credits +
                                (now - self._last_time) * self.requests_per_second)
        self._last_time = now
        cutoff = now - self.stale_seconds
        for key, last_request in tuple(self._pending.items()):
            if last_request < cutoff:
                del self._pending[key]
                self.expired += 1
        for key, last_grant in tuple(self._last_granted.items()):
            if last_grant < cutoff:
                del self._last_granted[key]
        if self._last_granted.get(observer) == now:
            self.deferred += 1
            return None
        if observer not in self._pending and len(self._pending) >= MAX_PENDING_OBSERVERS:
            self.queue_full += 1
            self.deferred += 1
            return None
        # Updating an existing key deliberately does not change FIFO order.
        self._pending[observer] = now
        if next(iter(self._pending)) != observer or self._credits + 1e-9 < 1.0:
            self.deferred += 1
            return None
        self._pending.pop(observer)
        self._credits = max(0.0, self._credits - 1.0)
        self._last_granted[observer] = now
        if len(self._last_granted) > MAX_PENDING_OBSERVERS:
            del self._last_granted[next(iter(self._last_granted))]
        self.granted += 1
        return PlanningJob()

    def finish(self, job: PlanningJob, seconds: float) -> None:
        self.searches += job.searches
        self.expansions += job.expansions
        if math.isfinite(seconds):
            self._durations.append(max(0.0, seconds) * 1000.0)

    def snapshot(self) -> dict[str, int | float]:
        """Small serializable baseline, with nearest-rank p95 over <=256 jobs."""
        samples = sorted(self._durations)
        p95 = samples[max(0, math.ceil(len(samples) * 0.95) - 1)] if samples else 0.0
        return {
            "requested": self.requested, "granted": self.granted,
            "deferred": self.deferred, "expired": self.expired,
            "queue_full": self.queue_full, "pending": len(self._pending),
            "searches": self.searches, "expansions": self.expansions,
            "samples": len(samples), "work_p95_ms": p95,
            "work_max_ms": max(samples, default=0.0),
            "rate": self.requests_per_second, "burst": self.burst,
        }
