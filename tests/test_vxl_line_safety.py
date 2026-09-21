"""Keep unchecked Cython indexing failures isolated from the pytest process."""

import subprocess
import sys
from pathlib import Path


def test_native_block_line_survives_repeated_forward_reverse_and_diagonal_calls() -> None:
    code = """
from aoslib.vxl import VXL
world = VXL(None, b'', 0)
cases = (
    ((1, 1, 20, 4, 1, 20), [(1, 1, 20), (2, 1, 20), (3, 1, 20), (4, 1, 20)]),
    ((4, 1, 20, 1, 1, 20), [(4, 1, 20), (3, 1, 20), (2, 1, 20), (1, 1, 20)]),
    ((1, 1, 20, 3, 3, 22), [(1, 1, 20), (2, 2, 21), (3, 3, 22)]),
    ((0, 511, 238, 0, 511, 238), [(0, 511, 238)]),
)
for _ in range(2000):
    for arguments, expected in cases:
        assert world.block_line(*arguments) == expected
print('8000 native lines passed')
"""
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", code],
        cwd=Path(__file__).resolve().parents[1], capture_output=True,
        text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "8000 native lines passed" in result.stdout
