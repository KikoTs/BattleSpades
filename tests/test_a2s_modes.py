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
