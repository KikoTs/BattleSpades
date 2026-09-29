"""CTF objective freeze during the end screen and departed-carrier drops."""

import asyncio
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from modes.classic_ctf import ClassicCTFMode  # noqa: E402
from modes.ctf import CTFMode  # noqa: E402
from server.game_constants import TEAM1, TEAM2  # noqa: E402
from shared.packet import ChangePlayer, DropPickup, PickPickup, SetScore  # noqa: E402

from tests.test_ctf_entities import _Connection, _packets, _Server  # noqa: E402


def _player(server, player_id, team, pos, *, register=True):
    player = SimpleNamespace(
        id=player_id, name=f"P{player_id}", team=team,
        x=pos[0], y=pos[1], z=pos[2], vx=0.0, vy=0.0, vz=0.0,
        pickup_id=None, pickup_burdensome=False, pickup_state=None,
        _world_object=None, alive=True, spawned=True, captures=0, score=0,
        connection=None,
    )
    if register:
        server.players[player_id] = player
    return player


def _started(mode_class=CTFMode):
    server = _Server()
    mode = mode_class(server)
    asyncio.run(mode.on_mode_start())
    return server, mode


def test_ctf_carrier_cannot_capture_during_end_screen():
    server, mode = _started()
    carrier = _player(server, 7, TEAM1, mode.base_positions[TEAM1])
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    mode.ended = True
    before = (server.teams[TEAM1].score, carrier.score, carrier.captures)
    packets_before = len(server.packets)

    asyncio.run(mode.on_tick(1))

    assert (server.teams[TEAM1].score, carrier.score, carrier.captures) == before
    assert mode.intel_holder[TEAM2] is carrier
    assert len(server.packets) == packets_before


def test_ctf_no_pickup_or_touch_return_during_end_screen(monkeypatch):
    server, mode = _started()
    # A dropped Blue intel sits next to a Blue defender (touch-return) and a
    # Blue runner stands on the home Green intel (pickup).
    defender = _player(server, 1, TEAM1, (200.0, 210.0, 50.0))
    runner = _player(server, 2, TEAM1, mode.intel_positions[TEAM2])
    mode.intel_positions[TEAM1] = (200.0, 210.0, 50.0)
    mode.intel_drop_time[TEAM1] = 1.0
    mode.intel_return_on_touch = True
    mode.ended = True
    monkeypatch.setattr("modes.ctf.time.time", lambda: 10_000.0)

    asyncio.run(mode.on_tick(1))

    assert mode.intel_holder == {TEAM1: None, TEAM2: None}
    assert runner.pickup_id is None
    assert defender.score == 0
    assert mode.intel_drop_time[TEAM1] == 1.0  # neither touch- nor auto-returned
    assert not _packets(server, PickPickup)
    assert not _packets(server, SetScore)


def test_ctf_match_winning_capture_stops_the_rest_of_the_tick():
    server, mode = _started()
    mode.score_limit = 1
    blue = _player(server, 1, TEAM1, mode.base_positions[TEAM1])
    green = _player(server, 2, TEAM2, mode.base_positions[TEAM2])
    asyncio.run(mode._pickup_intel(blue, TEAM2))
    asyncio.run(mode._pickup_intel(green, TEAM1))

    async def fake_end(winner):
        mode.ended = True
        mode.winner = winner

    mode._end_by_score = fake_end

    asyncio.run(mode.on_tick(1))

    assert mode.ended and mode.winner == TEAM1
    assert server.teams[TEAM1].score == 1
    assert server.teams[TEAM2].score == 0
    assert green.captures == 0
    assert mode.intel_holder[TEAM1] is green


def test_ctf_departed_carrier_drops_without_player_bound_packets():
    server, mode = _started()
    carrier = _player(server, 7, TEAM1, (200.0, 210.0, 40.0))
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    # The id is removed from the roster (or already reused) before the queued
    # leave hook runs.
    del server.players[carrier.id]
    server.packets.clear()
    created = len(server.created)

    asyncio.run(mode.on_player_leave(carrier))

    assert not _packets(server, DropPickup)
    assert not _packets(server, ChangePlayer)
    assert mode.intel_holder[TEAM2] is None
    flag = server.entity_registry.get(mode._intel_entities[TEAM2])
    assert flag is not None and (flag.x, flag.y) == (200.0, 210.0)
    assert len(server.created) == created + 1
    assert mode.intel_drop_time[TEAM2] > 0.0
    assert carrier.pickup_id is None


def test_ctf_reused_id_does_not_receive_departed_carrier_packets():
    server, mode = _started()
    carrier = _player(server, 7, TEAM1, (200.0, 210.0, 40.0))
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    replacement = _player(server, 7, TEAM2, (0.0, 0.0, 0.0))
    assert server.players[7] is replacement
    server.packets.clear()

    asyncio.run(mode.on_player_leave(carrier))
    joiner = _Connection(replacement)
    mode.reveal_to(joiner)

    assert not _packets(server, DropPickup)
    assert not _packets(server, ChangePlayer)
    assert not [data for data in joiner.sent if data[0] == ChangePlayer.id]
    assert mode.intel_holder[TEAM2] is None


def test_ctf_connected_carrier_leave_still_sends_retail_drop():
    """Leave before PlayerLeft: the carrier is still rostered, so DropPickup
    and the marker clear are valid and must still be sent."""
    server, mode = _started()
    carrier = _player(server, 7, TEAM1, (200.0, 210.0, 40.0))
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    mode._tick_carriers(1000.0)
    mode._tick_carriers(1000.0 + float(C.INTEL_MINIMAP_EXPOSURE_TIME))
    server.packets.clear()

    asyncio.run(mode.on_player_leave(carrier))

    drops = _packets(server, DropPickup)
    assert len(drops) == 1 and drops[0].player_id == carrier.id
    marker = _packets(server, ChangePlayer)
    assert marker[-1].player_id == carrier.id
    assert marker[-1].high_minimap_visibility == 0


def test_ctf_restart_does_not_clear_marker_of_departed_holder():
    server, mode = _started()
    carrier = _player(server, 7, TEAM1, (200.0, 210.0, 40.0))
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    del server.players[carrier.id]
    server.packets.clear()

    asyncio.run(mode.on_mode_start())

    assert not [
        packet for packet in _packets(server, ChangePlayer)
        if packet.player_id == carrier.id
    ]
    assert mode.intel_holder == {TEAM1: None, TEAM2: None}


def test_ctf_team_change_from_spectator_side_releases_held_intel():
    server, mode = _started()
    carrier = _player(server, 7, TEAM2, (200.0, 210.0, 40.0))
    asyncio.run(mode._pickup_intel(carrier, TEAM1))
    # Pathological order: the carrier reaches the hook with an old team that
    # is not TEAM1 (e.g. spectator id), which used to compute the wrong holder.
    asyncio.run(mode.on_player_team_change(carrier, 0, TEAM1))

    assert mode.intel_holder[TEAM1] is None


def test_classic_ctf_also_freezes_objectives_during_end_screen():
    server, mode = _started(ClassicCTFMode)
    carrier = _player(server, 7, TEAM1, mode.base_positions[TEAM1])
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    carrier.score = 0  # the home-grab "First to Claim Flag" is paid before the end
    mode.ended = True

    asyncio.run(mode.on_tick(1))

    assert server.teams[TEAM1].score == 0
    assert carrier.score == 0
    assert int(C.INTEL_PICKUP) == carrier.pickup_id
