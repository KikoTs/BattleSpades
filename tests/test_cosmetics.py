import asyncio
import json
from types import SimpleNamespace
import threading
import sys

from server.cosmetics import CAPABILITY, MAGIC, CosmeticReplication, appearance_packet, capable
from server.connection import Connection, outbound_packet_is_safe
from server.util import lzf_decompress
from server.revival_master import RevivalIdentity
from server.player import Player


class Peer:
    def __init__(self, player_id, account, native=False):
        self.player = Player(player_id, "Appearance test", team=2, weapon=6)
        self.player.account_public_id = account
        self.player.client_capabilities = (CAPABILITY,) if native else ()
        self.in_game = True
        self.packets = []

    def send(self, packet):
        assert outbound_packet_is_safe(packet, cosmetic_capable=capable(self))
        self.packets.append(packet)


def test_only_explicit_consumed_ticket_capability_enables_extensions():
    payload = dict(public_id="ply_abcdefghijklmnop", legacy_id="123", nickname="BattleSpades",
                   account_type="registered", identity_type="password")
    assert RevivalIdentity.from_payload(payload).client_capabilities == ()
    assert RevivalIdentity.from_payload({**payload, "client_capabilities": CAPABILITY}).client_capabilities == ()
    assert RevivalIdentity.from_payload({**payload, "client_capabilities": [CAPABILITY]}).client_capabilities == (CAPABILITY,)
    packet = appearance_packet(1, [])
    assert not outbound_packet_is_safe(packet)
    assert outbound_packet_is_safe(packet, cosmetic_capable=True)
    assert not outbound_packet_is_safe(bytes([240]) + b"other", cosmetic_capable=True)
    assert not outbound_packet_is_safe(MAGIC + bytes(8192), cosmetic_capable=True)
    for packet_id in range(256):
        if packet_id not in (3, 240):
            assert outbound_packet_is_safe(bytes([packet_id]))


def test_mixed_lobby_join_equip_clear_disconnect_and_slot_reuse():
    native, old = Peer(1,"ply_native",True), Peer(2,"ply_legacy")
    server = SimpleNamespace(connections={1:native,2:old})
    service = CosmeticReplication(SimpleNamespace(server=server))
    service.cached = {"ply_legacy":[{"slot":"weapon:6:world","cosmetic_id":"community-lee-enfield-v2"}]}
    service.publish()
    assert len(native.packets)==2 and old.packets==[]
    assert json.loads(native.packets[1][len(MAGIC):])["items"]=={"weapon:6:world":"community-lee-enfield-v2"}
    service.publish()
    assert len(native.packets)==2  # Unchanged outfits cause no traffic.
    newcomer = Peer(3,"ply_new",True)
    server.connections[3]=newcomer
    service.publish()
    assert len(newcomer.packets)==3 and old.packets==[]
    server.connections[2]=Peer(2,"ply_reused")
    service.publish()
    assert json.loads(native.packets[-1][len(MAGIC):])=={"player_id":2,"items":{}}
    del server.connections[3]
    service.publish()
    assert json.loads(native.packets[-1][len(MAGIC):])=={"player_id":3,"items":{}}
    assert newcomer not in service.sent


def test_loading_and_legacy_peers_never_receive_cosmetics():
    native, old = Peer(1,"one",True), Peer(2,"two")
    native.in_game=False
    service=CosmeticReplication(SimpleNamespace(server=SimpleNamespace(connections={1:native,2:old})))
    service.publish()
    assert native.packets==old.packets==[]


def test_actual_connection_send_frames_extensions_only_for_capable_peers(monkeypatch):
    allocated=[]
    def packet(data,flags):
        allocated.append((data,flags))
        return data
    monkeypatch.setitem(sys.modules,"enet",SimpleNamespace(Packet=packet,PACKET_FLAG_RELIABLE=1))
    server=SimpleNamespace(config=SimpleNamespace(log_suppress_packets=[],packet_trace=False))
    sent=[]
    transport=SimpleNamespace(send=lambda channel,data:sent.append((channel,data)))
    native=Connection(transport,server)
    native.in_game=True
    native.player=Peer(1,"ply_native",True).player
    appearance=appearance_packet(1,[{"slot":"weapon:60:world","cosmetic_id":"community-honey-badger-v2"}])
    native.send(appearance)
    assert len(sent)==1 and allocated[0][1]==1 and sent[0][0]==0
    assert sent[0][1][0]==0x30 and lzf_decompress(sent[0][1][1:])==appearance
    native.player.client_capabilities=()
    native.send(appearance)
    assert len(allocated)==1 and len(sent)==1


def test_only_catalog_ids_and_visual_world_slots_are_serialized():
    packet=appearance_packet(7,[{"slot":"damage","cosmetic_id":"999"},
      {"slot":"weapon:6:view","cosmetic_id":"hidden"},
      {"slot":"weapon:6:world","cosmetic_id":"../../secret"},
      {"slot":"class:12:hat","cosmetic_id":"field-engineer-helmet-v2"},None])
    assert json.loads(packet[len(MAGIC):])["items"]=={"class:12:hat":"field-engineer-helmet-v2"}


def test_delayed_request_does_not_block_simulation_and_close_cancels_polling():
    entered, release=threading.Event(),threading.Event()
    native=Peer(1,"ply_native",True)
    service=CosmeticReplication(SimpleNamespace(server=SimpleNamespace(connections={1:native})))
    def slow_fetch(_ids):
        entered.set()
        release.wait(2)
        return {}
    service._fetch=slow_fetch
    async def scenario():
        service.start()
        ticks=0
        try:
            for _ in range(20):
                await asyncio.sleep(.002)
                ticks+=1
            assert entered.is_set() and ticks==20
            await service.close()
            assert service.task is None and native.packets==[]
        finally:
            release.set()
    asyncio.run(scenario())
