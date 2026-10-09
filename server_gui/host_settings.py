"""The "Host" tab's friendly settings, mapped onto real config.toml keys.

Every field here reads and writes exactly one documented key (or a small,
documented pair) so the Advanced tab and the console server always agree:

=====================  ==============================================
Host tab               config.toml
=====================  ==============================================
Server name            [server] name (31 characters shown in browsers)
Game mode              [game] default_mode
Start map              [game] default_map
Rotation               [lobby] map_rotation ([] = retail playlist)
Max players            [server] max_players
Bots on/off            [bots] enabled
Bot mode / target      [bots] population_mode + fill_target / max_bots
Bot difficulty         [bots] difficulty
Join password          [server] password
Admin password         [admin] password
Auto-admin             [admin] auto_admin (verified steam:/aosplay: ids)
Port                   [server] port
=====================  ==============================================

Steam P2P is a launch option (``--steam-p2p``), not a config key.
"""

from __future__ import annotations

import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

from server_gui.config_doc import ConfigDocument
from shared.lan_discovery import STEAM_LAN_PORTS

RETAIL_GAME_PORT = 32887          # server/steam_master.py RETAIL_BROWSER_GAME_PORT
SHIPPED_SAMPLE_PORT = 27015       # config.toml sample default
MAX_NAME = 31                     # server/config.py MAX_SERVER_NAME_SIZE
SHIPPED_ADMIN_PASSWORD = "changeme"   # server/config.py DEFAULT_ADMIN_PASSWORD
# No 0/O, 1/l/I: the password is read aloud and copied off a screen.
_PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_admin_password(length: int = 16) -> str:
    """A random admin password (16 characters, about 93 bits)."""

    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(max(12, int(length))))


def admin_password_is_shipped_default(doc: ConfigDocument) -> bool:
    """Whether ``[admin] password`` is missing or still the sample "changeme".

    An operator who deliberately emptied it (login disabled) is respected.
    """

    return doc.get("admin", "password", None) in (None, SHIPPED_ADMIN_PASSWORD)


def ensure_admin_password(doc: ConfigDocument) -> str | None:
    """Replace the shipped default with a generated password; return it if so."""

    if not admin_password_is_shipped_default(doc):
        return None
    password = generate_admin_password()
    doc.set("admin", "password", password)
    return password


@dataclass
class HostSettings:
    name: str = "BattleSpades Server"
    mode: str = "tdm"
    start_map: str = "MayanJungle"
    rotation: str = "retail"              # retail / single / custom
    custom_rotation: list[str] = field(default_factory=list)
    max_players: int = 24
    bots_enabled: bool = True
    bot_mode: str = "backfill"            # backfill / fixed
    bot_target: int = 12
    difficulty: str = "mixed"
    password: str = ""
    admin_password: str = ""
    auto_admin: list[str] = field(default_factory=list)
    port: int = RETAIL_GAME_PORT


def read(doc: ConfigDocument) -> HostSettings:
    rotation_list = list(doc.get("lobby", "map_rotation", []) or [])
    start_map = str(doc.get("game", "default_map", "MayanJungle"))
    if not rotation_list:
        rotation = "retail"
    elif len(rotation_list) == 1 and rotation_list[0].casefold() == start_map.casefold():
        rotation = "single"
    else:
        rotation = "custom"
    population = str(doc.get("bots", "population_mode", "backfill"))
    bot_mode = "fixed" if population == "fixed" else "backfill"
    target_key = "max_bots" if bot_mode == "fixed" else "fill_target"
    return HostSettings(
        name=str(doc.get("server", "name", "BattleSpades Server")),
        mode=str(doc.get("game", "default_mode", "tdm")),
        start_map=start_map,
        rotation=rotation,
        custom_rotation=[str(m) for m in rotation_list],
        max_players=int(doc.get("server", "max_players", 24)),
        bots_enabled=bool(doc.get("bots", "enabled", False)),
        bot_mode=bot_mode,
        bot_target=int(doc.get("bots", target_key, 12)),
        difficulty=str(doc.get("bots", "difficulty", "mixed")),
        password=str(doc.get("server", "password", "")),
        admin_password=str(doc.get("admin", "password", "")),
        auto_admin=[str(value) for value in (doc.get("admin", "auto_admin", []) or [])],
        port=int(doc.get("server", "port", SHIPPED_SAMPLE_PORT)),
    )


