import asyncio
from types import SimpleNamespace

from server.connection import Connection
from shared.bytes import ByteReader
from shared.packet import SkyboxData


class _SkyboxConnection:
    def __init__(self, skybox_name: str | None, default: str = "User_Grassland.txt"):
        metadata = SimpleNamespace(skybox_name=skybox_name)
        self.server = SimpleNamespace(
            world_manager=SimpleNamespace(map_metadata=metadata),
            config=SimpleNamespace(default_skybox=default),
        )
        self.sent = []

    def send(self, data: bytes, **kwargs):
        self.sent.append((bytes(data), kwargs))


def test_send_skybox_uses_active_map_metadata():
    connection = _SkyboxConnection("ArcticBase.txt")

    asyncio.run(Connection.send_skybox(connection))

    data, options = connection.sent.pop()
    assert data[0] == SkyboxData.id
    packet = SkyboxData()
    packet.read(ByteReader(data[1:]))
    assert packet.value == "ArcticBase.txt"
    assert options["prefix"] == 0x30


def test_send_skybox_uses_configured_fallback_for_voxel_only_map():
    connection = _SkyboxConnection(None, default="WW1.txt")

    asyncio.run(Connection.send_skybox(connection))

    data, _options = connection.sent.pop()
    packet = SkyboxData()
    packet.read(ByteReader(data[1:]))
    assert packet.value == "WW1.txt"


def test_send_skybox_replaces_unsafe_fallback_with_stock_asset():
    connection = _SkyboxConnection(None, default="../../bad.txt")

    asyncio.run(Connection.send_skybox(connection))

    data, _options = connection.sent.pop()
    packet = SkyboxData()
    packet.read(ByteReader(data[1:]))
    assert packet.value == "User_Grassland.txt"
