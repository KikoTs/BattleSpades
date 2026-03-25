import sys

from Cython.Build import cythonize
from setuptools import Extension, setup


extra_compile_args = []
extra_link_args = []

if sys.platform == "win32":
    extra_compile_args = ["/O2"]
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
]


setup(
    name="BattleSpades",
    version="0.1.0",
    description="Ace of Spades Battle Builders Server",
    author="AoS Revival",
    packages=["shared", "aoslib", "server", "protocol", "modes", "commands", "plugins"],
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
    install_requires=["Cython", "toml"],
)
