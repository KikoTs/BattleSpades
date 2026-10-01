"""VIP sudden-death dodges, departed-boss packets and end-screen freeze."""

import asyncio

from modes.vip import VIPPhase
from server.game_constants import TEAM1, TEAM2
from tests.test_vip import _Connection, _new_mode, _player, _visibility_packets


def _active_with_blue_sudden_death():
    server, mode = _new_mode()
    _player(server, 1, TEAM1)
    _player(server, 3, TEAM1)
    _player(server, 5, TEAM1)
    _player(server, 2, TEAM2)
    _player(server, 4, TEAM2)
    asyncio.run(mode.on_tick(1))
    assert mode.phase is VIPPhase.ACTIVE
    blue_vip = mode.vips[TEAM1]
    green_vip = mode.vips[TEAM2]
    blue_vip.alive = blue_vip.spawned = False
    asyncio.run(mode.on_player_death(blue_vip, green_vip, 0))
    assert mode.respawn_enabled[TEAM1] is False
    return server, mode, blue_vip, green_vip


def test_dead_sudden_death_player_cannot_switch_to_respawning_team():
    server, mode, blue_vip, _green_vip = _active_with_blue_sudden_death()
    guard = next(
        p for p in server.players.values()
        if p.team == TEAM1 and p is not blue_vip
    )
    guard.alive = guard.spawned = False
    asyncio.run(mode.on_player_death(guard, None, 0))

    assert mode.allows_team_change(guard, TEAM2) is False
    assert mode.allows_team_change(blue_vip, TEAM2) is False
    # The spectator hop would only defer the same revival.
    assert mode.allows_team_change(guard, 0) is False


def test_live_sudden_death_player_cannot_trade_for_a_respawn():
    server, mode, blue_vip, _green_vip = _active_with_blue_sudden_death()
    survivor = next(
        p for p in server.players.values()
        if p.team == TEAM1 and p is not blue_vip and p.alive
    )
    assert mode.allows_team_change(survivor, TEAM2) is False


def test_respawning_team_members_may_still_switch():
    server, mode, _blue_vip, green_vip = _active_with_blue_sudden_death()
    green_regular = next(
        p for p in server.players.values()
        if p.team == TEAM2 and p is not green_vip
    )
    assert mode.allows_team_change(green_regular, TEAM1) is True
    # A live boss can never switch.
    assert mode.allows_team_change(green_vip, TEAM1) is False


def test_team_changes_are_free_again_in_the_next_subround():
    server, mode, blue_vip, _green_vip = _active_with_blue_sudden_death()
    asyncio.run(mode._begin_round(reset_players=False))

    assert mode.respawn_enabled == {TEAM1: True, TEAM2: True}
    assert mode.allows_team_change(blue_vip, TEAM2) is True


def test_joiner_on_sudden_death_team_starts_dead():
    server, mode, _blue_vip, _green_vip = _active_with_blue_sudden_death()
    blue_joiner = _player(server, 9, TEAM1, alive=False)
    green_joiner = _player(server, 10, TEAM2, alive=False)

    assert mode.can_player_respawn(blue_joiner) is False
    assert mode.can_player_respawn(green_joiner) is True


def test_joiner_during_subround_reset_is_queued_for_the_reset_respawn():
    server, mode = _new_mode()
    _player(server, 1, TEAM1)
    _player(server, 2, TEAM2)
    asyncio.run(mode._begin_round(reset_players=True))
    assert mode.phase is VIPPhase.RESETTING
    joiner = _player(server, 7, TEAM1, alive=False)

    assert mode.can_player_respawn(joiner) is False
    asyncio.run(mode.on_player_join(joiner))
    asyncio.run(mode.on_player_join(joiner))  # never queued twice
    assert sum(1 for queued in mode._round_reset_queue if queued is joiner) == 1

    asyncio.run(mode._drain_round_reset(0.0))

    assert joiner.alive is True
    assert mode.phase is not VIPPhase.RESETTING


def test_departed_vip_gets_no_marker_packets():
    server, mode = _new_mode()
    _player(server, 1, TEAM1)
    _player(server, 3, TEAM1)
    _player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    blue_vip = mode.vips[TEAM1]
    server.players.pop(blue_vip.id)
    server.teams[TEAM1].remove_player(blue_vip)
    server.packets.clear()

    asyncio.run(mode.on_player_leave(blue_vip))
    mode._clear_vip_markers()

    # The crown moved to the remaining blue player; the departed id hears
    # nothing.
    assert mode.vips[TEAM1] is not blue_vip
    assert not [
        packet for packet in _visibility_packets(server.packets)
        if packet.player_id == blue_vip.id
    ]


def test_reused_id_never_inherits_departed_vip_marker():
    server, mode = _new_mode()
    _player(server, 1, TEAM1)
    _player(server, 3, TEAM1)
    _player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    blue_vip = mode.vips[TEAM1]
    server.players.pop(blue_vip.id)
    server.teams[TEAM1].remove_player(blue_vip)
    replacement = _player(server, blue_vip.id, TEAM2)
    server.packets.clear()

    asyncio.run(mode.on_player_leave(blue_vip))
    mode._clear_vip_markers()
    joining = _Connection()
    mode.reveal_to(joining)

    assert not [
        packet for packet in _visibility_packets(server.packets + joining.sent)
        if packet.player_id == replacement.id
    ]


def test_leave_before_player_left_excludes_the_leaver_from_elimination():
    """With leave hooks running before PlayerLeft the last survivor is still
    rostered and alive; the round must still end for the wiped team."""
    server, mode = _new_mode()
    _player(server, 1, TEAM1)
    _player(server, 3, TEAM1)
    _player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    blue_vip, green_vip = mode.vips[TEAM1], mode.vips[TEAM2]
    blue_guard = next(
        p for p in server.players.values() if p.team == TEAM1 and p is not blue_vip
    )
    blue_vip.alive = blue_vip.spawned = False
    asyncio.run(mode.on_player_death(blue_vip, green_vip, 0))
    assert mode.phase is VIPPhase.ACTIVE

    asyncio.run(mode.on_player_leave(blue_guard))  # still in server.players

    assert server.teams[TEAM2].score == 1
    assert mode.phase is VIPPhase.INTERMISSION
    if mode._round_task is not None:
        mode._round_task.cancel()


def test_deaths_during_end_screen_change_nothing():
    server, mode = _new_mode()
    _player(server, 1, TEAM1)
    _player(server, 3, TEAM1)
    _player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    blue_vip, green_vip = mode.vips[TEAM1], mode.vips[TEAM2]
    mode.ended = True  # e.g. the time limit expired mid-sub-round
    server.packets.clear()
    scores = (server.teams[TEAM1].score, server.teams[TEAM2].score, green_vip.score)

    for player in list(server.players.values()):
        if player.team == TEAM1:
            player.alive = player.spawned = False
            asyncio.run(mode.on_player_death(player, green_vip, 0))

    assert (server.teams[TEAM1].score, server.teams[TEAM2].score, green_vip.score) == scores
    assert mode.vip_alive[TEAM1] is True
    assert mode.phase is VIPPhase.ACTIVE
    assert mode._round_task is None
    assert server.packets == []
