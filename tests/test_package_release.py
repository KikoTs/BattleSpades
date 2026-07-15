"""Allowlisted portable-release staging and archive tests."""

import hashlib
from pathlib import Path
import subprocess
import sys
import zipfile

from scripts.package_release import (
    ReleaseTarget,
    archive_release,
    stage_release,
)
from scripts.verify_release_assets import (
    expected_archive_names,
    write_checksum_manifest,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKED_MAPS = sorted(path.name for path in (PROJECT_ROOT / "maps").glob("*.vxl"))
TRACKED_PREFABS = sorted(
    path.name for path in (PROJECT_ROOT / "prefabs").glob("*.kv6")
)


def _fake_frozen_tree(tmp_path: Path) -> Path:
    frozen = tmp_path / "frozen"
    (frozen / "_internal").mkdir(parents=True)
    (frozen / "BattleSpades.exe").write_bytes(b"launcher")
    (frozen / "_internal" / "runtime.dll").write_bytes(b"runtime")
    return frozen


def test_release_name_contains_version_platform_and_architecture() -> None:
    """Artifact identity is explicit and never inferred from a runner later."""

    target = ReleaseTarget(
        platform="windows",
        architecture="arm64",
        version="0.0.1-alpha.1",
    )

    assert target.directory_name == (
        "BattleSpades-0.0.1-alpha.1-windows-arm64"
    )


def test_invalid_release_target_is_rejected() -> None:
    """Unsupported aliases cannot silently produce mislabeled binaries."""

    try:
        ReleaseTarget("win32", "amd64", "0.0.1-alpha.1")
    except ValueError as exc:
        assert "platform" in str(exc)
    else:
        raise AssertionError("invalid release target was accepted")


def test_stage_release_copies_only_required_operator_content(tmp_path: Path) -> None:
    """Maps/prefabs ship, while unrelated root executables never enter output."""

    target = ReleaseTarget("windows", "x86_64", "0.0.2-alpha.1")

    staged = stage_release(
        PROJECT_ROOT,
        _fake_frozen_tree(tmp_path),
        tmp_path / "out",
        target,
    )

    assert (staged / "BattleSpades.exe").is_file()
    assert (staged / "_internal" / "runtime.dll").is_file()
    assert (staged / "config.toml").is_file()
    assert (staged / "LICENSE").is_file()
    assert (staged / "VERSION").read_text(encoding="utf-8").strip() == target.version
    assert sorted(path.name for path in (staged / "maps").glob("*.vxl")) == TRACKED_MAPS
    assert sorted(path.name for path in (staged / "prefabs").glob("*.kv6")) == TRACKED_PREFABS
    assert (staged / "plugins" / "README.txt").is_file()
    assert (staged / "client_patches" / "INSTALL.txt").is_file()
    assert (staged / "client_patches" / "session_transition_patch.py").is_file()
    assert not (staged / "codex-command-runner.exe").exists()
    assert not (staged / "tests").exists()


def test_stage_release_refuses_existing_destination(tmp_path: Path) -> None:
    """A second build cannot merge stale files into a new artifact."""

    target = ReleaseTarget("windows", "x86_64", "0.0.1-alpha.1")
    output = tmp_path / "out"
    (output / target.directory_name).mkdir(parents=True)

    try:
        stage_release(PROJECT_ROOT, _fake_frozen_tree(tmp_path), output, target)
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing release destination was overwritten")


def test_archive_hash_matches_written_bytes(tmp_path: Path) -> None:
    """Published SHA-256 values describe the final closed zip bytes."""

    staged = tmp_path / "BattleSpades-0.0.1-alpha.1-linux-x86_64"
    staged.mkdir()
    (staged / "BattleSpades").write_bytes(b"launcher")

    archive, digest = archive_release(staged)

    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.namelist() == [
            f"{staged.name}/BattleSpades",
        ]


def test_pyinstaller_spec_keeps_operator_content_external() -> None:
    """The frozen runtime is onedir and does not bury mutable assets."""

    spec = (PROJECT_ROOT / "BattleSpades.spec").read_text(encoding="utf-8")

    assert "exclude_binaries=True" in spec
    assert "COLLECT(" in spec
    assert 'name="BattleSpades"' in spec
    assert '"server.bot_ai.recast"' in spec
    assert "maps" not in spec
    assert "prefabs" not in spec
    assert "config.toml" not in spec


def test_checksum_manifest_requires_all_six_archives(tmp_path: Path) -> None:
    """The publisher accepts a complete matrix and writes sorted hashes."""

    names = expected_archive_names("0.0.1-alpha.1")
    for index, name in enumerate(reversed(names)):
        (tmp_path / name).write_bytes(f"archive-{index}".encode("ascii"))

    manifest = write_checksum_manifest(tmp_path, "0.0.1-alpha.1")

    lines = manifest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6
    assert [line.split("  ", 1)[1] for line in lines] == sorted(names)


def test_checksum_manifest_rejects_partial_matrix(tmp_path: Path) -> None:
    """One missing native target blocks publication instead of going partial."""

    first = expected_archive_names("0.0.1-alpha.1")[0]
    (tmp_path / first).write_bytes(b"partial")

    try:
        write_checksum_manifest(tmp_path, "0.0.1-alpha.1")
    except RuntimeError as exc:
        assert "expected exactly 6 release archives" in str(exc)
    else:
        raise AssertionError("partial release matrix was accepted")


def test_checksum_cli_runs_without_installed_project_dependencies(
    tmp_path: Path,
) -> None:
    """The publish job can verify assets without installing server packages."""

    for name in expected_archive_names("0.0.2-alpha.1"):
        (tmp_path / name).write_bytes(name.encode("utf-8"))

    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            str(PROJECT_ROOT / "scripts" / "verify_release_assets.py"),
            "--asset-dir",
            str(tmp_path),
            "--project-root",
            str(PROJECT_ROOT),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "SHA256SUMS.txt").is_file()
