"""Flight through real ClientData ingestion, authority ticks and owner rows."""

import asyncio
import struct
from types import SimpleNamespace

import pytest

from tests.test_reversed_world_update import (
    C, ClientData, PacketHandler, make_player, make_world_manager,
    tofixed_orientation,
)
from server.config import ServerConfig
from server.replication import ReplicationService
from shared.bytes import ByteReader
from shared.packet import WorldUpdate


DT = 1.0 / 60.0


class FlightFlow:
    def __init__(self, pack):
        self.server = SimpleNamespace(
            config=ServerConfig(), world_manager=make_world_manager(),
            players={}, connections={}, entities={}, rocket_turrets={},
            loop_count=0,
            metrics=SimpleNamespace(record_world_packet=lambda *_args: None),
        )
        self.player, self.connection = make_player(self.server)
        self.player.class_id = int(
            C.CLASS_ENGINEER if pack == 68 else C.CLASS_ROCKETEER
        )
        self.player.loadout = [int(C.RIFLE_TOOL), int(C.BLOCK_TOOL), pack]
        self.player.spawn(100.5, 100.5, 59.75)
        self.owner_rows = []
        self.owner_reliable = []
        self.observer_rows = []
        def send_owner(data, reliable=False, **_kwargs):
            self.owner_rows.append(data)
            self.owner_reliable.append(reliable)
        self.connection.send = send_owner
        observer = SimpleNamespace(
            in_game=True, player=None,
            send=lambda data, **_kwargs: self.observer_rows.append(data),
        )
        self.server.connections = {0: self.connection, 1: observer}
        self.server.replication = ReplicationService(self.server)
        self.server.build_world_update_data = (
            self.server.replication.build_world_update_data
        )
        self.handler = PacketHandler(self.server)

    async def step(self, held, forward=False, deploy=False):
        self.server.loop_count += 1
        raw = bytes([ClientData.id]) + struct.pack(
            "<IBBHHHBBBf", self.server.loop_count, self.player.id,
            int(C.RIFLE_TOOL), tofixed_orientation(1.0), 0, 0, 0,
            (0x10 if held else 0) | (0x01 if forward else 0), 0x80 if deploy else 0, 0.0,
        )
        await self.handler.handle(self.player, raw)
        await self.player.simulate_tick(DT)
        self.server.replication.broadcast_world_updates()
        return self.player.z

    async def advance(self, frames, held):
        return [await self.step(held) for _ in range(frames)]

    def received_rows(self, payloads):
        for payload in payloads:
            packet = WorldUpdate(ByteReader(payload[1:]))
            if self.player.id in packet.player_updates:
                yield packet.player_updates[self.player.id]


@pytest.mark.parametrize("pack", [66, 67, 68])
def test_ground_launch_keeps_physics_position_before_first_owner_row(pack):
    async def run():
        flow = FlightFlow(pack)
        # First ClientData is latched. The next frame launches before the
        # first 30 Hz owner row, while its only cached position is spawn.
        before_launch = await flow.step(True)
        launch = await flow.step(True)
        assert flow.player._world_object.jump_this_frame
        assert flow.player.airborne
        assert flow.player.vz < 0.0
        assert launch < before_launch
        # Rewinding to the exact spawn floor contact cancels lift here.
        assert await flow.step(True) < launch
        assert flow.player.airborne
        assert flow.player.vz < 0.0
    asyncio.run(run())


def test_rocket_ascends_and_glider_sustains_height_with_slower_fuel_drain():
    async def run():
        rocket, glide = FlightFlow(66), FlightFlow(67)
        rocket_z = await rocket.advance(75, True)
        glide_z = await glide.advance(180, True)
        assert rocket_z[-1] < rocket_z[0] - 10.0
        assert rocket.player.vz < -0.5
        # The glider starts with an ordinary jump then approaches level flight.
        # It must not land and consume most of its fuel while stuck on ground.
        assert all(z < 59.0 for z in glide_z[30:])
        assert max(glide_z[60:]) - min(glide_z[60:]) < 3.0
        assert abs(glide.player.vz) < 0.02
        assert glide.player.jetpack_fuel > rocket.player.jetpack_fuel
        assert glide.player.jetpack_active
        assert glide.player._world_object.jetpack_passive
        for payloads in (glide.owner_rows, glide.observer_rows):
            rows = list(glide.received_rows(payloads))
            assert any(row[7] & 0x0C == 0x0C for row in rows)
        assert all(
            row[7] & 0x08 == 0 for row in rocket.received_rows(rocket.owner_rows)
        )
    asyncio.run(run())


@pytest.mark.parametrize("pack", [66, 67, 68])
def test_flight_release_stops_lift_and_refills_without_resetting_velocity(pack):
    async def run():
        flow = FlightFlow(pack)
        await flow.advance(45, True)
        fuel = flow.player.jetpack_fuel
        assert flow.player.jetpack_active
        # One latched frame still consumes the held input; the next is key-up.
        await flow.step(False)
        before = flow.player.vz
        row_count = len(flow.owner_rows)
        await flow.step(False)
        assert not flow.player.jetpack_active
        assert not flow.player._jetpack_physics_active
        assert not flow.player._world_object.jetpack_passive
        assert flow.player.vz > before
        # Release is off the ordinary cadence and still arrives immediately,
        # with a truthful inactive/passive state and this consumed input ACK.
        assert len(flow.owner_rows) == row_count + 1
        released = list(flow.received_rows(flow.owner_rows[-1:]))[0]
        assert released[7] & 0x0C == 0
        assert released[4] == flow.player.last_applied_input_loop
        assert flow.owner_reliable[-1]
        fuel_after_release = flow.player.jetpack_fuel
        await flow.advance(20, False)
        assert flow.player.jetpack_fuel > fuel_after_release
        assert flow.player.jetpack_fuel > fuel - 1.5
    asyncio.run(run())


@pytest.mark.parametrize("pack", [66, 67, 68])
def test_owner_acknowledgements_continue_at_normal_cadence_during_flight(pack):
    async def run():
        flow = FlightFlow(pack)
        await flow.advance(70, True)
        stamps = [
            row[4] for row in flow.received_rows(flow.owner_rows)
            if row[7] & 0x04
        ]
        assert len(stamps) >= 7
        assert all(0 < b - a <= 7 for a, b in zip(stamps, stamps[1:]))
        assert flow.player.last_applied_input_loop - stamps[-1] <= 6
    asyncio.run(run())


@pytest.mark.parametrize("pack,min_range,max_range,min_frames,max_frames", [
    (66, 7.0, 10.0, 85, 90),
    (67, 50.0, 60.0, 330, 338),
    (68, 12.0, 18.0, 313, 322),
])
def test_source_fuel_budget_produces_full_range_without_reignition(
    pack, min_range, max_range, min_frames, max_frames,
):
    """Range baseline for our documented policy, not original-server proof."""
    async def run():
        flow = FlightFlow(pack)
        flow.player.set_position(100.5, 100.5, 100.0)
        origin = flow.player.x
        for frame in range(600):
            await flow.step(True, forward=True)
            if flow.player._jetpack_requires_release:
                break
        assert min_frames <= frame <= max_frames
        assert min_range < flow.player.x - origin < max_range
        assert flow.player.jetpack_fuel == 0.0
        # Exhaustion cannot use regenerated fuel to retrigger without release.
        for _ in range(120):
            await flow.step(True, forward=True)
        assert not flow.player.jetpack_active
        assert not flow.player._jetpack_physics_active
        assert flow.player._jetpack_requires_release
    asyncio.run(run())
