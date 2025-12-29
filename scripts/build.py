#!/usr/bin/env python3
"""
Build script for Cython extensions.
Run this before starting the server:
    python scripts/build.py
"""

import subprocess
import sys
from pathlib import Path

# Ensure we're in the project root
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def build():
    """Build Cython extensions."""
    print("Building Cython extensions...")
    print("=" * 50)
    
    result = subprocess.run(
        [sys.executable, "setup.py", "build_ext", "--inplace"],
        cwd=project_root,
        capture_output=False,
    )
    
    if result.returncode == 0:
        print("=" * 50)
        print("Build successful!")
        print("\nYou can now run the server with:")
        print("    python run_server.py")
    else:
        print("=" * 50)
        print("Build failed!")
        sys.exit(1)


if __name__ == "__main__":
    build()
