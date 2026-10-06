"""Steam relay hosting for dedicated servers.

Some providers block a server's address and the AoSPlay web services while
Steam itself stays reachable. ``battlespades-steam-host`` (tools/steam_host)
logs on to Steam as a game server and accepts players over Steam's relay
network, forwarding each one to the game port from its own loopback UDP port.
This service launches one helper per Steam application, decides which players
may come in, and maps their loopback endpoints back to Steam identities so
bans, vote kicks and lockouts apply to ``steam:<id>`` instead of 127.0.0.1.

Steam P2P only connects players of the same application. Owners of Ace of
Spades attach as 224540; everyone else attaches as Spacewar (480), so both get
a helper by default.

The helper is optional: when it is missing or Steam is unavailable the direct
server keeps running exactly as before.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from server.steam_p2p import endpoint

logger = logging.getLogger(__name__)

ACE_OF_SPADES_APP_ID = 224540
SPACEWAR_APP_ID = 480
HELPER_NAME = "battlespades-steam-host.exe" if sys.platform == "win32" else "battlespades-steam-host"
#: Tag carrying each helper's SteamID in the Steam server listing and A2S.
RELAY_TAG_BY_APP = {ACE_OF_SPADES_APP_ID: "sdr", SPACEWAR_APP_ID: "sdr480"}


def player_steam_id(value: str) -> str:
    """Validate a public-universe individual SteamID64 reported by the helper."""

    if not value or len(value) > 20 or not value.isdigit():
        raise ValueError("invalid Steam id")
    number = int(value)
    if number >> 64 or number >> 56 != 1 or ((number >> 52) & 0xF) != 1 or not number & 0xFFFFFFFF:
        raise ValueError("not a Steam player account")
    return str(number)


class _Helper:
    """One running helper process and the lines it has printed."""

    def __init__(self, app_id: int, process: subprocess.Popen) -> None:
        self.app_id = app_id
        self.process = process
        self.lines: "queue.SimpleQueue[str]" = queue.SimpleQueue()
        self.steam_id: str | None = None
        threading.Thread(target=self._read, name=f"steam-host-{app_id}", daemon=True).start()

    def _read(self) -> None:
        try:
            for line in self.process.stdout:  # type: ignore[union-attr]
                self.lines.put(line.rstrip("\r\n"))
        except (OSError, ValueError):
            pass

    def send(self, *fields: str) -> None:
        try:
            self.process.stdin.write(" ".join(fields) + "\n")  # type: ignore[union-attr]
            self.process.stdin.flush()  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass

    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        self.send("QUIT")
        try:
            self.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
        except OSError:
            pass


class SteamHostService:
    RESTART_SECONDS = 15.0

    def __init__(self, server) -> None:
        self.server = server
        self.helpers: dict[int, _Helper] = {}
        #: loopback port -> (app id, helper connection, player SteamID)
        self.routes: dict[int, tuple[int, str, str]] = {}
        #: ENet peer -> the route it arrived on, kept after its UDP port retires
        self.peers: dict[object, tuple[int, str, str]] = {}
        self.task: asyncio.Task | None = None
        self.stopping = False
        self._restart_at: dict[int, float] = {}
        self._executable: Path | None = None

    # -- configuration -----------------------------------------------------

    def _settings(self):
        return getattr(self.server.config, "steam_host", None)

    def enabled(self) -> bool:
        settings = self._settings()
        return bool(getattr(settings, "enabled", False)) or os.environ.get("AOS_STEAM_HOST", "") == "1"

    def _find_executable(self) -> Path | None:
        configured = str(getattr(self._settings(), "executable", "") or "").strip()
        configured = configured or os.environ.get("AOS_STEAM_HOST_EXECUTABLE", "").strip()
        if configured:
            return Path(configured) if Path(configured).is_file() else None
        root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
        for candidate in (
            root / "steam-host" / HELPER_NAME,
            Path(getattr(sys, "_MEIPASS", root)) / "steam-host" / HELPER_NAME,
            root / "build" / "steam-host" / HELPER_NAME,
        ):
            if candidate.is_file():
                return candidate
        return None

    def _app_ids(self) -> tuple[int, ...]:
        configured = getattr(self._settings(), "app_ids", None) or (ACE_OF_SPADES_APP_ID, SPACEWAR_APP_ID)
        return tuple(dict.fromkeys(int(app) for app in configured if int(app) > 0))

    def _token_file(self) -> str:
        configured = str(getattr(self._settings(), "token_file", "") or "").strip()
        return configured or os.environ.get("AOS_STEAM_GSLT_FILE", "").strip()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if not self.enabled():
            return
        self._executable = self._find_executable()
        if self._executable is None:
            logger.error(
                "Steam relay hosting is enabled but %s was not found; the direct server keeps running",
                HELPER_NAME,
            )
            return
        for app_id in self._app_ids():
            self._spawn(app_id)
        self.task = asyncio.create_task(self._run(), name="steam-relay-host")

    def _spawn(self, app_id: int) -> None:
        assert self._executable is not None
        config = self.server.config
        command = [
            str(self._executable), "--control",
            "--game-port", str(int(config.port)),
            "--app-id", str(app_id),
            "--max-clients", str(int(getattr(self._settings(), "max_clients", 64) or 64)),
        ]
        # The relay hello names the AoSPlay server id the player needs a join
        # ticket for. A server that is not registered with AoSPlay sends none:
        # a client asking for a ticket for an unknown server could never join.
        master = getattr(self.server, "revival_master", None)
        server_id = str(getattr(master, "server_id", "") or "")
        if server_id and getattr(master, "enabled", False) and getattr(master, "write_token", ""):
            command += ["--server-id", server_id]
        # A game server token belongs to one application.
        token = self._token_file()
        if token and app_id == ACE_OF_SPADES_APP_ID:
            command += ["--token-file", token]
        if sys.platform != "win32":
            # Bundled as data, which does not always keep the executable bit.
            try:
                self._executable.chmod(self._executable.stat().st_mode | 0o111)
            except OSError:
                pass
        environment = dict(os.environ)
        runtime = str(getattr(self._settings(), "runtime_dir", "") or "").strip()
        runtime = runtime or os.environ.get("AOS_STEAM_RUNTIME_DIR", "").strip()
        if runtime and sys.platform != "win32":
            # Valve's steamclient.so, supplied by the operator from SteamCMD.
            environment["LD_LIBRARY_PATH"] = os.pathsep.join(
                part for part in (runtime, environment.get("LD_LIBRARY_PATH", "")) if part
            )
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(self._executable.parent),
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            logger.exception("Could not start the Steam relay host for app %s", app_id)
            self._restart_at[app_id] = time.monotonic() + self.RESTART_SECONDS
            return
        self.helpers[app_id] = _Helper(app_id, process)

    async def _run(self) -> None:
        try:
            while not self.stopping:
                for app_id, helper in tuple(self.helpers.items()):
                    while True:
                        try:
                            line = helper.lines.get_nowait()
                        except queue.Empty:
                            break
                        try:
                            self._event(helper, line.split(" "))
                        except (ValueError, IndexError):
                            logger.warning("Ignored a malformed Steam relay host message")
                    if not helper.alive() and helper.lines.empty():
                        logger.warning(
                            "Steam relay host for app %s stopped; retrying in %.0f s", app_id, self.RESTART_SECONDS
                        )
                        self._forget_helper(app_id)
                        self._restart_at[app_id] = time.monotonic() + self.RESTART_SECONDS
                now = time.monotonic()
                for app_id, when in tuple(self._restart_at.items()):
                    if now >= when:
                        del self._restart_at[app_id]
                        self._spawn(app_id)
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Steam relay hosting stopped; the direct server remains available")

    def _forget_helper(self, app_id: int) -> None:
        self.helpers.pop(app_id, None)
        for port, route in tuple(self.routes.items()):
            if route[0] == app_id:
                self._drop(app_id, route[1])
        self._publish_tags()

    async def close(self) -> None:
        self.stopping = True
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        for helper in tuple(self.helpers.values()):
            helper.stop()
        self.helpers.clear()
        self.routes.clear()
        self.peers.clear()
        self._publish_tags()

    # -- helper events -------------------------------------------------------

    def _event(self, helper: _Helper, fields: list[str]) -> None:
        verb = fields[0]
        if verb == "READY" and len(fields) == 4:
            helper.steam_id = str(int(fields[1]))
            logger.info(
                "Steam relay host ready: app %s, SteamID %s%s",
                helper.app_id, helper.steam_id, " (anonymous, changes on restart)" if fields[2] == "1" else "",
            )
            self._publish_tags()
        elif verb == "PEER" and len(fields) == 4:
            connection = str(int(fields[1]))
            port = int(fields[2])
            identity = player_steam_id(fields[3])
            key = "steam:" + identity
            kicked = getattr(getattr(self.server, "vote_manager", None), "match_kick_reason", lambda _: None)(key)
            banned = self.server.ban_manager.is_banned(key) is not None
            if not 0 < port < 65536 or port in self.routes or banned or kicked is not None:
                logger.info(
                    "Steam relay player %s refused (%s)", key,
                    "banned" if banned else "kicked from this match" if kicked is not None else "route in use",
                )
                helper.send("DENY", connection)
                return
            self.routes[port] = (helper.app_id, connection, identity)
            logger.info("Steam relay player %s admitted through app %s as 127.0.0.1:%d", key, helper.app_id, port)
            # Until this the helper forwards nothing to the game port.
            helper.send("ALLOW", connection)
        elif verb == "DROP" and len(fields) == 2:
            self._drop(helper.app_id, str(int(fields[1])))
        elif verb == "LOG":
            logger.info("Steam relay host (app %s): %s", helper.app_id, " ".join(fields[1:]))
        elif verb == "ERROR":
            logger.error("Steam relay host (app %s): %s", helper.app_id, " ".join(fields[1:]))

    def _drop(self, app_id: int, connection: str) -> None:
        self.routes = {
            port: route for port, route in self.routes.items() if (route[0], route[1]) != (app_id, connection)
        }
        for peer, route in tuple(self.peers.items()):
            if (route[0], route[1]) == (app_id, connection):
                client = self.server.connections.get(peer)
                if client is not None:
                    client.disconnect(reason=8)

    def _publish_tags(self) -> None:
        """Expose the live host SteamIDs to A2S and the Steam listing tags."""

        tags = tuple(
            f"{RELAY_TAG_BY_APP.get(app_id, f'sdr{app_id}')}={steam_id}"
            # Ace of Spades first: it is the id most players dial.
            for app_id, steam_id in sorted(
                self.host_ids().items(), key=lambda item: (item[0] != ACE_OF_SPADES_APP_ID, item[0])
            )
        )
        try:
            self.server.config.steam_relay_tags = tags
        except AttributeError:
            pass

    # -- queries --------------------------------------------------------------

    def host_ids(self) -> dict[int, str]:
        """Application id -> SteamID of each helper that is logged on."""

        return {
            app_id: helper.steam_id
            for app_id, helper in self.helpers.items()
            if helper.steam_id is not None and helper.alive()
        }

    def identity_for(self, peer) -> str | None:
        """``steam:<id>`` for a player who arrived through a relay host."""

        if not self.routes and not self.peers:
            return None
        cached = self.peers.get(peer)
        if cached is not None:
            return "steam:" + cached[2]
        address = endpoint(peer)
        if address is None or address[0] != "127.0.0.1":
            return None
        route = self.routes.get(address[1])
        if route is None:
            return None
        self.peers[peer] = route
        return "steam:" + route[2]

    def forget_peer(self, peer) -> None:
        self.peers.pop(peer, None)
