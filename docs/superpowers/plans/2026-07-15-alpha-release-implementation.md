# BattleSpades 0.0.1 Alpha Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and validate six portable `0.0.1-alpha.1` server archives and publish them atomically from a GitHub prerelease tag.

**Architecture:** A typed runtime-path layer makes source and frozen launches independent of the caller's working directory. A side-effect-free launcher provides normal startup, `--version`, and packaged `--check`; a checked-in PyInstaller spec and staging script construct an allowlisted `onedir` payload. A native GitHub Actions matrix builds both architectures of Windows, Linux, and macOS, while a final job publishes only after all six artifacts pass smoke validation.

**Tech Stack:** Python 3.12, pytest, PyInstaller 6.x, Cython, pyenet/ENet, Recast/Detour, PowerShell/bash, GitHub Actions.

## Global Constraints

- Canonical version: `0.0.1-alpha.1`; release tag: `v0.0.1-alpha.1`.
- Build six native targets: Windows/Linux/macOS on x86_64 and arm64.
- Package with PyInstaller `onedir`; never claim one-file, signed, or notarized output.
- Bundle every tracked VXL map and KV6 prefab in every archive.
- No Python or compiler may be required on the target host.
- Do not touch gameplay, replication, stock protocol, or bot behavior.
- Do not overwrite or commit unrelated dirty-worktree changes.
- Add no further commits unless the repository owner requests them.
- Every runtime behavior change follows red-green-refactor.

---

### Task 1: Canonical Version and Runtime Paths

**Files:**
- Create: `VERSION`
- Create: `server/runtime_paths.py`
- Create: `tests/test_runtime_paths.py`
- Modify: `server/config.py`
- Modify: `server/main.py`
- Modify: `server/prefabs.py`
- Modify: `setup.py`

**Interfaces:**
- Produces: `read_version(root: Path | None = None) -> str`.
- Produces: immutable `RuntimePaths(root, config, maps, prefabs, plugins, logs, bans)`.
- Produces: `RuntimePaths.discover(...)` and `RuntimePaths.resolve_configured_path(...)`.
- Produces: `apply_runtime_paths(config: ServerConfig, paths: RuntimePaths) -> ServerConfig`.
- Produces: `configure_prefab_search_dirs(*paths: Path) -> None`.
- Consumed by: Tasks 2, 3, 4, and 5.

- [ ] **Step 1: Write failing path and version tests**

```python
def test_source_runtime_root_ignores_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = RuntimePaths.discover(source_entry=PROJECT_ROOT / "run_server.py")
    assert paths.root == PROJECT_ROOT
    assert paths.config == PROJECT_ROOT / "config.toml"


def test_frozen_runtime_root_is_executable_parent(tmp_path):
    executable = tmp_path / "release" / "BattleSpades.exe"
    paths = RuntimePaths.discover(frozen=True, executable=executable)
    assert paths.root == executable.parent


def test_relative_config_paths_are_anchored_to_runtime_root(tmp_path):
    paths = RuntimePaths.from_root(tmp_path)
    config = ServerConfig(maps_path="custom-maps")
    apply_runtime_paths(config, paths)
    assert Path(config.maps_path) == tmp_path / "custom-maps"
    assert Path(config.bans_path) == tmp_path / "bans.json"


def test_version_has_expected_prerelease_value():
    assert read_version(PROJECT_ROOT) == "0.0.1-alpha.1"
```

- [ ] **Step 2: Run the tests and verify the missing-interface failure**

Run: `py -m pytest tests/test_runtime_paths.py -q`

Expected: collection fails because `server.runtime_paths` does not exist.

- [ ] **Step 3: Implement the minimal typed runtime-path layer**

```python
@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    config: Path
    maps: Path
    prefabs: Path
    plugins: Path
    logs: Path
    bans: Path

    @classmethod
    def from_root(cls, root: Path) -> "RuntimePaths":
        resolved = root.resolve()
        return cls(
            root=resolved,
            config=resolved / "config.toml",
            maps=resolved / "maps",
            prefabs=resolved / "prefabs",
            plugins=resolved / "plugins",
            logs=resolved / "logs",
            bans=resolved / "bans.json",
        )
```

`discover()` uses `sys.frozen`/`sys.executable` only when frozen and otherwise
anchors to the supplied source entry. `read_version()` rejects missing, empty,
multi-line, or whitespace-containing versions with `RuntimePathError`.

- [ ] **Step 4: Add runtime-owned config fields and consumers**

