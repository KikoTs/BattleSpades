"""Commando parachute: loadout, deploy rules, owner handoff, damage, physics.

Policy and live measurements: docs/PARACHUTE.md.
"""

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

from aoslib.world import Player as WorldPlayer
from server import player as player_module
from server.class_data import get_loadout
from server.flight_profile import BALANCED_FLIGHT
from server.player import Player
from shared import constants as C


DT = 1.0 / 60.0
CHUTE = int(C.A370)
SOLDIER_LOADOUT = [int(C.MINIGUN_TOOL), int(C.RPG_TOOL), CHUTE]


def make_player() -> Player:
    """Connection-less player: no predicting owner, physics follows at once."""
    player = Player(id=1, name="Test", team=3, weapon=int(C.RIFLE_TOOL), connection=None)
    player.class_id = int(C.CLASS_SOLDIER)
    player.loadout = list(SOLDIER_LOADOUT)
    player.spawn(10.0, 10.0, 10.0)
    return player


def network_soldier(z=59.75):
    """Retail-owner player on the flat test map (ground at z=62)."""
    from tests.test_reversed_world_update import make_player as network_player

    player, connection = network_player()
    # Fall damage is gated on a server config; the bare test server has none.
    from server.config import ServerConfig

    connection.server.config = ServerConfig()
    player.class_id = int(C.CLASS_SOLDIER)
    player.loadout = list(SOLDIER_LOADOUT)
    player.spawn(100.5, 100.5, z)
    return player, connection


def press(player, *, jump=None, hover=None):
    if jump is not None:
        player.input.jump = bool(jump)
    if hover is not None:
        player.input.hover = bool(hover)
    player._update_parachute(DT)


def airborne_falling(player, vz=0.2):
    player.airborne = True
    player.wade = False
    player.vz = vz


# --------------------------------------------------------------------------
# Loadout and replication
# --------------------------------------------------------------------------


def test_commando_loadout_offers_normal_parachute():
    assert CHUTE in get_loadout(int(C.CLASS_SOLDIER)).equipment


def test_spawn_honors_commando_parachute_choice():
    player = make_player()

    assert player.parachute_id == CHUTE
    assert player.parachute_active is False
    assert player._parachute_physics_active is False


def test_active_parachute_is_replicated_in_world_update_state():
    player = make_player()
    player.parachute_active = True

    assert player.pack_state_flags() & 0x01


# --------------------------------------------------------------------------
# Deploy triggers
# --------------------------------------------------------------------------


def test_airborne_space_press_opens_retail_canopy():
    """Stock world.pyd keeps the airborne SPACE request for chute holders."""
    player = make_player()
    player.airborne = False
    press(player, jump=True)  # ordinary ground jump: never a deploy
    assert not player.parachute_active

    airborne_falling(player)
    press(player, jump=True)  # still the same held press
    assert not player.parachute_active
    press(player, jump=False)
    press(player, jump=True)
    assert player.parachute_active
    assert player._parachute_physics_active  # no owner: immediate physics


def test_hover_press_still_opens_for_patched_and_native_clients():
    player = make_player()
    airborne_falling(player)
    press(player, hover=True)
    assert player.parachute_active

    # The deploy key is not the UGC Builder hover state.  Passing it through
    # would skip gravity instead of applying the canopy's 0.05 multiplier.
    world = SimpleNamespace(
        set_walk=lambda *args: None,
        set_crouch=lambda *args: None,
    )
    player._apply_input_state_to_world(
        trigger_jump=False, world_object=world, collisions=[],
    )
    assert world.hover is False
    assert world.parachute_active is True


def test_falling_without_press_does_not_auto_deploy():
    player = make_player()
    airborne_falling(player, vz=0.8)
    for _ in range(30):
        press(player, jump=False, hover=False)
    assert player.parachute_active is False


