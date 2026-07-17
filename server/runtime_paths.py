"""Resolve BattleSpades-owned files for source and frozen launches."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from server.config import ServerConfig


_VERSION_PATTERN = re.compile(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
)


class RuntimePathError(ValueError):
    """Raised when an application path or release version is unusable."""


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Absolute locations owned by one source checkout or release archive."""

    root: Path
    config: Path
    maps: Path
    prefabs: Path
    plugins: Path
    logs: Path
    bans: Path

    @classmethod
    def from_root(cls, root: str | Path) -> "RuntimePaths":
        """Construct the standard portable layout below ``root``."""

        resolved = Path(root).resolve()
        return cls(
            root=resolved,
            config=resolved / "config.toml",
            maps=resolved / "maps",
            prefabs=resolved / "prefabs",
            plugins=resolved / "plugins",
            logs=resolved / "logs",
            bans=resolved / "bans.json",
        )

    @classmethod
    def discover(
        cls,
        *,
        frozen: bool | None = None,
        executable: str | Path | None = None,
        source_entry: str | Path | None = None,
    ) -> "RuntimePaths":
        """Find the application root without using the current directory.

        Args:
            frozen: Explicit frozen-state override, primarily for tests.
            executable: Frozen executable path override.
            source_entry: Source entrypoint whose parent is the project root.

        Returns:
            The absolute portable runtime layout.

        Raises:
            RuntimePathError: If source discovery has no usable entrypoint.
        """

        is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        if is_frozen:
            launcher = Path(executable or sys.executable)
            return cls.from_root(launcher.resolve().parent)

        if source_entry is None:
            source_entry = Path(__file__).resolve().parents[1] / "run_server.py"
        entry = Path(source_entry).resolve()
        if not entry.name:
            raise RuntimePathError("source entrypoint path is empty")
        return cls.from_root(entry.parent)

    def resolve_configured_path(self, value: str | Path) -> Path:
        """Anchor a relative configured path to the application root."""

        configured = Path(value).expanduser()
        if configured.is_absolute():
            return configured.resolve()
        return (self.root / configured).resolve()


def read_version(root: str | Path | None = None) -> str:
    """Read and validate the canonical release version from ``VERSION``."""

    version_root = Path(root).resolve() if root is not None else RuntimePaths.discover().root
    version_file = version_root / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimePathError(f"cannot read release version: {version_file}") from exc
    if not _VERSION_PATTERN.fullmatch(version):
        raise RuntimePathError(f"invalid release version in {version_file}: {version!r}")
    return version


def apply_runtime_paths(config: ServerConfig, paths: RuntimePaths) -> ServerConfig:
    """Resolve every mutable server resource against one application root."""

    config.maps_path = str(paths.resolve_configured_path(config.maps_path))
    config.prefabs_path = str(paths.resolve_configured_path(config.prefabs_path))
    config.plugins_path = str(paths.resolve_configured_path(config.plugins_path))
    config.bans_path = str(paths.resolve_configured_path(config.bans_path))
    config.steam.runtime_dir = str(
        paths.resolve_configured_path(config.steam.runtime_dir or "steam-runtime")
    )
    if config.steam.steamclient_dir:
        config.steam.steamclient_dir = str(
            paths.resolve_configured_path(config.steam.steamclient_dir)
        )
    if config.steam.helper_path:
        config.steam.helper_path = str(
            paths.resolve_configured_path(config.steam.helper_path)
        )
    return config