Add `prefabs_path`, `plugins_path`, and `bans_path` to `ServerConfig`. Resolve
relative `maps_path` against the application root. Construct `BanManager` from
`config.bans_path`; configure the prefab registry from `config.prefabs_path`.
Remove the hard-coded `G:\AoSRevival` prefab fallback. Read `setup.py` metadata
from the root `VERSION` file rather than a string literal.

- [ ] **Step 5: Run focused and compatibility tests**

Run: `py -m pytest tests/test_runtime_paths.py tests/test_prefabs.py tests/test_commands.py -q`

Expected: all selected tests pass.

---

### Task 2: Side-Effect-Free Launcher and Packaged Health Check

**Files:**
- Create: `server/launcher.py`
- Create: `server/release_check.py`
- Create: `tests/test_launcher.py`
- Modify: `run_server.py`

**Interfaces:**
- Consumes: `RuntimePaths`, `read_version`, and `apply_runtime_paths` from Task 1.
- Produces: `run(argv: Sequence[str] | None = None) -> int`.
- Produces: `run_release_check(paths: RuntimePaths) -> CheckReport`.
- Produces: `CheckReport.ok`, `CheckReport.lines`, and `CheckReport.exit_code`.

- [ ] **Step 1: Write failing CLI behavior tests**

```python
def test_version_does_not_import_native_server(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "server.main", None)
    assert run(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "BattleSpades 0.0.1-alpha.1"


def test_check_returns_nonzero_when_config_is_missing(tmp_path, capsys):
    paths = RuntimePaths.from_root(tmp_path)
    assert run(["--check"], paths=paths) == 1
    assert "config.toml" in capsys.readouterr().err


def test_help_has_no_log_or_faulthandler_side_effect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run(["--help"]) == 0
    assert not (tmp_path / "logs").exists()
```

- [ ] **Step 2: Verify the new launcher tests fail for the expected reason**

Run: `py -m pytest tests/test_launcher.py -q`

Expected: collection fails because `server.launcher` does not exist.

- [ ] **Step 3: Implement argument dispatch before heavyweight imports**

```python
def run(
    argv: Sequence[str] | None = None,
    *,
    paths: RuntimePaths | None = None,
) -> int:
    multiprocessing.freeze_support()
    arguments = build_parser().parse_args(argv)
    runtime_paths = paths or RuntimePaths.discover(source_entry=ENTRYPOINT)
    if arguments.version:
        print(f"BattleSpades {read_version(runtime_paths.root)}")
        return 0
    if arguments.check:
        return emit_check_report(run_release_check(runtime_paths))
    return run_server(runtime_paths)
```

Move configuration, logging, faulthandler, native server imports, and asyncio
startup behind `run_server()`. Cleanup closes faulthandler and logging resources
in `finally` even when startup raises.

- [ ] **Step 4: Implement deterministic health checks**

Check config parsing, default map resolution, native imports, at least one VXL
and all required KV6 files, prefab registry construction, and a bounded spawn
child that echoes a token and exits. Report each check exactly once and fail
closed on timeout or import errors. The child target is module-level so Windows
`spawn` and PyInstaller can import it.

- [ ] **Step 5: Reduce `run_server.py` to the frozen-safe adapter**

```python
from server.launcher import run


if __name__ == "__main__":
    raise SystemExit(run())
```

- [ ] **Step 6: Run focused tests and source smoke commands**

Run: `py -m pytest tests/test_launcher.py tests/test_runtime_paths.py -q`

Run: `py run_server.py --version`

Expected: tests pass and the command prints `BattleSpades 0.0.1-alpha.1`.

Run: `py run_server.py --check`

Expected: all checks print `OK` and the process exits zero.

---

### Task 3: External Plugin Discovery

**Files:**
- Create: `server/plugin_loader.py`
- Create: `tests/test_plugin_loader.py`
- Modify: `server/main.py`
- Create: `release/plugins/README.txt`
- Create: `release/plugins/_example_plugin.py.disabled`

**Interfaces:**
- Consumes: `ServerConfig.plugins_path` from Task 1.
- Produces: `discover_plugin_classes(directory: Path) -> Iterator[type[BasePlugin]]`.
- Produces: `load_external_plugins(manager: PluginManager, directory: Path) -> int`.

- [ ] **Step 1: Write failing discovery and isolation tests**

```python
@pytest.mark.asyncio
async def test_external_plugin_loads_from_runtime_directory(tmp_path):
    (tmp_path / "hello.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    manager = PluginManager(FakeServer())
    assert await load_external_plugins(manager, tmp_path) == 1
    assert "Hello" in manager.plugins


@pytest.mark.asyncio
async def test_disabled_and_private_plugins_are_ignored(tmp_path):
    (tmp_path / "_private.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    (tmp_path / "sample.py.disabled").write_text(PLUGIN_SOURCE, encoding="utf-8")
    assert await load_external_plugins(PluginManager(FakeServer()), tmp_path) == 0


@pytest.mark.asyncio
async def test_broken_plugin_does_not_block_valid_neighbor(tmp_path):
    (tmp_path / "broken.py").write_text("raise RuntimeError('broken')")
    (tmp_path / "hello.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    assert await load_external_plugins(PluginManager(FakeServer()), tmp_path) == 1
```

