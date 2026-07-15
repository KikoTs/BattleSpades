import pytest

from protocol.runtime_packets import RuntimePlaceEntity, decode_runtime_packet


@pytest.mark.parametrize(
    ("packet_id", "payload_hex", "expected"),
    [
        # Captured from the retail client on 2026-07-11. Coordinates are
        # literal voxel shorts; the generated fixed-point loaders divide them
        # by 64 and make the server reject valid nearby placements.
        (89, "D1 50 00 00 00 53 01 A7 00 E3 00", (0, 339, 167, 227, None)),
        (1, "C6 57 00 00 B1 00 07 01 E2 00 04", (None, 177, 263, 226, 4)),
        (91, "B1 1A 01 00 01 78 00 90 01 E3 00", (1, 120, 400, 227, None)),
        (88, "43 64 00 00 00 53 00 D8 00 BC 00 00 00", (0, 83, 216, 188, None)),
    ],
)
def test_captured_deployable_packets_preserve_raw_voxel_coordinates(
    packet_id, payload_hex, expected
):
    packet = decode_runtime_packet(packet_id, bytes.fromhex(payload_hex))

    assert isinstance(packet, RuntimePlaceEntity)
    assert (packet.player_id, packet.x, packet.y, packet.z, packet.face) == expected


@pytest.mark.parametrize(
    ("packet_id", "payload_hex", "expected_yaw"),
    [
        (87, "01 00 00 00 03 64 00 C8 00 E3 00 40 00", 1.0),
        (88, "01 00 00 00 03 64 00 C8 00 E3 00 40 00", 1.0),
    ],
)
def test_turret_packets_keep_fixed_point_yaw(packet_id, payload_hex, expected_yaw):
    packet = decode_runtime_packet(packet_id, bytes.fromhex(payload_hex))

    assert isinstance(packet, RuntimePlaceEntity)
    assert (packet.x, packet.y, packet.z) == (100, 200, 227)
    assert packet.yaw == expected_yaw


@pytest.mark.parametrize("packet_id", [1, 87, 88, 89, 90, 91, 92, 97])
def test_deployable_runtime_decoder_rejects_wrong_payload_size(packet_id):
    with pytest.raises(ValueError):
        decode_runtime_packet(packet_id, b"\x00")
