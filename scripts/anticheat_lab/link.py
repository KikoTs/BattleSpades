"""Network impairment and ENet delivery semantics for the in-process lab.

One :class:`Link` is one direction of one client's connection. The sender
groups every packet of one frame/tick into a datagram (ENet does the same in
``enet_host_service``), the impairment decides that datagram's fate, and the
receiver applies ENet's three delivery classes:

* ``RELIABLE``     - never lost: a dropped datagram is resent after the
  retransmission timeout; delivered in order, so later reliable AND later
  sequenced packets wait behind a lost one (head-of-line blocking).
* ``SEQUENCED``    - ENet flag 0 (WorldUpdate): lost with its datagram, and
  discarded when a newer sequenced packet was already delivered.
* ``UNSEQUENCED``  - ClientData / ClockSync: lost with its datagram, delivered
  on arrival in any order.

The impairment has the same knobs as ``scripts/udp_lag_proxy.py`` (one-way
delay, uniform jitter, random loss) plus burst loss (two-state Gilbert
model) and delay spikes (a stalled link that then delivers everything it
held at once, which is what bufferbloat and Wi-Fi retries look like).
"""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from typing import Callable, Optional

RELIABLE = "reliable"
SEQUENCED = "sequenced"
UNSEQUENCED = "unsequenced"


@dataclass(frozen=True)
class NetProfile:
    """One network condition. Times in milliseconds, one way."""

    name: str
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    loss: float = 0.0
    # Gilbert burst loss: chance per datagram to enter a burst, and the mean
    # burst length in datagrams. Inside a burst every datagram is lost.
    burst_rate: float = 0.0
    burst_length: float = 0.0
    # Delay spikes: expected spikes per second and their length.
    spike_rate: float = 0.0
    spike_ms: float = 0.0

    @property
    def rtt_ms(self) -> float:
        return 2.0 * self.delay_ms

    def describe(self) -> str:
        parts = [f"{self.delay_ms:g} ms one way"]
        if self.jitter_ms:
            parts.append(f"+/-{self.jitter_ms:g} ms jitter")
        if self.loss:
            parts.append(f"{self.loss * 100:g}% loss")
        if self.burst_rate:
            parts.append(
                f"bursts {self.burst_rate * 100:g}%/dgram x{self.burst_length:g}"
            )
        if self.spike_rate:
            parts.append(f"{self.spike_ms:g} ms spikes {self.spike_rate:g}/s")
        return ", ".join(parts)


# The matrix the false-positive run covers. ``delay_ms`` is ONE WAY, so the
# names carry the round trip a player would see as "ping".
PROFILES: dict[str, NetProfile] = {
    profile.name: profile
    for profile in (
        NetProfile("lan"),
        NetProfile("ping80", delay_ms=40.0, jitter_ms=8.0, loss=0.005),
        NetProfile("ping80_jitter", delay_ms=40.0, jitter_ms=30.0, loss=0.01),
        NetProfile("ping150", delay_ms=75.0, jitter_ms=30.0, loss=0.01),
        NetProfile("ping250", delay_ms=125.0, jitter_ms=30.0, loss=0.02),
        NetProfile(
            "ping150_burst", delay_ms=75.0, jitter_ms=30.0, loss=0.01,
            burst_rate=0.004, burst_length=12.0,
        ),
        NetProfile(
            "ping250_burst", delay_ms=125.0, jitter_ms=30.0, loss=0.02,
            burst_rate=0.006, burst_length=18.0,
        ),
        NetProfile(
            "ping80_spikes", delay_ms=40.0, jitter_ms=15.0, loss=0.005,
            spike_rate=0.15, spike_ms=350.0,
        ),
    )
}

# The four headline conditions of the assignment (0 / 80 / 150 / 250 ms).
CORE_PROFILES = ("lan", "ping80_jitter", "ping150_burst", "ping250_burst")


class Impairment:
    """Seeded fate of each datagram on one direction of a link."""

    def __init__(self, profile: NetProfile, seed: int) -> None:
        self.profile = profile
        self.rng = random.Random(seed)
        self._burst_left = 0
        self._spike_until = -1.0
        self._last_now: Optional[float] = None
        self.sent = 0
        self.lost = 0

    def schedule(self, now: float) -> Optional[float]:
        """Arrival time of a datagram sent at ``now``; ``None`` when lost."""

        profile = self.profile
        self.sent += 1
        elapsed = 0.0 if self._last_now is None else max(0.0, now - self._last_now)
        self._last_now = now
        if profile.spike_rate > 0.0 and now >= self._spike_until:
            if self.rng.random() < profile.spike_rate * elapsed:
                self._spike_until = now + profile.spike_ms / 1000.0
        if self._burst_left > 0:
            self._burst_left -= 1
            self.lost += 1
            return None
        if profile.burst_rate > 0.0 and self.rng.random() < profile.burst_rate:
            mean = max(1.0, profile.burst_length)
            # Geometric length with the requested mean.
            length = 1
            while self.rng.random() > 1.0 / mean and length < 600:
                length += 1
            self._burst_left = length - 1
            self.lost += 1
            return None
        if profile.loss > 0.0 and self.rng.random() < profile.loss:
            self.lost += 1
            return None
        extra = (
            self.rng.uniform(-profile.jitter_ms, profile.jitter_ms)
            if profile.jitter_ms > 0.0 else 0.0
        )
        arrival = now + max(0.0, profile.delay_ms + extra) / 1000.0
        if now < self._spike_until:
            # The link is stalled: everything leaves when the stall ends.
            arrival = max(arrival, self._spike_until + profile.delay_ms / 1000.0)
        return arrival