def test_press_during_ascent_waits_for_descent():
    player = make_player()
    airborne_falling(player, vz=-0.3)
    press(player, jump=True)
    assert not player.parachute_active and player._parachute_deploy_pending
    press(player, jump=False)
    player.vz = -0.01
    press(player)
    assert not player.parachute_active
    player.vz = 0.0
    press(player)
    assert player.parachute_active and not player._parachute_deploy_pending


def test_native_owner_airborne_space_opens_like_retail():
    """Client parity P1-19: native owners deploy with SPACE mid-air too."""
    player = make_player()
    player.connection = SimpleNamespace(flight_profile=BALANCED_FLIGHT)
    airborne_falling(player)
    press(player, jump=True)
    assert player.parachute_active
    # The native client predicts the same edge in the same step.
    assert player._parachute_physics_active


def test_native_owner_z_stays_an_extra_binding():
    """Decision D3: the native Z/hover edge still deploys."""
    player = make_player()
    player.connection = SimpleNamespace(flight_profile=BALANCED_FLIGHT)
    airborne_falling(player)
    press(player, hover=True)
    assert player.parachute_active and player._parachute_physics_active


def test_native_owner_space_and_z_share_one_deploy_per_fall():
    player = make_player()
    player.connection = SimpleNamespace(flight_profile=BALANCED_FLIGHT)
    airborne_falling(player)
    press(player, jump=True)
    assert player.parachute_active
    player.vz = -0.2  # lifted: the canopy spills
    press(player, jump=False)
    assert not player.parachute_active
    player.vz = 0.2
    press(player, hover=True)
    press(player, jump=True)
    assert not player.parachute_active


def test_bot_space_is_locomotion_only():
    player = make_player()
    player.is_bot = True
    airborne_falling(player)
    press(player, jump=True)
    press(player, jump=False)
    press(player, jump=True)
    assert not player.parachute_active
    press(player, hover=True)
    assert player.parachute_active and player._parachute_physics_active


# --------------------------------------------------------------------------
# Anti-exploit rules
# --------------------------------------------------------------------------


def test_flat_ground_hop_cannot_open_a_canopy():
    player, _ = network_soldier()
    world = player._ensure_world_object()
    # Jump in place and press again at every frame of the arc.
    for _ in range(5):
        player.input.jump = True
        asyncio.run(player.update(DT))
        if player.airborne:
            break
    assert player.airborne
    pressed = True
    for _ in range(90):
        pressed = not pressed
        player.input.jump = pressed
        asyncio.run(player.update(DT))
        assert not player.parachute_active
        if not player.airborne:
            break
    assert player._parachute_deploy_pending is False
    assert not world.parachute_active


def test_clearance_gate_measures_feet_to_ground():
    player, _ = network_soldier(z=59.75 - 4.0)  # four blocks up
    airborne_falling(player)
    clearance = player._parachute_ground_clearance()
    assert clearance == pytest.approx(4.0, abs=0.01)
    press(player, jump=True)
    assert not player.parachute_active and player._parachute_deploy_pending

    player.set_position(100.5, 100.5, 59.75 - 7.0)
    airborne_falling(player)
    press(player, jump=True)  # still armed from the earlier press
    assert player.parachute_active


def test_single_deploy_per_fall_after_timeout_collapse(monkeypatch):
    monkeypatch.setattr(player_module, "PARACHUTE_MAX_OPEN_SECONDS", 0.5)
    player = make_player()
    airborne_falling(player)
    press(player, jump=True)
    assert player.parachute_active
    for _ in range(40):
        press(player, jump=False)
    assert not player.parachute_active
    assert player.last_parachute_event["reason"] == "timeout"
    press(player, jump=True)
    press(player, jump=False)
    press(player, jump=True)
    assert not player.parachute_active  # no reopening before landing

    player.airborne = False
    press(player, jump=False)
    airborne_falling(player)
    press(player, jump=True)
    assert player.parachute_active  # landing re-armed it


