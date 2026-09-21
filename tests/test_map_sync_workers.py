"""Map-transfer workers must not access live terrain or lose join edits."""

import asyncio
import struct
import threading
import zlib
from types import SimpleNamespace

import pytest

from server.connection import Connection
from server.world_manager import MapSyncSnapshot, WorldManager


def make_transfer():
    owner_thread = threading.get_ident()
    column = bytes((0, 62, 62, 0)) + struct.pack("<I", 0x7F123456)
    live = {"column": column}

    def serialize(columns):
        assert threading.get_ident() == owner_thread
        return b"".join(struct.pack("<II", *xy) + live["column"] for xy in columns)

    wm = WorldManager(SimpleNamespace())
    wm.map = SimpleNamespace(source_z_shift=0, serialize_columns=serialize)
    wm.map_raw_bytes = column * (512 * 512)
    wm.dirty_columns = {(0, 0)}
    marks = []
    server = SimpleNamespace(
        world_manager=wm,
        config=SimpleNamespace(log_suppress_packets=set(), map_sync_mode="full"),
        mark_map_snapshot_complete=lambda connection: marks.append(wm.topology_version),
        reserved_player_ids=set(),
    )
    peer = SimpleNamespace(address="test", disconnect=lambda reason: None)
    connection = Connection(peer, server)
    sent = []

    async def validation(_packet_class, timeout):
        return SimpleNamespace(crc=0)

    connection.wait_for = validation
    connection.send = lambda data, **kwargs: sent.append(data)
    return wm, live, server, connection, sent, marks


async def wait_started(event):
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0.001)


def test_worker_snapshot_is_immutable_and_watermark_precedes_compression(monkeypatch):
    wm, live, _server, connection, _sent, marks = make_transfer()
    started, release = threading.Event(), threading.Event()
    original = MapSyncSnapshot.build_chunks
    jobs = []

    def build(snapshot):
        jobs.append(snapshot)
        started.set()
        assert release.wait(3)
        return original(snapshot)

    monkeypatch.setattr(MapSyncSnapshot, "build_chunks", build)

    async def run():
        transfer = asyncio.create_task(connection.send_map_data())
        try:
            await wait_started(started)
            assert marks == [0]
            # An edit during compression belongs to the catch-up journal,
            # not the already-captured snapshot or its watermark.
            live["column"] = bytes((0, 62, 62, 0)) + struct.pack("<I", 0x7FABCDEF)
            wm.topology_version += 1
        finally:
            release.set()
        assert await transfer

    asyncio.run(run())
    frozen = zlib.decompress(b"".join(original(jobs[0])))
    assert frozen[12:16] == struct.pack("<I", 0x7F123456)
    assert marks == [0]
    assert wm._prepared_sync_chunks is None, "changed revisions cannot reuse old chunks"


@pytest.mark.parametrize("retire", ["disconnect", "scene_reload", "map_replace", "shutdown"])
def test_worker_does_not_send_map_after_connection_or_scene_retirement(monkeypatch, retire):
    wm, _live, server, connection, sent, _marks = make_transfer()
    started, release = threading.Event(), threading.Event()

    def build(_snapshot):
        started.set()
        assert release.wait(3)
        return [b"map-snapshot"]

    monkeypatch.setattr(MapSyncSnapshot, "build_chunks", build)

    async def run():
        transfer = asyncio.create_task(connection.send_map_data())
        try:
            await wait_started(started)
            before = list(sent)
            if retire == "disconnect":
                connection.disconnect()
            elif retire == "scene_reload":
                connection.reset_for_scene_reload()
            elif retire == "map_replace":
                wm.map = object()
            else:
                server._stopping = True
                connection.retire_for_server_shutdown()
        finally:
            release.set()
        assert not await transfer
        assert sent == before
        assert not connection.map_sent

    asyncio.run(run())


def test_cancelled_transfer_keeps_worker_slot_until_thread_finishes(monkeypatch):
    wm, _live, server, first, _sent, _marks = make_transfer()
    _other_wm, _other_live, _other_server, second, _sent2, _marks2 = make_transfer()
    second.server = server
    started, release = threading.Event(), threading.Event()
    jobs = []

    def build(snapshot):
        jobs.append(snapshot)
        started.set()
        assert release.wait(3)
        return [b"map-snapshot"]

    monkeypatch.setattr(MapSyncSnapshot, "build_chunks", build)

    async def run():
        transfer = asyncio.create_task(first.send_map_data())
        next_transfer = None
        try:
            await wait_started(started)
            transfer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await transfer
            assert wm.map_sync_lock.locked()
            next_transfer = asyncio.create_task(second.send_map_data())
            await asyncio.sleep(0.15)
            assert len(jobs) == 1
        finally:
            release.set()
        assert await next_transfer
        assert not wm.map_sync_lock.locked()

    asyncio.run(run())


def test_revision_cache_never_poisoned_pristine_full_map():
    wm, live, _server, _connection, _sent, _marks = make_transfer()
    live["column"] = bytes((0, 62, 62, 0)) + struct.pack("<I", 0x7FABCDEF)
    first = wm.iter_full_sync_chunks({(0, 0)})
    assert wm.capture_map_sync({(0, 0)}, full=True).cached_chunks == tuple(first)
    assert wm.iter_full_sync_chunks({(0, 0)}) == first
    assert wm._full_sync_chunks is None
    pristine = wm.iter_full_sync_chunks(set())
    assert pristine != first
    wm.topology_version += 1
    assert wm.capture_map_sync({(0, 0)}, full=True).cached_chunks is None
