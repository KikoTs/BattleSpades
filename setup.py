import sys
from glob import glob
from pathlib import Path

from Cython.Build import cythonize
from setuptools import Extension, setup


PROJECT_ROOT = Path(__file__).resolve().parent
PACKAGE_VERSION = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()


extra_compile_args = []
extra_link_args = []

if sys.platform == "win32":
    # VS 18 can ICE while LTCG-linking the generated shared/packet.c. Keep
    # optimization but disable whole-program code generation for extensions.
    extra_compile_args = ["/O2", "/GL-"]
    extra_link_args = ["/MANIFEST:NO"]
else:
    extra_compile_args = ["-O3", "-ffast-math"]
    extra_link_args = ["-O3"]


extensions = [
    Extension(
        "shared.bytes",
        ["shared/bytes.pyx"],
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
    ),
    Extension(
        "shared.glm",
        ["shared/glm.pyx"],
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
    ),
    Extension(
        "shared.packet",
        ["shared/packet.pyx"],
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
    ),
    Extension(
        "aoslib.vxl",
        ["aoslib/vxl.pyx"],
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
    ),
    Extension(
        "aoslib.kv6",
        ["aoslib/kv6.pyx"],
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
    ),
    Extension(
        "aoslib.world",
        ["aoslib/world.pyx"],
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
    ),
    Extension(
        "server.bot_ai.recast",
        [
            "server/bot_ai/recast.pyx",
            "server/bot_ai/recast_bridge.cpp",
            *glob("vendor/recastnavigation/Recast/Source/*.cpp"),
            *glob("vendor/recastnavigation/Detour/Source/*.cpp"),
            *glob("vendor/recastnavigation/DetourCrowd/Source/*.cpp"),
        ],
        include_dirs=[
            "server/bot_ai",
            "vendor/recastnavigation/Recast/Include",
            "vendor/recastnavigation/Detour/Include",
            "vendor/recastnavigation/DetourCrowd/Include",
        ],
        language="c++",
        define_macros=[("RC_DISABLE_ASSERTS", "1")],
        extra_compile_args=(
            extra_compile_args + (["/EHsc", "/std:c++17"] if sys.platform == "win32" else ["-std=c++17"])
        ),
        extra_link_args=extra_link_args,
    ),
]


setup(
    name="BattleSpades",
    version=PACKAGE_VERSION,
    description="Ace of Spades Battle Builders Server",
    author="AoS Revival",
    packages=[
        "shared", "aoslib", "server", "server.bot_ai", "protocol",
        "modes", "commands", "plugins",
    ],
    ext_modules=cythonize(
        extensions,
        force=True,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
        },
        annotate=True,
    ),
    python_requires=">=3.8",
    install_requires=["Cython", "toml", "py_trees>=2.5,<2.6"],
)
