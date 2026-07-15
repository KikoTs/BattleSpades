# Building BattleSpades

BattleSpades has two compiled pieces: its own **Cython extensions** and the
**pyenet** networking binding. Both build from source, so every target needs a
C toolchain and Python development headers.

## Prerequisites

| Platform | Install |
|---|---|
| **Windows x64** | [Visual Studio Build Tools](https://visualstudio.microsoft.com/downloads/) → *Desktop development with C++* (MSVC + Windows 10/11 SDK). Python from python.org. |
| **Debian / Ubuntu** | `sudo apt install build-essential python3-dev python3-pip` |
| **Fedora / RHEL** | `sudo dnf install gcc python3-devel python3-pip` |
| **Alpine** | `apk add build-base python3-dev py3-pip` |
| **macOS** | `xcode-select --install` |

Python **3.12** is the pinned release-build runtime.

## The easy path

```bash
# Linux / macOS
./scripts/install.sh

# Windows (PowerShell)
.\scripts\install.ps1
```

This runs `pip install -r requirements.txt` (which compiles `pyenet`) and then
`python setup.py build_ext --inplace` (which compiles the Cython core).

## Manual build

```bash
pip install -r requirements.txt
python setup.py build_ext --inplace      # or: python scripts/build.py
```

Re-run `build_ext` after editing any `.pyx`/`.pxd`. **Stop the server first** —
a running process locks the compiled `.pyd`/`.so`.

## Portable release build

Install the pinned release toolchain, compile the native modules, freeze the
one-directory launcher, and stage the archive:

```bash
python -m pip install -r requirements-release.txt
python setup.py build_ext --inplace
python -m pytest tests -q
python -m PyInstaller --noconfirm --clean BattleSpades.spec
python scripts/package_release.py --platform linux --architecture x86_64
```

Use `windows`, `linux`, or `macos` for `--platform` and `x86_64` or `arm64`
for `--architecture`. The labels must match the machine that compiled the
native modules. The command writes a versioned directory and zip under
`release-dist/`.

Validate the staged launcher from a working directory outside the release:

```bash
/absolute/path/to/release-dist/BattleSpades-*/BattleSpades --version
/absolute/path/to/release-dist/BattleSpades-*/BattleSpades --check
```

On Windows use `BattleSpades.exe`. `--check` validates configuration, maps,
prefabs, ENet/Cython/Recast imports, and a real spawned worker without opening
the gameplay listener.

## Verifying the build

```bash
py -m pytest tests/ -q          # 87 tests should pass
py scripts/replay_parity.py     # movement parity — must print ALL PASS
python run_server.py            # boots on port 27015
```

## Multi-platform / cross-compilation

The release matrix covers **Windows, Linux, and macOS** on **x86_64 and arm64**.
The Cython extensions cross-compile cleanly with the usual `setup.py` flags; the
friction is entirely **ENet**.

### The ENet problem

`pyenet` bundles the ENet C source and builds it per target. On mainstream
`x86_64` Linux/Windows with a wheel or a local compiler this is painless. For
other targets you may have to build ENet + pyenet for that architecture:

- **arm64 Linux** (e.g. a Raspberry Pi or an ARM VPS): install the toolchain
  (`build-essential python3-dev`) and let `pip install pyenet` compile natively
  *on the target*, or use a matching manylinux/ARM build environment
  (e.g. `cibuildwheel`, or a QEMU-backed container) to produce a wheel.
- **Static / portable builds**: because ENet is a native C dependency, a fully
  static single-file distribution requires bundling the compiled binding for
  each OS/arch combination.

This per-architecture ENet dance is exactly why the [roadmap](ROADMAP.md) calls
for replacing the C `pyenet` dependency with a **native Go ENet** implementation
— that would collapse "build ENet three times" into one portable binary per
platform.

### Producing wheels

To build a redistributable wheel of the Cython extensions for a target:

```bash
pip install build
python -m build --wheel
```

(You still need `pyenet` available for that platform at runtime.)
