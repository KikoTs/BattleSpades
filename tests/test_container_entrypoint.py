"""Container configuration contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import toml

from scripts.container_entrypoint import (
    ContainerConfigurationError,
    build_runtime_config,
    load_template,
    validate_launch_config,
    write_runtime_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _template() -> dict:
    return load_template(PROJECT_ROOT / "config.toml")


def test_container_defaults_disable_platform_specific_steam(tmp_path: Path) -> None:
    document = build_runtime_config(
        _template(),
        {"BATTLESPADES_ADMIN_PASSWORD": "strong-local-password"},
        data_directory=tmp_path,
    )

    assert document["server"]["port"] == 27015
    assert document["steam"]["enabled"] is False
    assert document["steam"]["runtime_dir"] == ""
    assert document["admin"]["password"] == "strong-local-password"
    assert document["admin"]["bans_path"] == str(tmp_path / "bans.json")


def test_container_applies_independent_instance_overrides(tmp_path: Path) -> None:
    document = build_runtime_config(
        _template(),
        {
            "BATTLESPADES_SERVER_NAME": "EU CTF / Alpha",
            "BATTLESPADES_PORT": "27025",
            "BATTLESPADES_MAX_PLAYERS": "32",
            "BATTLESPADES_MODE": "CTF",
            "BATTLESPADES_MAP": "MayanJungle",
            "BATTLESPADES_BOT_COUNT": "6",
            "BATTLESPADES_REGION": "EUROPE",
            "BATTLESPADES_OFFICIAL": "false",
            "BATTLESPADES_REQUIRE_IDENTITY": "yes",
            "BATTLESPADES_ADMIN_PASSWORD": "instance-password-123",
        },
        data_directory=tmp_path,
    )

    assert document["server"]["name"] == "EU CTF / Alpha"
    assert document["server"]["port"] == 27025
    assert document["server"]["max_players"] == 32
    assert document["game"]["default_mode"] == "ctf"
    assert document["game"]["default_map"] == "MayanJungle"
    assert document["game"]["bot_count"] == 6
    assert document["bots"]["enabled"] is True
    assert document["bots"]["population_mode"] == "fixed"
    assert document["bots"]["fill_target"] == 6
    assert document["bots"]["max_bots"] == 6
    assert document["revival"]["region"] == "europe"
    assert document["revival"]["official"] is False
    assert document["revival"]["require_identity"] is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BATTLESPADES_PORT", "0"),
        ("BATTLESPADES_MAX_PLAYERS", "256"),
        ("BATTLESPADES_BOT_COUNT", "-1"),
        ("BATTLESPADES_MAP", "../secret"),
        ("BATTLESPADES_MODE", "ctf;shutdown"),
        ("BATTLESPADES_OFFICIAL", "maybe"),
        ("BATTLESPADES_ADMIN_PASSWORD", "short"),
    ],
)
def test_container_rejects_unsafe_overrides(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    with pytest.raises(ContainerConfigurationError):
        build_runtime_config(
            _template(),
            {
                name: value,
                "BATTLESPADES_ADMIN_PASSWORD": (
                    value
                    if name == "BATTLESPADES_ADMIN_PASSWORD"
                    else "strong-local-password"
                ),
            },
            data_directory=tmp_path,
        )


def test_container_refuses_default_admin_password_for_normal_launch(
    tmp_path: Path,
) -> None:
    document = build_runtime_config(
        _template(),
        {},
        data_directory=tmp_path,
    )

    with pytest.raises(ContainerConfigurationError):
        validate_launch_config(document, [], {})

    validate_launch_config(document, ["--check"], {})
    validate_launch_config(
        document,
        [],
        {"BATTLESPADES_ALLOW_INSECURE_DEFAULTS": "true"},
    )


def test_runtime_config_is_parseable_and_does_not_include_master_token(
    tmp_path: Path,
) -> None:
    runtime_path = tmp_path / "runtime" / "config.toml"
    document = build_runtime_config(
        _template(),
        {
            "BATTLESPADES_ADMIN_PASSWORD": "strong-local-password",
            "AOS_MASTER_WRITE_TOKEN": "do-not-serialize-this-token",
        },
        data_directory=tmp_path,
    )

    write_runtime_config(document, runtime_path)
    parsed = toml.load(runtime_path)

    assert parsed["admin"]["password"] == "strong-local-password"
    assert "do-not-serialize-this-token" not in runtime_path.read_text(encoding="utf-8")
