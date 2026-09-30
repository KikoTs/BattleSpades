"""A2S discovery must advertise the active retail scene variant."""

from __future__ import annotations

from server.a2s_query import A2SHandler
from server.config import ServerConfig
from server.main import BattleSpadesServer
from scripts.check_steam_registration import parse_a2s_info


def _tags(mode: str) -> frozenset[str]:
    server = BattleSpadesServer(ServerConfig(default_mode=mode))
    # Extra metadata may follow either mode or classic. Assert the actual EDF
    # keyword field instead of relying on those tags being its final bytes.
    server.config.steam.texture_skin = "mafia"
    info = parse_a2s_info(A2SHandler(server)._make_info_response())
    assert info["protocol"] == 168
    assert info["folder"] == "aceofspades"
    assert info["game_id"] == 224540
    tags = frozenset(info["tags"].split(";"))
    assert "v168" in tags and "skin=mafia" in tags
    return tags


def test_a2s_classic_ctf_keeps_public_category_and_classic_tag() -> None:
    tags = _tags("cctf")

    # The retail browser filters SERVERMODE_PUBLIC=1, not CTF's session ID 8.
    assert {tag for tag in tags if tag.startswith("mode=")} == {"mode=0001"}
    assert "classic" in tags


def test_a2s_tdm_does_not_claim_to_be_classic_ctf() -> None:
    tags = _tags("tdm")

    assert {tag for tag in tags if tag.startswith("mode=")} == {"mode=0001"}
    assert "classic" not in tags


def test_a2s_names_the_gameplay_mode_for_the_master() -> None:
    # The AoSPlay master once read the category tag as MODE_DEMOLITION (1)
    # and listed every server as "Demolition!".
    for mode in ("tdm", "ctf", "cctf", "zom", "vip", "mh", "tc", "dia", "dem", "oc"):
        tags = _tags(mode)
        assert {tag for tag in tags if tag.startswith("gamemode=")} == {
            f"gamemode={mode}"
        }
        assert {tag for tag in tags if tag.startswith("mode=")} == {"mode=0001"}


def test_a2s_gameplay_keyword_follows_the_retail_tags() -> None:
    server = BattleSpadesServer(ServerConfig(default_mode="zombie"))
    info = parse_a2s_info(A2SHandler(server)._make_info_response())
    tags = info["tags"].split(";")

    # Retail reads tags[1] as the playlist, so the extension must trail.
    assert tags[0] == "v168" and tags[1].startswith("playlist=")
    assert tags[-1] == "gamemode=zom"
    assert len(info["tags"].encode("utf-8")) < 128


# Measured 2026-09-29 on the official fleet's Steam query ports. The AoSPlay
# master reads the mode from this map field (aos_revival
# scripts/test-master-gameplay-mode.mjs checks the same nine names), so the
# field and the ``gamemode=`` keyword must always name the same mode.
OFFICIAL_FLEET_MAPS = (
    ("tdm", "SpookyMansion", "TDM_SpookyMansion"),
    ("ctf", "TokyoNeon", "CTF_TokyoNeon"),
    ("zom", "Frontier", "ZOM_Frontier"),
    ("vip", "Alcatraz", "VIP_Alcatraz"),
    ("dia", "Atlantis", "DIA_Atlantis"),
    ("dem", "BlockNess", "DEM_BlockNess"),
    ("tc", "CityOfChicago", "TC_CityOfChicago"),
    ("mh", "CastleWars", "MH_CastleWars"),
    ("cctf", "Classic", "CCTF_Classic"),
)


def test_steam_map_field_and_gameplay_keyword_name_the_same_mode() -> None:
    from server.steam_master import build_steam_map_name

    for mode, map_name, steam_map in OFFICIAL_FLEET_MAPS:
        assert build_steam_map_name(mode, map_name) == steam_map
        server = BattleSpadesServer(ServerConfig(default_mode=mode))
        info = parse_a2s_info(A2SHandler(server)._make_info_response())
        keyword = [tag for tag in info["tags"].split(";") if tag.startswith("gamemode=")]
        assert keyword == [f"gamemode={steam_map.split('_', 1)[0].lower()}"]
