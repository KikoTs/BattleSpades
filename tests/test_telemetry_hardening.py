"""Production-safety tests for opt-in, bounded diagnostics."""

import queue
import json
from pathlib import Path
from types import SimpleNamespace

import server.config as server_config
from server.config import ServerConfig
from server.debug_parity import DebugParityManager, DebugParitySession
from server.telemetry import TelemetryService


def test_production_diagnostics_default_to_disabled(tmp_path):
    config = ServerConfig()
    server = SimpleNamespace(config=config)

    manager = DebugParityManager(server, base_directory=tmp_path)

    assert config.debug_parity is False
    assert config.packet_trace is False
    assert config.debug_selfrow is False
    assert config.movement_debug_capture is False
    assert manager.socket is None
    assert manager._writer_thread is None
    manager.close()


def test_debug_parity_config_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / 'server.toml'
    path.touch()
    # Several legacy tests install a minimal ``toml`` stub before collection.
    # Patching the loader here keeps this test independent of collection order.
    monkeypatch.setattr(server_config.toml, 'load', lambda unused: {
        'debug': {
            'debug_parity': True,
            'debug_parity_queue_capacity': 1,
            'debug_parity_sample_hz': 500,
            'debug_parity_flush_interval': 0,
            'debug_parity_flush_batch': 0,
        },
    })

    config = server_config.load_config(path)

    assert config.debug_parity is True
    assert config.debug_parity_queue_capacity == 64
    assert config.debug_parity_sample_hz == 10.0
    assert config.debug_parity_flush_interval == 0.1
    assert config.debug_parity_flush_batch == 1


def test_prefab_runtime_limits_are_bounded(tmp_path, monkeypatch):
    """Malformed production limits cannot create unbounded prefab tick work."""
    path = tmp_path / "server.toml"
    path.write_text("[network]\n", encoding="utf-8")
    monkeypatch.setattr(server_config.toml, "load", lambda unused: {
        "network": {
            "prefab_queue_limit": 10000,
            "prefab_cell_batch_limit": 0,
        },
    })

    config = server_config.load_config(path)

    assert config.prefab_queue_limit == 128
    assert config.prefab_cell_batch_limit == 1


def test_full_capture_queue_drops_without_waiting(tmp_path):
    config = ServerConfig(debug_parity=False)
    manager = DebugParityManager(
        SimpleNamespace(config=config), base_directory=tmp_path
    )
    # Model an enabled writer whose disk consumer is currently stalled.
    manager._writer_thread = object()
    manager._capture_queue = queue.Queue(maxsize=1)
    session = DebugParitySession(
        player_id=1,
        player_name='Miner',
        session_id='bounded',
        capture_path=Path(tmp_path) / 'capture.ndjson',
    )

    manager._write_record(session, {'kind': 'first'})
    manager._write_record(session, {'kind': 'dropped'})

    assert manager._capture_queue.qsize() == 1
    assert manager.dropped_records == 1
    # Do not call close(): the deliberately fake writer has no thread to join.


def test_debug_selfrow_uses_bounded_writer_queue(tmp_path):
    config = ServerConfig(debug_selfrow=True)
    server = SimpleNamespace(config=config, loop_count=120)
    manager = DebugParityManager(server, base_directory=tmp_path)
    player = SimpleNamespace(
        id=4,
        name="Walker",
        last_applied_input_loop=118,
        x=1.25,
        y=2.5,
        z=3.75,
    )

    try:
        manager.write_selfrow_sample(player, stamp=118)
    finally:
        manager.close()

    path = Path(tmp_path) / "selfrow_samples.ndjson"
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["kind"] == "selfrow"
    assert record["stamp"] == 118
    assert record["player_id"] == 4


def test_telemetry_service_combines_bounded_metrics_and_log_drop_count():
    service = TelemetryService(SimpleNamespace(dropped_records=7))
    service.metrics.record_tick(2.5)

    snapshot = service.snapshot()

    assert snapshot["tick_samples"] == 1
    assert snapshot["tick_avg_ms"] == 2.5
    assert snapshot["logging_dropped_records"] == 7
