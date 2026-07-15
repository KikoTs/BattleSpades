"""Held-block palette packet invariants measured from the retail server."""

import asyncio
from types import SimpleNamespace

import pytest

import shared.constants as C
from server.handlers.team import handle_set_color
from shared.bytes import ByteReader
from shared.packet import SetColor


class RecordingServer:
    def __init__(self):
        self.broadcasts = []

    def broadcast(self, data, exclude=None, reliable=True, gameplay=True):
        self.broadcasts.append((bytes(data), exclude, reliable, gameplay))


class PalettePlayer:
    def __init__(self, *, alive=True, spawned=True, tool=C.BLOCK_TOOL):
        self.id = 7
        self.alive = alive
        self.spawned = spawned
        self.tool = tool
        self.block_color = 0x707070

    def set_color(self, value):
        self.block_color = int(value)


@pytest.mark.parametrize(
    "tool",
    [
        C.BLOCK_TOOL,
        C.FLAREBLOCK_TOOL,
        C.SNOWBLOWER_TOOL,
        C.UGC_SNOWBLOWER_TOOL,
    ],
)
def test_set_color_updates_server_and_observers_without_echoing_sender(tool):
    server = RecordingServer()
    player = PalettePlayer(tool=tool)
    packet = SimpleNamespace(value=0x123456)

    asyncio.run(handle_set_color(server, player, packet))

    assert player.block_color == 0x123456
    assert len(server.broadcasts) == 1
    data, excluded, reliable, gameplay = server.broadcasts[0]
    assert excluded is player
    assert reliable is True
    assert gameplay is True
    echoed = SetColor(ByteReader(data[1:]))
    assert echoed.player_id == player.id
    assert echoed.value == 0x123456


@pytest.mark.parametrize(
    ("alive", "spawned", "tool"),
    [
        (False, True, C.BLOCK_TOOL),
        (True, False, C.BLOCK_TOOL),
        (True, True, C.RIFLE_TOOL),
    ],
)
def test_set_color_rejects_dead_unspawned_or_non_block_tool_players(
    alive, spawned, tool
):
    server = RecordingServer()
    player = PalettePlayer(alive=alive, spawned=spawned, tool=tool)

    asyncio.run(handle_set_color(server, player, SimpleNamespace(value=0xABCDEF)))

    assert player.block_color == 0x707070
    assert server.broadcasts == []
