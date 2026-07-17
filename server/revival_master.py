"""AoS Revival master-server registration, join identity, and result bridge.

All network I/O runs through ``asyncio.to_thread``.  A slow or unavailable web
service can therefore refuse a ranked join or delay a heartbeat, but it can
never stall the authoritative 60 Hz simulation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
import re
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from server.mode_data import get as get_mode_data


logger = logging.getLogger(__name__)

JOIN_CODE_PATTERN = re.compile(r"^~[A-Za-z0-9_-]{14}$")
SERVER_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MODE_TOTAL_STAT = {
    "tdm": 192,
    "vip": 193,
    "tc": 194,
    "occupation": 195,
    "occ": 195,
    "diamond_mine": 196,
    "dia": 196,
    "ctf": 197,
    "zombie": 198,
    "zom": 198,
    "demolition": 199,
    "dem": 199,
    "multihill": 200,
    "mh": 200,
}


class RevivalMasterError(RuntimeError):
    """Base error for an unavailable or rejecting master service."""


class JoinTicketRejected(RevivalMasterError):
    """The join code is invalid, expired, consumed, or server-mismatched."""


class JoinTicketUnavailable(RevivalMasterError):
    """The master could not safely decide whether the join code is valid."""


@dataclass(frozen=True)
class RevivalIdentity:
    public_id: str
    legacy_id: str
    nickname: str
    account_type: str
    identity_type: str
    ranked_eligible: bool
    steam_id: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RevivalIdentity":
        public_id = str(payload.get("public_id") or "")
        legacy_id = str(payload.get("legacy_id") or "")
        nickname = str(payload.get("nickname") or "").strip()
        account_type = str(payload.get("account_type") or "")
        identity_type = str(payload.get("identity_type") or "")
        if (
            not public_id.startswith("ply_")
            or not legacy_id.isdigit()
            or not nickname
            or account_type not in {"guest", "registered"}
            or identity_type not in {"guest", "password", "steam"}
        ):
            raise JoinTicketRejected("master returned an invalid player identity")
        return cls(
            public_id=public_id,
            legacy_id=legacy_id,
            nickname=nickname,
            account_type=account_type,
            identity_type=identity_type,
            ranked_eligible=bool(payload.get("ranked_eligible", False)),
            steam_id=(
                str(payload["steam_id"])
                if payload.get("steam_id") is not None
                else None
            ),
        )


def is_join_code(value: object) -> bool:
    return bool(JOIN_CODE_PATTERN.fullmatch(str(value or "")))


class RevivalMasterService:
    """Non-blocking bridge owned by one :class:`BattleSpadesServer`."""

    def __init__(self, server) -> None:
        self.server = server
        self.config = getattr(server.config, "revival", None)
        self._heartbeat_task: asyncio.Task | None = None
        self._closing = False
        self._player_baselines: dict[int, tuple[int, int, int, int]] = {}
        self._departed: dict[str, dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config is not None and getattr(self.config, "enabled", False))

    @property
    def write_token(self) -> str:
        return os.environ.get("AOS_MASTER_WRITE_TOKEN", "").strip()

    @property
    def base_url(self) -> str:
        configured = os.environ.get("AOS_MASTER_URL") or getattr(
            self.config, "base_url", "https://www.aosplay.net"
        )
        return str(configured).rstrip("/")

    @property
    def public_host(self) -> str:
        configured = os.environ.get("AOS_PUBLIC_HOST") or getattr(
            self.config, "public_host", "127.0.0.1"
        )
        return str(configured).strip()

    @property
    def server_id(self) -> str:
        derived = "%s:%d" % (
            self.public_host,
            int(self.server.config.port),
        )
        configured = os.environ.get("AOS_SERVER_ID") or getattr(
            self.config, "server_id", ""
        )
        identifier = str(configured).strip() or derived
        if not SERVER_ID_PATTERN.fullmatch(identifier):
            raise RevivalMasterError(
                "revival server_id must contain only letters, digits, ., _, :, or -"
            )
        if identifier != derived:
            raise RevivalMasterError(
                "revival server_id must equal public_host:server.port (%s)" % derived
            )
        return identifier

    async def start(self) -> None:
        if not self.enabled:
            logger.info("AoS Revival master registration disabled")
            return
        if not self.write_token:
            logger.warning(
                "AoS Revival master disabled: AOS_MASTER_WRITE_TOKEN is not set"
            )
            return
        try:
            await self.publish_heartbeat()
        except RevivalMasterError as error:
            logger.warning("Initial Revival heartbeat failed: %s", error)
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="aos-revival-heartbeat",
        )

    async def close(self) -> None:
        self._closing = True
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _heartbeat_loop(self) -> None:
        interval = min(
            60.0,
            max(15.0, float(getattr(self.config, "heartbeat_interval_seconds", 30.0))),
        )
        while not self._closing:
            try:
                await asyncio.sleep(interval)
                await self.publish_heartbeat()
            except asyncio.CancelledError:
                raise
            except RevivalMasterError as error:
                logger.warning("Revival heartbeat failed: %s", error)
            except Exception:
                logger.exception("Unexpected Revival heartbeat failure")

    def _request_json(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer %s" % self.write_token,
                "Content-Type": "application/json",
                "User-Agent": "BattleSpades/1.0 RevivalBridge/1",
            },
        )
        timeout = min(
            15.0,
            max(1.0, float(getattr(self.config, "request_timeout_seconds", 5.0))),
        )
        try:
            with urlopen(
                request,
                timeout=timeout,
                context=ssl.create_default_context(),
            ) as response:
                status = int(response.status)
                raw = response.read()
        except HTTPError as error:
            status = int(error.code)
            raw = error.read()
        except (URLError, TimeoutError, OSError) as error:
            raise RevivalMasterError(str(error)) from error
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RevivalMasterError("master returned invalid JSON") from error
        return status, decoded if isinstance(decoded, dict) else {}

    async def _post(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not self.write_token:
            raise RevivalMasterError("AOS_MASTER_WRITE_TOKEN is not configured")
        return await asyncio.to_thread(self._request_json, path, payload)

    def heartbeat_payload(self) -> dict[str, Any]:
        game_port = int(self.server.config.port)
        steam = getattr(self.server.config, "steam", None)
        query_port = (
            int(steam.effective_query_port(game_port))
            if steam is not None and bool(getattr(steam, "enabled", False))
            else game_port
        )
        mode = get_mode_data(
            getattr(
                self.server.config,
                "game_mode",
                self.server.config.default_mode,
            )
        )
        world = getattr(self.server, "world_manager", None)
        map_name = str(
            getattr(world, "map_name", "")
            or getattr(
                self.server.config,
                "map_name",
                self.server.config.default_map,
            )
        )
        players = tuple(getattr(self.server, "players", {}).values())
        bot_count = min(
            len(players),
            sum(bool(getattr(player, "is_bot", False)) for player in players),
        )
        human_count = len(players) - bot_count
        texture_skin = (
            str(getattr(steam, "texture_skin", "") or "") if steam else ""
        )
        if not texture_skin and mode.mafia:
            texture_skin = "mafia"
        tags = [
            "revival",
            "protocol=168",
            "identity=ticket-v1",
            "mode=%04d" % int(mode.mode_id),
        ]
        return {
            "identifier": self.server_id,
            "name": str(self.server.config.server_name),
            "ip": self.public_host,
            "port": game_port,
            "queryPort": query_port,
            # `players` is the total browser population. The explicit fields
            # preserve human/bot semantics without making the retail UI lie.
            "players": len(players),
            "human_players": human_count,
            "bots": bot_count,
            "max_players": int(self.server.config.max_players),
            "map": map_name,
            "game_mode": mode.code.upper(),
            "mode_tla": mode.code,
            "version": str(
                getattr(steam, "game_version", "1.0.0.0") if steam else "1.0.0.0"
            ),
            "region": str(getattr(self.config, "region", "europe")),
            "official": bool(getattr(self.config, "official", False)),
            "playlist_id": int(getattr(steam, "playlist_id", 0) if steam else 0),
            "texture_skin": texture_skin or None,
            "classic": bool(mode.classic),
            "monitor": False,
            "beta": False,
            "tags": tags,
        }

    async def publish_heartbeat(self) -> None:
        status, payload = await self._post(
            "/api/master/servers/heartbeat",
            self.heartbeat_payload(),
        )
        if status != 200 or not payload.get("accepted"):
            raise RevivalMasterError(
                payload.get("detail") or payload.get("error") or "heartbeat rejected"
            )
        logger.debug("Published Revival heartbeat for %s", self.server_id)

    async def consume_join_ticket(self, ticket: str) -> RevivalIdentity:
        if not is_join_code(ticket):
            raise JoinTicketRejected("invalid join-code format")
        try:
            status, payload = await self._post(
                "/api/master/auth/consume-ticket",
                {"ticket": ticket, "server_id": self.server_id},
            )
        except RevivalMasterError as error:
            raise JoinTicketUnavailable(str(error)) from error
        if status == 401:
            raise JoinTicketRejected(
                payload.get("message") or "join code is expired or already used"
            )
        if status != 200 or not payload.get("authenticated"):
            raise JoinTicketUnavailable(
                payload.get("message") or payload.get("error") or "identity service rejected the join"
            )
        player = payload.get("player")
        if not isinstance(player, dict):
            raise JoinTicketUnavailable("identity service omitted the player record")
        return RevivalIdentity.from_payload(player)

    @staticmethod
    def bind_player(player, identity: RevivalIdentity | None) -> None:
        if identity is None:
            player.account_public_id = None
            player.account_legacy_id = None
            player.account_nickname = None
            player.identity_type = "legacy"
            player.ranked_eligible = False
            return
        player.account_public_id = identity.public_id
        player.account_legacy_id = identity.legacy_id
        player.account_nickname = identity.nickname
        player.identity_type = identity.identity_type
        player.ranked_eligible = identity.ranked_eligible

    @staticmethod
    def _counters(player) -> tuple[int, int, int, int]:
        return (
            max(0, int(getattr(player, "kills", 0))),
            max(0, int(getattr(player, "deaths", 0))),
            max(0, int(getattr(player, "captures", 0))),
            max(0, int(getattr(player, "score", 0))),
        )

    def _player_delta(self, player) -> tuple[int, int, int, int]:
        current = self._counters(player)
        previous = self._player_baselines.get(id(player), (0, 0, 0, 0))
        return tuple(max(0, current[index] - previous[index]) for index in range(4))

    def accumulate_departing_player(self, player) -> None:
        legacy_id = getattr(player, "account_legacy_id", None)
        if not legacy_id or bool(getattr(player, "is_bot", False)):
            return
        kills, deaths, captures, score = self._player_delta(player)
        if not any((kills, deaths, captures, score)):
            self._player_baselines.pop(id(player), None)
            return
        record = self._departed.setdefault(
            str(legacy_id),
            {
                "name": str(getattr(player, "account_nickname", None) or player.name),
                "kills": 0,
                "deaths": 0,
                "captures": 0,
                "score": 0,
                "team": int(getattr(player, "team", -1)),
            },
        )
        record["kills"] += kills
        record["deaths"] += deaths
        record["captures"] += captures
        record["score"] += score
        self._player_baselines.pop(id(player), None)

    def _result_players(self, winner: int | None):
        combined: dict[str, dict[str, Any]] = {
            legacy_id: dict(values) for legacy_id, values in self._departed.items()
        }
        connected_snapshots: dict[int, tuple[int, int, int, int]] = {}
        for player in self.server.players.values():
            legacy_id = getattr(player, "account_legacy_id", None)
            if not legacy_id or bool(getattr(player, "is_bot", False)):
                continue
            delta = self._player_delta(player)
            connected_snapshots[id(player)] = self._counters(player)
            record = combined.setdefault(
                str(legacy_id),
                {
                    "name": str(getattr(player, "account_nickname", None) or player.name),
                    "kills": 0,
                    "deaths": 0,
                    "captures": 0,
                    "score": 0,
                    "team": int(getattr(player, "team", -1)),
                },
            )
            record["kills"] += delta[0]
            record["deaths"] += delta[1]
            record["captures"] += delta[2]
            record["score"] += delta[3]

        mode_name = str(self.server.config.default_mode).lower()
        mode_total = MODE_TOTAL_STAT.get(mode_name)
        players = []
        for legacy_id, record in combined.items():
            kills = int(record["kills"])
            deaths = int(record["deaths"])
            captures = int(record["captures"])
            score = int(record["score"])
            if not any((kills, deaths, captures, score)):
                continue
            stats = {
                "1": [kills, kills],
                "220": [deaths, 0],
                "201": [1, score],
            }
            if captures:
                # CTF's recovered capture stat. Other modes still retain the
                # honest generic score totals above.
                stats["49"] = [captures, captures]
            if mode_total is not None:
                stats[str(mode_total)] = [1, score]
            if winner is None:
                stats["161"] = [1, 0]
            elif int(record["team"]) == int(winner):
                stats["159"] = [1, 0]
            else:
                stats["160"] = [1, 0]
            players.append(
                {
                    "steamid": legacy_id,
                    "name": record["name"],
                    "total": [1, score],
                    "stats": stats,
                }
            )
        return players, connected_snapshots

    async def submit_round_results(self, winner: int | None = None) -> None:
        if not self.enabled or not self.write_token:
            return
        players, snapshots = self._result_players(winner)
        if not players:
            return
        event_id = "round_%s" % uuid4().hex
        status, payload = await self._post(
            "/api/master/stats",
            {
                "event_id": event_id,
                "server_id": self.server_id,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "players": players,
            },
        )
        if status != 200 or not payload.get("accepted"):
            raise RevivalMasterError(
                payload.get("detail") or payload.get("error") or "result submission rejected"
            )
        self._departed.clear()
        self._player_baselines.update(snapshots)
        logger.info(
            "Revival round results accepted: event=%s updated=%s ignored=%s",
            event_id,
            payload.get("updated", 0),
            payload.get("ignored", 0),
        )

    def schedule_round_results(self, winner: int | None = None) -> None:
        if not self.enabled or not self.write_token:
            return

        async def submit() -> None:
            try:
                await self.submit_round_results(winner)
            except RevivalMasterError as error:
                logger.warning("Revival result submission failed: %s", error)
            except Exception:
                logger.exception("Unexpected Revival result submission failure")

        asyncio.create_task(submit(), name="aos-revival-round-results")