- [ ] **Step 2: Verify plugin tests fail before implementation**

Run: `py -m pytest tests/test_plugin_loader.py -q`

Expected: collection fails because `server.plugin_loader` does not exist.

- [ ] **Step 3: Implement explicit path loading with stable unique module names**

Use `importlib.util.spec_from_file_location`, validate that the resolved file is
inside the configured plugin directory, inspect only classes defined by that
module, and delegate lifecycle/error handling to `PluginManager.load_plugin`.
Log import failures and continue. Do not add the external directory globally to
`sys.path`.

- [ ] **Step 4: Replace package scanning in `BattleSpadesServer._load_plugins`**

Call `load_external_plugins(self.plugin_manager, Path(self.config.plugins_path))`.
Keep the method and its failure-isolation contract for compatibility.

- [ ] **Step 5: Run plugin tests**

Run: `py -m pytest tests/test_plugin_loader.py tests/test_telemetry_hardening.py -q`

Expected: all selected tests pass.

---

### Task 4: Allowlisted Release Staging and PyInstaller Spec

**Files:**
- Create: `BattleSpades.spec`
- Create: `scripts/package_release.py`
- Create: `tests/test_package_release.py`
- Create: `release/README.txt`
- Create: `release/THIRD_PARTY_NOTICES.md`
- Create: `requirements-release.txt`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: root `VERSION`, PyInstaller `dist/BattleSpades`, tracked maps and prefabs.
- Produces: `ReleaseTarget(platform: str, architecture: str)`.
- Produces: `stage_release(project_root, frozen_dir, output_root, target) -> Path`.
- Produces: `archive_release(staged_dir: Path) -> tuple[Path, str]`.

- [ ] **Step 1: Write failing manifest and naming tests**

```python
def test_release_name_contains_version_platform_and_architecture():
    target = ReleaseTarget("windows", "arm64")
    assert target.directory_name == (
        "BattleSpades-0.0.1-alpha.1-windows-arm64"
    )


def test_stage_release_copies_required_content_and_rejects_stray_exes(tmp_path):
    staged = stage_release(PROJECT_ROOT, fake_frozen_tree(tmp_path), tmp_path, TARGET)
    assert (staged / "config.toml").is_file()
    assert sorted(p.name for p in (staged / "maps").glob("*.vxl")) == TRACKED_MAPS
    assert sorted(p.name for p in (staged / "prefabs").glob("*.kv6")) == TRACKED_PREFABS
    assert not (staged / "codex-command-runner.exe").exists()


def test_archive_hash_matches_written_bytes(tmp_path):
    archive, digest = archive_release(sample_release_tree(tmp_path))
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
```

- [ ] **Step 2: Verify packaging tests fail before implementation**

Run: `py -m pytest tests/test_package_release.py -q`

Expected: collection fails because `scripts.package_release` does not exist.

- [ ] **Step 3: Implement deterministic allowlisted staging**

Copy only the frozen output plus the exact mutable/documentation paths defined
in the design. Fail if there are no VXL maps, no KV6 prefabs, a required notice
is missing, or the target output already exists. Use `zipfile` with normalized
forward-slash paths and write the SHA-256 after closing the archive.

- [ ] **Step 4: Add the PyInstaller spec**

Freeze `run_server.py` as `BattleSpades`, set `contents_directory="_internal"`,
collect dynamic submodules from `commands`, `modes`, `protocol`, `server`, and
`server.bot_ai`, and collect required metadata/native binaries from `enet`,
`py_trees`, and `toml`. Do not embed config, maps, prefabs, or external plugins
inside `_internal`; the staging script adds them beside the launcher.

- [ ] **Step 5: Pin release dependencies and write notices**

Pin Python build tools and runtime dependencies in `requirements-release.txt`.
Copy the unaltered Recast/Detour Zlib notice into
`release/THIRD_PARTY_NOTICES.md` and identify other bundled dependencies and
their upstream license locations. State clearly that macOS output is unsigned
and unnotarized.

- [ ] **Step 6: Run manifest tests and build a local Windows archive**

Run: `py -m pytest tests/test_package_release.py -q`

Run: `python -m PyInstaller --noconfirm --clean BattleSpades.spec`

