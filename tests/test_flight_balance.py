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
    (66, 22.5, 195, 200), (67, 90.0, 615, 620), (68, 24.0, 735, 740),
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


# ---------------------------------------------------------------------------
# BSFP v2 (2026-10-01): Engineer flight speed and a canopy that does not stall
# a slow fall. Stock owners, bots and v1 natives keep the world.pyd literals.
# ---------------------------------------------------------------------------
from server.flight_profile import (  # noqa: E402
    BALANCED_FLIGHT_V2, CAPABILITY_V2, PROFILE_MAGIC_V2, canopy_vz_step,
    profile_for_capability, ticket_flight_capability,
)

RETAIL_TERMINAL = 0.05 * 32.0   # stock canopy: 1.6 blocks/s
# Engineer class accel 0.7 x InitialInfo speed scale (1.0 at default rules).
ENGINEER_ACCEL = 0.7
V2_TERMINAL = 0.15625 * 32.0    # BattleSpades canopy: 5 blocks/s


def test_v2_capability_selects_the_tuned_profile_and_v1_stays_unchanged():
    raw = bytes([105]) + struct.pack("<i", 3) + b"key"
    assert ticket_flight_capability(raw) == 0
    assert ticket_flight_capability(raw + CAPABILITY) == 1
    assert ticket_flight_capability(raw + CAPABILITY_V2) == 2
    assert ticket_has_flight_capability(raw + CAPABILITY_V2)
    assert not ticket_flight_capability(raw + CAPABILITY_V2 + b"x")
    assert profile_for_capability(0) is RETAIL_FLIGHT
    assert profile_for_capability(1) is BALANCED_FLIGHT
    assert profile_for_capability(2) is BALANCED_FLIGHT_V2
    assert BALANCED_FLIGHT.engineer_flight_accel == pytest.approx(0.1)
    assert BALANCED_FLIGHT.canopy_gravity_scale == pytest.approx(0.05)
    assert not BALANCED_FLIGHT.canopy_free_fall_floor

    async def run():
        connection = make_connection(DummyServer())
        connection.send_connection_data = lambda: asyncio.sleep(0)
        await connection.handle_pre_join_packet(raw + CAPABILITY_V2)
        assert connection.flight_profile is BALANCED_FLIGHT_V2
        assert connection.flight_profile_capable
        assert connection.steam_key == b"key"
    asyncio.run(run())


def test_v2_profile_wire_golden():
    # Also consumed by the native InitialInfo decoder regression.
    encoded = BALANCED_FLIGHT_V2.encode()
    assert encoded.startswith(PROFILE_MAGIC_V2)
    assert encoded.hex() == (
        "425346500207400080074002e0010005000500050001a000"
    )
    assert len(encoded) == 24


async def _engineer_cruise_blocks(profile, frames=60):
    """Horizontal blocks/second over ``frames`` of steady W+SPACE flight."""
    flow = FlightFlow(68)
    flow.connection.flight_profile = profile
    flow.player.set_position(100.5, 100.5, 120.0)
    for _ in range(240):  # ignite and reach horizontal terminal speed (tau 1 s)
        await flow.step(True, forward=True)
    start = flow.player.x
    for _ in range(frames):
        await flow.step(True, forward=True)
        assert flow.player._jetpack_physics_active and flow.player.airborne
    return (flow.player.x - start) * 60.0 / frames


@pytest.mark.parametrize("profile", [RETAIL_FLIGHT, BALANCED_FLIGHT])
def test_engineer_flight_speed_stays_retail_for_stock_and_v1_owners(profile):
    # Stock world.pyd: active Engineer air accel 0.1 x class accel 0.7, drag
    # 1+dt: terminal 0.07 native = 2.24 blocks/s (40% of the Engineer's
    # 5.6 blocks/s walk).
    blocks = asyncio.run(_engineer_cruise_blocks(profile))
    assert blocks == pytest.approx(ENGINEER_ACCEL * 0.1 * 32.0, rel=0.03)


def test_engineer_flies_at_ground_walking_speed_with_the_v2_profile():
    # 0.25 x 0.7 = 0.175 native = 5.6 blocks/s: the Engineer's own walking
    # speed (0.7 / ground friction 4) and 2.5x the stock flight speed.
    retail = asyncio.run(_engineer_cruise_blocks(RETAIL_FLIGHT))
    tuned = asyncio.run(_engineer_cruise_blocks(BALANCED_FLIGHT_V2))
    assert tuned == pytest.approx(ENGINEER_ACCEL * 0.25 * 32.0, rel=0.03)
    assert tuned == pytest.approx(ENGINEER_ACCEL / 4.0 * 32.0, rel=0.03)
    assert tuned / retail == pytest.approx(2.5, rel=0.03)


