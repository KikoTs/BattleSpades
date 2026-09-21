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
from server.result_outbox import ResultOutbox
from server.profile_stats import snapshot as profile_snapshot
from server.cosmetics import CAPABILITY, CosmeticReplication


logger = logging.getLogger(__name__)

JOIN_CODE_PATTERN = re.compile(r"^~[A-Za-z0-9_-]{14}$")
SERVER_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MODE_TOTAL_STAT = {
    "tdm": 192,
    "vip": 193,
    "tc": 194,
    "occupation": 195,
    "occ": 195,
    "oc": 195,
    "diamond_mine": 196,
    "dia": 196,
    "ctf": 197,
    "cctf": 197,
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
    assigned_team: int | None = None
    client_capabilities: tuple[str, ...] = ()

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
            client_capabilities=((CAPABILITY,) if isinstance(payload.get("client_capabilities"), list)
                                 and CAPABILITY in payload["client_capabilities"] else ()),
            assigned_team=(payload.get("assigned_team")
                           if type(payload.get("assigned_team")) is int
                           and payload["assigned_team"] in {2, 3} else None),
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
        self.cosmetics = CosmeticReplication(self)
        self._closing = False
        self._player_baselines: dict[int, tuple[int, int, int, int]] = {}
        self._profile_baselines: dict[int, dict[int, list[int]]] = {}
        self._participation_baselines: dict[int, tuple[float, float, float]] = {}
        self._round_started_at = datetime.now(timezone.utc)
        self._round_match_id = uuid4().hex
        self._round_completed = False
        self._departed: dict[str, dict[str, Any]] = {}
        self._pending_results: dict[str, dict[str, Any]] = {}
        self._result_task: asyncio.Task | None = None
        self._result_lock = asyncio.Lock()
        self._result_outbox = ResultOutbox(
            getattr(self.config, "results_path", "state/round-results.sqlite3"),
            os.environ.get("AOS_MATCH_RESULTS_DIRECTORY") if os.environ.get("AOS_RELAY_LOBBY_ID") else None,
        )

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

    def _public_port(self, name: str, fallback: int) -> int:
        configured = os.environ.get(name)
        if configured is None or not configured.strip():
            return fallback
        try:
            port = int(configured)
        except ValueError as error:
            raise RevivalMasterError(
                f"{name} must be an integer UDP port"
            ) from error
        if not 1 <= port <= 65_535:
            raise RevivalMasterError(f"{name} must be between 1 and 65535")
        return port

    @property
    def public_port(self) -> int:
        """Externally reachable game port, which may be tunnel-mapped."""

        return self._public_port("AOS_PUBLIC_PORT", int(self.server.config.port))

    @property
    def public_query_port(self) -> int:
        """Externally reachable A2S port, independent from the listen socket."""

        configured = os.environ.get("AOS_PUBLIC_QUERY_PORT")
        if configured is not None and configured.strip():
            return self._public_port("AOS_PUBLIC_QUERY_PORT", self.public_port)
        # A public game-port override normally represents a single-port ENet
        # relay such as Playit, which forwards A2S on that same UDP endpoint.
        if os.environ.get("AOS_PUBLIC_PORT", "").strip():
            return self.public_port
        steam = getattr(self.server.config, "steam", None)
        if steam is not None and bool(getattr(steam, "enabled", False)):
            return int(steam.effective_query_port(int(self.server.config.port)))
        return self.public_port

    @property
    def mode_code(self) -> str:
        """Use the same canonical wire identity for discovery and XP evidence."""
        return get_mode_data(
            getattr(self.server.config, "game_mode", self.server.config.default_mode)
        ).code

    @property
    def server_id(self) -> str:
        derived = "%s:%d" % (
            self.public_host,
            self.public_port,
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
                "revival server_id must equal public_host:public_port (%s)" % derived
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
        self._start_result_flush()

        self.cosmetics.start()

    async def close(self) -> None:
        if self._closing:
            return
        # Capture the final live counters before raising the shutdown gate.
        # Quitting mid-round records activity, without a completion/win bonus.
        if not self._round_completed:
            try:
                self._capture_round_results(None, finished=False)
            except Exception:
                logger.exception("Could not capture final Revival results during shutdown")
        self._closing = True
        await self.cosmetics.close()
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._result_task is not None:
            self._result_task.cancel()
            try:
                await self._result_task
            except asyncio.CancelledError:
                pass
            self._result_task = None
        async with self._result_lock:
            try:
                await self._persist_pending_results()
            except Exception:
                logger.exception("Could not persist pending Revival results during shutdown")

    async def _heartbeat_loop(self) -> None:
        interval = min(
            60.0,
            max(15.0, float(getattr(self.config, "heartbeat_interval_seconds", 30.0))),
        )
        while not self._closing:
            try:
                await asyncio.sleep(interval)
                # Result retries must continue even if heartbeats are rejected.
                self._start_result_flush()
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
        listen_port = int(self.server.config.port)
        game_port = self.public_port
        steam = getattr(self.server.config, "steam", None)
        query_port = self.public_query_port
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
        if listen_port != game_port:
            tags.extend(("public_port_mapped", "listen_port=%d" % listen_port))
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
        player.client_capabilities = identity.client_capabilities if identity else ()
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
        profile = self._profile_delta(player)
        participation = self._participation_delta(player)
        if not any((kills, deaths, captures, score)) and not profile and not participation[0]:
            self._player_baselines.pop(id(player), None)
            self._profile_baselines.pop(id(player), None)
            self._participation_baselines.pop(id(player), None)
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
        self._merge_profile(record, profile)
        self._merge_participation(record, player, participation)
        record["profile_managed"] = record.get("profile_managed", False) or hasattr(player, "profile_stats")
        self._player_baselines.pop(id(player), None)
        self._profile_baselines.pop(id(player), None)
        self._participation_baselines.pop(id(player), None)

    @staticmethod
    def _participation_counters(player: Any) -> tuple[float, float, float]:
        state = getattr(player, "profile_stats", None)
        return tuple(max(0.0, float(getattr(state, key, 0.0))) for key in
                     ("connected_seconds", "active_seconds", "afk_seconds"))

    def _participation_delta(self, player: Any) -> tuple[float, float, float]:
        current = self._participation_counters(player)
        previous = self._participation_baselines.get(id(player), (0.0, 0.0, 0.0))
        return tuple(max(0.0, current[index] - previous[index]) for index in range(3))

    @staticmethod
    def _merge_participation(record: dict[str, Any], player: Any, values: tuple[float, float, float]) -> None:
        previous = record.get("participation_seconds", (0.0, 0.0, 0.0))
        record["participation_seconds"] = tuple(previous[i] + values[i] for i in range(3))
        state = getattr(player, "profile_stats", None)
        for key in ("human_opponents", "bot_opponents"):
            record[key] = max(record.get(key, 0), int(getattr(state, key, 0)))

    def begin_round(self) -> None:
        """Reset HTTP evidence boundaries without changing any game packet."""
        self._round_started_at = datetime.now(timezone.utc)
        self._round_match_id = uuid4().hex
        self._round_completed = False
        self._departed.clear()
        self._participation_baselines = {
            id(player): self._participation_counters(player)
            for player in self.server.players.values()
        }
        for player in self.server.players.values():
            state = getattr(player, "profile_stats", None)
            if state is not None:
                state.human_opponents = state.bot_opponents = 0
                state.opponent_sample_seconds = 0.0
                state.activity_seen = False

    def reset_scoreboard_baselines(self) -> None:
        """Pair a match-score reset with its result-delta baseline reset.

        Profile and participation counters are cumulative and retain their
        independent baselines. Finished-round snapshots are already immutable.
        """
        self._player_baselines = {
            id(player): self._counters(player)
            for player in self.server.players.values()
        }

    def _profile_delta(self, player) -> dict[int, list[int]]:
        previous = self._profile_baselines.get(id(player), {})
        delta = {}
        for stat, pair in profile_snapshot(player).items():
            baseline = previous.get(stat, [0, 0])
            values = [max(0, pair[index] - baseline[index]) for index in range(2)]
            if any(values):
                delta[stat] = values
        return delta

    @staticmethod
    def _merge_profile(record, profile) -> None:
        values = record.setdefault("profile", {})
        for stat, pair in profile.items():
            total = values.setdefault(stat, [0, 0])
            total[0] += pair[0]
            total[1] += pair[1]

    def _result_players(self, winner: int | None, *, finished: bool = True):
        combined: dict[str, dict[str, Any]] = {
            legacy_id: {**values, "profile": {
                stat: list(pair) for stat, pair in values.get("profile", {}).items()
            }} for legacy_id, values in self._departed.items()
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
            record["team"] = int(getattr(player, "team", -1))
            record["connected"] = True
            self._merge_participation(record, player, self._participation_delta(player))
            self._merge_profile(record, self._profile_delta(player))
            record["profile_managed"] = record.get("profile_managed", False) or hasattr(player, "profile_stats")

        mode_name = self.mode_code
        mode_total = MODE_TOTAL_STAT.get(mode_name)
        players = []
        for legacy_id, record in combined.items():
            kills = int(record["kills"])
            deaths = int(record["deaths"])
            captures = int(record["captures"])
            score = int(record["score"])
            if (not any((kills, deaths, captures, score)) and not record.get("profile")
                    and not record.get("participation_seconds", (0,))[0]):
                continue
            stats = {
                "1": [kills, kills],
                "220": [deaths, 0],
                "201": [1, score],
            }
            if captures and mode_name in {"ctf", "cctf", "classic_ctf"}:
                # CTF's recovered capture stat. Other modes still retain the
                # honest generic score totals above.
                stats["49"] = [captures, captures]
            if record.get("profile_managed"):
                # Team/class-change deaths and scoreboard refreshes are not
                # new profile deaths, kills or captures.
                stats["1"] = [0, 0]
                stats["220"] = [0, 0]
                stats.pop("49", None)
            stats.update({str(stat): list(pair) for stat, pair in record.get("profile", {}).items()})
            if mode_total is not None:
                stats[str(mode_total)] = [1, score]
            if not finished or not record.get("connected"):
                pass
            elif winner is None:
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
                    "participation": {
                        "connected_seconds": int(record.get("participation_seconds", (0, 0, 0))[0]),
                        "active_seconds": int(record.get("participation_seconds", (0, 0, 0))[1]),
                        "afk_seconds": int(record.get("participation_seconds", (0, 0, 0))[2]),
                        "human_opponents": min(100, record.get("human_opponents", 0)),
                        "bot_opponents": min(100, record.get("bot_opponents", 0)),
                        "result": ("unfinished" if not finished or not record.get("connected") else
                                   "draw" if winner is None else
                                   "win" if int(record["team"]) == int(winner) else "loss"),
                    },
                }
            )
        return players, connected_snapshots

    def _capture_round_results(self, winner: int | None, *, finished: bool = True) -> None:
        if (bool(getattr(self.server.config, "ugc_runtime", False))
                or self.mode_code in {"ugc", "tut"}
                or str(getattr(self.server.config, "default_mode", "")).lower() == "tutorial"):
            return
        if not self.enabled or not self.write_token or self._closing:
            return
        if len(self._pending_results) >= 256:
            raise RevivalMasterError("round result memory queue is full; check the durable outbox")
        players, snapshots = self._result_players(winner, finished=finished)
        if finished:
            self._round_completed = True
        if not players:
            return
        event_id = "round_%s" % uuid4().hex
        ended_at = datetime.now(timezone.utc)
        duration = max(0, int((ended_at - self._round_started_at).total_seconds()))
        self._pending_results[event_id] = {
            "event_id": event_id,
            "server_id": self.server_id,
            "recorded_at": ended_at.isoformat(),
            "players": players,
        }
        relay_id = os.environ.get("AOS_RELAY_LOBBY_ID", "").strip()
        if relay_id:
            self._pending_results[event_id]["relay_lobby_id"] = relay_id
        map_crc = getattr(getattr(self.server, "world_manager", None), "map_file_crc", None)
        if 30 <= duration <= 14400 and isinstance(map_crc, int):
            self._pending_results[event_id]["match"] = {
                "match_id": self._round_match_id,
                "playlist_id": str(getattr(getattr(self.server.config, "steam", None), "playlist_id", 0)),
                "mode_id": self.mode_code,
                "map_crc": f"{map_crc & 0xFFFFFFFF:08x}",
                "duration_seconds": duration,
                "started_at": self._round_started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
            }
            for result in players:
                evidence = result["participation"]
                evidence["connected_seconds"] = min(duration, evidence["connected_seconds"])
                evidence["active_seconds"] = min(evidence["connected_seconds"], evidence["active_seconds"])
                evidence["afk_seconds"] = min(evidence["connected_seconds"] - evidence["active_seconds"], evidence["afk_seconds"])
        else:
            for result in players:
                result.pop("participation", None)
        # Reserve these counters before the first await or the round reset.
        # The immutable event now owns them, independent of HTTP success.
        self._departed.clear()
        self._player_baselines.update(snapshots)
        self._profile_baselines.update({
            id(player): profile_snapshot(player) for player in self.server.players.values()
            if id(player) in snapshots
        })
        self._participation_baselines.update({
            id(player): self._participation_counters(player) for player in self.server.players.values()
            if id(player) in snapshots
        })

    @staticmethod
    async def _outbox_io(operation, *args):
        # Cancelling to_thread does not stop SQLite. Wait for the transaction
        # before releasing the async lock or beginning shutdown persistence.
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _persist_pending_results(self) -> None:
        events = list(self._pending_results.values())
        if not events:
            return
        await self._outbox_io(self._result_outbox.put_many, events)
        for event in events:
            self._pending_results.pop(event["event_id"], None)

    async def flush_round_results(self) -> None:
        if not self.enabled or not self.write_token:
            return
        async with self._result_lock:
            while True:
                await self._persist_pending_results()
                if self._closing:
                    return
                events = await self._outbox_io(self._result_outbox.pending, self.server_id)
                if not events:
                    return
                for event in events:
                    status, payload = await self._post("/api/master/stats", event)
                    if status != 200 or not payload.get("accepted"):
                        raise RevivalMasterError(
                            payload.get("detail") or payload.get("error") or "result submission rejected"
                        )
                    await self._outbox_io(self._result_outbox.acknowledge, event["event_id"])
                    logger.info("Revival round results accepted: event=%s", event["event_id"])

    async def submit_round_results(self, winner: int | None = None) -> None:
        self._capture_round_results(winner)
        await self.flush_round_results()

    def _start_result_flush(self) -> None:
        if self._closing or (self._result_task is not None and not self._result_task.done()):
            return

        async def flush() -> None:
            try:
                await self.flush_round_results()
            except RevivalMasterError as error:
                logger.warning("Revival results retained for retry: %s", error)
            except Exception:
                logger.exception("Revival result outbox failure; pending results retained")

        self._result_task = asyncio.create_task(flush(), name="aos-revival-round-results")

    def schedule_round_results(self, winner: int | None = None) -> None:
        if not self.enabled or not self.write_token or self._closing:
            return
        try:
            self._capture_round_results(winner)
        except Exception:
            logger.exception("Could not capture Revival round results")
            return
        self._start_result_flush()
