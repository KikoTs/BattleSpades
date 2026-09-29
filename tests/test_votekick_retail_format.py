"""Pin the retail vote-kick GenericVoteMessage(47) contents.

Recovered from the stock ``aoslib.hud.hud.pyd`` (byte-identical in the Steam
install and the dev client): ``GenericVotingHUD.decode_string`` does
``ast.literal_eval`` and then ``strings.get_by_id(value[0])`` -- or, when
``value[0]`` is itself a tuple, ``strings.get_by_id(value[0][0])`` with
``value[0][1]`` listing the argument indexes that are string ids too -- and
finally ``template.format(*value[1])``.  The stock client resolves::

    title        ('VOTE_TO_KICK_TITLE', ())         -> "Vote Kick"
    description  (('VOTE_TO_KICK_DESCRIPTION', (1,)),
                  (target, 'KICK_REASON_ABUSE', starter))
                 -> "Vote to Kick <target> for Abuse? Vote initiated by <starter>"
    candidates   ('KICK_YES', ()), ('KICK_NO', ())  -> "Yes", "No"
    CLOSED title VOTE_KICK_SUCCESSFUL / _UNSUCCESSFUL / _CANCELLED

Verified live 2026-09-26 against the stock hud.pyd (two clients).
"""

import ast
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

from shared.bytes import ByteReader  # noqa: E402
from shared.packet import GenericVoteMessage  # noqa: E402
from server import voting  # noqa: E402


# English templates from aoslib/strings/english.py (retail client).
STRINGS = {
    "VOTE_TO_KICK_TITLE": "Vote Kick",
    "VOTE_TO_KICK_DESCRIPTION": "Vote to Kick {0} for {1}? Vote initiated by {2}",
    "KICK_YES": "Yes",
    "KICK_NO": "No",
    "VOTE_KICK_SUCCESSFUL": "{0} has been kicked by {1} for {2}",
    "VOTE_KICK_UNSUCCESSFUL": "{0}'s vote to kick {1} failed",
    "VOTE_KICK_CANCELLED": "Vote to kick {0} cancelled by {1}",
    "KICK_REASON_GRIEFING": "Griefing",
    "KICK_REASON_HACKING": "Hacking",
    "KICK_REASON_ABUSE": "Abuse",
}


def decode_string(text):
    """Python model of the native GenericVotingHUD.decode_string."""
    value = ast.literal_eval(text)
    if isinstance(value[0], tuple):
        template = STRINGS.get(value[0][0], value[0][0])
        localise = value[0][1]
    else:
        template = STRINGS.get(value[0], value[0])
        localise = []
    args = ()
    for index in range(len(value[1])):
        arg = value[1][index]
        if index in localise:
            arg = STRINGS.get(arg, arg)
        args += (arg,)
    return template.format(*args)


class FakeServer:
    def __init__(self):
        self.sent = []
        self.players = {}
        self.connections = {}

    def broadcast(self, data):
        self.sent.append(bytes(data))


class FakePlayer:
    def __init__(self, pid, name):
        self.id = pid
        self.name = name
        self.disconnected = None
        self.connection = SimpleNamespace(in_game=True)

    def disconnect(self, reason=0):
        self.disconnected = reason


def _server(names):
    srv = FakeServer()
    for index, name in enumerate(names):
        player = FakePlayer(index, name)
        srv.players[index] = player
        srv.connections[index] = player.connection
    return srv


def _packets(srv):
    return [
        GenericVoteMessage(ByteReader(data[1:]))
        for data in srv.sent
        if data[0] == 47
    ]


def test_start_packet_exact_bytes():
    srv = _server(["Alice", "Bob", "Carol"])
    vm = voting.VoteManager(srv)
    assert vm.start_kick(srv.players[0], srv.players[1], voting.KICK_ABUSE, 100.0)

    def string(text):
        raw = text.encode("ascii")
        return raw + b"\x00"

    # id, starter id, START, 2 candidates, (name, votes)*, title, desc, flags
    expected = (
        b"\x2f\x00\x00\x02\x00"
        + string("('KICK_YES', ())") + (1).to_bytes(4, "little")
        + string("('KICK_NO', ())") + (0).to_bytes(4, "little")
        + string("('VOTE_TO_KICK_TITLE', ())")
        + string(
            "(('VOTE_TO_KICK_DESCRIPTION', (1,)), "
            "('Bob', 'KICK_REASON_ABUSE', 'Alice'))"
        )
        + b"\x05"  # allow_revote | can_vote << 2
    )
    assert srv.sent[0] == expected


