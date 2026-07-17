"""Isolated Steam master-server advertisement for the retired retail client.

The original Ace of Spades wrapper loads ``steam_api.dll`` in a 32-bit Python
process.  BattleSpades is released as a native 64-bit application, so loading
that DLL in-process would fail before gameplay starts.  ``SteamMasterService``
instead supervises a small 32-bit helper and exchanges bounded line messages
with it.  Steam callbacks, heartbeats, and DLL failures therefore never run on
the authoritative 60 Hz simulation thread.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
from typing import TYPE_CHECKING, Optional

from server.mode_data import get as get_mode_data

if TYPE_CHECKING:
    from server.config import ServerConfig, SteamMasterConfig
    from server.main import BattleSpadesServer


logger = logging.getLogger(__name__)

STEAM_APP_ID = 224540
STEAM_PRODUCT = "aos"
STEAM_GAME_DIR = "aceofspades"
STEAM_DESCRIPTION = "Ace of Spades"
# The 2015 retail browser ignores Steam's returned game port and always joins
# this port.  Keep this recovered client invariant visible to operators rather
# than advertising a row that the stock executable could not enter.
RETAIL_BROWSER_GAME_PORT = 32887
_BRIDGE_NAME = "battlespades-steam-bridge.exe"


@dataclass(frozen=True)
class SteamRuntimeInspection:
    """Validated identity of an operator-supplied legacy Steam runtime."""

    runtime_dir: Path
    steam_api: Path
    steamclient: Optional[Path]
    app_id_file: Optional[int]
    machine: int


@dataclass(frozen=True)
class SteamAdvertisement:
    """One immutable live server-list state sent to the helper."""

    server_name: str
    map_name: str
    max_players: int
    player_count: int
    bot_count: int
    tags: str
    region: str


def build_game_tags(config: "ServerConfig", mode_code: Optional[str] = None) -> str:
    """Build the exact semicolon-delimited tags consumed by retail AoS.

    The stock browser filters on ``mode=%04d`` and may additionally filter on
    ``region=<name>``.  Keep ordering identical to the recovered server wrapper
    so old clients and external query tools see one stable representation.
    """

    steam = config.steam
    mode = get_mode_data(mode_code or config.game_mode)
    tags = [
        f"v{int(steam.protocol_version)}",
        f"playlist={int(steam.playlist_id)}",
    ]
    if steam.region:
        tags.append(f"region={steam.region}")
    tags.append(f"mode={int(mode.mode_id):04d}")
    if mode.classic:
        tags.append("classic")
    skin = steam.texture_skin or ("mafia" if mode.mafia else "")
    if skin:
        tags.append(f"skin={skin}")
    encoded = ";".join(tags)
    if len(encoded.encode("utf-8")) >= 128:
        raise ValueError("Steam game tags exceed the legacy 127-byte limit")
    return encoded


def build_steam_map_name(mode_code: str, map_name: str) -> str:
    """Reproduce the retail ``<MODE>_<MapName>`` browser map field.

    Its wrapper removes spaces and uppercases the following character, e.g.
    ``tdm`` + ``City of Chicago`` becomes ``TDM_CityOfChicago``.
    """

    raw = f"{str(mode_code).upper()}_{str(map_name).strip()}"
    words = raw.split(" ")
    if len(words) == 1:
        return raw[:79]
    joined = words[0] + "".join(
        word[:1].upper() + word[1:] for word in words[1:] if word
    )
    return joined[:79]


def effective_query_port(config: "ServerConfig") -> int:
    """Return the Steam query port advertised to connected retail clients."""

    if not config.steam.enabled:
        return int(config.port)
    return config.steam.effective_query_port(config.port)


def _read_pe_machine(path: Path) -> int:
    """Read a PE COFF machine without importing or executing the DLL."""

    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise ValueError(f"{path} is not a PE image")
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        stream.seek(pe_offset)
        signature = stream.read(6)
    if len(signature) != 6 or signature[:4] != b"PE\0\0":
        raise ValueError(f"{path} has no PE signature")
    return struct.unpack_from("<H", signature, 4)[0]


def inspect_runtime(runtime_dir: Path) -> SteamRuntimeInspection:
    """Validate the supplied DLL directory without loading foreign code.

    The bridge implements the recovered 32-bit ``SteamGameServer011`` ABI, so
    accepting a 64-bit or ARM DLL here would only create a confusing helper
    crash.  ``steamclient.dll`` is recorded when present but is not mandatory;
    a normal Steam installation may supply it through ``steam_api.dll``.
    """

    runtime_dir = runtime_dir.expanduser().resolve()
    steam_api = runtime_dir / "steam_api.dll"
    if not steam_api.is_file():
        raise FileNotFoundError(f"missing {steam_api}")
    machine = _read_pe_machine(steam_api)
    if machine != 0x014C:
        raise ValueError(
            f"{steam_api} is machine 0x{machine:04x}; the legacy bridge needs x86"
        )
    steamclient = runtime_dir / "steamclient.dll"
    if not steamclient.is_file():
        steamclient = None
    app_id_path = runtime_dir / "steam_appid.txt"
    app_id_file: Optional[int] = None
    if app_id_path.is_file():
        try:
            app_id_file = int(app_id_path.read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError):
            logger.warning("Ignoring malformed %s", app_id_path)
    return SteamRuntimeInspection(
        runtime_dir=runtime_dir,
        steam_api=steam_api,
        steamclient=steamclient,
        app_id_file=app_id_file,
        machine=machine,
    )


def _expanded_path(value: str) -> Optional[Path]:
    if not value:
        return None
    return Path(os.path.expandvars(value)).expanduser().resolve()


def resolve_runtime_dir(config: "SteamMasterConfig") -> Path:
    """Resolve an explicit or conventional operator-owned runtime folder."""

    explicit = _expanded_path(
        config.runtime_dir or os.environ.get("BATTLESPADES_STEAM_RUNTIME", "")
    )
    if explicit is not None:
        return explicit
    executable_dir = Path(sys.executable).resolve().parent
    candidate = executable_dir / "steam-runtime"
    if candidate.is_dir():
        return candidate
    return (Path.cwd() / "steam-runtime").resolve()


def resolve_steamclient_dir(
    config: "SteamMasterConfig",
    supplied_runtime: Path,
) -> Optional[Path]:
    """Find a compatible x86 Steam client redistributable directory.

    The small legacy ``steamclient.dll`` beside recovered clients was observed
    hanging in ``SteamGameServer_Init``.  By default we instead stage the
    current x86 Steam installation and its two required support libraries.
    """

    if config.use_supplied_steamclient:
        return supplied_runtime
    explicit_value = (
        config.steamclient_dir
        or os.environ.get("BATTLESPADES_STEAMCLIENT_RUNTIME", "")
    )
    explicit = _expanded_path(explicit_value)
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    if sys.platform == "win32":
        try:
            import winreg

            registry_locations = (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\WOW6432Node\Valve\Steam",
                    "InstallPath",
                ),
            )
            for hive, key_name, value_name in registry_locations:
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        value, _ = winreg.QueryValueEx(key, value_name)
                    candidates.append(Path(str(value)))
                except OSError:
                    continue
        except ImportError:
            pass
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        if program_files_x86:
            candidates.append(Path(program_files_x86) / "Steam")
    required = ("steamclient.dll", "tier0_s.dll", "vstdlib_s.dll")
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.expanduser().resolve()
        folded = str(candidate).casefold()
        if folded in seen:
            continue
        seen.add(folded)
        if all((candidate / name).is_file() for name in required):
            machine = _read_pe_machine(candidate / "steamclient.dll")
            if machine != 0x014C:
                if explicit is not None and candidate == explicit:
                    raise ValueError(
                        f"{candidate / 'steamclient.dll'} is not x86"
                    )
                continue
            return candidate
    if explicit is not None:
        raise FileNotFoundError(
            f"{explicit} must contain steamclient.dll, tier0_s.dll, and "
            "vstdlib_s.dll"
        )
    return None


def resolve_helper_path(config: "SteamMasterConfig") -> Path:
    """Resolve the configured, frozen, or source-tree x86 bridge executable."""

    explicit = _expanded_path(
        config.helper_path or os.environ.get("BATTLESPADES_STEAM_HELPER", "")
    )
    if explicit is not None:
        return explicit
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "steam" / _BRIDGE_NAME
    candidates = (
        Path("build/steam-bridge/Release") / _BRIDGE_NAME,
        Path("build/steam-bridge") / _BRIDGE_NAME,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve()


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


class SteamMasterService:
    """Supervise Steam registration outside the gameplay process.

    ``start`` and ``close`` run on the asyncio owner thread.  A publisher task
    samples immutable server metadata once per configured interval; it never
    runs from ``SimulationRuntime.tick``.  The child owns all callbacks and
    UDP heartbeats.  Missing DLLs, backend outages, and child crashes degrade
    only discovery unless ``require_registration`` is explicitly enabled.
    """

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server
        self.config = server.config.steam
        self.inspection: Optional[SteamRuntimeInspection] = None
        self.client_runtime_dir: Optional[Path] = None
        self.helper_path: Optional[Path] = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self.steam_id = 0
        self.public_ip = 0
        self.logged_on = False
        self.secure = False
        self.last_error = ""
        self._closing = False
        self._ready = asyncio.Event()
        self._registered = asyncio.Event()
        self._supervisor_task: Optional[asyncio.Task] = None
        self._publisher_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()
        self._last_advertisement: Optional[SteamAdvertisement] = None
        self._temp_dir: Optional[tempfile.TemporaryDirectory] = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def query_active(self) -> bool:
        """Return whether the helper currently owns its Steam query socket."""

        process = self.process
        return bool(
            self._ready.is_set()
            and process is not None
            and process.returncode is None
        )

    def snapshot(self) -> SteamAdvertisement:
        """Capture current public metadata on the asyncio/server owner thread."""

        config = self.server.config
        mode = get_mode_data(config.game_mode)
        world = getattr(self.server, "world_manager", None)
        map_name = str(getattr(world, "map_name", "") or config.map_name)
        players = tuple(getattr(self.server, "players", {}).values())
        bot_count = sum(bool(getattr(player, "is_bot", False)) for player in players)
        return SteamAdvertisement(
            server_name=str(config.server_name)[:63],
            map_name=build_steam_map_name(mode.code, map_name),
            max_players=max(1, min(255, int(config.max_players))),
            player_count=min(255, len(players)),
            bot_count=min(255, int(bot_count)),
            tags=build_game_tags(config, mode.code),
            region=str(self.config.region),
        )

    async def start(self) -> None:
        """Validate local inputs and start registration supervision."""

        if not self.enabled or self._supervisor_task is not None:
            return
        if (
            self.config.public
            and int(self.server.config.port) != RETAIL_BROWSER_GAME_PORT
        ):
            logger.warning(
                "The stock Ace of Spades server browser always connects rows to "
                "UDP %d; game port %d requires direct connect or an updated client",
                RETAIL_BROWSER_GAME_PORT,
                int(self.server.config.port),
            )
        try:
            self.inspection = inspect_runtime(resolve_runtime_dir(self.config))
            self.client_runtime_dir = resolve_steamclient_dir(
                self.config,
                self.inspection.runtime_dir,
            )
            self.helper_path = resolve_helper_path(self.config)
            if not self.helper_path.is_file():
                raise FileNotFoundError(f"missing Steam bridge {self.helper_path}")
            if self.inspection.app_id_file not in (None, STEAM_APP_ID):
                logger.warning(
                    "%s contains app id %s; the bridge uses an isolated app id %s "
                    "instead (480 is Spacewar)",
                    self.inspection.runtime_dir / "steam_appid.txt",
                    self.inspection.app_id_file,
                    STEAM_APP_ID,
                )
            if self.config.use_supplied_steamclient and self.inspection.steamclient is None:
                logger.warning(
                    "No steamclient.dll beside %s; Steam must supply a compatible "
                    "client runtime",
                    self.inspection.steam_api,
                )
            elif self.client_runtime_dir is not None:
                logger.info(
                    "Using compatible x86 Steam client runtime from %s",
                    self.client_runtime_dir,
                )
            else:
                logger.warning(
                    "No standalone x86 Steam client runtime found; "
                    "SteamGameServer_Init requires the desktop Steam client"
                )
        except Exception as exc:
            self.last_error = str(exc)
            if self.config.require_registration:
                raise RuntimeError(f"Steam registration unavailable: {exc}") from exc
            logger.warning("Steam master registration disabled: %s", exc)
            return

        self._closing = False
        self._supervisor_task = asyncio.create_task(
            self._supervise(), name="steam-master-supervisor"
        )
        self._publisher_task = asyncio.create_task(
            self._publish_loop(), name="steam-master-publisher"
        )
        if self.config.require_registration:
            try:
                await asyncio.wait_for(
                    self._registered.wait(),
                    timeout=float(self.config.startup_timeout_seconds),
                )
            except asyncio.TimeoutError as exc:
                await self.close()
                raise RuntimeError(
                    "Steam helper started but did not log on before the startup timeout"
                ) from exc

    async def close(self) -> None:
        """Stop heartbeats, log off, and reap the helper without blocking ticks."""

        self._closing = True
        await self._send_line("QUIT")
        process = self.process
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        for task in (self._publisher_task, self._supervisor_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._publisher_task, self._supervisor_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._publisher_task = None
        self._supervisor_task = None
        self.process = None
        self.logged_on = False
        self.steam_id = 0
        self.public_ip = 0
        self.secure = False
        self._ready.clear()
        self._registered.clear()
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None

    async def _supervise(self) -> None:
        backoffs = (1.0, 2.0, 5.0, 30.0)
        attempt = 0
        while not self._closing:
            try:
                await self._run_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.error("Steam bridge failed: %s", exc)
            finally:
                self.process = None
                self.logged_on = False
                self.steam_id = 0
                self.public_ip = 0
                self.secure = False
                self._ready.clear()
                self._registered.clear()
                self._last_advertisement = None
            if self._closing:
                return
            delay = backoffs[min(attempt, len(backoffs) - 1)]
            attempt += 1
            logger.warning("Restarting Steam bridge in %.0f second(s)", delay)
            await asyncio.sleep(delay)

    async def _run_once(self) -> None:
        assert self.inspection is not None
        assert self.helper_path is not None
        advertisement = self.snapshot()
        mode = 3 if self.config.secure else (2 if self.config.public else 1)

        # Old steam_api builds inspect steam_appid.txt in the working directory.
        # Use a private directory so a supplied Spacewar file is never edited.
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
        self._temp_dir = tempfile.TemporaryDirectory(prefix="battlespades-steam-")
        workdir = Path(self._temp_dir.name)
        (workdir / "steam_appid.txt").write_text(
            f"{STEAM_APP_ID}\n", encoding="ascii"
        )
        staged_runtime = workdir / "runtime"
        staged_runtime.mkdir()
        shutil.copy2(
            self.inspection.steam_api,
            staged_runtime / "steam_api.dll",
        )
        if (
            self.config.use_supplied_steamclient
            and self.inspection.steamclient is not None
        ):
            shutil.copy2(
                self.inspection.steamclient,
                staged_runtime / "steamclient.dll",
            )
        elif self.client_runtime_dir is not None:
            for name in ("steamclient.dll", "tier0_s.dll", "vstdlib_s.dll"):
                shutil.copy2(
                    self.client_runtime_dir / name,
                    staged_runtime / name,
                )

        args = [
            str(self.helper_path),
            "--runtime",
            str(staged_runtime),
            "--app-id",
            str(STEAM_APP_ID),
            "--steam-port",
            str(int(self.config.steam_port)),
            "--game-port",
            str(int(self.server.config.port)),
            "--query-port",
            str(effective_query_port(self.server.config)),
            "--server-mode",
            str(mode),
            "--version",
            str(self.config.game_version),
            "--name-b64",
            _b64(advertisement.server_name),
            "--map-b64",
            _b64(advertisement.map_name),
            "--max-players",
            str(advertisement.max_players),
            "--players",
            str(advertisement.player_count),
            "--tags-b64",
            _b64(advertisement.tags),
            "--region-b64",
            _b64(advertisement.region),
        ]
        env = dict(os.environ)
        env["SteamAppId"] = str(STEAM_APP_ID)
        env["SteamGameId"] = str(STEAM_APP_ID)
        self.process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(workdir),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info(
            "Steam bridge pid=%s game=%d query=%d steam=%d",
            self.process.pid,
            self.server.config.port,
            effective_query_port(self.server.config),
            self.config.steam_port,
        )
        self._ready.clear()
        stdout_task = asyncio.create_task(self._read_stdout(self.process))
        stderr_task = asyncio.create_task(self._read_stderr(self.process))
        try:
            await asyncio.wait_for(
                self._wait_until_ready_or_exit(self.process),
                timeout=float(self.config.startup_timeout_seconds),
            )
            await stdout_task
            return_code = await self.process.wait()
            if not self._closing and return_code != 0:
                raise RuntimeError(f"Steam bridge exited with code {return_code}")
        except asyncio.TimeoutError as exc:
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            raise RuntimeError(
                "SteamGameServer_Init exceeded the helper startup timeout"
            ) from exc
        finally:
            if not stdout_task.done():
                stdout_task.cancel()
            stderr_task.cancel()
            for task in (stdout_task, stderr_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _wait_until_ready_or_exit(
        self, process: asyncio.subprocess.Process
    ) -> None:
        """Wait for helper initialization while detecting an early exit."""

        while not self._ready.is_set():
            if process.returncode is not None:
                raise RuntimeError(
                    f"Steam bridge exited before READY with code {process.returncode}"
                )
            await asyncio.sleep(0.05)

    async def _read_stdout(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while True:
            raw = await process.stdout.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            fields = line.split("\t")
            kind = fields[0] if fields else ""
            if kind == "READY":
                self._ready.set()
                self._last_advertisement = self.snapshot()
                logger.info("Steam GameServer011 initialized")
            elif kind == "STATUS" and len(fields) >= 5:
                was_logged_on = self.logged_on
                self.logged_on = fields[1] == "1"
                self.secure = fields[2] == "1"
                try:
                    self.steam_id = int(fields[3])
                    self.public_ip = int(fields[4])
                except ValueError:
                    logger.warning("Ignoring malformed Steam status: %r", line)
                    continue
                if self.logged_on:
                    self._registered.set()
                else:
                    self._registered.clear()
                if self.logged_on and not was_logged_on:
                    logger.info(
                        "Steam master logon complete: steam_id=%d public_ip=0x%08x",
                        self.steam_id,
                        self.public_ip,
                    )
                elif was_logged_on and not self.logged_on:
                    logger.warning("Steam master logon was lost")
            elif kind == "FATAL":
                message = "\t".join(fields[1:]) or "unknown bridge failure"
                self.last_error = message
                logger.error("Steam bridge: %s", message)
            elif kind not in ("STOPPED", ""):
                logger.debug("Steam bridge: %s", line)

    async def _read_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while True:
            raw = await process.stderr.readline()
            if not raw:
                return
            logger.warning(
                "Steam bridge stderr: %s",
                raw.decode("utf-8", "replace").rstrip(),
            )

    async def _publish_loop(self) -> None:
        interval = float(self.config.publish_interval_seconds)
        while not self._closing:
            await asyncio.sleep(interval)
            if not self._ready.is_set():
                continue
            advertisement = self.snapshot()
            if advertisement == self._last_advertisement:
                continue
            if await self._send_advertisement(advertisement):
                self._last_advertisement = advertisement

    async def _send_advertisement(
        self, advertisement: SteamAdvertisement
    ) -> bool:
        # SteamGameServer011 has no independent unauthenticated-human count.
        # Until ticket authentication is wired, SetBotPlayerCount carries the
        # total population so the browser's players/max value stays truthful.
        fields = (
            "SET",
            _b64(advertisement.server_name),
            _b64(advertisement.map_name),
            str(advertisement.max_players),
            str(advertisement.player_count),
            _b64(advertisement.tags),
            _b64(advertisement.region),
        )
        return await self._send_line("\t".join(fields))

    async def _send_line(self, line: str) -> bool:
        process = self.process
        if process is None or process.returncode is not None or process.stdin is None:
            return False
        async with self._write_lock:
            try:
                process.stdin.write((line + "\n").encode("utf-8"))
                await process.stdin.drain()
                return True
            except (BrokenPipeError, ConnectionError, RuntimeError):
                return False
