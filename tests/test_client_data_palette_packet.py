"""Retail ClientData player-id/palette-bit decoding invariants."""

from shared.bytes import ByteReader
from shared.packet import ClientData


def test_client_data_read_splits_palette_bit_from_player_id():
    """Bit 7 is palette state, not part of the seven-bit player identifier.

    This payload was generated and decoded by the retail Python 2
    ``shared.packet.pyd``: raw player byte ``0x83`` becomes player 3 with the
    palette active.
    """
    payload = bytes.fromhex("0700000083050040000000000000000000")

    packet = ClientData(ByteReader(payload))

    assert packet.player_id == 3
    assert packet.palette_enabled is True


def test_client_data_read_keeps_plain_player_id_when_palette_is_closed():
    payload = bytes.fromhex("0700000003050040000000000000000000")

    packet = ClientData(ByteReader(payload))

    assert packet.player_id == 3
    assert packet.palette_enabled is False