def test_start_packet_decodes_to_stock_localised_text():
    srv = _server(["Alice", "Bob", "Carol"])
    vm = voting.VoteManager(srv)
    vm.start_kick(srv.players[0], srv.players[1], voting.KICK_HACKING, 100.0)
    start = _packets(srv)[0]

    assert start.message_type == voting.VOTE_START
    assert start.player_id == 0
    assert decode_string(start.title) == "Vote Kick"
    assert (
        decode_string(start.description)
        == "Vote to Kick Bob for Hacking? Vote initiated by Alice"
    )
    assert [decode_string(c["name"]) for c in start.candidates] == ["Yes", "No"]
    assert [c["votes"] for c in start.candidates] == [1, 0]
    assert start.can_vote and start.allow_revote


def test_update_keeps_texts_and_moves_the_tally():
    srv = _server(["Alice", "Bob", "Carol", "Dave", "Eve"])
    vm = voting.VoteManager(srv)
    vm.start_kick(srv.players[0], srv.players[1], voting.KICK_GRIEFING, 100.0)
    start = _packets(srv)[0]
    vm.cast_wire_candidate(srv.players[2], start.candidates[1]["name"])
    update = _packets(srv)[-1]

    assert update.message_type == voting.VOTE_UPDATE
    assert update.title == start.title
    assert update.description == start.description
    assert [c["votes"] for c in update.candidates] == [1, 1]
    # The starter may change their mind (allow_revote).
    vm.cast_wire_candidate(srv.players[0], start.candidates[1]["name"])
    assert [c["votes"] for c in _packets(srv)[-1].candidates] == [0, 2]


def test_passed_vote_result_title_and_kick_reason_disconnect():
    srv = _server(["Alice", "Bob", "Carol", "Dave"])
    vm = voting.VoteManager(srv)
    vm.start_kick(srv.players[0], srv.players[3], voting.KICK_GRIEFING, 100.0)
    vm.cast(srv.players[1], yes=True)
    closed = _packets(srv)[-1]

    assert closed.message_type == voting.VOTE_CLOSED
    assert not closed.can_vote
    assert closed.title == (
        "(('VOTE_KICK_SUCCESSFUL', (2,)), "
        "('Dave', 'Alice', 'KICK_REASON_GRIEFING'))"
    )
    assert decode_string(closed.title) == "Dave has been kicked by Alice for Griefing"
    # ERROR_KICK_GRIEFING: the stock client shows
    # "You have been kicked for Griefing until the end of the current match."
    assert srv.players[3].disconnected == 23


def test_reason_maps_to_retail_kick_disconnect_codes():
    assert voting.KICK_DISCONNECT_REASONS == {
        voting.KICK_GRIEFING: 23,
        voting.KICK_HACKING: 24,
        voting.KICK_ABUSE: 25,
    }
    for reason, code in voting.KICK_DISCONNECT_REASONS.items():
        srv = _server(["A", "B", "C"])
        vm = voting.VoteManager(srv)
        vm.start_kick(srv.players[0], srv.players[1], reason, 100.0)
        vm.cast(srv.players[2], yes=True)
        assert srv.players[1].disconnected == code


def test_failed_vote_result_title():
    srv = _server(["Alice", "Bob", "Carol", "Dave", "Eve", "Finn"])
    vm = voting.VoteManager(srv)
    vm.start_kick(srv.players[0], srv.players[1], voting.KICK_ABUSE, 100.0)
    vm.tick(now=100.0 + voting.VOTE_DURATION + 1)
    closed = _packets(srv)[-1]

    assert closed.message_type == voting.VOTE_CLOSED
    assert closed.title == "('VOTE_KICK_UNSUCCESSFUL', ('Alice', 'Bob'))"
    assert decode_string(closed.title) == "Alice's vote to kick Bob failed"
    assert srv.players[1].disconnected is None


def test_starter_cancel_result_title():
    srv = _server(["Alice", "Bob", "Carol"])
    vm = voting.VoteManager(srv)
    vm.start_kick(srv.players[0], srv.players[1], voting.KICK_ABUSE, 100.0)
    vm.cancel(by_starter=True)
    closed = _packets(srv)[-1]

    assert closed.title == "('VOTE_KICK_CANCELLED', ('Bob', 'Alice'))"
    assert decode_string(closed.title) == "Vote to kick Bob cancelled by Alice"


def test_target_leaving_closes_as_failed_with_captured_names():
    srv = _server(["Alice", "Bob", "Carol"])
    vm = voting.VoteManager(srv)
    vm.start_kick(srv.players[0], srv.players[1], voting.KICK_ABUSE, 100.0)
    del srv.players[1]
    vm.forget_player(1)
    closed = _packets(srv)[-1]

    assert closed.message_type == voting.VOTE_CLOSED
    assert decode_string(closed.title) == "Alice's vote to kick Bob failed"