def test_canopy_spills_when_lifted():
    player = make_player()
    airborne_falling(player)
    press(player, jump=True)
    assert player.parachute_active
    player.vz = -0.3  # an explosion throws the player upward
    press(player, jump=False)
    assert not player.parachute_active
    assert player.last_parachute_event["reason"] == "lifted"


def test_no_jetpack_and_parachute_stacking():
    player = make_player()
    player.jetpack_id = 66
    airborne_falling(player)
    press(player, jump=True)
    press(player, hover=True)
    assert not player.parachute_active

    player.jetpack_id = 0
    press(player, jump=False, hover=False)
    press(player, jump=True)
    assert player.parachute_active
    player.jetpack_id = 67  # e.g. a jetpack crate picked up mid-air
    press(player, jump=False)
    assert not player.parachute_active


class _EngineWade:
    """Real world.Player with its held wade/airborne flags pinned for a test."""

    def __init__(self, real, *, wade, airborne):
        self._real = real
        self.wade = wade
        self.airborne = airborne

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_teleport_out_of_water_does_not_refuse_the_next_canopy():
    """set_position must not carry the old position's wade into a new fall.

    The native mover holds wade while airborne and only re-evaluates it on
    ground contact, so a player teleported out of water into the air kept
    wade=True and _update_parachute treated the fall as grounded (VR round 6
    parachute harness finding).
    """
    player, _ = network_soldier()
    engine = _EngineWade(player._world_object, wade=True, airborne=False)
    player._world_object = engine
    player._sync_cached_vectors()
    assert player.wade  # standing in water

    player.set_position(100.5, 100.5, 59.75 - 10.0)
    assert not player.wade
    engine.airborne = True  # the mover still holds wade=True in the air
    player._sync_cached_vectors()
    assert player.airborne and not player.wade
    player.vz = 0.2
    press(player, jump=True)
    assert player.parachute_active

    # Ground contact: the mover re-evaluates wade and the mask retires.
    engine.airborne = False
    player._sync_cached_vectors()
    assert player.wade
    engine.airborne = True
    player._sync_cached_vectors()
    assert player.wade  # a real airborne wade (jump from water) is kept


def test_water_closes_canopy():
    player = make_player()
    airborne_falling(player)
    press(player, jump=True)
    assert player.parachute_active
    player.wade = True
    press(player, jump=False)
    assert not player.parachute_active


def test_death_retires_canopy_before_next_replicated_row():
    player = make_player()
    player.parachute_active = True
    player._parachute_physics_active = True
    player._parachute_deploy_last_held = True

    player.die()

    assert not player.parachute_active
    assert not player._parachute_physics_active
    assert not player._parachute_deploy_last_held
    assert player.pack_state_flags() & 0x01 == 0


def test_unequipped_player_cannot_deploy_or_keep_a_canopy():
    player = make_player()
    player.parachute_id = 0
    airborne_falling(player)
    player.parachute_active = True
    press(player, hover=True)
    assert not player.parachute_active


# --------------------------------------------------------------------------
# Native physics and fall damage
# --------------------------------------------------------------------------


def test_world_parachute_matches_stock_gravity():
    normal = WorldPlayer(None)
    chute = WorldPlayer(None)
    for body in (normal, chute):
        body.set_position(100.5, 100.5, 100.0)
        body.update(DT, [])
        assert body.airborne is True
        body.set_velocity(1.0, 0.0, 0.0)
    chute.parachute = CHUTE
    chute.parachute_active = True

    normal.update(DT, [])
    chute.update(DT, [])

    # world.pyd Player.update @ 0x10012EFB: 0.05 * dt * gravity.
    expected_chute_vz = (DT * 1.0 * 0.05) / (1.0 + DT)
    assert chute.velocity.z == pytest.approx(expected_chute_vz, abs=1e-6)
    assert chute.velocity.z < normal.velocity.z


