"""Native bridge integration gate. --loopback uses a separately compiled binary.

The loopback gate exercises the actual native forwarding, IPC admission barrier,
frame validation and teardown; it is NOT a remote SDR connectivity test.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'client_patches' / 'retail_mousefix'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from aos_steam_bridge import BridgeClient, decode, encode


def wait(clients, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for client in clients:
            for fields in client.poll():
                if fields[0] == 'ERROR':
                    raise RuntimeError(decode(fields[2], 1024))
                if predicate(client, fields):
                    return fields
        time.sleep(.005)
    raise TimeoutError('Native relay gate timed out')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('helper', type=Path)
    parser.add_argument('--loopback', action='store_true')
    args = parser.parse_args()
    clients = []
    sockets = []
    old_port = os.environ.get('AOS_RELAY_TEST_PORT')
    try:
        if args.loopback:
            if args.helper.name != 'aos-retail-relay-test.exe':
                raise ValueError('Loopback gate requires the separately compiled test helper')
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
                reservation.bind(('127.0.0.1', 0))
                os.environ['AOS_RELAY_TEST_PORT'] = str(reservation.getsockname()[1])
        host = BridgeClient(str(args.helper)); clients.append(host); host.start()
        wait(clients, lambda c, f: f[0] == 'READY')
        print('AppID 224540: initialized; Steam user identity validated')
        if not args.loopback:
            wait(clients, lambda c, f: f[0] == 'STATUS' and f[1] == '100')
            print('Steam relay access: available (no remote peer tested)')
            return
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sockets.append(server)
        server.bind(('127.0.0.1', 0)); server.setblocking(False)
        session = uuid.uuid4().hex
        host.send('HOST','1',server.getsockname()[1],168,session,0,
                  encode('Local validation'),encode('arena'),encode('tdm'),24,0,0,0,'')
        wait(clients, lambda c, f: f[0] == 'HOSTED')
        joiner = BridgeClient(str(args.helper)); clients.append(joiner); joiner.start()
        wait(clients, lambda c, f: c is joiner and f[0] == 'READY')
        joiner.send('JOIN','2',host.steam,168,session)
        admitted = []
        def connected(client, fields):
            if fields[0] == 'PEER':
                admitted.append(fields)
                client.send('ALLOW',fields[1])
            return fields[0] == 'JOINED'
        joined = wait(clients, connected)
        assert len(admitted) == 1
        game = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sockets.append(game)
        game.bind(('127.0.0.1',0)); game.setblocking(False)
        target = ('127.0.0.1',int(joined[2]))
        packets = [os.urandom(n) for n in (1, 32, 128, 1024, 1400, 2048, 4096)]
        for packet in packets:
            deadline = time.monotonic() + 5
            game.sendto(packet,target)
            received = False
            while time.monotonic() < deadline:
                for client in clients:
                    for event in client.poll():
                        if event[0] in ('ERROR','LEFT'):
                            raise RuntimeError(str(event))
                try:
                    data, source = server.recvfrom(65535)
                    assert data == packet
                    assert source[1] == int(admitted[0][2])
                    server.sendto(data,source)
                except BlockingIOError:
                    pass
                try:
                    data, source = game.recvfrom(65535)
                    assert data == packet and source == target
                    received = True
                    break
                except BlockingIOError:
                    pass
                time.sleep(.001)
            assert received, 'Packet failed at size %d' % len(packet)
        # A second local sender must not steal the pinned return address.
        intruder = socket.socket(socket.AF_INET,socket.SOCK_DGRAM); sockets.append(intruder)
        intruder.sendto(b'wrong-local-source',target)
        time.sleep(.1)
        try:
            leaked = server.recv(65535)
        except BlockingIOError:
            leaked = None
        assert leaked is None, 'Unrelated local sender reached the server'
        joiner.send('CANCEL','2')
        wait(clients,lambda c,f: f[0]=='DROP')
        print('Native loopback forwarding: %d bidirectional payloads passed (1..4096 bytes)' % len(packets))
        print('Admission barrier, local sender pinning and disconnect cleanup passed')
        # Run actual ENet endpoints through the same native bridge, including
        # compression, protocol connect data and reliable fragmentation.
        import enet
        server_port = server.getsockname()[1]
        server.close()
        native_server = enet.Host(enet.Address(b'127.0.0.1', server_port), 4, 2, 0, 0)
        native_server.compress_with_range_coder()
        joiner.send('JOIN','3',host.steam,168,session)
        joined = wait(clients,connected)
        native_client = enet.Host(None, 1, 2, 0, 0)
        native_client.compress_with_range_coder()
        peer = native_client.connect(enet.Address(b'127.0.0.1',int(joined[2])),2,168)
        expected = [b'retail-protocol-168', os.urandom(16384)]
        replies = []
        saw_connect = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(replies) < len(expected):
            for controller in clients:
                for event in controller.poll():
                    if event[0] in ('ERROR','LEFT'):
                        raise RuntimeError(str(event))
            event = native_server.service(0)
            if event.type == enet.EVENT_TYPE_CONNECT:
                assert event.data == 168
                saw_connect = True
            elif event.type == enet.EVENT_TYPE_RECEIVE:
                event.peer.send(event.channelID, enet.Packet(event.packet.data, enet.PACKET_FLAG_RELIABLE))
            event = native_client.service(0)
            if event.type == enet.EVENT_TYPE_CONNECT:
                for payload in expected:
                    peer.send(0,enet.Packet(payload,enet.PACKET_FLAG_RELIABLE))
            elif event.type == enet.EVENT_TYPE_RECEIVE:
                replies.append(event.packet.data)
            time.sleep(.001)
        assert saw_connect and replies == expected, 'ENet bridge round-trip failed'
        peer.disconnect_now()
        joiner.send('CANCEL','3')
        wait(clients,lambda c,f:f[0]=='DROP')
        print('ENet: protocol 168, range compression, reliable 16 KiB fragmentation and reconnect passed')
        # A stale browser row must not silently connect to a replacement host.
        joiner.send('JOIN','4',host.steam,168,uuid.uuid4().hex)
        def rejected(client, fields):
            if fields[0]=='PEER': client.send('ALLOW',fields[1])
            return fields[0]=='LEFT' and fields[1]=='4'
        wait(clients,rejected)
        print('Stale host-instance rejection passed')
    finally:
        for client in clients: client.close()
        time.sleep(.25)
        for client in clients: client.reap(force=True)
        for sock in sockets: sock.close()
        if old_port is None: os.environ.pop('AOS_RELAY_TEST_PORT',None)
        else: os.environ['AOS_RELAY_TEST_PORT'] = old_port


if __name__ == '__main__':
    main()
