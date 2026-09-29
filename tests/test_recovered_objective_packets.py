"""Golden wire vectors for objective packets recovered from the retail client.

These are not self-roundtrip tests: every expected byte string was generated
by the clean 32-bit Python 2 ``shared/packet.pyd`` shipped with the game.  That
keeps a matching bug in this repository's writer and reader from blessing
itself and protects the native client from malformed server-only packets.
"""

from shared.packet import (
    HelpMessage,
    LockToZone,
    MinimapZoneClear,
    ProgressBar,
    TeamProgress,
    TerritoryBaseState,
)


def _set(packet, **values):
    for name, value in values.items():
        setattr(packet, name, value)
    return packet


def test_minimap_zone_clear_matches_retail_python2_vector():
    packet = _set(
        MinimapZoneClear(),
        A2018=1,
        A2020=2,
        A2022=3,
        A2019=4,
        A2021=5,
        A2023=6,
    )
    assert bytes(packet.generate()).hex() == "2c010002000300040005000600"


def test_lock_to_zone_matches_retail_python2_vector():
    packet = _set(
        LockToZone(),
        A2018=1,
        A2020=2,
        A2022=3,
        A2019=4,
        A2021=5,
        A2023=6,
    )
    assert bytes(packet.generate()).hex() == "6c010002000300040005000600"


def test_territory_base_state_matches_retail_python2_vector():
    packet = _set(
        TerritoryBaseState(),
        base_index=4,
        action=2,
        controlled_by=1,
        attacked_by=3,
        capture_amount=0.625,
    )
    assert bytes(packet.generate()).hex() == "6a040201032800"


def test_territory_base_state_detail_free_actions_are_three_bytes():
    """ENTERING/LEAVING/CONTENDED/UNCONTENDED carry no owner/attacker/amount.

    The retail reader stops after base_index + action for the actions in
    constants.TC_DETAIL_NOT_REQUIRED. The old always-long writer left four
    trailing bytes that the client parsed as the next packet in the same
    datagram: ``03`` -> EntityUpdates -> NoDataLeft, killing the client
    whenever a Territory Control base became contested.
    """
    from shared import constants as C
    from shared.bytes import ByteReader

    assert sorted(C.TC_DETAIL_NOT_REQUIRED) == [3, 4, 6, 7]
    for action in C.TC_DETAIL_NOT_REQUIRED:
        packet = _set(
            TerritoryBaseState(),
            base_index=2,
            action=action,
            controlled_by=3,
            attacked_by=3,
            capture_amount=1.0,
        )
        data = bytes(packet.generate())
        assert data.hex() == "6a02%02x" % action
        reader = ByteReader(data[1:])
        decoded = TerritoryBaseState(reader)
        assert (decoded.base_index, decoded.action) == (2, action)
        assert reader.data_left() == 0
    # The crash vector captured live (base C contested): exactly 3 bytes now.
    contested = _set(
        TerritoryBaseState(),
        base_index=2,
        action=int(C.TC_BASE_CONTENDED),
        controlled_by=3,
        attacked_by=3,
        capture_amount=1.0,
    )
    assert bytes(contested.generate()) == bytes.fromhex("6a0206")
    # Detail actions keep the full 7-byte form.
    for action in (0, 1, 2, 5):
        packet = _set(TerritoryBaseState(), base_index=1, action=action)
        assert len(bytes(packet.generate())) == 7


def test_help_message_matches_retail_big_endian_delay_vector():
    packet = _set(
        HelpMessage(),
        delay=1.5,
        message_ids=["TEST_KEY", "SECOND"],
    )
    assert bytes(packet.generate()).hex() == (
        "6d3fc0000002544553545f4b4559005345434f4e4400"
    )


def test_team_progress_matches_retail_flag_and_percent_vector():
    packet = _set(
        TeamProgress(),
        team_id=1,
        visible=1,
        show_particle=1,
        show_previous=0,
        show_as_percent=1,
        percent=0.625,
        icon_id=4,
    )
    assert bytes(packet.generate()).hex() == "75010b280004"


def test_progress_bar_active_state_matches_retail_fixed16_vector():
    packet = _set(
        ProgressBar(),
        progress=0.5,
        rate=0.25,
        color1=(1, 2, 3),
        color2=(4, 5, 6),
    )
    assert bytes(packet.generate()).hex() == "4120001000030201060504"


def test_server_only_objective_packets_have_no_client_handler():
    # Importing the dispatcher loads all production handler registration
    # modules. These packet ids describe server-owned presentation/rule state;
    # accepting them from a peer would invert authority even if their bytes are
    # otherwise well formed.
    import protocol.packet_handler  # noqa: F401
    from protocol.handler_registry import HANDLERS

    server_only = {25, 44, 65, 106, 108, 109, 117}
    assert server_only.isdisjoint(HANDLERS)
