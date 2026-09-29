"""server.hud_packets: MinimapBillboard(41)/Clear(42), POIFocus(18),
ProgressBar(65, disabled), TeamLockScore(81), TeamInfiniteBlocks(82)."""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

import pytest

import server.hud_packets as hp
from server.game_constants import TEAM1, TEAM2
from shared.bytes import ByteReader
from shared.packet import (
    MinimapBillboard,
    MinimapBillboardClear,
    POIFocus,
    SetScore,
    TeamInfiniteBlocks,
    TeamLockScore,
)


class FakeConn:
    def __init__(self, player_id, *, in_game=True, known=None, fail=False):
        self.in_game = in_game
        self.player = SimpleNamespace(id=player_id, team=TEAM1, is_bot=False)
        self.player.connection = self
        self.known_entity_ids = set(known or ())
        self.sent: list[bytes] = []
        self.fail = fail

    def send(self, data, reliable=True, prefix=0x30):
        if self.fail:
            raise RuntimeError("peer gone")
        self.sent.append(bytes(data))


class FakeTeam:
    def __init__(self, team_id, name):
        self.id = team_id
        self.name = name
        self.score = 0


def make_server(*conns):
    return SimpleNamespace(
        connections={i: c for i, c in enumerate(conns)},
        teams={TEAM1: FakeTeam(TEAM1, "Blue"), TEAM2: FakeTeam(TEAM2, "Green")},
    )


def decode(cls, data):
    assert data[0] == cls.id
    pkt = cls()
    pkt.read(ByteReader(data[1:]))
    return pkt


# --------------------------------------------------------------------------
# MinimapBillboard / Clear
# --------------------------------------------------------------------------


def test_add_billboard_wire_fields_and_in_game_filter():
    a, b, loading = FakeConn(0), FakeConn(1), FakeConn(2, in_game=False)
    server = make_server(a, b, loading)
    assert hp.add_billboard(
        server, hp.BILLBOARD_ID_BASE, 3, "minimap_bomb", (100.5, 200.25, 40.0),
        color=(255, 64, 0),
    )
    assert loading.sent == []
    for conn in (a, b):
        (data,) = conn.sent
        pkt = decode(MinimapBillboard, data)
        assert pkt.entity_id == hp.BILLBOARD_ID_BASE
        assert pkt.key == 3
        assert pkt.icon_name == "minimap_bomb"
        assert (pkt.x, pkt.y, pkt.z) == (100.5, 200.25, 40.0)
        assert pkt.color == (255, 64, 0)
        assert pkt.tracking == 0


def test_add_billboard_accepts_glm_like_and_int_color():
    conn = FakeConn(0)
    server = make_server(conn)
    pos = SimpleNamespace(x=1.0, y=2.0, z=3.0)
    assert hp.add_billboard(server, 9000, 0, "minimap_base", pos, color=0x00FF80)
    pkt = decode(MinimapBillboard, conn.sent[0])
    assert (pkt.x, pkt.y, pkt.z) == (1.0, 2.0, 3.0)
    assert pkt.color == (0, 255, 128)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(icon_name="no_such_icon"),
        dict(icon_name=""),
        dict(position=(1.0, math.nan, 2.0)),
        dict(position=(1.0, 2.0)),
        dict(position=None),
        dict(entity_id=0x8000),
        dict(key=256),
        dict(entity_id="x"),
    ],
)
def test_add_billboard_rejects_bad_input_without_sending(kwargs):
    conn = FakeConn(0)
    server = make_server(conn)
    args = dict(entity_id=9001, key=0, icon_name="minimap_bomb", position=(1, 2, 3))
    args.update(kwargs)
    assert not hp.add_billboard(
        server, args["entity_id"], args["key"], args["icon_name"], args["position"]
    )
    assert conn.sent == []
    state = getattr(server, "_hud_packets_state", None)
    assert state is None or not state.billboards


def test_icon_whitelist_covers_retail_minimap_art():
    for name in ("minimap_bomb", "minimap_base", "Minimap_Zombie", "marker_radar_station_16",
                 "tc_minimap_a", "minimap_intel", "minimap_diamond"):
        assert name in hp.BILLBOARD_ICONS


def test_tracking_billboard_only_reaches_peers_that_know_the_entity():
    knows, blind = FakeConn(0, known={5}), FakeConn(1, known=set())
    server = make_server(knows, blind)
    assert hp.add_billboard(server, 5, 0, "minimap_intel", (0, 0, 0), tracking=True)
    assert len(knows.sent) == 1 and blind.sent == []
    assert decode(MinimapBillboard, knows.sent[0]).tracking == 1


