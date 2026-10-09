"""Process-local network launch policy shared by console and desktop hosts."""

from __future__ import annotations

import argparse
from typing import Any
from urllib.parse import urlsplit


def normalize_master_url(value: str) -> str:
    """Accept an HTTPS origin, or HTTP on an exact loopback host."""
    problem = "master URL must be an HTTPS origin (HTTP is allowed only on localhost, 127.0.0.1 or ::1)"
    if not value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValueError(problem)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(problem) from exc
    if (
        not host or parsed.username is not None or parsed.password is not None
        or parsed.path not in ("", "/") or parsed.query or parsed.fragment
        or "?" in value or "#" in value or "\\" in value or "%" in parsed.netloc
        or (port is not None and not 1 <= port <= 65535)
        or parsed.netloc.endswith(":")
        or not (parsed.scheme == "https" or (
            parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
        ))
    ):
        raise ValueError(problem)
    return value.rstrip("/")


def add_network_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the same public network switches to both host entry points."""
    parser.add_argument(
        "--offline", action="store_true",
        help="host direct/LAN games without master registration, account checks, uploads or Steam relays",
    )
    parser.add_argument(
        "--master-url", type=normalize_master_url, metavar="ORIGIN",
        help="override the Revival master origin for this process (HTTPS or loopback HTTP)",
    )


def apply_network_document(data: dict[str, Any], *, offline: bool, master_url: str | None) -> None:
    """Apply launch overrides before the parsed TOML is validated; never save it."""
    if offline:
        # Disabled external services do not need their DLLs, ports, credentials
        # or a usable production master configuration on an offline machine.
        data["revival"] = {"enabled": False, "require_identity": False, "official": False}
        data["steam"] = {"enabled": False, "require_registration": False}
        data["steam_host"] = {"enabled": False}
        data["updates"] = {"update_check_enabled": False}
    if master_url is not None:
        revival = data.setdefault("revival", {})
        if not isinstance(revival, dict):
            raise ValueError("revival must be a TOML table")
        revival["base_url"] = normalize_master_url(master_url)


def apply_network_config(config: Any, *, offline: bool, master_url: str | None) -> Any:
    """Keep the offline policy effective even when environment helpers are set."""
    config.offline_mode = offline
    if master_url is not None:
        config.master_url_override = normalize_master_url(master_url)
        config.revival.base_url = config.master_url_override
    if offline:
        config.revival.enabled = False
        config.revival.require_identity = False
        config.revival.official = False
        config.steam.enabled = False
        config.steam.require_registration = False
        config.steam_host.enabled = False
        config.steam_p2p_enabled = False
        config.update_check_enabled = False
    return config