def parse_auto_admin(text: str) -> list[str]:
    """Split the Host tab's comma/space separated auto-admin field."""

    return [part for part in text.replace(",", " ").split() if part]


def problems(settings: HostSettings) -> list[str]:
    """Problems the Host tab shows next to its fields before saving."""

    found = []
    if not settings.name.strip():
        found.append("Give the server a name.")
    if len(settings.password.encode("utf-8")) > 64:
        found.append("The join password can be at most 64 bytes.")
    if not 1 <= int(settings.port) <= 65535:
        found.append("The port must be between 1 and 65535.")
    if not 1 <= int(settings.max_players) <= 255:
        found.append("Max players must be between 1 and 255.")
    if settings.bot_target < 0:
        found.append("The bot count cannot be negative.")
    if settings.rotation == "custom" and not settings.custom_rotation:
        found.append("Pick at least one map for the custom rotation.")
    from server.config import normalize_admin_identity

    bad = [value for value in settings.auto_admin if normalize_admin_identity(value) is None]
    if bad:
        found.append("Auto-admin takes steam:<SteamID64> or aosplay:<account id>, not names: "
                     + ", ".join(bad[:3]))
    return found


def apply(doc: ConfigDocument, settings: HostSettings) -> None:
    doc.set("server", "name", settings.name.strip())
    doc.set("game", "default_mode", settings.mode)
    doc.set("game", "default_map", settings.start_map)
    if settings.rotation == "retail":
        doc.set("lobby", "map_rotation", [])
    elif settings.rotation == "single":
        doc.set("lobby", "map_rotation", [settings.start_map])
    else:
        doc.set("lobby", "map_rotation", list(settings.custom_rotation))
    doc.set("server", "max_players", int(settings.max_players))
    doc.set("bots", "enabled", bool(settings.bots_enabled))
    target = max(0, int(settings.bot_target))
    if settings.bot_mode == "fixed":
        doc.set("bots", "population_mode", "fixed")
        doc.set("bots", "max_bots", target)
    else:
        doc.set("bots", "population_mode", "backfill")
        doc.set("bots", "fill_target", target)
        if int(doc.get("bots", "max_bots", 0) or 0) < target:
            doc.set("bots", "max_bots", target)
    doc.set("bots", "difficulty", settings.difficulty)
    doc.set("server", "password", settings.password)
    if settings.admin_password:
        doc.set("admin", "password", settings.admin_password)
    if list(doc.get("admin", "auto_admin", []) or []) != list(settings.auto_admin):
        doc.set("admin", "auto_admin", list(settings.auto_admin))
    doc.set("server", "port", int(settings.port))


def server_ports(doc: ConfigDocument, port: int | None = None) -> list[int]:
    """UDP ports players and browsers reach: game port, plus Steam ones if listed."""

    game = int(port if port is not None else doc.get("server", "port", SHIPPED_SAMPLE_PORT))
    ports = [game]
    if bool(doc.get("steam", "enabled", False)):
        query = int(doc.get("steam", "query_port", 0) or 0) or game + 1
        ports.append(query)
        ports.append(int(doc.get("steam", "steam_port", 8766)))
    return sorted(set(ports))


def query_port(doc: ConfigDocument) -> int:
    game = int(doc.get("server", "port", SHIPPED_SAMPLE_PORT))
    return int(doc.get("steam", "query_port", 0) or 0) or game + 1


def firewall_ports(doc: ConfigDocument) -> list[int]:
    """Include LAN scan ports in local firewall rules, not router mappings."""
    ports = set(server_ports(doc))
    game = int(doc.get("server", "port", SHIPPED_SAMPLE_PORT))
    if doc.get("server", "lan_discovery", True) and game not in STEAM_LAN_PORTS:
        ports.update(STEAM_LAN_PORTS)
    return sorted(ports)


