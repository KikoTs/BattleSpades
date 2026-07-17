"""Steam discovery identity, configuration, and isolation contracts."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from server.config import ServerConfig, load_config
from server.steam_master import (
    RETAIL_BROWSER_GAME_PORT,
    STEAM_APP_ID,
    SteamMasterService,
    build_game_tags,
    build_steam_map_name,
    effective_query_port,
    inspect_runtime,
)
from scripts.check_steam_registration import (
    ProbeError,
    matching_master_records,
    parse_a2s_info,
)


def test_retail_tags_and_spaced_map_name_are_exact() -> None:
    config = ServerConfig(default_mode="cctf")
    config.steam.region = "eu"

    assert build_game_tags(config) == (
        "v168;playlist=8;region=eu;mode=0008;classic"
    )
    assert build_steam_map_name("tdm", "City of Chicago") == (
        "TDM_CityOfChicago"
    )


def test_effective_query_port_is_separate_only_when_enabled() -> None:
    config = ServerConfig(port=27015)
    assert effective_query_port(config) == 27015

    config.steam.enabled = True
    assert effective_query_port(config) == 27016
    config.steam.query_port = 28016
    assert effective_query_port(config) == 28016


def test_config_rejects_spacewar_identity(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[steam]\nenabled=true\napp_id=480\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Spacewar"):
        load_config(path)


def test_config_rejects_colliding_steam_ports(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[server]\nport=27015\n[steam]\nenabled=true\n"
        "steam_port=27015\nquery_port=27016\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be distinct"):
        load_config(path)


def test_config_rejects_tag_delimiter_injection(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[steam]\nregion="eu;mode=0000"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="steam.region"):
        load_config(path)


def test_disabled_auto_query_does_not_reject_last_game_port(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[server]\nport=65535\n[steam]\nenabled=false\nquery_port=0\n",
        encoding="utf-8",
    )

    assert load_config(path).port == 65535


def _write_fake_pe(path: Path, machine: int) -> None:
    header = bytearray(0x40)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x40)
    path.write_bytes(bytes(header) + b"PE\0\0" + struct.pack("<H", machine))


def test_runtime_inspection_requires_x86_and_records_appid(tmp_path: Path) -> None:
    _write_fake_pe(tmp_path / "steam_api.dll", 0x014C)
    (tmp_path / "steamclient.dll").write_bytes(b"legacy")
    (tmp_path / "steam_appid.txt").write_text("480\n", encoding="ascii")

    result = inspect_runtime(tmp_path)

    assert result.machine == 0x014C
    assert result.app_id_file == 480
    assert result.steamclient == tmp_path / "steamclient.dll"


def test_runtime_inspection_rejects_wrong_architecture(tmp_path: Path) -> None:
    _write_fake_pe(tmp_path / "steam_api.dll", 0x8664)

    with pytest.raises(ValueError, match="needs x86"):
        inspect_runtime(tmp_path)


def test_snapshot_coalesces_live_population_and_native_tags() -> None:
    config = ServerConfig(name="Live", default_mode="vip", max_players=24)
    config.steam.enabled = True
    server = SimpleNamespace(
        config=config,
        world_manager=SimpleNamespace(map_name="CityOfChicago"),
        players={
            1: SimpleNamespace(is_bot=False),
            2: SimpleNamespace(is_bot=True),
        },
    )
    service = SteamMasterService(server)

    snapshot = service.snapshot()

    assert snapshot.server_name == "Live"
    assert snapshot.map_name == "VIP_CityOfChicago"
    assert snapshot.player_count == 2
    assert snapshot.bot_count == 1
    assert snapshot.tags.endswith("mode=0007;skin=mafia")


def test_disabled_service_is_a_noop() -> None:
    async def scenario() -> None:
        config = ServerConfig()
        service = SteamMasterService(
            SimpleNamespace(
                config=config,
                world_manager=SimpleNamespace(map_name="classicgen"),
                players={},
            )
        )
        await service.start()
        assert service.process is None
        await service.close()

    asyncio.run(scenario())


def test_public_registration_warns_about_stock_browser_game_port(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        config = ServerConfig(port=27015)
        config.steam.enabled = True
        config.steam.public = True
        config.steam.runtime_dir = str(tmp_path)
        service = SteamMasterService(
            SimpleNamespace(
                config=config,
                world_manager=SimpleNamespace(map_name="classicgen"),
                players={},
            )
        )
        await service.start()
        await service.close()

    caplog.set_level(logging.WARNING, logger="server.steam_master")
    asyncio.run(scenario())

    assert "always connects rows to UDP 32887" in caplog.text


def test_real_app_identity_is_not_spacewar() -> None:
    assert STEAM_APP_ID == 224540
    assert RETAIL_BROWSER_GAME_PORT == 32887


def test_public_registration_probe_selects_only_real_aos_records() -> None:
    records = matching_master_records(
        {
            "response": {
                "success": True,
                "servers": [
                    {
                        "addr": "88.80.155.252:27016",
                        "appid": 224540,
                        "gamedir": "aceofspades",
                        "lan": False,
                        "gameport": 27015,
                    },
                    {
                        "addr": "127.0.0.1:27016",
                        "appid": 224540,
                        "gamedir": "aceofspades",
                        "lan": True,
                    },
                    {"appid": 480, "gamedir": "aceofspades"},
                ],
            }
        }
    )

    assert records == [
        {
            "addr": "88.80.155.252:27016",
            "appid": 224540,
            "gamedir": "aceofspades",
            "lan": False,
            "gameport": 27015,
        }
    ]


def test_public_registration_probe_rejects_malformed_payload() -> None:
    with pytest.raises(ProbeError, match="did not report success"):
        matching_master_records({"response": {"success": False}})


def test_a2s_probe_decodes_steam_owned_query_response() -> None:
    packet = bytearray(b"\xff\xff\xff\xffI")
    packet.append(17)
    for value in (
        "BattleSpades Server",
        "TDM_MayanJungle",
        "aceofspades",
        "Ace of Spades",
    ):
        packet.extend(value.encode("ascii") + b"\0")
    packet.extend(struct.pack("<H", 0))
    packet.extend(bytes((12, 24, 12)))
    packet.extend(b"dw\0\0")
    packet.extend(b"1.0.0.0\0")

    info = parse_a2s_info(bytes(packet))

    assert info["protocol"] == 17
    assert info["folder"] == "aceofspades"
    assert info["players"] == 12
    assert info["max_players"] == 24
    assert info["version"] == "1.0.0.0"
