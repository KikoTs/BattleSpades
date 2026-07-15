# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir definition for the portable BattleSpades runtime."""

from pathlib import Path

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

analysis = Analysis(
    [str(project_root / "run_server.py")],
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

executable = EXE(
    python_archive,
    analysis.scripts,
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

distribution = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BattleSpades",
)
