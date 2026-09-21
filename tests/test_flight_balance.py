"""Local negotiated flight: finite longer travel, recharge and wire agreement."""
import asyncio
import struct
from types import SimpleNamespace

import pytest

from server.flight_profile import (
    BALANCED_FLIGHT, RETAIL_FLIGHT, CAPABILITY, PROFILE_MAGIC,
    ticket_has_flight_capability,
)
from shared.bytes import ByteReader
from shared.packet import SteamSessionTicket
from tests.test_jetpack import make_player, hold_jetpack, DT
from tests.test_parachute import make_player as make_soldier
from tests.test_flight_authority_flow import FlightFlow
from tests.test_reversed_spawn_handshake import DummyServer, make_connection
from server.config import ServerConfig
from server.builders import build_initial_info
import shared.constants as C


def tuned_player(pack):
    player = make_player()
    player.connection = SimpleNamespace(flight_profile=BALANCED_FLIGHT)
    player.jetpack_id = pack
    return player


def test_capability_is_explicit_and_outside_authentication_ticket():
    ticket = b"same-opaque-auth-key"
    raw = bytes([105]) + struct.pack("<i", len(ticket)) + ticket
    assert not ticket_has_flight_capability(raw)
    assert ticket_has_flight_capability(raw + CAPABILITY)
    decoded = SteamSessionTicket(ByteReader((raw + CAPABILITY)[1:]))
    assert decoded.ticket == ticket
    for malformed in (raw + CAPABILITY + b"x", raw + CAPABILITY[:-1], b"\x69\xff\xff\xff\xff" + CAPABILITY):
        assert not ticket_has_flight_capability(malformed)


def test_connection_selects_profile_before_sending_initial_info():
    async def run():
        for capable in (False, True):
            connection = make_connection(DummyServer())
            observed = []
            async def capture():
                observed.append(connection.flight_profile)
            connection.send_connection_data = capture
            raw = bytes([105]) + struct.pack("<i", 3) + b"key"
            await connection.handle_pre_join_packet(raw + (CAPABILITY if capable else b""))
            assert observed == [BALANCED_FLIGHT if capable else RETAIL_FLIGHT]
            assert connection.steam_key == b"key"
    asyncio.run(run())


def test_balanced_profile_wire_golden():
    # Also consumed by native InitialInfo decoder regression; fixed /64 units.
    assert BALANCED_FLIGHT.encode().hex() == "425346500103400080074002e001000500050005"
    assert len(BALANCED_FLIGHT.encode()) == 20
    assert BALANCED_FLIGHT.encode().startswith(PROFILE_MAGIC)


def test_initial_info_extension_only_reaches_negotiated_peers():
    async def run():
        server = DummyServer()
        server.config = ServerConfig()
        original = bytes(build_initial_info(server).generate())
        for capable in (False, True):
            connection = make_connection(server)
            connection.flight_profile_capable = capable
            connection.flight_profile = BALANCED_FLIGHT if capable else RETAIL_FLIGHT
            sent = []
            connection.send = sent.append
            await connection.send_info()
            assert sent == [original + (BALANCED_FLIGHT.encode() if capable else b"")]
    asyncio.run(run())


@pytest.mark.parametrize("pack,burn", [(66, 3.0), (67, 10.0), (68, 12.0)])
def test_extended_budget_exhausts_without_airborne_recharge_or_reignition(pack, burn):
    player = tuned_player(pack)
    player.airborne = True
    hold_jetpack(player, burn + 0.6)
    assert player._jetpack_requires_release
    assert player.jetpack_fuel == 0.0
    hold_jetpack(player, 15.0)
    assert not player.jetpack_active and player.jetpack_fuel == 0.0
    player.input.jump = False
    for _ in range(600):
        player._update_jetpack(DT)
    assert player.jetpack_fuel == 0.0
    # Release-tapping cannot generate fuel while airborne either.
    for frame in range(300):
        player.input.jump = frame % 40 < 25
        player._update_jetpack(DT)
    assert player.jetpack_fuel == 0.0
    assert not player.jetpack_active


