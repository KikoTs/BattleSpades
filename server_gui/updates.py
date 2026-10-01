"""'Check for updates' using the server's own manifest reader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from server.update_check import DEFAULT_MANIFEST_URL, check_for_update


@dataclass
class UpdateStatus:
    ok: bool
    current: str
    latest: str = ""
    message: str = ""
    newer: bool = False


def current_version(root: Path) -> str:
    try:
        from server.runtime_paths import read_version

        return read_version(root)
    except Exception:
        return "unknown"


def check(root: Path, url: str | None = None, *, checker=check_for_update) -> UpdateStatus:
    current = current_version(root)
    try:
        release = checker(current, url or DEFAULT_MANIFEST_URL)
    except Exception as exc:  # offline, bad manifest, TLS...
        return UpdateStatus(False, current, message=f"Could not check for updates: {exc}")
    if release is None:
        return UpdateStatus(True, current, current, f"BattleSpades server {current} is up to date.")
    return UpdateStatus(
        True, current, release.version,
        f"Version {release.version} is available (you have {current}). Stop the server, then run "
        "'BattleSpades --update' to download and stage it, or get it from https://www.aosplay.net.",
        newer=True,
    )
