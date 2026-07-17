"""Golden retail wire vectors for the Map Creator protocol."""

from shared.bytes import ByteReader
from shared.packet import (
    ErasePrefabAction,
    InitialUGCBatch,
    PlaceUGC,
    StateData,
    UGCBatchEntity,
)


def test_state_data_uses_native_u16_prefab_count() -> None:
    """Grassland's 373 constructs require the recovered high count byte."""

    packet = StateData()
    packet.player_id = 0
    packet.prefabs = ["A"] * 373
    raw = bytes(packet.generate())

    # With empty team names/classes, the prefab count begins at byte 56 of
    # the complete packet.  373 == 0x0175 in the native little-endian layout.
    assert raw[56:58] == b"\x75\x01"
    parsed = StateData(ByteReader(raw[1:]))
    assert parsed.prefabs == ["A"] * 373


def test_place_ugc_uses_raw_voxel_shorts() -> None:
    packet = PlaceUGC()
    packet.loop_count = 0x01020304
    packet.x, packet.y, packet.z = 511, 255, 238
    packet.ugc_item_id = 18
    packet.placing = 1

    raw = bytes(packet.generate())

    assert raw == bytes.fromhex("61 04 03 02 01 ff 01 ff 00 ee 00 12 01")
    parsed = PlaceUGC(ByteReader(raw[1:]))
    assert (
        parsed.loop_count,
        parsed.x,
        parsed.y,
        parsed.z,
        parsed.ugc_item_id,
        parsed.placing,
    ) == (0x01020304, 511, 255, 238, 18, 1)


def test_initial_ugc_batch_uses_u32_count_and_eight_byte_records() -> None:
    first = UGCBatchEntity()
    first.mode = 0
    first.x, first.y, first.z = 1, 2, 3
    first.ugc_item_id = 4
    second = UGCBatchEntity()
    second.mode = 8
    second.x, second.y, second.z = 511, 400, 238
    second.ugc_item_id = 18

    packet = InitialUGCBatch()
    packet.items = [first, second]
    raw = bytes(packet.generate())

    assert raw == bytes.fromhex(
        "62 02 00 00 00"
        " 00 01 00 02 00 03 00 04"
        " 08 ff 01 90 01 ee 00 12"
    )
    parsed = InitialUGCBatch(ByteReader(raw[1:]))
    assert [
        (item.mode, item.x, item.y, item.z, item.ugc_item_id)
        for item in parsed.items
    ] == [(0, 1, 2, 3, 4), (8, 511, 400, 238, 18)]


def test_erase_prefab_uses_compiled_proxy_layout() -> None:
    """Packet 31 alone uses fixed positions after its two u32 ranges."""

    packet = ErasePrefabAction()
    packet.loop_count = 0x01020304
    packet.prefab_name = "UGC_prefab_treeapple"
    packet.player_id = 7
    packet.prefab_yaw = 1
    packet.prefab_pitch = 2
    packet.prefab_roll = 3
    packet.from_block_index = 0x10203040
    packet.to_block_index = 0x50607080
    packet.position = (511, 400, 238)

    raw = bytes(packet.generate())
    assert raw == (
        b"\x1f\x04\x03\x02\x01UGC_prefab_treeapple\x00"
        b"\x07\x01\x02\x03\x40\x30\x20\x10\x80\x70\x60\x50"
        b"\xc0\x7f\x00\x64\x80\x3b"
    )
    parsed = ErasePrefabAction(ByteReader(raw[1:]))
    assert parsed.prefab_name == "UGC_prefab_treeapple"
    assert parsed.position == (511, 400, 238)
    assert (parsed.prefab_yaw, parsed.prefab_pitch, parsed.prefab_roll) == (1, 2, 3)
    assert (parsed.from_block_index, parsed.to_block_index) == (
        0x10203040,
        0x50607080,
    )
