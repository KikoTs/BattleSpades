"""Prove the responsiveness gate rejects the original unyielding scan."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import server.world_manager as wm

wm._scan_yield = lambda delay: None
code = pytest.main([
    "tests/test_map_load_stall.py", "-q", "-s",
    "-k", "test_background_map_load_keeps_main_thread_responsive",
])
assert code == pytest.ExitCode.TESTS_FAILED, (
    "Unyielding negative control must fail the responsiveness budget", code,
)