def test_readd_replaces_and_remove_forgets():
    conn = FakeConn(0)
    server = make_server(conn)
    hp.add_billboard(server, 9002, 0, "minimap_bomb", (1, 1, 1))
    hp.add_billboard(server, 9002, 0, "minimap_bomb", (2, 2, 2))
    assert list(server._hud_packets_state.billboards) == [9002]
    assert server._hud_packets_state.billboards[9002].position == (2.0, 2.0, 2.0)
    assert hp.remove_billboard(server, 9002)
    assert decode(MinimapBillboardClear, conn.sent[-1]).entity_id == 9002
    assert not server._hud_packets_state.billboards


def test_reveal_billboards_to_late_joiner_respects_audience():
    a, b = FakeConn(0), FakeConn(1)
    server = make_server(a, b)
    hp.add_billboard(server, 9003, 0, "minimap_bomb", (1, 1, 1))  # everyone
    hp.add_billboard(server, 9004, 0, "minimap_base", (2, 2, 2), recipients=[a.player])
    joiner = FakeConn(0)  # reuses player id 0 (the addressed player)
    other = FakeConn(7)
    server.connections[5] = joiner
    server.connections[6] = other
    assert hp.reveal_billboards(server, joiner) == 2
    assert hp.reveal_billboards(server, other) == 1
    assert decode(MinimapBillboard, other.sent[0]).entity_id == 9003


def test_partial_remove_then_clear_all():
    a, b = FakeConn(0), FakeConn(1)
    server = make_server(a, b)
    hp.add_billboard(server, 9005, 0, "minimap_bomb", (1, 1, 1))
    hp.remove_billboard(server, 9005, recipients=[a])
    assert server._hud_packets_state.billboards[9005].player_ids == frozenset({1})
    assert decode(MinimapBillboardClear, a.sent[-1]).entity_id == 9005
    assert len(b.sent) == 1
    hp.clear_all_billboards(server)
    assert not server._hud_packets_state.billboards
    assert decode(MinimapBillboardClear, b.sent[-1]).entity_id == 9005


def test_send_failures_are_swallowed():
    bad, good = FakeConn(0, fail=True), FakeConn(1)
    server = make_server(bad, good)
    assert hp.add_billboard(server, 9006, 0, "minimap_bomb", (1, 1, 1))
    assert len(good.sent) == 1
    assert hp.poi_focus(server, (1, 2, 3))


def test_server_without_connections_is_a_safe_noop():
    server = SimpleNamespace()
    assert hp.add_billboard(server, 9007, 0, "minimap_bomb", (1, 1, 1))
    assert hp.remove_billboard(server, 9007)
    assert hp.reveal_billboards(server, None) == 0


# --------------------------------------------------------------------------
# POIFocus
# --------------------------------------------------------------------------


def test_poi_focus_encodes_target_for_selected_recipients():
    a, b = FakeConn(0), FakeConn(1)
    server = make_server(a, b)
    assert hp.poi_focus(server, (388.5, 252.5, 232.75), recipients=[b.player])
    assert a.sent == []
    pkt = decode(POIFocus, b.sent[0])
    assert (pkt.target_x, pkt.target_y, pkt.target_z) == (388.5, 252.5, 232.75)
    assert not hp.poi_focus(server, (math.inf, 0, 0))


# --------------------------------------------------------------------------
# ProgressBar (disabled: crashes the stock client on draw)
# --------------------------------------------------------------------------


def test_progress_bar_is_never_sent():
    conn = FakeConn(0)
    server = make_server(conn)
    assert hp.PROGRESS_BAR_SUPPORTED is False
    assert hp.set_progress(server, None, 0.5, 0.1, color=(0, 255, 0)) is False
    assert hp.clear_progress(server, None) is False
    assert conn.sent == []


# --------------------------------------------------------------------------
# TeamLockScore / TeamInfiniteBlocks
# --------------------------------------------------------------------------


