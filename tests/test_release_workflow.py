"""Static contract tests for the native GitHub release workflow."""

from pathlib import Path
import re
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
RELEASE_REQUIREMENTS = PROJECT_ROOT / "requirements-release.txt"
DEVELOPMENT_REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
SETUP = PROJECT_ROOT / "setup.py"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_release_workflow_declares_all_native_targets() -> None:
    """The matrix covers both modern architectures on all three systems."""

    rows = set(
        re.findall(
            r"platform:\s*(windows|linux|macos)\s*\n"
            r"\s+architecture:\s*(x86_64|arm64)",
            _workflow_text(),
        )
    )

    assert rows == {
        ("windows", "x86_64"),
        ("windows", "arm64"),
        ("linux", "x86_64"),
        ("linux", "arm64"),
        ("macos", "x86_64"),
        ("macos", "arm64"),
    }


def test_release_job_requires_complete_build_matrix() -> None:
    """Publishing is downstream of the complete matrix and tag-only."""

    text = _workflow_text()

    assert "release:\n    name: Publish prerelease\n    needs: build" in text
    assert "startsWith(github.ref, 'refs/tags/v')" in text
    assert "scripts/verify_release_assets.py" in text


def test_release_title_does_not_import_server_runtime(tmp_path: Path) -> None:
    """The publish-only runner has neither game dependencies nor extensions."""
    command = re.search(r"python -c '([^']+)'", _workflow_text())
    assert command is not None
    package = tmp_path / "server"
    package.mkdir()
    (package / "__init__.py").write_text(
        'raise AssertionError("Publishing must not initialize the server runtime")\n',
        encoding="utf-8",
    )
    (package / "build_info.py").write_bytes(
        (PROJECT_ROOT / "server" / "build_info.py").read_bytes()
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", command.group(1)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "BattleSpades Beta 0.3"


def test_tag_is_validated_against_version_file() -> None:
    """A mistyped tag cannot publish assets with conflicting versions."""

    text = _workflow_text()

    assert "VERSION" in text
    assert re.search(r"expected_tag\s*=\s*f['\"]v\{version\}['\"]", text)
    assert "GITHUB_REF_NAME" in text


def test_github_owned_actions_are_pinned_to_full_shas() -> None:
    """Mutable major-version action tags are not trusted in release jobs."""

    action_refs = re.findall(r"uses:\s*(actions/[^@\s]+)@([^\s#]+)", _workflow_text())

    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", reference) for _name, reference in action_refs)


def test_freezer_uses_the_selected_python_interpreter() -> None:
    """PyInstaller must share the interpreter that received dependencies."""

    text = _workflow_text()

    assert "python -m PyInstaller --noconfirm --clean BattleSpades.spec" in text
    assert "run: pyinstaller " not in text.lower()


def test_enet_is_built_from_pinned_vendored_sources() -> None:
    """All six targets avoid pyenet's conflicting/incomplete source package."""

    for requirements_file in (RELEASE_REQUIREMENTS, DEVELOPMENT_REQUIREMENTS):
        requirements = requirements_file.read_text(encoding="utf-8").splitlines()
        assert not any(line.strip().lower().startswith("pyenet") for line in requirements)

    setup = SETUP.read_text(encoding="utf-8")
    assert 'Extension(\n        "enet"' in setup
    assert '"vendor/pyenet/enet.pyx"' in setup
    assert 'glob("vendor/pyenet/enet/*.c")' in setup
    assert (PROJECT_ROOT / "vendor" / "pyenet" / "LICENSE").is_file()
    assert (PROJECT_ROOT / "vendor" / "pyenet" / "enet" / "LICENSE").is_file()