def _drop(player, frames=3000, press_at=None, press_clearance=None):
    """Run authority frames; press SPACE at a frame or at a clearance."""
    for frame in range(frames):
        down = False
        if press_at is not None and frame == press_at:
            down = True
        if press_clearance is not None and player.airborne:
            clearance = player._parachute_ground_clearance()
            if clearance is not None and clearance <= press_clearance:
                down = True
                press_clearance = None
        player.input.jump = down
        asyncio.run(player.update(DT))
        # Every frame carries an owner self row, as a 60 Hz retail link would.
        player.record_owner_anchor(frame, player.position)
        if frame > 2 and not player.airborne:
            return frame
    return frames


def test_long_fall_deployed_chute_has_retail_descent_and_no_damage():
    """0.05 gravity, ordinary drag: ~1.6 blocks/s; a braked landing is free."""
    player, _ = network_soldier(z=19.75)  # 40 blocks above standing contact
    frame = _drop(player, press_at=2)
    assert 1200 < frame < 1800  # ~25 s, not a 2 s free fall
    assert player.health == 100
    assert not player.parachute_active


def test_free_fall_damage_is_unchanged():
    player, _ = network_soldier(z=19.75)
    _drop(player)
    assert player.health < 10  # 40 blocks is near-lethal for Soldier


def test_last_moment_canopy_does_not_erase_fall_damage():
    """The native canopy zeroes fall distance every frame; authority does not
    let a canopy opened just before impact launder a 40-block fall."""
    reference, _ = network_soldier(z=19.75)
    _drop(reference)
    free_fall_damage = 100 - reference.health
    assert free_fall_damage > 90

    # Below the 6-block clearance the press is refused outright.
    too_late, _ = network_soldier(z=19.75)
    _drop(too_late, press_clearance=5.0)
    assert too_late.last_parachute_event is None
    assert too_late.health == reference.health

    # Just above it the canopy opens but brakes only ~6 blocks from ~28
    # blocks/s: the landing costs what a free fall to that speed costs.
    late, _ = network_soldier(z=19.75)
    _drop(late, press_clearance=6.5)
    assert late.last_parachute_event is not None
    assert 15 <= 100 - late.health < free_fall_damage

    # Opening with room to brake is what the item is for.
    early, _ = network_soldier(z=19.75)
    _drop(early, press_clearance=25.0)
    assert early.health == 100


def test_speed_damage_is_monotonic_and_zero_when_braked():
    player, _ = network_soldier(z=19.75)
    values = [
        player._parachute_speed_damage(DT, speed, True)
        for speed in (0.05, 0.3, 0.55, 0.65, 0.75, 0.85, 0.95)
    ]
    assert values[0] == 0 and values[1] == 0
    assert values == sorted(values)
    assert values[-1] == player.movement_profile.falling_damage_max_damage


def test_held_deploy_after_landing_does_not_protect_the_next_fall():
    async def run():
        player, _ = network_soldier()
        world = player._ensure_world_object()
        player.set_position(100.5, 100.5, 29.75)
        world.set_velocity(0.0, 0.0, 0.0)
        player.update_action_input(False, False, hover=True)
        await player.update(DT)
        assert player.airborne
        # Held Z from before the fall is not an edge.
        for _ in range(360):
            await player.update(DT)
            assert not player.parachute_active
            if player.last_fall_result > 0:
                break
        assert player.last_fall_result > 0
        assert player.pack_state_flags() & 0x01 == 0

    asyncio.run(run())


# --------------------------------------------------------------------------
# Retail owner handoff (WorldUpdate bit 0x01 -> owner physics)
# --------------------------------------------------------------------------


def test_retail_owner_physics_follows_the_queued_row():
    player, _ = network_soldier(z=19.75)
    player.last_applied_input_loop = 1000
    player.input_history = {}
    airborne_falling(player)
    press(player, jump=True)
    assert player.parachute_active
    assert not player._parachute_physics_active  # owner has not seen it

    player.record_owner_anchor(1000, (0.0, 0.0, 0.0))
    for label in (1001, 1002):
        player.last_applied_input_loop = label
        press(player, jump=False)
        assert not player._parachute_physics_active
    player.last_applied_input_loop = 1003  # measured onset: S + 3
    press(player, jump=False)
    assert player._parachute_physics_active


