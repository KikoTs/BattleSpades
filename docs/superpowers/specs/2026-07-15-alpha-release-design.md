# BattleSpades 0.0.1 Alpha Release Design

## Status

Approved on 2026-07-15. The first public release is an unsigned prerelease,
published as `0.0.1-alpha.1`. The tracked VXL maps and KV6 prefabs are included
in every archive with the project owner's approval.

## Goal

Produce portable BattleSpades server downloads for every modern operating
system and CPU combination supported by the project:

| Operating system | Architectures |
| --- | --- |
| Windows | x86_64, arm64 |
| Linux | x86_64, arm64 |
| macOS | x86_64, arm64 |

Each download must start without a separately installed Python runtime or C/C++
toolchain. The same archive also contains the editable server configuration,
maps, prefabs, plugin directory, license notices, and concise operator
instructions.

32-bit x86, ARM32, musl Linux, and other legacy or specialist targets are not
part of `0.0.1-alpha.1`. Supporting them requires separate dependency and
runtime validation rather than relabeling one of the six native artifacts.

## Selected Packaging Approach

Use PyInstaller in `onedir` mode and zip the resulting directory. A one-directory
build still gives the operator one launcher and requires no system Python. It is
preferred to PyInstaller `onefile` because BattleSpades contains multiple native
extensions and starts a multiprocessing bot worker. Avoiding per-launch
temporary extraction makes startup, worker spawning, diagnostics, and failed
launches easier to reason about during the alpha.

Rejected alternatives:

- **PyInstaller one-file:** visually simpler, but it extracts the runtime before
  every launch and increases frozen multiprocessing and native-library risk.
- **Wheel or source archive:** appropriate for developers, but requires Python,
  a compiler, and platform-specific ENet/Cython build knowledge from operators.
- **Container-only Linux release:** useful later, but does not replace native
  Windows and macOS server downloads.

## Version and Artifact Contract

The canonical version is stored in a root `VERSION` file and contains
`0.0.1-alpha.1`. Packaging metadata, `BattleSpades --version`, workflow tag
validation, archive names, and release metadata read this one value.

The release tag is `v0.0.1-alpha.1`, and the GitHub release is marked as a
prerelease. The six assets are:

```text
BattleSpades-0.0.1-alpha.1-windows-x86_64.zip
BattleSpades-0.0.1-alpha.1-windows-arm64.zip
BattleSpades-0.0.1-alpha.1-linux-x86_64.zip
BattleSpades-0.0.1-alpha.1-linux-arm64.zip
BattleSpades-0.0.1-alpha.1-macos-x86_64.zip
BattleSpades-0.0.1-alpha.1-macos-arm64.zip
```

The release also contains `SHA256SUMS.txt`. A release is atomic: if any target
fails to build or pass its packaged smoke test, no partial GitHub release is
published.

## Distribution Layout

Each zip expands into exactly one versioned directory:

