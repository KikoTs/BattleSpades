"""A2S discovery must advertise the active retail scene variant."""

from __future__ import annotations

from server.a2s_query import A2SHandler
from server.config import ServerConfig
from server.main import BattleSpadesServer


def _info(mode: str) -> bytes:
    server = BattleSpadesServer(ServerConfig(default_mode=mode))
    return A2SHandler(server)._make_info_response()


def test_a2s_classic_ctf_keeps_ctf_id_and_adds_classic_tag() -> None:
    response = _info("cctf")

    assert b"mode=0008;classic\0" in response


def test_a2s_tdm_does_not_claim_to_be_classic_ctf() -> None:
    response = _info("tdm")

    assert b"mode=0006\0" in response
    assert b";classic\0" not in response