@pytest.mark.parametrize("pack", [66, 67, 68])
def test_recharge_requires_landing_release_idle_and_damage_cooldown(pack, monkeypatch):
    player = tuned_player(pack)
    player.jetpack_fuel = 0.0
    player.airborne = False
    player.input.jump = True
    for _ in range(120):
        player._update_jetpack(DT)
    assert player.jetpack_fuel == 0.0
    player.input.jump = False
    for _ in range(59):
        player._update_jetpack(DT)
    assert player.jetpack_fuel == 0.0
    player._update_jetpack(DT)
    assert player.jetpack_fuel == pytest.approx(20.0 * DT)
    monkeypatch.setattr("server.player.time.time", lambda: 100.0)
    player._last_damage_at = 100.0
    for _ in range(180):
        player._update_jetpack(DT)
    assert player.jetpack_fuel == pytest.approx(20.0 * DT)
    monkeypatch.setattr("server.player.time.time", lambda: 103.0)
    for _ in range(300):
        player._update_jetpack(DT)
    assert player.jetpack_fuel == 100.0


def test_early_parachute_press_waits_for_descent_without_jump_boost():
    player = make_soldier()
    player.connection = SimpleNamespace(flight_profile=BALANCED_FLIGHT)
    player.airborne = True
    player.vz = -0.2
    player.input.hover = True
    player._update_parachute()
    assert not player.parachute_active and player._parachute_deploy_pending
    player.input.hover = False
    player.vz = -0.01
    player._update_parachute()
    assert not player.parachute_active
    player.vz = 0.0
    player._update_parachute()
    assert player.parachute_active and not player._parachute_deploy_pending
    player.airborne = False
    player._update_parachute()
    assert not player.parachute_active
    player.airborne = True
    player._update_parachute()
    assert not player.parachute_active


@pytest.mark.parametrize("pack,min_range,min_frames,max_frames", [
    (66, 25.0, 195, 200), (67, 100.0, 615, 620), (68, 30.0, 735, 740),
])
def test_longer_range_through_real_clientdata_and_replicated_fuel(pack, min_range, min_frames, max_frames):
    async def run():
        flow = FlightFlow(pack)
        flow.connection.flight_profile = BALANCED_FLIGHT
        flow.player.set_position(100.5, 100.5, 100.0)
        for frame in range(1000):
            await flow.step(True, forward=True)
            if flow.player._jetpack_requires_release:
                break
        assert min_frames <= frame <= max_frames
        assert flow.player.x - 100.5 > min_range
        assert flow.player.jetpack_fuel == 0.0
        for _ in range(30):
            await flow.step(True, forward=True)
        rows = list(flow.received_rows(flow.owner_rows))
        assert rows and not rows[-1][7] & 0x04
        assert not flow.player._jetpack_physics_active
    asyncio.run(run())


def test_soldier_chute_clientdata_to_safe_landing_and_replicated_close():
    async def run():
        flow = FlightFlow(72)
        flow.connection.flight_profile = BALANCED_FLIGHT
        flow.player.class_id = int(C.CLASS_SOLDIER)
        flow.player.loadout = [int(C.MINIGUN_TOOL), int(C.RPG_TOOL), 72]
        flow.player.spawn(100.5, 100.5, 19.75)
        initial_hp = flow.player.health
        await flow.step(False)
        await flow.step(False)
        assert flow.player.airborne
        await flow.step(False, deploy=True)
        assert flow.player.parachute_active
        for frame in range(1800):
            await flow.step(False, deploy=True)
            assert flow.player.last_fall_result <= 0
            if not flow.player.airborne:
                break
        assert 1300 < frame < 1700  # 40 blocks at the original ~1.6 blocks/s
        assert flow.player.health == initial_hp
        assert not flow.player.parachute_active
        for _ in range(8):
            await flow.step(False, deploy=True)
        rows = list(flow.received_rows(flow.owner_rows))
        assert any(row[8] & 1 for row in rows)
        assert not rows[-1][8] & 1
        # Keeping the key held through touchdown cannot open a second fall.
        flow.player.set_position(100.5, 100.5, 19.75)
        for _ in range(25):
            await flow.step(False, deploy=True)
            assert not flow.player.parachute_active
    asyncio.run(run())