```text
BattleSpades-0.0.1-alpha.1-<platform>-<architecture>/
|-- BattleSpades[.exe]
|-- _internal/                 PyInstaller runtime and native libraries
|-- config.toml                Editable, ready-to-run configuration
|-- maps/                      All tracked VXL maps and metadata sidecars
|-- prefabs/                   All tracked KV6 class and mode prefabs
|-- plugins/                   External plugins, instructions, disabled example
|-- LICENSE
|-- THIRD_PARTY_NOTICES.md
|-- README.txt                 Short operator quick start
`-- VERSION
```

`logs/` and `bans.json` are runtime state and are created on first use. Build
trees, caches, tests, reverse-engineering material, traces, crash dumps, local
configuration overrides, and unrelated executables are never copied into the
release. In particular, root development helpers such as
`codex-command-runner.exe` and `codex-windows-sandbox-setup.exe` are excluded by
an allowlist-based manifest rather than filename filtering.

The shipped `config.toml` remains editable and ready to run. `README.txt` warns
operators to replace the default `admin_password = "changeme"` before exposing
the server publicly. Upgrading is performed by extracting a new version and
copying intentional configuration, maps, plugins, and bans into it; the release
pipeline never attempts to overwrite an existing installation.

## Runtime Path Model

Introduce one small runtime-path component responsible for application-owned
files. It resolves the application root as:

- `Path(sys.executable).resolve().parent` in a frozen build;
- the repository root containing `run_server.py` in a source build.

The launcher passes or derives explicit absolute paths for:

- `config.toml`;
- `logs/` and the configured log file;
- `bans.json`;
- relative `maps_path` values;
- the bundled `prefabs/` registry;
- the external `plugins/` directory.

The launcher does not depend on the caller's current working directory and does
not globally change it. Absolute paths in configuration remain absolute;
relative paths are anchored to the application root. This preserves portable
zip behavior while allowing deliberate external map directories.

## Frozen Entrypoint and Worker Safety

`run_server.py` becomes a small command-line entrypoint with these stable
operations:

- no argument: start the server normally;
- `--version`: print the canonical version and exit successfully;
- `--check`: validate the packaged runtime and exit without opening a public
  gameplay listener.

`multiprocessing.freeze_support()` executes before server imports that may start
worker infrastructure. This is required so a frozen child process enters the
worker bootstrap instead of recursively starting another server.

`--check` verifies:

1. the version and application root;
2. configuration parsing and the default map reference;
3. imports of ENet and every BattleSpades Cython/native extension;
4. presence and readability of the bundled maps and prefabs;
5. construction of the prefab registry;
6. a bounded frozen multiprocessing child handshake followed by clean shutdown.

It returns a nonzero status and a concise actionable message on failure. It does
not modify gameplay state, create a public socket, or run indefinitely.

## Plugin Behavior

The frozen application keeps `BasePlugin` and `PluginManager` as bundled server
code. User plugins live beside the executable in `plugins/` and are loaded from
explicit file paths. Discovery remains bounded to top-level Python files whose
names do not begin with `_`.

The example plugin ships disabled, so a fresh server does not unexpectedly run
sample gameplay logic. Plugin import or initialization failures are logged and
do not prevent the server from starting, matching the current fault-isolation
contract. Operator documentation states that plugins execute trusted arbitrary
Python code inside the server process.

## PyInstaller Specification

A checked-in PyInstaller spec file is the only definition of frozen imports and
data. It includes:

- all Python server, command, protocol, mode, and bot-worker modules needed by
  dynamic imports;
- `pyenet` and its native library;
- the seven BattleSpades Cython/native extensions;
- Recast/Detour code reached by the bot worker;
- runtime metadata required by `toml` and `py_trees`.

Mutable operator content is staged outside `_internal` after PyInstaller builds
the application. Maps, prefabs, configuration, and external plugins therefore
remain visible and editable rather than being hidden in PyInstaller's runtime
area.

## Native Build Matrix

Every target builds natively with Python 3.12. Cross-compiling one artifact and
renaming it for another architecture is forbidden because ENet, Cython, and
Recast/Detour all contain target-native code.

The initial GitHub-hosted runner matrix is:

| Artifact | Runner family |
| --- | --- |
| Windows x86_64 | Windows x64 |
| Windows arm64 | Windows ARM64 |
| Linux x86_64 | Ubuntu x64 |
| Linux arm64 | Ubuntu ARM64 |
| macOS x86_64 | macOS Intel |
| macOS arm64 | macOS Apple Silicon |

Runner labels are explicit instead of `*-latest` where GitHub provides a stable
versioned label. The job records the OS version, Python version, compiler, and
machine architecture in its log and verifies the frozen executable reports the
expected architecture.

## GitHub Actions Flow

One release workflow supports two entry paths:

- `workflow_dispatch`: build and retain the six workflow artifacts without
  creating a GitHub release;
- a pushed `v*-alpha.*` tag: validate that the tag equals `v` plus `VERSION`,
  build all targets, and publish the prerelease after every target succeeds.

Each matrix job performs:

1. checkout with submodules disabled;
2. Python 3.12 setup and dependency-cache restoration;
3. dependency installation from a release lock file;
4. native extension compilation;
5. the relevant source test gate;
6. PyInstaller `onedir` construction;
7. allowlist-based payload staging;
8. packaged `--version` and `--check` execution;
9. archive creation and per-archive SHA-256 generation;
10. workflow artifact upload.

The final release job downloads all six artifacts, verifies their names and
checksums, creates `SHA256SUMS.txt`, and uses the repository token to create the
GitHub prerelease and attach the assets. GitHub-owned actions are pinned to full
commit SHAs. A failure in build, tests, smoke validation, or manifest validation
prevents publication.

## Dependency Reproducibility and Notices

The current broad development requirements are not sufficient for reproducible
release binaries. Add a release constraint/lock file that pins PyInstaller,
PyInstaller hooks, Cython, pyenet, toml, py_trees, setuptools, and wheel to
versions exercised by the matrix. Development requirements may remain broader.

`THIRD_PARTY_NOTICES.md` identifies the bundled Python runtime, PyInstaller,
ENet/pyenet, Cython-generated runtime portions where applicable, py_trees,
toml, and Recast/Detour. The vendored Recast/Detour Zlib notice is reproduced
without alteration. BattleSpades' own `LICENSE` is included separately.

## macOS Signing Decision

`0.0.1-alpha.1` ships without an Apple Developer ID certificate and without
notarization. No signing secrets are required by the workflow. Where the build
tool applies ad-hoc signatures to Mach-O files, those signatures are preserved,
but the release is explicitly documented as unnotarized and may trigger a
Gatekeeper warning.

The spec and workflow keep signing as a later post-build stage so Developer ID
signing, hardened runtime validation, notarization, and stapling can be added
without changing the archive contract. The alpha must not claim to be signed or
notarized.

## Tests and Acceptance Gates

Implementation follows test-driven development. Before path or entrypoint code
changes, tests cover source and simulated-frozen root selection, relative and
absolute configured paths, non-repository working directories, version parsing,
configuration failures, missing assets, and child-process cleanup.

Release-specific acceptance gates are:

- the normal source test suite passes on the primary platform;
- native import and focused packaging tests pass on every matrix target;
- both `BattleSpades --version` and `BattleSpades --check` pass from a working
  directory outside the extracted archive;
- the archive manifest contains every tracked map and prefab and only approved
  top-level payload paths;
- the reported executable architecture matches the artifact name;
- the check leaves no worker process running;
- no target-specific build output is reused by a different matrix target;
- checksum verification succeeds after the final job downloads the archives;
- GitHub publishes either all six archives or none.

After CI exists, one Windows x86_64 archive is also launched locally and allowed
to bind its configured port, load the default map, start and stop the bot worker,
and shut down cleanly. Equivalent packaged checks run natively in CI for the
other targets; physical retail-client gameplay remains a separate protocol
validation concern rather than a packaging gate.

## Documentation Deliverables

The implementation adds or updates:

- `README.md`: downloadable-release quick start and supported artifact matrix;
- `docs/BUILDING.md`: reproducible local packaging commands;
- `docs/RELEASING.md`: version bump, tag, workflow, checksum, and rollback
  procedure;
- release `README.txt`: extraction, startup, firewall/UDP port, configuration,
  admin password, plugin trust, logs, bans, and macOS Gatekeeper guidance;
- `THIRD_PARTY_NOTICES.md`: bundled dependency attribution.

## Out of Scope for the First Alpha

- Apple Developer ID signing and notarization;
- Windows Authenticode signing;
- Linux packages such as deb, rpm, AppImage, or containers;
- auto-update or in-place upgrade logic;
- a graphical launcher or service installer;
- 32-bit and musl targets;
- changing gameplay, replication, bot behavior, or the stock network protocol.