def test_handoff_counts_backlog_and_round_trip():
    player, connection = network_soldier()
    player.last_applied_input_loop = 500
    player.input_history = {}
    assert player._parachute_handoff_frames() == 3
    # Two newer labels already received: onset is at least N + 2.
    player.input_history = {501: None, 502: None}
    assert player._parachute_handoff_frames() == 4
    player.input_history = {}
    connection.peer = SimpleNamespace(roundTripTime=100)
    assert player._parachute_handoff_frames() == 3 + 6


def test_unsent_transition_reaches_physics_after_safety_window():
    player, _ = network_soldier(z=19.75)
    airborne_falling(player)
    press(player, jump=True)
    assert player.parachute_active
    for _ in range(player_module.PARACHUTE_UNSENT_HANDOFF_FRAMES + 3):
        press(player, jump=False)
    assert player._parachute_physics_active


def test_landing_closes_advertised_canopy_immediately_and_physics_after_handoff():
    player, _ = network_soldier()
    world = player._ensure_world_object()
    player.set_position(100.5, 100.5, 59.7)
    world.set_velocity(0.0, 0.0, 0.1)
    player.airborne = True
    player.parachute_active = True
    player._parachute_physics_active = True
    player._parachute_owner_state = True
    player._parachute_used_this_fall = True
    asyncio.run(player.update(DT))

    assert not player.airborne
    assert not player.parachute_active
    assert player.pack_state_flags() & 0x01 == 0
    # The retail owner still flies its canopy until the close row lands.
    assert player._parachute_physics_active
    player.record_owner_anchor(0, player.position)
    for _ in range(4):
        asyncio.run(player.update(DT))
    assert not player._parachute_physics_active
    assert not world.parachute_active


def test_batching_is_refused_while_a_handoff_is_pending():
    player = make_player()
    player._parachute_physics_schedule = deque([[5, True]])
    player.last_applied_input_loop = 1
    frame = SimpleNamespace(
        topology_version=None, movement_flags=(False,) * 8, action_flags=None,
    )
    player.input_history = {2: frame, 3: frame}
    server = SimpleNamespace(world_manager=None, world_mutations=None)
    assert not player._backlog_pair_safe(server)


def test_retail_clientdata_space_deploy_end_to_end():
    """Real ClientData -> authority -> owner/observer WorldUpdate rows."""
    from tests.test_flight_authority_flow import FlightFlow

    async def run():
        flow = FlightFlow(72)
        flow.player.class_id = int(C.CLASS_SOLDIER)
        flow.player.loadout = list(SOLDIER_LOADOUT)
        flow.player.spawn(100.5, 100.5, 19.75)
        for _ in range(3):
            await flow.step(False)
        assert flow.player.airborne
        await flow.step(True)  # SPACE edge in the air
        await flow.step(True)
        assert flow.player.parachute_active
        deployed_label = flow.player.last_applied_input_loop
        onset = None
        for _ in range(20):
            await flow.step(False)
            if onset is None and flow.player._parachute_physics_active:
                onset = flow.player.last_applied_input_loop
        assert onset is not None
        # Physics trails the first owner row carrying bit 0x01 by the
        # measured retail handoff, never precedes the advertisement.
        assert onset > deployed_label
        owner = list(flow.received_rows(flow.owner_rows))
        observer = list(flow.received_rows(flow.observer_rows))
        assert any(row[8] & 0x01 for row in owner)
        assert any(row[8] & 0x01 for row in observer)
        for _ in range(2400):
            await flow.step(False)
            if not flow.player.airborne:
                break
        assert not flow.player.airborne
        assert flow.player.health == 100
        for _ in range(8):
            await flow.step(False)
        assert not list(flow.received_rows(flow.owner_rows))[-1][8] & 0x01
        assert not flow.player._parachute_physics_active

    asyncio.run(run())
