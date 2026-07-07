"""Persistent ban list (keyed by client IP).

Bans survive restarts (bans.json in the working dir). Temporary bans store an
expiry timestamp and are auto-purged on lookup.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def address_host(peer) -> str:
    """A stable per-client key from an ENet peer address (IP without the
    ephemeral source port)."""
    try:
        addr = str(peer.address)
    except Exception:
        return "unknown"
    # ENet address stringifies as "host:port"; drop the port.
    return addr.rsplit(":", 1)[0].strip().strip("b'\"")


def parse_duration(text: Optional[str]) -> int:
    """'30m' / '2h' / '1d' / '90' (seconds) -> seconds. 'perma'/'' -> 0 (forever).
    Returns -1 if the token isn't a duration (so callers can treat it as a
    reason word instead)."""
    if not text:
        return 0
    t = text.strip().lower()
    if t in ("perma", "permanent", "forever"):
        return 0
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if t[-1] in units and t[:-1].isdigit():
        return int(t[:-1]) * units[t[-1]]
    if t.isdigit():
        return int(t)
    return -1


class BanManager:
    def __init__(self, path: str = "bans.json"):
        self.path = Path(path)
        self.bans: dict = {}  # ip -> {name, reason, until (0 = permanent)}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.bans = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Could not read %s; starting with an empty ban list", self.path)
                self.bans = {}

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.bans, indent=2), encoding="utf-8")
        except Exception:
            logger.error("Failed to write %s", self.path, exc_info=True)

    def add(self, ip: str, name: str, reason: str, duration_seconds: int = 0) -> None:
        until = 0 if duration_seconds <= 0 else time.time() + duration_seconds
        self.bans[ip] = {"name": name, "reason": reason, "until": until}
        self._save()

    def is_banned(self, ip: str) -> Optional[dict]:
        entry = self.bans.get(ip)
        if not entry:
            return None
        if entry.get("until") and time.time() >= entry["until"]:
            del self.bans[ip]
            self._save()
            return None
        return entry

    def remove(self, ip: str) -> bool:
        if ip in self.bans:
            del self.bans[ip]
            self._save()
            return True
        return False
