"""CLI and packaged health-check behavior."""

from pathlib import Path
import sys

import pytest

from server.launcher import run
from server.release_check import run_release_check
from server.runtime_paths import RuntimePaths


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_version_does_not_import_native_server(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Version reporting works even when gameplay/native imports are broken."""

    monkeypatch.delitem(sys.modules, "server.main", raising=False)

    assert run(["--version"], paths=RuntimePaths.from_root(PROJECT_ROOT)) == 0

    assert capsys.readouterr().out.strip() == "BattleSpades 0.0.3-alpha.1"
    assert "server.main" not in sys.modules


def test_help_has_no_log_side_effect(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Argument discovery does not initialize logging or native runtime."""

    assert run(["--help"], paths=RuntimePaths.from_root(tmp_path)) == 0

    assert "BattleSpades" in capsys.readouterr().out
    assert not (tmp_path / "logs").exists()


def test_check_returns_nonzero_when_config_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A partial archive fails closed with an actionable missing-file line."""

    assert run(["--check"], paths=RuntimePaths.from_root(tmp_path)) == 1

    captured = capsys.readouterr()
    assert "FAIL config.toml" in captured.err
    assert captured.out == ""


def test_source_release_check_passes() -> None:
    """The checked-out runtime satisfies the same checks as a staged bundle."""

    report = run_release_check(RuntimePaths.from_root(PROJECT_ROOT))

    assert report.ok, "\n".join(report.lines)
    assert report.exit_code == 0
    assert any(line.startswith("OK native imports") for line in report.lines)
    assert any(line.startswith("OK worker spawn") for line in report.lines)
