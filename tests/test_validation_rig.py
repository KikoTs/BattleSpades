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
