"""Wire metadata and real occupancy contracts for the Linux Steam sidecar."""
import os
import struct
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import Mock

import pytest

from scripts.check_steam_registration import ProbeError, parse_a2s_info
from scripts import steam_linux_sidecar as sidecar


@pytest.mark.parametrize("bind_ip, numeric_ip", [("0.0.0.0", 0), ("192.0.2.17", 0xC0000211)])
def test_native_ipv4_binding_precedes_user_creation(monkeypatch, tmp_path, bind_ip, numeric_ip):
    """SDK 1.37 requires binding before the local user opens backend sockets."""
    calls = []
    library = Mock()
    library.CreateInterface.return_value = 10

    def native_call(obj, index, result, types, *args):
        calls.append((obj, index, args))
        if obj == 10 and index == 3:
            sidecar.c.cast(args[0], sidecar.c.POINTER(sidecar.c.c_int))[0] = 20
            return 30
        if obj == 10 and index == 6:
            return 40
        return True

    monkeypatch.setattr(sidecar.sys, "platform", "linux")
    monkeypatch.setattr(sidecar.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(sidecar.c, "CDLL", lambda *args, **kwargs: library)
    monkeypatch.setattr(sidecar, "vcall", native_call)
    monkeypatch.setenv("SteamAppId", "224540")
    monkeypatch.setenv("SteamGameId", "224540")
    sidecar.SteamServer(tmp_path, 32887, 32888, bind_ip)
    client_methods = [index for obj, index, args in calls if obj == 10]
    assert client_methods == ([7, 3, 6] if numeric_ip else [3, 6])
    if numeric_ip:
        assert calls[0] == (10, 7, (numeric_ip, 0))
    assert (40, 0, (numeric_ip, 32887, 32888, 12, 224540, b"1.0.0.0")) in calls


def upstream(**changes):
    info = dict(folder="aceofspades", name="Our server", map="City of Chicago",
                players=12, bots=12, max_players=24, password=False,
                tags="v168;playlist=8;mode=0006")
    return info | changes


def test_old_gameplay_tag_is_converted_to_retail_category():
    result = sidecar.advertisement(upstream(), "tdm", "europe")
    assert result["tags"] == "v168;playlist=8;region=europe;mode=0001"
    assert result["map"] == "TDM_CityOfChicago"
    assert result["players"] == result["bots"] == 12


def test_native_map_prefix_and_classic_skin_are_preserved():
    result = sidecar.advertisement(upstream(map="TDM_Atlantis", tags="v168;mode=0006;classic;skin=mafia"), "tdm", "")
    assert result["map"] == "TDM_Atlantis"
    assert result["tags"] == "v168;playlist=8;mode=0001;classic;skin=mafia"


@pytest.mark.parametrize("region, canonical", [
    ("america", "us_east"), ("NA", "us_east"), ("us-west", "us_west"),
    (" US East ", "us_east"), ("EU", "europe"), ("oceania", "australia"),
])
def test_region_aliases_advertise_browser_filter_values(region, canonical):
    result = sidecar.advertisement(upstream(), "tdm", region)
    assert f"region={canonical}" in result["tags"].split(";")


@pytest.mark.parametrize("mode", ["dem", "mh", "oc", "cctf"])
def test_sidecar_cli_accepts_new_public_modes_and_canonical_region(mode):
    parser = sidecar.build_parser()
    arguments = parser.parse_args([
        "--runtime", ".", "--source-port", "32887", "--mode", mode,
        "--region", "us_east",
    ])
    result = sidecar.advertisement(upstream(), arguments.mode, arguments.region)
    assert result["map"] == f"{mode.upper()}_CityOfChicago"
    assert "mode=0001" in result["tags"].split(";")
    assert "region=us_east" in result["tags"].split(";")
    if mode == "cctf":
        assert "classic" in result["tags"].split(";")


def test_public_mode_and_alias_contract_matches_server():
    from server import mode_data

    public = set(mode_data.MODES) - {"nor", "tut", "ugc"}
    assert set(sidecar.PUBLIC_MODES) == public
    for name in public | set(sidecar._MODE_ALIASES):
        arguments = sidecar.build_parser().parse_args([
            "--runtime", ".", "--source-port", "32887", "--mode", name,
        ])
        assert arguments.mode == mode_data.get(name).code


@pytest.mark.parametrize("region", ["us_west", "us_east", "europe", "asia", "australia", ""])
def test_canonical_regions_survive_cli_and_advertisement(region):
    arguments = sidecar.build_parser().parse_args([
        "--runtime", ".", "--source-port", "32887", "--mode", "tdm",
        "--region", region,
    ])
    tags = sidecar.advertisement(upstream(), arguments.mode, arguments.region)["tags"].split(";")
    assert ([tag for tag in tags if tag.startswith("region=")]
            == ([f"region={region}"] if region else []))


@pytest.mark.parametrize("mode", ["tc", "vip", "cctf", "dem", "mh", "oc"])
def test_mode_flags_match_main_server_advertisement(mode):
    from server.config import ServerConfig
    from server.steam_master import build_game_tags

    config = ServerConfig()
    config.steam.region = "us_east"
    expected = build_game_tags(config, mode)
    assert sidecar.advertisement(upstream(tags=""), mode, "us_east")["tags"] == expected


@pytest.mark.parametrize("option, value", [
    ("--mode", "typo"), ("--mode", "ugc"), ("--mode", "tutorial"),
    ("--region", "somewhere"), ("--region", "europe;mode=0006"),
])
def test_bad_advertisement_options_fail_before_query(monkeypatch, option, value):
    query = Mock(side_effect=AssertionError("must not query on invalid CLI"))
    monkeypatch.setattr(sidecar, "query_a2s", query)
    with pytest.raises(SystemExit) as result:
        sidecar.main(["--runtime", ".", "--source-port", "32887", "--mode", "tdm", option, value])
    assert result.value.code == 2
    query.assert_not_called()


def test_legacy_region_is_canonical_for_both_tags_and_steam(monkeypatch):
    calls = {}
    handlers = {}

    class FakeSteam:
        def __init__(self, *args):
            pass

        def update(self, data, rows):
            calls["tags"] = data["tags"]

        def start(self, region):
            calls["region"] = region

        def poll(self):
            handlers[sidecar.signal.SIGTERM](None, None)
            return True, 1, 0

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(sidecar, "SteamServer", FakeSteam)
    monkeypatch.setattr(sidecar, "query_a2s", lambda *args: upstream())
    monkeypatch.setattr(sidecar, "query_players", lambda *args: [])
    monkeypatch.setattr(sidecar.signal, "signal", lambda sig, fn: handlers.setdefault(sig, fn))
    monkeypatch.setattr(sidecar.time, "sleep", lambda seconds: None)
    assert sidecar.main([
        "--runtime", ".", "--source-port", "32887", "--mode", "demolition",
        "--region", "america",
    ]) == 0
    assert calls["region"] == "us_east"
    assert "region=us_east" in calls["tags"].split(";")
    assert calls["closed"]


def test_standalone_install_does_not_require_server_package(tmp_path):
    source = Path(sidecar.__file__).parent
    for name in ("steam_linux_sidecar.py", "check_steam_registration.py"):
        shutil.copyfile(source / name, tmp_path / name)
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONPATH", "PYTHONSAFEPATH"}}
    result = subprocess.run(
        [sys.executable, str(tmp_path / "steam_linux_sidecar.py"), "--help"],
        cwd=tmp_path, env=environment, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "cctf" in result.stdout and "us_east" in result.stdout


@pytest.mark.parametrize("changes", [{"bots": 13}, {"players": 25}, {"folder": "other"}])
def test_invalid_upstream_is_not_advertised(changes):
    with pytest.raises(ProbeError):
        sidecar.advertisement(upstream(**changes), "tdm", "europe")


def test_bot_count_is_separate_from_total_population(monkeypatch):
    calls = []
    next_id = iter(range(100, 200))

    def fake_call(obj, index, result, types, *args):
        calls.append((index, args))
        return next(next_id) if index == 25 else True

    monkeypatch.setattr(sidecar, "vcall", fake_call)
    steam = object.__new__(sidecar.SteamServer)
    steam.server = 1
    steam.users = []
    data = sidecar.advertisement(upstream(players=3, bots=2), "tdm", "europe")
    steam.update(data, [("Bot One", 5), ("Bot Two", 9), ("Guest", 0)])
    assert steam.users == [100, 101, 102]
    assert (13, (2,)) in calls
    assert (27, (102, b"Guest", 0)) in calls
    calls.clear()
    steam.update(data | {"players": 2}, [("Bot One", 6), ("Bot Two", 10)])
    assert steam.users == [100, 101]
    assert (26, (102,)) in calls
    assert not any(index == 25 for index, args in calls)


def info_packet():
    packet = b"\xff\xff\xff\xffI\x11"
    packet += b"Server\0TDM_Atlantis\0aceofspades\0Ace of Spades\0"
    packet += struct.pack("<HBBB", 0, 12, 24, 12) + b"dl\0\0" + b"1.0.0.0\0"
    return packet


def test_extended_fields_verify_full_appid_retail_port_and_tags():
    packet = info_packet() + b"\xb1" + struct.pack("<HQ", 32887, 90293115813403669)
    packet += b"v168;playlist=8;mode=0001\0" + struct.pack("<Q", 224540)
    info = parse_a2s_info(packet)
    assert info["game_port"] == 32887
    assert info["game_id"] == 224540
    assert info["tags"].endswith("mode=0001")


@pytest.mark.parametrize("suffix", [b"\x80\x01", b"\x10\0\0", b"\x20unterminated", b"\x01\0"])
def test_truncated_extended_fields_fail_closed(suffix):
    with pytest.raises(ProbeError, match="truncated"):
        parse_a2s_info(info_packet() + suffix)
