"""
Cython build configuration for BattleSpades aoslib.
"""

from setuptools import setup, Extension
from Cython.Build import cythonize
import numpy as np
import sys
import os

# Compiler settings
extra_compile_args = []
extra_link_args = []

if sys.platform == 'win32':
    extra_compile_args = ['/O2']
else:
    extra_compile_args = ['-O3', '-ffast-math']
    extra_link_args = ['-O3']

# Define extensions
extensions = [
    Extension(
        "aoslib.bytes",
        ["aoslib/bytes.pyx"],
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
    ),
    Extension(
        "aoslib.glm",
        ["aoslib/glm.pyx"],
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
    ),
    Extension(
        "aoslib.vxl",
        ["aoslib/vxl.pyx"],
        include_dirs=[np.get_include()],
        language="c++",
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
    ),
    Extension(
        "aoslib.packet",
        ["aoslib/packet.pyx"],
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
    ),
]

setup(
    name="BattleSpades",
    version="0.1.0",
    description="Ace of Spades Battle Builders Server",
    author="AoS Revival",
    packages=["aoslib", "server", "protocol", "modes", "commands", "plugins"],
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            'language_level': '3',
            'boundscheck': False,
            'wraparound': False,
            'cdivision': True,
        },
        annotate=True,
    ),
    python_requires=">=3.8",
    install_requires=[
        "numpy",
        "Cython",
        "toml",
    ],
)