async def _canopy_descent(profile, free_fall_frames):
    """Soldier falls ``free_fall_frames`` from rest, then opens (Z) the chute.

    Returns (vz at deploy, vz after 1 s of canopy, blocks fallen in that 1 s).
    """
    flow = FlightFlow(72)
    flow.connection.flight_profile = profile
    flow.player.class_id = int(C.CLASS_SOLDIER)
    flow.player.loadout = [int(C.MINIGUN_TOOL), int(C.RPG_TOOL), 72]
    flow.player.spawn(100.5, 100.5, 0.0)
    flow.player.set_position(100.5, 100.5, 0.0)
    await flow.step(False)
    for _ in range(free_fall_frames):
        await flow.step(False)
    assert flow.player.airborne and not flow.player.parachute_active
    deploy_vz = flow.player.vz
    await flow.step(False, deploy=True)
    assert flow.player.parachute_active
    start = flow.player.z
    for _ in range(59):
        await flow.step(False, deploy=True)
        assert flow.player.parachute_active
    return deploy_vz, flow.player.vz, flow.player.z - start


def test_retail_canopy_opened_slowly_settles_at_1_6_blocks_per_second():
    deploy_vz, vz, fallen = asyncio.run(_canopy_descent(BALANCED_FLIGHT, 3))
    assert deploy_vz < 0.08  # ~2 blocks/s: the chute opens near the top
    # Stock canopy gravity 0.05: the fall settles at its 1.6 blocks/s terminal.
    assert vz * 32.0 == pytest.approx(RETAIL_TERMINAL, abs=0.25)
    assert fallen < 2.0


def test_v2_canopy_opened_slowly_reaches_5_blocks_per_second_almost_at_once():
    deploy_vz, vz, fallen = asyncio.run(_canopy_descent(BALANCED_FLIGHT_V2, 3))
    assert deploy_vz < 0.08  # ~2 blocks/s: the chute opens near the top
    assert vz * 32.0 == pytest.approx(V2_TERMINAL, rel=0.01)
    # Free fall up to the terminal takes ~10 frames; ~4.5 blocks in the first second.
    assert 4.0 < fallen < 5.0


@pytest.mark.parametrize("profile,terminal", [
    (BALANCED_FLIGHT, RETAIL_TERMINAL), (BALANCED_FLIGHT_V2, V2_TERMINAL),
])
def test_fast_deploy_still_brakes_with_the_stock_canopy_drag(profile, terminal):
    deploy_vz, vz, fallen = asyncio.run(_canopy_descent(profile, 50))
    assert deploy_vz > 0.5  # ~16+ blocks/s when the chute opens
    expected = deploy_vz
    for _ in range(60):
        expected = canopy_vz_step(expected, DT, 1.0, profile)
    # Above the canopy terminal both profiles are the stock canopy recurrence.
    assert vz == pytest.approx(expected, rel=0.01)
    assert terminal / 32.0 < vz < deploy_vz


def test_canopy_step_matches_the_stock_formula_above_terminal_and_floors_below():
    for vz in (0.2, 0.5, 1.0):
        stock = (vz + DT * 0.15625) / (1.0 + DT)
        assert canopy_vz_step(vz, DT, 1.0, BALANCED_FLIGHT_V2) == pytest.approx(stock)
    assert canopy_vz_step(0.0, DT, 1.0, BALANCED_FLIGHT_V2) == pytest.approx(DT / (1.0 + DT))
    assert canopy_vz_step(0.15, DT, 1.0, BALANCED_FLIGHT_V2) == pytest.approx(0.15625)
    assert canopy_vz_step(0.0, DT, 1.0, BALANCED_FLIGHT) == pytest.approx(DT * 0.05 / (1.0 + DT))


def test_v2_soldier_chute_lands_a_40_block_drop_safely_in_about_8_seconds():
    async def run():
        flow = FlightFlow(72)
        flow.connection.flight_profile = BALANCED_FLIGHT_V2
        flow.player.class_id = int(C.CLASS_SOLDIER)
        flow.player.loadout = [int(C.MINIGUN_TOOL), int(C.RPG_TOOL), 72]
        flow.player.spawn(100.5, 100.5, 19.75)
        initial_hp = flow.player.health
        await flow.step(False)
        await flow.step(False)
        await flow.step(False, deploy=True)
        assert flow.player.parachute_active
        for frame in range(1800):
            await flow.step(False, deploy=True)
            assert flow.player.last_fall_result <= 0
            if not flow.player.airborne:
                break
        assert 440 < frame < 540  # 40 blocks at 5 blocks/s (stock: ~1500)
        assert flow.player.health == initial_hp
    asyncio.run(run())
