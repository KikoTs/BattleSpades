"""KICKED_SCORE_REASON (224): the profile counter of kicks.

Retail lists it with the profile statistics (Steam stat name
'KICKED_SCORE_REASON') and no client code reads it, so the retail server
wrote it. Every kick path ends in Connection.disconnect.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import shared.constants as C
from server import conduct
from server.connection import Connection
from server.revival_master import RevivalMasterService
from tests.test_revival_master import make_server

KICKED = int(C.KICKED_SCORE_REASON)


class Peer:
    address = SimpleNamespace(host="203.0.113.9", port=40100)

    def __init__(self):
        self.reasons = []

    def disconnect(self, reason=0):
        self.reasons.append(int(reason))


def _joined(server=None, *, bot=False):
    server = server or SimpleNamespace(
        config=SimpleNamespace(default_mode="tdm"), mode=None,
        players={}, connections={},
    )
    connection = Connection(Peer(), server)
    player = SimpleNamespace(
        id=3, name="Kicked", is_bot=bot, connection=connection,
        account_legacy_id="1000000007", team=2,
        kills=0, deaths=0, captures=0, score=0,
    )
    player.disconnect = connection.disconnect
    connection.player = player
    return connection, player


def _count(player) -> int:
    state = getattr(player, "profile_stats", None)
    return 0 if state is None else state.values.get(KICKED, [0, 0])[0]


def test_stat_id_is_the_retail_one():
    assert KICKED == 224
    assert C.SCORE_REASON.KICKED_SCORE_REASON == 224


@pytest.mark.parametrize("reason", [
    "ERROR_KICKED", "ERROR_KICK_GRIEFING", "ERROR_KICK_HACKING",
    "ERROR_KICK_ABUSE", "ERROR_AFK_TIMEOUT", "ERROR_BANNED",
    "ERROR_TEMP_BANNED",
])
def test_every_kick_reason_counts_once(reason):
    connection, player = _joined()
    code = int(getattr(C.DISCONNECT, reason))

    connection.disconnect(code)
    connection.disconnect(code)

    assert _count(player) == 1
    assert connection.peer.reasons == [code, code]


@pytest.mark.parametrize("reason", [
    "ERROR_UNDEFINED", "ERROR_FULL", "ERROR_TIMEOUT", "ERROR_MATCH_ENDED",
    "ERROR_DATA", "ERROR_RANKED_SERVER", "ERROR_NOTICKET",
])
def test_other_disconnects_do_not_count(reason):
    connection, player = _joined()
    connection.disconnect(int(getattr(C.DISCONNECT, reason)))
    assert _count(player) == 0


def test_bots_and_peers_without_a_player_are_not_counted():
    connection, bot = _joined(bot=True)
    connection.disconnect(int(C.DISCONNECT.ERROR_KICKED))
    assert _count(bot) == 0

    loading = Connection(Peer(), SimpleNamespace(config=SimpleNamespace()))
    loading.disconnect(int(C.DISCONNECT.ERROR_KICKED))
    assert loading.peer.reasons == [int(C.DISCONNECT.ERROR_KICKED)]


def test_conduct_kick_is_counted():
    connection, player = _joined()
    connection.server.broadcast = lambda *args, **kwargs: None

    conduct._kick(
        connection.server, player, int(C.DISCONNECT.ERROR_AFK_TIMEOUT),
        "afk idle=600s", "Kicked was kicked for being AFK.",
    )

    assert _count(player) == 1


def test_the_kick_reaches_the_departure_record(monkeypatch):
    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "x" * 48)
    server = make_server()
    server.mode = None
    service = RevivalMasterService(server)
    connection, player = _joined(server)

    connection.disconnect(int(C.DISCONNECT.ERROR_KICK_GRIEFING))
    service.accumulate_departing_player(player)

    record = service._departed[str(player.account_legacy_id)]
    assert record["profile"][KICKED][0] == 1
