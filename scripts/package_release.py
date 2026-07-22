"""Stage and archive an allowlisted portable BattleSpades release."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform as platform_module
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.runtime_paths import read_version


_PLATFORMS = frozenset({"windows", "linux", "macos"})
_ARCHITECTURES = frozenset({"x86_64", "arm64"})


@dataclass(frozen=True, slots=True)
class ReleaseTarget:
    """Validated identity of one native release artifact."""

    platform: str
    architecture: str
    version: str

    def __post_init__(self) -> None:
        if self.platform not in _PLATFORMS:
            raise ValueError(f"unsupported release platform: {self.platform!r}")
        if self.architecture not in _ARCHITECTURES:
            raise ValueError(
                f"unsupported release architecture: {self.architecture!r}"
            )
        if not self.version or any(character.isspace() for character in self.version):
            raise ValueError(f"invalid release version: {self.version!r}")

    @property
    def directory_name(self) -> str:
        """Return the canonical archive root and asset basename."""

        return (
            f"BattleSpades-{self.version}-{self.platform}-{self.architecture}"
        )


def _copy_files(source: Path, destination: Path, pattern: str) -> int:
    """Copy sorted regular files matching a required asset pattern."""

    files = sorted(path for path in source.glob(pattern) if path.is_file())
    destination.mkdir(parents=True, exist_ok=True)
    for path in files:
        if path.is_symlink():
            raise ValueError(f"release assets may not be symlinks: {path}")
        shutil.copy2(path, destination / path.name)
    return len(files)


def stage_release(
    project_root: Path,
    frozen_dir: Path,
    output_root: Path,
    target: ReleaseTarget,
) -> Path:
    """Build one visible/editable release tree from approved inputs.

    Args:
        project_root: Repository containing configuration and game assets.
        frozen_dir: PyInstaller one-directory output for the target machine.
        output_root: Parent directory for the versioned staged tree.
        target: Validated platform, architecture, and version identity.

    Returns:
        Absolute path to the staged release directory.

    Raises:
        FileExistsError: If stale output could contaminate this build.
        FileNotFoundError: If a required frozen or repository input is absent.
        ValueError: If no maps/prefabs exist or the version does not match.
    """

    root = Path(project_root).resolve()
    frozen = Path(frozen_dir).resolve()
    destination = Path(output_root).resolve() / target.directory_name
    if destination.exists():
        raise FileExistsError(f"release destination already exists: {destination}")
    if not frozen.is_dir():
        raise FileNotFoundError(f"PyInstaller output is missing: {frozen}")
    executable_suffix = ".exe" if target.platform == "windows" else ""
    launchers = (
        frozen / f"BattleSpades{executable_suffix}",
        frozen / f"BattleSpadesTutorial{executable_suffix}",
        frozen / f"BattleSpadesMapCreator{executable_suffix}",
    )
    missing_launchers = [str(path) for path in launchers if not path.is_file()]
    if missing_launchers:
        raise FileNotFoundError(
            "frozen launcher is missing: " + ", ".join(missing_launchers)
        )
    repository_version = read_version(root)
    if repository_version != target.version:
        raise ValueError(
            f"target version {target.version!r} does not match VERSION "
            f"{repository_version!r}"
        )

    required_files = {
        root / "config.toml": "config.toml",
        root / "LICENSE": "LICENSE",
        root / "VERSION": "VERSION",
        root / "release" / "README.txt": "README.txt",
        root / "release" / "THIRD_PARTY_NOTICES.txt": "THIRD_PARTY_NOTICES.txt",
        root / "release" / "STEAM_RUNTIME.txt": "steam-runtime/README.txt",
        root / "client_patches" / "INSTALL.txt": "client_patches/INSTALL.txt",
        root / "client_patches" / "session_transition_patch.py": (
            "client_patches/session_transition_patch.py"
        ),
        root / "client_patches" / "clipboard_input_patch.py": (
            "client_patches/clipboard_input_patch.py"
        ),
        root / "client_patches" / "character_jump_smoothing.py": (
            "client_patches/character_jump_smoothing.py"
        ),
    }
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"required release inputs are missing: {', '.join(missing)}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(frozen, destination)
    try:
        for source, relative_name in required_files.items():
            target_path = destination / relative_name
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target_path)

        map_count = _copy_files(root / "maps", destination / "maps", "*.vxl")
        _copy_files(root / "maps", destination / "maps", "*.json")
        prefab_count = _copy_files(
            root / "prefabs",
            destination / "prefabs",
            "*.kv6",
        )
        _copy_files(
            root / "release" / "plugins",
            destination / "plugins",
            "*",
        )
        if map_count == 0:
            raise ValueError("release must contain at least one VXL map")
        if prefab_count == 0:
            raise ValueError("release must contain at least one KV6 prefab")
        if not (destination / "plugins" / "README.txt").is_file():
            raise FileNotFoundError("release plugin instructions are missing")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def archive_release(staged_dir: Path) -> tuple[Path, str]:
    """Zip a staged directory and return its path and lowercase SHA-256."""

    staged = Path(staged_dir).resolve()
    if not staged.is_dir():
        raise FileNotFoundError(f"staged release is missing: {staged}")
    archive = staged.parent / f"{staged.name}.zip"
    if archive.exists():
        raise FileExistsError(f"release archive already exists: {archive}")

    with zipfile.ZipFile(
        archive,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as bundle:
        for path in sorted(
            (candidate for candidate in staged.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.as_posix(),
        ):
            if path.is_symlink():
                raise ValueError(f"release archive may not contain symlinks: {path}")
            relative = path.relative_to(staged.parent).as_posix()
            bundle.write(path, relative)

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return archive, digest


def _host_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise ValueError(f"unsupported host platform: {sys.platform!r}")


def _host_architecture() -> str:
    machine = platform_module.machine().lower()
    aliases = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    try:
        return aliases[machine]
    except KeyError as exc:
        raise ValueError(f"unsupported host architecture: {machine!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    """Create the release-staging command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--frozen-dir", type=Path, default=Path("dist/BattleSpades"))
    parser.add_argument("--output-root", type=Path, default=Path("release-dist"))
    parser.add_argument("--platform", choices=sorted(_PLATFORMS), default=_host_platform())
    parser.add_argument(
        "--architecture",
        choices=sorted(_ARCHITECTURES),
        default=_host_architecture(),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Stage and archive the current native build."""

    arguments = build_parser().parse_args(argv)
    project_root = arguments.project_root.resolve()
    target = ReleaseTarget(
        arguments.platform,
        arguments.architecture,
        read_version(project_root),
    )
    staged = stage_release(
        project_root,
        arguments.frozen_dir,
        arguments.output_root,
        target,
    )
    archive, digest = archive_release(staged)
    print(f"archive={archive}")
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
