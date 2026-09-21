"""Round-result persistence and bounded storage regressions."""
import pytest
import json

from server.result_outbox import ResultOutbox


def test_host_mirror_survives_restart_and_repairs_interrupted_write(tmp_path):
    directory = tmp_path / "account" / "pending"
    outbox = ResultOutbox(tmp_path / "session" / "results.sqlite3", directory)
    report = event("round-1")
    outbox.put_many([report])
    mirrored = directory / "round-1.json"
    assert json.loads(mirrored.read_text()) == report
    mirrored.unlink()
    outbox.put_many([report])
    assert json.loads(mirrored.read_text()) == report
    restarted = ResultOutbox(outbox.path, directory)
    restarted.acknowledge("round-1")
    assert not mirrored.exists() and not restarted.pending("one:27015")
    with pytest.raises(ValueError, match="identifier"):
        restarted.put_many([event("../escape")])
    assert not restarted.pending("one:27015")


def event(identifier, server="one:27015"):
    return {"event_id": identifier, "server_id": server, "players": []}


def test_partition_deduplication_and_payload_identity(tmp_path):
    outbox = ResultOutbox(tmp_path / "results.sqlite3")
    outbox.put_many([event("a"), event("b", "two:27015")])
    outbox.put_many([event("a")])
    assert outbox.pending("one:27015") == [event("a")]
    with pytest.raises(ValueError, match="different payload"):
        outbox.put_many([event("c"), event("a", "changed:27015")])
    assert outbox.pending("one:27015") == [event("a")], "failed batch must roll back"
    outbox.acknowledge("a")
    assert not outbox.pending("one:27015")
    assert outbox.pending("two:27015") == [event("b", "two:27015")]


def test_storage_limits_fail_without_dropping_prior_events(tmp_path):
    outbox = ResultOutbox(tmp_path / "results.sqlite3")
    outbox.MAX_EVENTS = 1
    outbox.put_many([event("a")])
    with pytest.raises(OSError, match="full"):
        outbox.put_many([event("b")])
    assert outbox.pending("one:27015") == [event("a")]
    oversized = event("c", "two:27015")
    oversized["padding"] = "x" * outbox.MAX_EVENT_BYTES
    with pytest.raises(ValueError, match="size limit"):
        outbox.put_many([oversized])
    assert not outbox.pending("two:27015")
