"""Join password for private servers (packets 111, 112, 113).

Retail layouts: ``PasswordNeeded(112)`` is the bare id, ``PasswordProvided
(113)`` and ``Password(111)`` carry one NUL-terminated string. The retail
LoadingMenu showed a masked box on 112 and answered with 113, but that branch
is disabled in the shipped client (its passwords went through the Steam
lobby), so only the native BattleSpades client can complete this challenge.

The server asks after the client's first packet and sends nothing else - no
InitialInfo, map or state - until the answer is right. See docs/PROTOCOL.md,
"Password-protected servers".
"""
from __future__ import annotations

import hmac
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Longer answers are wrong without being compared.
MAX_PASSWORD_BYTES = 64
# Two answers closer together than this are a script, not a person.
MIN_ATTEMPT_INTERVAL = 0.5
# Bound on remembered addresses; oldest lockouts are dropped first.
MAX_TRACKED_ADDRESSES = 4096

ACCEPTED = "accepted"
RETRY = "retry"
REFUSED = "refused"


def _encode(text: object) -> bytes:
    return ("" if text is None else str(text)).encode("utf-8", "replace")


class JoinPasswordGate:
    """The configured password and the addresses currently locked out."""

    def __init__(self, config):
        self.config = config
        self._locked_until: dict[str, float] = {}

    @property
    def secret(self) -> bytes:
        value = getattr(self.config, "join_password", "")
        return _encode(value) if isinstance(value, str) else b""

    @property
    def enabled(self) -> bool:
        return bool(self.secret)

    @property
    def max_attempts(self) -> int:
        return max(1, int(getattr(self.config, "password_max_attempts", 3)))

    @property
    def timeout(self) -> float:
        return max(1.0, float(getattr(self.config, "password_timeout_seconds", 60.0)))

    @property
    def lockout(self) -> float:
        return max(0.0, float(getattr(self.config, "password_lockout_seconds", 60.0)))

    def matches(self, candidate: object) -> bool:
        """Constant-time comparison of one answer against the secret."""
        given = _encode(candidate)
        secret = self.secret
        if not secret or len(given) > MAX_PASSWORD_BYTES:
            # Still spend a comparison so the length is not a timing oracle.
            hmac.compare_digest(secret, secret)
            return False
        return hmac.compare_digest(given, secret)

    def locked_out(self, address: str, now: Optional[float] = None) -> bool:
        until = self._locked_until.get(str(address))
        if until is None:
            return False
        if (time.monotonic() if now is None else now) >= until:
            self._locked_until.pop(str(address), None)
            return False
        return True

    def lock(self, address: str, now: Optional[float] = None) -> None:
        if self.lockout <= 0.0:
            return
        now = time.monotonic() if now is None else now
        if len(self._locked_until) >= MAX_TRACKED_ADDRESSES:
            for stale in sorted(self._locked_until, key=self._locked_until.get)[
                : MAX_TRACKED_ADDRESSES // 4
            ]:
                self._locked_until.pop(stale, None)
        self._locked_until[str(address)] = now + self.lockout


def gate_for(server) -> JoinPasswordGate:
    """The server's gate, created on first use."""
    gate = getattr(server, "join_password_gate", None)
    if not isinstance(gate, JoinPasswordGate) or gate.config is not server.config:
        gate = JoinPasswordGate(server.config)
        server.join_password_gate = gate
    return gate


def peer_host(connection) -> str:
    """The address without its port: a lockout is per machine."""
    relay = getattr(getattr(connection, 'server', None), 'steam_p2p', None)
    if relay is not None:
        identity = relay.identity_for(connection.peer)
        if identity is not None:
            return identity
    address = getattr(getattr(connection, "peer", None), "address", None)
    host = getattr(address, "host", None)
    if host is None and isinstance(address, tuple) and address:
        host = address[0]
    if host is None:
        host = str(address).rsplit(":", 1)[0]
    return str(host)


class JoinPasswordSession:
    """One connection's progress through the challenge."""

    def __init__(self):
        self.cleared = False
        self.asked_at: Optional[float] = None
        self.attempts = 0
        self.last_attempt_at: Optional[float] = None

    @property
    def pending(self) -> bool:
        return self.asked_at is not None and not self.cleared

    def answer(self, gate: JoinPasswordGate, candidate: object,
               now: Optional[float] = None) -> str:
        """Judge one answer: ACCEPTED, RETRY (ask again) or REFUSED (kick)."""
        now = time.monotonic() if now is None else now
        rushed = (
            self.last_attempt_at is not None
            and now - self.last_attempt_at < MIN_ATTEMPT_INTERVAL
        )
        self.last_attempt_at = now
        # Compare even a rushed answer, so pacing reveals nothing.
        correct = gate.matches(candidate)
        if correct and not rushed:
            self.cleared = True
            return ACCEPTED
        self.attempts += 1
        if self.attempts >= gate.max_attempts:
            return REFUSED
        return RETRY
