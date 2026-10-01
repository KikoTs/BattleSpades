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
    "server_gui",
):
    hiddenimports.extend(collect_submodules(package_name))

datas = []
binaries = []
# Stamp once during the actual freeze, never when a client joins. Reproducible
# builders may supply SOURCE_DATE_EPOCH; the JSON remains in the onedir bundle.
sys.path.insert(0, str(project_root))
from server.build_info import write_build_info
build_info_path = write_build_info(project_root / "build" / "metadata" / "build_info.json")
datas.append((str(build_info_path), "."))
# The desktop host (BattleSpadesServer) uses CustomTkinter on the Tk that
# ships with Python, plus tomlkit for comment-preserving config edits.
for dependency_name in ("enet", "toml", "py_trees", "tomlkit", "customtkinter", "darkdetect"):
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

    # Optional modern transport for the retail drop-in. Both files stay in a
    # child-process directory; neither replaces a retail/legacy Steam DLL.
    retail_relay = project_root / "out" / "retail-mousefix" / "relay"
    relay_files = [retail_relay / name for name in ("aos-retail-relay.exe", "steam_api64.dll")]
    if all(path.is_file() for path in relay_files):
        datas.extend((str(path), "relay") for path in relay_files)

# Desktop host assets: our own icon and the OFL heading font with its licence.
for gui_asset in ("assets", "fonts"):
    for asset in sorted((project_root / "server_gui" / gui_asset).glob("*")):
        if asset.is_file():
            datas.append((str(asset), f"server_gui/{gui_asset}"))

analysis = Analysis(
    [
        str(project_root / "run_server.py"),
        str(project_root / "run_tutorial.py"),
        str(project_root / "run_map_creator.py"),
        str(project_root / "run_server_gui.py"),
    ],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # CustomTkinter's ``packaging`` dependency would otherwise drag in the
    # build-time setuptools/pkg_resources and their deprecation warning.
    excludes=["setuptools", "pkg_resources"],
    noarchive=False,
    optimize=0,
)
python_archive = PYZ(analysis.pure)

# One dependency analysis keeps the portable directory compact.  Runtime-hook
# scripts belong to all executables; retain exactly one public entrypoint in
# each table while preserving any PyInstaller runtime-hook scripts.
entrypoint_names = {"run_server", "run_tutorial", "run_map_creator", "run_server_gui"}


def scripts_for(entrypoint):
    return [
        item for item in analysis.scripts
        if item[0] == entrypoint or item[0] not in entrypoint_names
    ]


server_scripts = scripts_for("run_server")
tutorial_scripts = scripts_for("run_tutorial")
map_creator_scripts = scripts_for("run_map_creator")
server_gui_scripts = scripts_for("run_server_gui")

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

# Windowed desktop host: starts BattleSpades(.exe) as a child process and
# shares the same operator configuration and runtime.
gui_icon = project_root / "server_gui" / "assets" / "battlespades-server.ico"
server_gui_executable = EXE(
    python_archive,
    server_gui_scripts,
    [],
    exclude_binaries=True,
    name="BattleSpadesServer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(gui_icon) if sys.platform == "win32" and gui_icon.is_file() else None,
    contents_directory="_internal",
)

distribution = COLLECT(
    executable,
    tutorial_executable,
    map_creator_executable,
    server_gui_executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BattleSpades",
)
