"""Disguise survives rejected actions and ends on an accepted attack."""

import asyncio

import pytest
import shared.constants as C
from protocol.packet_handler import PacketHandler
from tests.test_reversed_combat import DummyServer, make_player, make_shoot_packet
from server.game_constants import TEAM1
from shared.packet import DisguisePacket
from tests.test_equipment_handlers import _server_player


@pytest.mark.parametrize("earlier_action", ["walk", "jump"])
def test_disguise_activation_survives_older_buffered_movement(earlier_action):
    server, player, _ = _server_player(C.DISGUISE_TOOL, [C.DISGUISE_TOOL])
    idle = (False,) * 8
    old_action = list(idle)
    old_action[0 if earlier_action == "walk" else 4] = True
    player.record_input_frame(10, tuple(old_action), player.orientation)
    for loop in range(11, 15):
        player.record_input_frame(loop, idle, player.orientation)
    packet = DisguisePacket()
    packet.loop_count = 13
    packet.active = 1

    async def scenario():
        await PacketHandler(server).handle(player, bytes(packet.generate()))
        while player.input_history:
            await player._consume_input_frame(server, 1 / 60)
            assert player.disguised, player.last_applied_input_loop
        assert player.pack_state_flags() & 0x02
        assert player.disguise_stock == 1
        # Movement begun after the activation still breaks disguise.
        for loop in (15, 16):
            player.record_input_frame(loop, tuple(old_action), player.orientation)
            await player._consume_input_frame(server, 1 / 60)
        assert not player.disguised

    asyncio.run(scenario())


@pytest.mark.parametrize("label", [0, 0xFFFFFFFF, 100000])
def test_invalid_or_wrapped_activation_label_does_not_suppress_movement(label):
    server, player, _ = _server_player(C.DISGUISE_TOOL, [C.DISGUISE_TOOL])
    player._input_newest_label = 20
    player._applied_input_source_loop = 19
    player.input.up = True
    assert server.deployable_actions.set_disguise(player, active=True, loop_count=label)

    player._check_disguise_stationary()

    assert not player.disguised


def test_disguise_input_guard_expires_if_the_stream_stalls(monkeypatch):
    server, player, _ = _server_player(C.DISGUISE_TOOL, [C.DISGUISE_TOOL])
    player._input_newest_label = 20
    player._applied_input_source_loop = 18
    player.input.up = True
    assert server.deployable_actions.set_disguise(player, active=True, loop_count=20)
    deadline = player._disguise_pending_input[1]
    monkeypatch.setattr("server.player.time.monotonic", lambda: deadline - 0.01)
    player._check_disguise_stationary()
    assert player.disguised

    monkeypatch.setattr("server.player.time.monotonic", lambda: deadline + 0.01)
    player._check_disguise_stationary()

    assert not player.disguised


def test_stationary_disguise_persists_after_input_guard_expires(monkeypatch):
    server, player, _ = _server_player(C.DISGUISE_TOOL, [C.DISGUISE_TOOL])
    player._input_newest_label = 20
    player._applied_input_source_loop = 20
    assert server.deployable_actions.set_disguise(player, active=True, loop_count=20)
    deadline = player._disguise_pending_input[1]
    monkeypatch.setattr("server.player.time.monotonic", lambda: deadline + 60.0)
    player.input.crouch = True
    player.set_orientation_vector(0, 1, 0)
    for _ in range(180):
        player._check_disguise_stationary()
        assert player.pack_state_flags() & 0x02
    assert player.disguise_stock == 1


@pytest.mark.parametrize("rejection", ["origin", "empty", "cooldown", "wrong_tool"])
def test_rejected_shoot_packet_does_not_break_disguise(rejection):
    server = DummyServer()
    player, _ = make_player(server, 1, "Disguised", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    player.set_orientation_vector(0, 0, -1)
    packet = make_shoot_packet(player)
    if rejection == "origin":
        packet.x += 100
    elif rejection == "empty":
        player.ammo_clip = 0
    elif rejection == "cooldown":
        assert player.consume_shot()
    else:
        player.set_tool(C.DISGUISE_TOOL, raw=True)
    player.disguised = True
    ammo = player.ammo_clip

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.disguised
    assert player.pack_state_flags() & 0x02
    assert player.ammo_clip == ammo


def test_accepted_shot_breaks_disguise_even_when_it_misses():
    server = DummyServer()
    player, _ = make_player(server, 1, "Disguised", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    player.set_orientation_vector(0, 0, -1)
    packet = make_shoot_packet(player)
    player.disguised = True
    ammo = player.ammo_clip

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert not player.disguised
    assert not player.pack_state_flags() & 0x02
    assert player.ammo_clip == ammo - 1