def test_invalid_reason_does_not_open_a_ballot():
    srv = _server(["Alice", "Bob", "Carol"])
    vm = voting.VoteManager(srv)
    assert not vm.start_kick(srv.players[0], srv.players[1], voting.KICK_CANCEL, 100.0)
    assert not vm.start_kick(srv.players[0], srv.players[1], 99, 100.0)
    assert not vm.active and srv.sent == []


def test_names_are_python2_safe_literals():
    """Non-ASCII names need a u'' literal: the client is Python 2 and a UTF-8
    byte string formatted into a unicode template raises UnicodeDecodeError.
    Braces and quotes in names are plain format *arguments*, never parsed."""
    srv = _server(["José {0}", "O'Neil \"x\" \\", "C"])
    vm = voting.VoteManager(srv)
    vm.start_kick(srv.players[0], srv.players[1], voting.KICK_ABUSE, 100.0)
    start = _packets(srv)[0]

    assert start.description.isascii()
    assert "u'Jos\\xe9 {0}'" in start.description
    assert decode_string(start.description) == (
        "Vote to Kick O'Neil \"x\" \\ for Abuse? Vote initiated by José {0}"
    )


def test_retail_vote_text_shapes():
    assert voting._retail_vote_text("VOTE_TO_KICK_TITLE") == "('VOTE_TO_KICK_TITLE', ())"
    assert (
        voting._retail_vote_text("VOTE_KICK_CANCELLED", ("a",))
        == "('VOTE_KICK_CANCELLED', ('a',))"
    )
    assert (
        voting._retail_vote_text("X_Y", ("a", "KICK_NO"), localised=(1,))
        == "(('X_Y', (1,)), ('a', 'KICK_NO'))"
    )
    for bad in ((("a",), (1,)), (("a",), (-1,))):
        try:
            voting._retail_vote_text("X_Y", bad[0], localised=bad[1])
        except ValueError:
            continue
        raise AssertionError("out-of-range localised index accepted")


def test_vote_kicked_address_is_refused_until_the_match_ends():
    from server.voting import VoteManager

    manager = VoteManager.__new__(VoteManager)
    manager._match_kicks = {"10.0.0.5": 25}
    assert manager.match_kick_reason("10.0.0.5") == 25
    assert manager.match_kick_reason("10.0.0.6") is None
    manager.clear_match_kicks()
    assert manager.match_kick_reason("10.0.0.5") is None


# ---------------------------------------------------------------------------
# Retail kick denials: LocalisedMessage(50) to the starter, never silence.
# KickVotePlayerSelect.packet_received (hud.pyd) closes the menu 0.5 s after
# VOTE_TOO_SOON / VOTE_IN_PROGRESS / FOR_SPECTATOR / NOT_ENOUGH_PLAYERS.
# ---------------------------------------------------------------------------

from shared.packet import LocalisedMessage  # noqa: E402


class TeamPlayer(FakePlayer):
    def __init__(self, pid, name, team):
        super().__init__(pid, name)
        self.team = team
        self.outbox = []

    def send(self, data, reliable=True):
        self.outbox.append(bytes(data))


def _team_server(teams):
    srv = FakeServer()
    for index, team in enumerate(teams):
        player = TeamPlayer(index, f"P{index}", team)
        srv.players[index] = player
        srv.connections[index] = player.connection
    return srv


def _denials(player):
    return [
        (packet.string_id, list(packet.parameters))
        for packet in (
            LocalisedMessage(ByteReader(data[1:]))
            for data in player.outbox
            if data[0] == 50
        )
    ]


def test_spectator_starter_is_denied_with_retail_string():
    srv = _team_server([0, 2, 2, 2])
    vm = voting.VoteManager(srv)
    assert not vm.start_kick(srv.players[0], srv.players[1], voting.KICK_ABUSE, 100.0)
    assert _denials(srv.players[0]) == [("KICK_DENIED_FOR_SPECTATOR", [])]
    assert srv.sent == []


def test_self_kick_is_denied_with_retail_string():
    srv = _team_server([2, 2, 2])
    vm = voting.VoteManager(srv)
    assert not vm.start_kick(srv.players[0], srv.players[0], voting.KICK_ABUSE, 100.0)
    assert _denials(srv.players[0]) == [("KICK_DENIED_REASON_SELF_KICK", [])]