def lan_discovery_note(doc: ConfigDocument) -> str:
    """Describe the next startup's LAN discovery setting."""
    if not doc.get("server", "lan_discovery", True):
        return "Extra LAN listener disabled. Direct A2S queries still work on the game port."
    game = int(doc.get("server", "port", SHIPPED_SAMPLE_PORT))
    endpoint = f"UDP {game}" if game in STEAM_LAN_PORTS else "the first free UDP port in 27015-27020"
    return (f"Steam LAN discovery uses {endpoint}. Allow the server through your local firewall. "
            "No Steam runtime or router forwarding needed. Applies when the server starts.")


@dataclass
class Readiness:
    available: bool
    reason: str = ""
    warning: str = ""


def steam_p2p_readiness(root: Path, doc: ConfigDocument, *, platform: str = sys.platform,
                        helper: Path | None = None, steam_running: bool | None = None) -> Readiness:
    """Can ``--steam-p2p`` start here? Mirrors server/steam_p2p.py's checks."""

    if platform != "win32":
        return Readiness(False, "Steam P2P hosting currently needs a Windows host.")
    if helper is None:
        return Readiness(False, "The Steam relay helper (relay/aos-retail-relay.exe) is not in this build.")
    if bool(doc.get("revival", "require_identity", False)):
        return Readiness(False, "Turn off [revival] require_identity to host over Steam P2P.")
    if steam_running is False:
        return Readiness(True, warning="Steam is not running. Start Steam and sign in so friends can join.")
    return Readiness(True)


def steam_running() -> bool | None:
    """Windows: Steam records its live process id in the registry."""

    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as key:
            pid, _kind = winreg.QueryValueEx(key, "pid")
        return bool(pid)
    except OSError:
        return None


def steam_browser_readiness(root: Path, doc: ConfigDocument, *, platform: str = sys.platform) -> list[tuple[str, str]]:
    """What listing in the original Steam server browser still needs."""

    notes: list[tuple[str, str]] = []
    if platform != "win32":
        notes.append(("bad", "The Steam browser registrar uses a Windows helper; it is not available on this OS."))
    runtime_dir = str(doc.get("steam", "runtime_dir", "") or "")
    runtime = (Path(runtime_dir) if Path(runtime_dir).is_absolute() else root / (runtime_dir or "steam-runtime"))
    if (runtime / "steam_api.dll").is_file():
        notes.append(("ok", f"Steam runtime found in {runtime}."))
    else:
        notes.append(("warn", f"Put the original 32-bit steam_api.dll in {runtime} (not included for licensing reasons; "
                              "see steam-runtime/README.txt)."))
    port = int(doc.get("server", "port", SHIPPED_SAMPLE_PORT))
    if port != RETAIL_GAME_PORT:
        notes.append(("warn", f"The stock browser always connects to port {RETAIL_GAME_PORT}; your port is {port}."))
    notes.append(("info", f"Forward UDP {', '.join(map(str, server_ports(doc)))} on your router (game, query, Steam)."))
    return notes


def join_info(*, name: str, public_ip: str | None, lan_ip: str | None, port: int,
              password: bool, steam_hosted: bool, steam_lobby: str = "") -> str:
    """Text for the clipboard: how friends join, without secrets."""

    lines = [f"BattleSpades server: {name}"]
    if public_ip:
        lines.append(f"Internet: {public_ip}:{port}")
    if lan_ip:
        lines.append(f"Same network (LAN): {lan_ip}:{port}")
    if steam_hosted:
        lines.append("Steam: find it as [Steam] " + name + " in the in-game server browser "
                     "(retail client with the Steam relay drop-in; no port forwarding needed)")
        if steam_lobby:
            lines.append(f"Steam lobby id: {steam_lobby}")
    if password:
        lines.append("Password required (ask the host).")
    lines.append("Get the game client: https://www.aosplay.net")
    return "\n".join(lines)
