"""Display release identity and stable, reproducible build-day metadata.

This label is independent of VERSION and the retail protocol/Steam versions.
The freezer writes metadata once; joining players never change its date.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


DISPLAY_RELEASE = "BattleSpades Beta 1.1"
BUILD_INFO_FILENAME = "build_info.json"
SERVER_REPOSITORY = "https://github.com/KikoTs/BattleSpades"
CLIENT_REPOSITORY = "https://github.com/KikoTs/BattleSpadesClient"
PROJECT_WEBSITE = "https://aosplay.net"


@dataclass(frozen=True, slots=True)
class BuildInfo:
    release: str
    date: str
    source: str = "build"

    @property
    def date_label(self) -> str:
        """Distinguish a packaged build day from an unbuilt source fallback."""

        return self.date if self.source == "build" else f"{self.source} {self.date}"


def _utc_day(epoch: int | float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).date().isoformat()


def write_build_info(path: Path, *, epoch: int | None = None) -> Path:
    """Write deterministic JSON at build time, honoring SOURCE_DATE_EPOCH."""

    if epoch is None:
        configured = os.environ.get("SOURCE_DATE_EPOCH")
        epoch = int(configured) if configured is not None else int(time.time())
    if epoch < 0:
        raise ValueError("build epoch must be nonnegative")
    metadata = {
        "schema": 1,
        "release": DISPLAY_RELEASE,
        "build_epoch": epoch,
        "build_date": _utc_day(epoch),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def load_build_info(root: Path, *, binary: Path | None = None) -> BuildInfo:
    """Read a packaged stamp, with a stable file-date fallback for dev trees."""

    root = Path(root)
    candidates = (root / BUILD_INFO_FILENAME, root / "_internal" / BUILD_INFO_FILENAME)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            epoch = data["build_epoch"]
            if (
                data.get("schema") == 1
                and isinstance(epoch, int)
                and not isinstance(epoch, bool)
                and epoch >= 0
                and data.get("build_date") == _utc_day(epoch)
                and data.get("release") == DISPLAY_RELEASE
            ):
                return BuildInfo(DISPLAY_RELEASE, data["build_date"])
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            continue
    fallback = Path(binary) if binary is not None else root / "server" / "build_info.py"
    try:
        return BuildInfo(DISPLAY_RELEASE, _utc_day(fallback.stat().st_mtime),
                         "binary" if binary is not None else "source")
    except (OSError, ValueError, OverflowError):
        return BuildInfo(DISPLAY_RELEASE, "unknown", "date")


@lru_cache(maxsize=1)
def runtime_build_info() -> BuildInfo:
    """Resolve metadata once for this process, independent of login time."""

    if bool(getattr(sys, "frozen", False)):
        binary = Path(sys.executable).resolve()
        return load_build_info(binary.parent, binary=binary)
    return load_build_info(Path(__file__).resolve().parents[1])
