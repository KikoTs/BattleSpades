"""Bot construction schematics: authored structures, block-line plans, sites.

See docs/BOT_SCHEMATICS_2026-10-01.md for the format and the request API.
"""

from .library import by_tag, get, library, make_bridge, make_ring, make_stair
from .model import PALETTE, Placement, Schematic, fit, rotation_for_facing, structure_placement
from .planner import (
    MAX_LINE_CELLS, PLAN_REACH, BuildPlan, BuildStep, VoxelView, find_stand,
    plan_build, validate_plan,
)

__all__ = [
    "BuildPlan", "BuildStep", "MAX_LINE_CELLS", "PALETTE", "PLAN_REACH", "Placement",
    "Schematic", "VoxelView", "by_tag", "find_stand", "fit", "get", "library",
    "make_bridge", "make_ring", "make_stair", "plan_build", "rotation_for_facing",
    "structure_placement", "validate_plan",
]
