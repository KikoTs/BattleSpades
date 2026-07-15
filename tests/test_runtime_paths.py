"""Runtime layout tests for source and frozen BattleSpades launches."""

from pathlib import Path

import pytest

from server.config import ServerConfig
from server.runtime_paths import (
    RuntimePathError,
    RuntimePaths,
    apply_runtime_paths,
    read_version,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_source_runtime_root_ignores_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source paths are based on the entrypoint, not the shell directory."""

    monkeypatch.chdir(tmp_path)

    paths = RuntimePaths.discover(source_entry=PROJECT_ROOT / "run_server.py")

    assert paths.root == PROJECT_ROOT
    assert paths.config == PROJECT_ROOT / "config.toml"
    assert paths.maps == PROJECT_ROOT / "maps"
    assert paths.prefabs == PROJECT_ROOT / "prefabs"


def test_frozen_runtime_root_is_executable_parent(tmp_path: Path) -> None:
    """A frozen launcher owns resources beside its executable."""

    executable = tmp_path / "release" / "BattleSpades.exe"

    paths = RuntimePaths.discover(frozen=True, executable=executable)

    assert paths.root == executable.parent.resolve()
    assert paths.logs == executable.parent.resolve() / "logs"


def test_relative_config_paths_are_anchored_to_runtime_root(
    tmp_path: Path,
) -> None:
    """Portable relative paths resolve inside the extracted archive."""

    paths = RuntimePaths.from_root(tmp_path)
    config = ServerConfig(maps_path="custom-maps")

    returned = apply_runtime_paths(config, paths)

    assert returned is config
    assert Path(config.maps_path) == tmp_path.resolve() / "custom-maps"
    assert Path(config.prefabs_path) == paths.prefabs
    assert Path(config.plugins_path) == paths.plugins
    assert Path(config.bans_path) == paths.bans


def test_absolute_configured_map_path_is_preserved(tmp_path: Path) -> None:
    """Operators can deliberately keep maps outside the release directory."""

    external_maps = (tmp_path / "external-maps").resolve()
    config = ServerConfig(maps_path=str(external_maps))

    apply_runtime_paths(config, RuntimePaths.from_root(tmp_path / "release"))

    assert Path(config.maps_path) == external_maps


def test_prefab_registry_can_be_bound_to_release_directory(tmp_path: Path) -> None:
    """A release never falls back to a developer's game installation."""

    from server import prefabs

    try:
        prefabs.configure_prefab_search_dirs(tmp_path / "prefabs")

        assert prefabs.get_registry().search_dirs == (
            str((tmp_path / "prefabs").resolve()),
        )
    finally:
        prefabs.configure_prefab_search_dirs(*prefabs.PREFAB_SEARCH_DIRS)


def test_server_consumes_runtime_owned_bans_and_prefabs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server construction uses resolved state paths instead of cwd defaults."""

    from server import bans, prefabs
    from server.main import BattleSpadesServer

    calls: dict[str, Path] = {}

    class RecordingBanManager:
        def __init__(self, path: str) -> None:
            calls["bans"] = Path(path)

    def record_prefabs(path: str | Path) -> None:
        calls["prefabs"] = Path(path)

    monkeypatch.setattr(bans, "BanManager", RecordingBanManager)
    monkeypatch.setattr(prefabs, "configure_prefab_search_dirs", record_prefabs)
    config = apply_runtime_paths(
        ServerConfig(),
        RuntimePaths.from_root(tmp_path),
    )

    BattleSpadesServer(config)

    assert calls == {
        "bans": (tmp_path / "bans.json").resolve(),
        "prefabs": (tmp_path / "prefabs").resolve(),
    }


def test_version_has_expected_prerelease_value() -> None:
    """All release metadata reads one canonical SemVer prerelease value."""

    assert read_version(PROJECT_ROOT) == "0.0.1-alpha.1"


@pytest.mark.parametrize("value", ["", "0.0.1 alpha", "0.0.1\nother"])
def test_invalid_version_file_is_rejected(tmp_path: Path, value: str) -> None:
    """Malformed release versions fail rather than create ambiguous assets."""

    (tmp_path / "VERSION").write_text(value, encoding="utf-8")

    with pytest.raises(RuntimePathError):
        read_version(tmp_path)
