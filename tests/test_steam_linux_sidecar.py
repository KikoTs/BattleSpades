"""Wire metadata and real occupancy contracts for the Linux Steam sidecar."""
import struct

import pytest

from scripts.check_steam_registration import ProbeError, parse_a2s_info
from scripts import steam_linux_sidecar as sidecar


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