Run: `py scripts/package_release.py --platform windows --architecture x86_64`

Expected: one versioned zip and matching SHA-256 are produced under
`release-dist/`; no repository-only executable or trace appears in the zip.

---

### Task 5: Native GitHub Actions Release Matrix

**Files:**
- Create: `.github/workflows/release.yml`
- Create: `tests/test_release_workflow.py`

**Interfaces:**
- Consumes: `VERSION`, `requirements-release.txt`, `BattleSpades.spec`, and `scripts/package_release.py`.
- Produces: six workflow artifacts, `SHA256SUMS.txt`, and an atomic prerelease.

- [ ] **Step 1: Write failing workflow contract tests**

```python
def test_release_workflow_declares_all_native_targets():
    workflow = load_workflow(PROJECT_ROOT / ".github/workflows/release.yml")
    pairs = {(row["platform"], row["architecture"]) for row in matrix_rows(workflow)}
    assert pairs == {
        ("windows", "x86_64"), ("windows", "arm64"),
        ("linux", "x86_64"), ("linux", "arm64"),
        ("macos", "x86_64"), ("macos", "arm64"),
    }


def test_release_job_depends_on_complete_build_matrix():
    workflow = load_workflow(WORKFLOW)
    assert workflow["jobs"]["release"]["needs"] == ["build"]
    assert workflow["jobs"]["release"]["if"] == "startsWith(github.ref, 'refs/tags/v')"


def test_tag_must_match_version_file():
    assert "v$(cat VERSION)" in WORKFLOW.read_text(encoding="utf-8")
```

- [ ] **Step 2: Verify the workflow contract fails because the file is absent**

Run: `py -m pytest tests/test_release_workflow.py -q`

Expected: failure naming the missing `.github/workflows/release.yml`.

- [ ] **Step 3: Implement the six-target matrix**

Use explicit native runner labels for Windows x64/ARM64, Ubuntu x64/ARM64, and
macOS Intel/Apple Silicon. Set up Python 3.12, install release dependencies,
build extensions, run focused/full tests as appropriate, freeze, run packaged
`--version` and `--check` from outside the bundle, stage, zip, and upload one
artifact per row. Print OS/compiler/Python/machine diagnostics in every row.

- [ ] **Step 4: Implement atomic tag publication**

The release job runs only for tags, downloads all matrix artifacts, rejects a
count other than six, verifies hashes, writes `SHA256SUMS.txt`, and creates a
GitHub prerelease with all seven files. `workflow_dispatch` retains build
artifacts but never publishes. Set `contents: write` only on the release job.
Pin GitHub-owned actions to full commit SHAs.

- [ ] **Step 5: Run workflow contract tests**

Run: `py -m pytest tests/test_release_workflow.py -q`

Expected: all tests pass.

---

### Task 6: Operator and Maintainer Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/BUILDING.md`
- Create: `docs/RELEASING.md`

**Interfaces:**
- Consumes: implemented commands, filenames, workflow, and unsigned-mac policy.
- Produces: exact end-user launch and maintainer release procedures.

- [ ] **Step 1: Add the release download contract to README**

Document all six artifacts, extraction, `--check`, normal startup, UDP port,
configuration location, and the default admin password warning. Do not claim a
release is signed, notarized, or currently published before its tag exists.

- [ ] **Step 2: Add reproducible local packaging commands**

Document Python 3.12, native compiler prerequisites, release dependency install,
extension build, PyInstaller invocation, staging command, and packaged smoke
commands for Windows and POSIX shells.

- [ ] **Step 3: Add the maintainer release runbook**

Document version changes, clean-worktree review, manual workflow build, artifact
inspection, annotated tag creation, atomic-release behavior, checksums, GitHub
prerelease verification, rollback/deletion, and later signing entry points.

- [ ] **Step 4: Run documentation and full verification**

Run: `rg -n "0\.1\.0|TBD|TODO" VERSION setup.py README.md docs/BUILDING.md docs/RELEASING.md release`

Expected: no stale package version or placeholders in release-facing files.

Run: `py -m pytest tests/test_runtime_paths.py tests/test_launcher.py tests/test_plugin_loader.py tests/test_package_release.py tests/test_release_workflow.py -q`

Expected: all release tests pass.

Run: `py -m pytest tests -q`

Expected: the complete available suite passes; pre-existing environment skips
are reported separately and no release-related failure remains.

Run from a directory outside the extracted Windows archive:

```powershell
& "<archive>\BattleSpades.exe" --version
& "<archive>\BattleSpades.exe" --check
```

Expected: version output is exact, every health check is `OK`, exit status is
zero, and Task Manager shows no orphaned BattleSpades child process.