def test_team_lock_score_sets_flag_and_resyncs_on_unlock():
    conn = FakeConn(0)
    server = make_server(conn)
    assert hp.set_team_lock_score(server, TEAM1, True)
    assert server.teams[TEAM1].locked_score is True
    assert hp.team_score_locked(server, TEAM1)
    pkt = decode(TeamLockScore, conn.sent[0])
    assert (pkt.team_id, pkt.locked) == (TEAM1, 1)
    server.teams[TEAM1].score = 4
    assert hp.set_team_lock_score(server, server.teams[TEAM1], False)
    unlock, resync = conn.sent[1:]
    assert decode(TeamLockScore, unlock).locked == 0
    score = decode(SetScore, resync)
    assert (score.type, score.specifier, score.value) == (0, TEAM1, 4)


def test_team_infinite_blocks_flag_and_packet():
    conn = FakeConn(0)
    server = make_server(conn)
    assert hp.set_team_infinite_blocks(server, TEAM2, True)
    assert hp.team_infinite_blocks(server, TEAM2)
    assert not hp.team_infinite_blocks(server, TEAM1)
    pkt = decode(TeamInfiniteBlocks, conn.sent[0])
    assert (pkt.team_id, pkt.infinite_blocks) == (TEAM2, 1)


def test_team_rules_reject_unknown_team_and_replay_to_joiner():
    conn = FakeConn(0)
    server = make_server(conn)
    assert not hp.set_team_lock_score(server, 99, True)
    assert not hp.set_team_infinite_blocks(server, "nope", True)
    assert conn.sent == []
    hp.set_team_lock_score(server, TEAM1, True)
    hp.set_team_infinite_blocks(server, TEAM2, True)
    joiner = FakeConn(3)
    server.connections[9] = joiner
    assert hp.reveal_hud_state(server, joiner) == 2
    assert decode(TeamLockScore, joiner.sent[0]).team_id == TEAM1
    assert decode(TeamInfiniteBlocks, joiner.sent[1]).team_id == TEAM2


# --------------------------------------------------------------------------
# admin commands
# --------------------------------------------------------------------------


def _run_command(server, text):
    from commands import handle_command

    player = SimpleNamespace(id=0, name="admin", admin=True, messages=[])
    player.send = lambda data, *a, **k: player.messages.append(bytes(data))
    server.config = SimpleNamespace(log_commands=False)
    asyncio.run(handle_command(server, player, text))
    return player


def test_lockscore_and_infiniteblocks_commands():
    conn = FakeConn(0)
    server = make_server(conn)
    _run_command(server, "lockscore 1 on")
    assert hp.team_score_locked(server, TEAM1) and not hp.team_score_locked(server, TEAM2)
    _run_command(server, "infiniteblocks all on")
    assert hp.team_infinite_blocks(server, TEAM1) and hp.team_infinite_blocks(server, TEAM2)
    _run_command(server, "infblocks green off")
    assert not hp.team_infinite_blocks(server, TEAM2)
    before = list(conn.sent)
    player = _run_command(server, "lockscore bogus on")
    assert conn.sent == before and player.messages


def test_team_rule_commands_are_admin_only():
    from commands.command_handler import get_command

    assert get_command("lockscore").admin_only
    assert get_command("infiniteblocks").admin_only


# --------------------------------------------------------------------------
# Demolition airstrike focus
# --------------------------------------------------------------------------


def test_demolition_airstrike_focuses_team_players_only():
    from modes.demolition import DemolitionMode

    blue, green, spec = FakeConn(0), FakeConn(1), FakeConn(2)
    green.player.team = TEAM2
    spec.player.team = -1
    bot = SimpleNamespace(id=3, team=TEAM1, is_bot=True, connection=FakeConn(3))
    server = make_server(blue, green, spec)
    server.players = {0: blue.player, 1: green.player, 2: spec.player, 3: bot}
    mode = SimpleNamespace(
        server=server,
        base_zones={
            TEAM1: SimpleNamespace(center=(120.5, 252.5, 227.75)),
            TEAM2: SimpleNamespace(center=(388.5, 252.5, 232.75)),
        },
    )
    DemolitionMode._focus_airstrike(mode, (TEAM2,))
    for conn in (blue, green):
        pkt = decode(POIFocus, conn.sent[0])
        assert (pkt.target_x, pkt.target_y) == (388.5, 252.5)
    assert spec.sent == [] and bot.connection.sent == []

    blue.sent.clear(), green.sent.clear()
    DemolitionMode._focus_airstrike(mode, (TEAM1, TEAM2))
    # A draw: each team watches the base it destroyed.
    assert decode(POIFocus, blue.sent[0]).target_x == 388.5
    assert decode(POIFocus, green.sent[0]).target_x == 120.5
