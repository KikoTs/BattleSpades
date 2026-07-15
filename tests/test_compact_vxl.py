"""Collision-only worker VXL parity tests."""

from server.bot_ai.compact_vxl import CompactVoxelMap
from server.runtime_vxl import ServerVXL


def test_compact_decoder_matches_native_implicit_underground_column() -> None:
    # One 1x1 source column centered in the 512 map. Byte 3 references z=239
    # to suppress legacy vertical shifting; the final top run at z=62 implies
    # solid underground through the fixed floor.
    raw = bytes((0, 62, 62, 239)) + (0x80706050).to_bytes(4, "little")
    compact = CompactVoxelMap(raw)
    native = ServerVXL(1, raw, len(raw), 3)

    for z in (0, 61, 62, 100, 238, 239):
        assert compact.get_solid(255, 255, z) == native.get_solid(255, 255, z)
    assert compact.get_solid(254, 255, 62) == native.get_solid(254, 255, 62)


def test_compact_live_delta_changes_only_requested_cell() -> None:
    raw = bytes((0, 62, 62, 239)) + (0x80706050).to_bytes(4, "little")
    compact = CompactVoxelMap(raw)

    compact.remove_point_nochecks(255, 255, 80)
    compact.set_point(254, 255, 40, 0x80112233)

    assert compact.get_solid(255, 255, 80) is False
    assert compact.get_solid(255, 255, 81) is True
    assert compact.get_solid(254, 255, 40) is True