@dataclass(order=True)
class _Datagram:
    arrival: float
    order: int
    commands: list = field(compare=False)


@dataclass
class _Command:
    kind: str
    payload: object
    reliable_seq: int       # own sequence (RELIABLE) or the last one sent before it
    sequenced_seq: int = 0  # SEQUENCED only
    sent_at: float = 0.0


class Link:
    """One direction of one connection."""

    def __init__(
        self,
        impairment: Impairment,
        deliver: Callable[[object, str], None],
        *,
        rto_floor: float = 0.05,
    ) -> None:
        self.impairment = impairment
        self.deliver = deliver
        self.rto_floor = float(rto_floor)
        self._outbox: list[_Command] = []
        self._in_flight: list[_Datagram] = []
        self._order = 0
        self._reliable_sent = 0
        self._sequenced_sent = 0
        # Receiver state.
        self._reliable_delivered = 0
        self._sequenced_delivered = 0
        self._held_reliable: dict[int, _Command] = {}
        self._held_sequenced: list[_Command] = []
        self.stats = {
            "datagrams": 0, "lost": 0, "retransmits": 0,
            "delivered": 0, "sequenced_dropped": 0,
        }

    # -- sender ------------------------------------------------------------

    def send(self, payload, kind: str, now: float) -> None:
        if kind == RELIABLE:
            self._reliable_sent += 1
            command = _Command(kind, payload, self._reliable_sent, 0, now)
        elif kind == SEQUENCED:
            self._sequenced_sent += 1
            command = _Command(
                kind, payload, self._reliable_sent, self._sequenced_sent, now
            )
        else:
            command = _Command(kind, payload, self._reliable_sent, 0, now)
        self._outbox.append(command)

    def flush(self, now: float) -> None:
        """Put everything queued since the last flush into one datagram."""

        if not self._outbox:
            return
        commands, self._outbox = self._outbox, []
        self._transmit(commands, now)

    def _rto(self) -> float:
        profile = self.impairment.profile
        return max(
            self.rto_floor,
            (2.0 * profile.delay_ms + 4.0 * profile.jitter_ms) / 1000.0,
        )

    def _transmit(self, commands: list, now: float) -> None:
        self.stats["datagrams"] += 1
        arrival = self.impairment.schedule(now)
        if arrival is None:
            self.stats["lost"] += 1
            resend = [c for c in commands if c.kind == RELIABLE]
            if resend:
                # ENet resends unacknowledged reliable commands after its
                # round-trip timeout; the resend is a datagram of its own.
                self.stats["retransmits"] += 1
                self._order += 1
                heapq.heappush(
                    self._in_flight,
                    _Datagram(now + self._rto(), self._order, ["resend", resend]),
                )
            return
        self._order += 1
        heapq.heappush(self._in_flight, _Datagram(arrival, self._order, commands))

    # -- receiver ----------------------------------------------------------

    def pump(self, now: float) -> None:
        """Deliver every datagram that has arrived by ``now``."""

        while self._in_flight and self._in_flight[0].arrival <= now:
            datagram = heapq.heappop(self._in_flight)
            commands = datagram.commands
            if commands and commands[0] == "resend":
                # A retransmission leaves the sender now and is impaired again.
                self._transmit(commands[1], datagram.arrival)
                continue
            for command in commands:
                self._receive(command)

    def _receive(self, command: _Command) -> None:
        if command.kind == UNSEQUENCED:
            self._deliver(command)
            return
        if command.kind == RELIABLE:
            if command.reliable_seq <= self._reliable_delivered:
                return  # duplicate of a retransmission
            self._held_reliable[command.reliable_seq] = command
            self._release()
            return
        # SEQUENCED
        if command.reliable_seq > self._reliable_delivered:
            self._held_sequenced.append(command)
            return
        self._deliver_sequenced(command)

    def _release(self) -> None:
        progressed = True
        while progressed:
            progressed = False
            following = self._held_reliable.pop(self._reliable_delivered + 1, None)
            if following is not None:
                self._reliable_delivered = following.reliable_seq
                self._deliver(following)
                progressed = True
            if self._held_sequenced:
                ready = [
                    c for c in self._held_sequenced
                    if c.reliable_seq <= self._reliable_delivered
                ]
                if ready:
                    self._held_sequenced = [
                        c for c in self._held_sequenced
                        if c.reliable_seq > self._reliable_delivered
                    ]
                    for command in sorted(ready, key=lambda c: c.sequenced_seq):
                        self._deliver_sequenced(command)
                    progressed = True

    def _deliver_sequenced(self, command: _Command) -> None:
        if command.sequenced_seq <= self._sequenced_delivered:
            self.stats["sequenced_dropped"] += 1
            return
        self._sequenced_delivered = command.sequenced_seq
        self._deliver(command)

    def _deliver(self, command: _Command) -> None:
        self.stats["delivered"] += 1
        self.deliver(command.payload, command.kind)

    @property
    def idle(self) -> bool:
        return not self._in_flight and not self._outbox
