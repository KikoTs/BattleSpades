# Building BattleSpades

BattleSpades compiles its Cython gameplay extensions, Recast/Detour wrapper,
and vendored **pyenet/ENet** networking binding from source. Every target needs
a C/C++ toolchain and Python development headers.

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

This installs the Python dependencies and then runs
`python setup.py build_ext --inplace`, which builds every native module,
including the vendored ENet transport.

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
py -m pytest tests/ -q          # complete suite must pass
py scripts/replay_parity.py     # movement parity — must print ALL PASS
python run_server.py            # boots on port 27015
```

## Multi-platform / cross-compilation

The release matrix covers **Windows, Linux, and macOS** on **x86_64 and arm64**.
Each GitHub-hosted runner compiles natively for its declared architecture. The
repository vendors pyenet 1.3.17 and ENet 1.3.17, so no target depends on PyPI
having a compatible prebuilt `pyenet` wheel or source-build environment.

Do not cross-label an artifact built on another architecture. Use the native
runner from `.github/workflows/release.yml`, or reproduce that toolchain on the
actual target architecture.

### Producing wheels

To build a redistributable wheel of the Cython extensions for a target:

```bash
pip install build
python -m build --wheel
```

The resulting wheel contains the locally compiled `enet` extension; a separate
PyPI `pyenet` installation is not required at runtime.