def test_vote_in_progress_is_denied_to_the_second_starter():
    srv = _team_server([2, 2, 2, 3, 3, 3])
    vm = voting.VoteManager(srv)
    assert vm.start_kick(srv.players[0], srv.players[3], voting.KICK_ABUSE, 100.0)
    assert not vm.start_kick(srv.players[4], srv.players[1], voting.KICK_ABUSE, 101.0)
    assert _denials(srv.players[4]) == [("KICK_DENIED_REASON_VOTE_IN_PROGRESS", [])]
    assert _denials(srv.players[0]) == []


def test_not_enough_players_on_the_starters_team():
    srv = _team_server([2, 2, 3, 3, 3])
    vm = voting.VoteManager(srv)
    assert not vm.start_kick(srv.players[0], srv.players[2], voting.KICK_ABUSE, 100.0)
    assert _denials(srv.players[0]) == [("KICK_NOT_ENOUGH_PLAYERS", [])]
    # Three on the starter's team is enough.
    assert vm.start_kick(srv.players[2], srv.players[0], voting.KICK_ABUSE, 100.0)


def test_bots_do_not_make_a_team_big_enough_to_kick():
    # A lone human with bot teammates gets the retail denial instead of the
    # old silence (bots cannot vote, and the bot target was ignored).
    srv = _team_server([2, 2, 2, 3, 3, 3])
    for pid in (1, 2, 4, 5):
        srv.players[pid].is_bot = True
    vm = voting.VoteManager(srv)
    assert not vm.start_kick(srv.players[0], srv.players[4], voting.KICK_ABUSE, 100.0)
    assert _denials(srv.players[0]) == [("KICK_NOT_ENOUGH_PLAYERS", [])]
    assert srv.sent == []


def test_retail_cooldown_is_300_seconds_with_the_remaining_count():
    assert voting.VOTE_COOLDOWN == 300.0
    srv = _team_server([2, 2, 2, 3, 3, 3])
    vm = voting.VoteManager(srv)
    assert vm.start_kick(srv.players[0], srv.players[3], voting.KICK_ABUSE, 100.0)
    vm.tick(now=100.0 + voting.VOTE_DURATION + 1)  # fails, closes
    assert not vm.active
    assert not vm.start_kick(srv.players[0], srv.players[4], voting.KICK_ABUSE, 160.0)
    assert _denials(srv.players[0]) == [
        ("KICK_DENIED_REASON_VOTE_TOO_SOON", ["240"])
    ]
    # Another starter is not affected by player 0's cooldown.
    assert vm.start_kick(srv.players[1], srv.players[4], voting.KICK_ABUSE, 160.0)
    vm.cancel(by_starter=True, now=161.0)
    assert vm.start_kick(srv.players[0], srv.players[4], voting.KICK_ABUSE, 400.0)


def test_cancelled_vote_waits_the_retail_45_seconds():
    assert voting.CANCELLED_VOTE_COOLDOWN == 45.0
    srv = _team_server([2, 2, 2, 3, 3, 3])
    vm = voting.VoteManager(srv)
    assert vm.start_kick(srv.players[0], srv.players[3], voting.KICK_ABUSE, 100.0)
    vm.cancel(by_starter=True, now=110.0)
    assert not vm.start_kick(srv.players[0], srv.players[3], voting.KICK_ABUSE, 150.0)
    assert _denials(srv.players[0]) == [
        ("KICK_DENIED_REASON_VOTE_TOO_SOON", ["5"])
    ]
    assert vm.start_kick(srv.players[0], srv.players[3], voting.KICK_ABUSE, 155.0)


def test_kick_host_is_denied_in_map_creator_sessions():
    srv = _team_server([2, 2, 2])
    srv.mode = SimpleNamespace(is_host=lambda player: player.id == 1)
    vm = voting.VoteManager(srv)
    assert not vm.start_kick(srv.players[0], srv.players[1], voting.KICK_ABUSE, 100.0)
    assert _denials(srv.players[0]) == [("KICK_DENIED_REASON_KICK_HOST", [])]


def test_cooldowns_follow_lobby_config():
    srv = _team_server([2, 2, 2, 3, 3, 3])
    srv.config = SimpleNamespace(
        votekick_cooldown_seconds=10.0,
        votekick_cancelled_cooldown_seconds=2.0,
        votekick_min_team_players=0,
    )
    vm = voting.VoteManager(srv)
    assert vm.start_kick(srv.players[0], srv.players[3], voting.KICK_ABUSE, 100.0)
    vm.tick(now=100.0 + voting.VOTE_DURATION + 1)
    assert vm.start_kick(srv.players[0], srv.players[3], voting.KICK_ABUSE, 111.0)
    vm.cancel(by_starter=True, now=112.0)
    assert vm.start_kick(srv.players[0], srv.players[3], voting.KICK_ABUSE, 114.0)
