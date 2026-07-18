"""Per-session configuration contracts shared with the retail launcher."""

from pathlib import Path

import pytest

from server import launcher, tutorial_launcher, ugc_launcher
from server.release_check import CheckItem, CheckReport
from server.runtime_paths import RuntimePaths


def _write_config(path: Path, *, port: int = 32887) -> Path:
    """Create the smallest valid configuration used by CLI routing tests."""

    path.write_text(f"[server]\nport = {port}\n", encoding="utf-8")
    return path


def _passing_report() -> CheckReport:
    """Return the smallest successful bounded health report."""

    return CheckReport((CheckItem("test", True, "ok"),))


def test_runtime_layout_can_select_external_config_without_moving_assets(
    tmp_path: Path,
) -> None:
    """Temporary host settings never re-anchor maps or mutable server state."""

    root = tmp_path / "server"
    config = _write_config(tmp_path / "session.toml")

    paths = RuntimePaths.from_root(root).with_config(config)

    assert paths.config == config.resolve()
    assert paths.root == root.resolve()
    assert paths.maps == root.resolve() / "maps"
    assert paths.prefabs == root.resolve() / "prefabs"
    assert paths.logs == root.resolve() / "logs"


def test_dedicated_launcher_passes_external_config_and_port_in_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal frozen entrypoint consumes a session file without rewriting it."""

    config = _write_config(tmp_path / "session.toml")
    original = config.read_bytes()
    observed: dict[str, object] = {}

    def fake_run(paths: RuntimePaths, *, config_transform=None, **_kwargs) -> int:
        observed["paths"] = paths
        runtime = type("Config", (), {"port": 1})()
        observed["config"] = config_transform(runtime)
        return 0

    monkeypatch.setattr(launcher, "_run_server", fake_run)

    result = launcher.run(
        ["--config", str(config), "--port", "40123"],
        paths=RuntimePaths.from_root(tmp_path / "bundle"),
    )

    assert result == 0
    assert observed["paths"].config == config.resolve()
    assert observed["config"].port == 40123
    assert config.read_bytes() == original


@pytest.mark.parametrize(
    ("module", "expected_prefix"),
    [
        (launcher, "Server startup failed:"),
        (tutorial_launcher, "Tutorial startup failed:"),
        (ugc_launcher, "Map Creator startup failed:"),
    ],
)
def test_all_launchers_reject_a_missing_explicit_config(
    module,
    expected_prefix: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typo cannot silently launch a server with unrelated defaults."""

    result = module.run(
        ["--config", str(tmp_path / "missing.toml")],
        paths=RuntimePaths.from_root(tmp_path / "bundle"),
    )

    assert result == 1
    assert expected_prefix in capsys.readouterr().err


@pytest.mark.parametrize(
    "module",
    [launcher, tutorial_launcher, ugc_launcher],
)
def test_all_launchers_reject_malformed_explicit_config(
    module,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Generated-session corruption cannot silently start with defaults."""

    config = tmp_path / "broken.toml"
    config.write_text("[server\nport = nope\n", encoding="utf-8")

    result = module.run(
        ["--config", str(config)],
        paths=RuntimePaths.from_root(tmp_path / "bundle"),
    )

    assert result == 1
    assert "cannot parse configuration file" in capsys.readouterr().err


def test_tutorial_check_uses_explicit_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tutorial health checks inspect the same session file used at startup."""

    config = _write_config(tmp_path / "tutorial.toml")
    observed: dict[str, Path] = {}

    def fake_check(paths: RuntimePaths) -> CheckReport:
        observed["config"] = paths.config
        return _passing_report()

    monkeypatch.setattr(tutorial_launcher, "_tutorial_check", fake_check)

    assert tutorial_launcher.run(
        ["--check", "--config", str(config)],
        paths=RuntimePaths.from_root(tmp_path / "bundle"),
    ) == 0
    assert observed["config"] == config.resolve()


def test_map_creator_check_uses_explicit_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Map Creator reads launcher defaults from its selected session TOML."""

    config = _write_config(tmp_path / "creator.toml")
    observed: dict[str, Path] = {}

    def fake_check(paths: RuntimePaths, _retail_root) -> CheckReport:
        observed["config"] = paths.config
        return _passing_report()

    monkeypatch.setattr(ugc_launcher, "_ugc_check", fake_check)

    assert ugc_launcher.run(
        ["--check", "--config", str(config)],
        paths=RuntimePaths.from_root(tmp_path / "bundle"),
    ) == 0
    assert observed["config"] == config.resolve()


@pytest.mark.parametrize(
    "module",
    [launcher, tutorial_launcher, ugc_launcher],
)
@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_all_launchers_reject_invalid_port(
    module,
    port: str,
    tmp_path: Path,
) -> None:
    """Invalid host ports fail during argument parsing before any side effects."""

    assert module.run(
        ["--port", port],
        paths=RuntimePaths.from_root(tmp_path),
    ) == 2
