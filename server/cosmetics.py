"""Account appearances, on an explicitly negotiated BattleSpades-only envelope.

No model data, gameplay attributes or client claims enter this channel. The
master resolves equipped IDs for identities established by consumed tickets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CAPABILITY = "battlespades-cosmetics-v1"
PACKET_ID = 240
MAGIC = bytes((PACKET_ID,)) + b"BSC1"
MAX_PACKET_BYTES = 8192
_ID = re.compile(r"^[a-z0-9-]{1,80}$")
_SLOT = re.compile(r"^(weapon:[0-9]{1,3}:world|class:[0-9]{1,3}:(body|hat)|tombstone)$")
logger = logging.getLogger(__name__)


def capable(connection) -> bool:
    """Never infer support from a nickname, ENet version, or launcher identity."""
    return CAPABILITY in getattr(getattr(connection, "player", None), "client_capabilities", ())


def appearance_packet(player_id: int, items: object) -> bytes:
    if type(player_id) is not int or not 0 <= player_id <= 255:
        raise ValueError("Invalid cosmetic player ID")
    clean: dict[str, str] = {}
    if isinstance(items, list):
        for item in items[:64]:
            if not isinstance(item, dict):
                continue
            slot, cosmetic = item.get("slot"), item.get("cosmetic_id")
            if isinstance(slot, str) and isinstance(cosmetic, str) and _SLOT.fullmatch(slot) and _ID.fullmatch(cosmetic):
                clean[slot] = cosmetic
    packet = MAGIC + json.dumps({"player_id": player_id, "items": clean},
                                sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(packet) > MAX_PACKET_BYTES:
        raise ValueError("Cosmetic appearance exceeds transport limit")
    return packet


class CosmeticReplication:
    def __init__(self, bridge) -> None:
        self.bridge = bridge
        self.task: asyncio.Task | None = None
        self.cached: dict[str, list] = {}
        self.sent: dict[object, dict[int, bytes]] = {}

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="battlespades-cosmetics")

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        self.cached.clear()
        self.sent.clear()

    def _fetch(self, ids: list[str]) -> dict[str, list]:
        result = {}
        for offset in range(0, len(ids), 32):
            batch = ids[offset:offset + 32]
            query = urlencode({"player_id": batch}, doseq=True)
            request = Request(self.bridge.base_url + "/api/cosmetics/equipped?" + query,
                              headers={"Accept": "application/json", "User-Agent": "BattleSpades/1.0"})
            with urlopen(request, timeout=5, context=ssl.create_default_context()) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError("Appearance response is too large")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise ValueError("Unsupported appearance response")
            rows = payload.get("players")
            if not isinstance(rows, list) or len(rows) > 32:
                raise ValueError("Invalid appearance players")
            for row in rows:
                if isinstance(row, dict) and row.get("player_id") in batch and isinstance(row.get("items"), list):
                    result[row["player_id"]] = row["items"]
        return result

    def publish(self) -> None:
        connections = tuple(getattr(self.bridge.server, "connections", {}).values())
        recipients = {c for c in connections if c.in_game and capable(c)}
        self.sent = {c: rows for c, rows in self.sent.items() if c in recipients}
        # Resolve the live roster AFTER awaiting HTTP. A disconnected object's
        # response must never label the next person reusing its player index.
        players = [c.player for c in connections if c.player is not None]
        current = {p.id: appearance_packet(p.id, self.cached.get(
            getattr(p, "account_public_id", None), [])) for p in players}
        for connection in recipients:
            previous = self.sent.setdefault(connection, {})
            for player_id in previous.keys() - current.keys():
                connection.send(appearance_packet(player_id, []))
            for player_id, packet in current.items():
                if previous.get(player_id) != packet:
                    connection.send(packet)
            self.sent[connection] = current.copy()

    async def _run(self) -> None:
        while True:
            try:
                connections = tuple(getattr(self.bridge.server, "connections", {}).values())
                if any(c.in_game and capable(c) for c in connections):
                    ids = sorted({c.player.account_public_id for c in connections
                                  if c.player and getattr(c.player, "account_public_id", None)})
                    fresh = await asyncio.to_thread(self._fetch, ids)
                    self.cached = {key: fresh.get(key, self.cached.get(key, [])) for key in ids}
                    self.publish()
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, TypeError):
                logger.warning("Appearance refresh unavailable; retaining last verified outfits", exc_info=True)
            except Exception:
                # A presentation failure must not stop future refreshes or
                # propagate through master shutdown into the game lifecycle.
                logger.exception("Appearance publication failed; retrying next refresh")
            await asyncio.sleep(5)
