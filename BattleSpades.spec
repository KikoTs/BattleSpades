# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir definition for the portable BattleSpades runtime."""

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH).resolve()

hiddenimports = [
    "enet",
    "shared.bytes",
    "shared.glm",
    "shared.packet",
    "aoslib.vxl",
    "aoslib.kv6",
    "aoslib.world",
    "server.bot_ai.recast",
]
for package_name in (
    "commands",
    "modes",
    "protocol",
    "server",
    "server.bot_ai",
):
    hiddenimports.extend(collect_submodules(package_name))

datas = []
binaries = []
for dependency_name in ("enet", "toml", "py_trees"):
    dependency_datas, dependency_binaries, dependency_imports = collect_all(
        dependency_name
    )
    datas.extend(dependency_datas)
    binaries.extend(dependency_binaries)
    hiddenimports.extend(dependency_imports)

# The legacy AoS Steamworks DLL is x86 even when the server is x86_64.  Ship
# our independently built helper, never Valve's proprietary runtime files.
if sys.platform == "win32":
    steam_bridge = (
        project_root
        / "build"
        / "steam-bridge"
        / "Release"
        / "battlespades-steam-bridge.exe"
    )
    if steam_bridge.is_file():
        # Treat the independently launched Win32 executable as opaque data;
        # PyInstaller must not reject it as a mismatched in-process binary.
        datas.append((str(steam_bridge), "steam"))

analysis = Analysis(
    [
        str(project_root / "run_server.py"),
        str(project_root / "run_tutorial.py"),
        str(project_root / "run_map_creator.py"),
    ],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
python_archive = PYZ(analysis.pure)

# One dependency analysis keeps the portable directory compact.  Runtime-hook
# scripts belong to all executables; retain exactly one public entrypoint in
# each table while preserving any PyInstaller runtime-hook scripts.
entrypoint_names = {"run_server", "run_tutorial", "run_map_creator"}


def scripts_for(entrypoint):
    return [
        item for item in analysis.scripts
        if item[0] == entrypoint or item[0] not in entrypoint_names
    ]


server_scripts = scripts_for("run_server")
tutorial_scripts = scripts_for("run_tutorial")
map_creator_scripts = scripts_for("run_map_creator")

executable = EXE(
    python_archive,
    server_scripts,
    [],
    exclude_binaries=True,
    name="BattleSpades",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory="_internal",
)

tutorial_executable = EXE(
    python_archive,
    tutorial_scripts,
    [],
    exclude_binaries=True,
    name="BattleSpadesTutorial",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory="_internal",
)

map_creator_executable = EXE(
    python_archive,
    map_creator_scripts,
    [],
    exclude_binaries=True,
    name="BattleSpadesMapCreator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory="_internal",
)

distribution = COLLECT(
    executable,
    tutorial_executable,
    map_creator_executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BattleSpades",
)
