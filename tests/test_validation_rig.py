import json
from pathlib import Path

from scripts.parity_artifact import ParityArtifact
from scripts.parity_clients import build_client_specs
from scripts.run_validation_server import parse_args
from server.config import ServerConfig
from server.validation import build_validation_config


def test_validation_config_overrides_runtime_values_without_mutating_source():
    source = ServerConfig(
        port=27015,
        default_map="CityOfChicago",
        default_mode="tdm",
    )

    result = build_validation_config(
        source,
        port=27016,
        map_name="ArcticBase",
        mode="tdm",
    )

    assert result.port == 27016
    assert result.default_map == "ArcticBase"
    assert result.default_mode == "tdm"
    assert result.name.endswith("[VALIDATION]")
    assert source.port == 27015
    assert source.default_map == "CityOfChicago"


def test_validation_config_refuses_public_port():
    import pytest

    with pytest.raises(ValueError, match="public server port"):
        build_validation_config(ServerConfig(port=27015), port=27015)


def test_validation_launcher_defaults_are_isolated():
    args = parse_args([])

    assert args.port == 27016
    assert args.map_name == "ArcticBase"
    assert args.mode == "tdm"
    assert args.config == Path("config.toml")


def test_two_client_specs_use_unique_tracer_ports():
    specs = build_client_specs("127.0.0.1:27016")

    assert [spec.console_port for spec in specs] == [32896, 32897]
    assert [spec.tracer_port for spec in specs] == [32895, 32898]
    assert all(spec.connect_target == "127.0.0.1:27016" for spec in specs)
    assert len({spec.capture_dir for spec in specs}) == 2


def test_parity_artifact_preserves_correlated_snapshots(tmp_path):
    artifact = ParityArtifact("movement_walk")
    artifact.record(
        "walk_start",
        server={"loop": 10},
        client_a={"loop": 12},
        client_b={"tool": 7},
    )

    path = artifact.write(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["scenario"] == "movement_walk"
    assert data["samples"][0]["marker"] == "walk_start"
    assert data["samples"][0]["server"]["loop"] == 10
