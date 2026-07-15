"""Characterization and safety gates for the isolated bot runtime."""

from __future__ import annotations

import asyncio
import random
import queue
import time
from dataclasses import replace
from types import SimpleNamespace

import shared.constants as C
from modes.zombie import ZombieMode, ZombiePhase
from shared.bytes import ByteReader
from shared.packet import CreatePlayer
from server.bot_ai.director import BotDirector
from server.bot_ai.director import _BotConnection
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import (
    BotAction,
    BotActionKind,
    BotIntent,
    BotProfile,
    EntitySnapshot,
    LookIntent,
    MapSnapshot,
    MovementAffordance,
    MovementIntent,
    PerceptionFrame,
    PlayerSnapshot,
    StimulusKind,
    VoxelChange,
)
from server.bot_ai.profiles import ProfileFactory
from server.bot_ai.supervisor import AIWorkerSupervisor
from server.bot_ai.stimuli import BotStimulusBus
from server.bot_ai.worker import BotBrain, WorkerVoxelWorld, _process_worker_batch
from server.game_constants import DEFAULT_WEAPON_TOOL
from server.config import ServerConfig
from server.main import BattleSpadesServer
from server.game_constants import TEAM1, TEAM2
from server.simulation_runtime import SimulationRuntime


def _profile() -> BotProfile:
    return BotProfile(
        name="TestBot",
        difficulty="normal",
        skill=0.6,
        aggression=0.5,
        caution=0.5,
        teamwork=0.5,
        creativity=0.5,
        reaction_time=0.0,
        tracking_delay=0.1,
        turn_speed=4.0,
        turn_acceleration=12.0,
        recoil_control=0.6,
        burst_discipline=0.6,
        preferred_range=20.0,
        aim_noise=0.05,
    )


def _player_snapshot(
    player_id: int,
    team: int,
    position: tuple[float, float, float],
    *,
    is_bot: bool = False,
) -> PlayerSnapshot:
    return PlayerSnapshot(
        player_id=player_id,
        generation=1,
        team=team,
        class_id=0,
        alive=True,
        spawned=True,
        position=position,
        eye=position,
        orientation=(1.0, 0.0, 0.0),
        velocity=(0.0, 0.0, 0.0),
        health=100,
        tool=DEFAULT_WEAPON_TOOL,
        blocks=50,
        ammo_clip=10,
        ammo_reserve=50,
        is_bot=is_bot,
    )


def _frame(
    frame_id: int,
    observer: PlayerSnapshot,
    enemy: PlayerSnapshot,
) -> PerceptionFrame:
    return PerceptionFrame(
        frame_id=frame_id,
        map_epoch=1,
        mode_epoch=1,
        topology_version=0,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=time.monotonic(),
        mode_id="tdm",
        players=(observer, enemy),
        profile=_profile(),
    )


def test_peerless_bot_connection_is_an_active_server_owned_player() -> None:
    connection = _BotConnection(SimpleNamespace())

    assert connection.in_game is True
    assert connection.peer is None
    assert connection.send(b"owner-only") is None


def test_spawned_bot_advertises_real_weapon_and_remote_display_bit() -> None:
    """Peerless bots must publish the same held-tool state as retail players."""

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    director = BotDirector(server, supervisor=SimpleNamespace())

    bot = asyncio.run(
        director.add_bot(
            team=TEAM1,
            name="VisibleBot",
            class_id=int(C.CLASS_SOLDIER),
        )
    )

    assert bot is not None
    assert bot.tool in bot.loadout
    assert bot.is_weapon_tool() is True
    assert bot.input.can_display_weapon is True
    snapshot = bot.world_update_snapshot()
    assert snapshot[7] & 0x10
    assert snapshot[9] == bot.tool


def test_active_zombie_bot_create_player_commits_native_variant_and_prefabs() -> None:
    """The first retail announcement must already describe the Zombie body."""

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    mode = ZombieMode(server)
    mode.phase = ZombiePhase.ACTIVE
    server.mode = mode
    broadcasts: list[bytes] = []
    server.broadcast = lambda data, *_args, **_kwargs: broadcasts.append(bytes(data))
    director = BotDirector(server, supervisor=SimpleNamespace())

    bot = asyncio.run(director.add_bot(team=TEAM1, name="WireZombie"))

    assert bot is not None
    create_data = next(
        data for data in broadcasts if data[0] == CreatePlayer.id
    )
    create = CreatePlayer(ByteReader(create_data[1:]))
    zombie_prefabs = tuple(
        C.PREFAB_LISTS[int(C.CLASS_PREFABS_ZOMBIE)]
    )
    assert bot.team == TEAM2
    assert bot.class_id in {
        int(C.CLASS_ZOMBIE),
        int(C.CLASS_FAST_ZOMBIE),
        int(C.CLASS_JUMP_ZOMBIE),
    }
    assert create.class_id == bot.class_id
    assert tuple(create.loadout) == tuple(bot.loadout)
    assert tuple(create.prefabs) == tuple(bot.prefabs) == zombie_prefabs
    assert int(C.ZOMBIEHAND_TOOL) in create.loadout
    assert int(C.ZOMBIE_PREFAB_TOOL) in create.loadout
    assert bot.tool == int(C.ZOMBIEHAND_TOOL)
    assert bot.world_update_snapshot()[9] == int(C.ZOMBIEHAND_TOOL)


def test_bot_primary_action_pulse_survives_one_replication_interval() -> None:
    """A 60 Hz bot melee pulse must be visible to the 30 Hz WorldUpdate."""

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    director = BotDirector(server, supervisor=SimpleNamespace())
    bot = asyncio.run(
        director.add_bot(
            team=TEAM1,
            name="MiningBot",
            class_id=int(C.CLASS_MINER),
        )
    )
    assert bot is not None
    runtime = director._runtime[bot.id]
    now = time.monotonic()
    runtime.intent = BotIntent(
        bot_id=bot.id,
        bot_generation=runtime.generation,
        frame_id=41,
        map_epoch=1,
        mode_epoch=1,
        topology_version=0,
        created_at=now,
        expires_at=now + 1.0,
        movement=MovementIntent(),
        action=BotAction(
            BotActionKind.MELEE,
            tool_id=int(C.SUPERSPADE_TOOL),
        ),
    )
    director.gateway.execute = lambda _player, _action: True

    server.loop_count = 100
    director._apply_motor(runtime, now, 1.0 / 60.0)
    assert bot.pack_action_flags() & 0x01
    assert runtime.action_primary_until_loop == 102

    for loop_count in (101, 102):
        server.loop_count = loop_count
        director._apply_motor(runtime, now + 0.01, 1.0 / 60.0)
        assert bot.pack_action_flags() & 0x01

    server.loop_count = 103
    director._apply_motor(runtime, now + 0.02, 1.0 / 60.0)
    assert not (bot.pack_action_flags() & 0x01)


def _facing_fixture(class_id: int = int(C.CLASS_MINER)):
    """Real server + one bot, oriented along +x with a settled aim motor."""

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    director = BotDirector(server, supervisor=SimpleNamespace())
    bot = asyncio.run(
        director.add_bot(team=TEAM1, name="FacingBot", class_id=class_id)
    )
    assert bot is not None
    runtime = director._runtime[bot.id]
    runtime.profile = _profile()
    bot.set_orientation_vector(1.0, 0.0, 0.0)
    runtime.motor.yaw = 0.0
    runtime.motor.pitch = 0.0
    runtime.motor.yaw_velocity = 0.0
    runtime.motor.pitch_velocity = 0.0
    runtime.motor.yaw_noise = 0.0
    runtime.motor.pitch_noise = 0.0
    return server, director, bot, runtime


def _melee_intent(bot, runtime, now, *, target, expires, visible=False,
                  position=None, tool_id=int(C.SUPERSPADE_TOOL)):
    return BotIntent(
        bot_id=bot.id,
        bot_generation=runtime.generation,
        frame_id=7,
        map_epoch=1,
        mode_epoch=1,
        topology_version=0,
        created_at=now,
        expires_at=expires,
        movement=MovementIntent(),
        look=LookIntent(target, visible=visible),
        action=BotAction(
            BotActionKind.MELEE,
            tool_id=tool_id,
            position=position,
        ),
    )


def test_melee_dig_waits_for_aim_convergence_before_executing() -> None:
    """A dig latched while facing 90 degrees away must not swing early."""

    server, director, bot, runtime = _facing_fixture()
    eye = tuple(float(value) for value in bot.eye)
    target = (eye[0], eye[1] + 3.0, eye[2])
    now = time.monotonic()
    runtime.intent = _melee_intent(
        bot, runtime, now, target=target, expires=now + 2.0, position=target
    )
    executed: list[tuple[float, float, float]] = []
    director.gateway.execute = lambda player, _action: (
        executed.append(
            (float(player.o_x), float(player.o_y), float(player.o_z))
        )
        or True
    )

    director._apply_motor(runtime, now, 1.0 / 60.0)

    assert executed == []
    assert runtime.pending_action is not None

    for tick in range(1, 120):
        director._apply_motor(runtime, now + tick / 60.0, 1.0 / 60.0)

    assert len(executed) == 1
    o_x, o_y, o_z = executed[0]
    dx = target[0] - eye[0]
    dy = target[1] - eye[1]
    dz = target[2] - eye[2]
    distance = (dx * dx + dy * dy + dz * dz) ** 0.5
    cos_error = (o_x * dx + o_y * dy + o_z * dz) / distance
    import math

    tolerance = math.cos(math.atan2(0.45, max(distance, 0.75)) + 0.03)
    assert cos_error >= tolerance - 1e-6
    assert runtime.pending_action is None


def test_unconverged_pending_action_expires_without_firing() -> None:
    """A bot too slow to face the target must drop the swing, not misfire."""

    server, director, bot, runtime = _facing_fixture()
    runtime.profile = replace(_profile(), turn_speed=0.3, turn_acceleration=1.0)
    eye = tuple(float(value) for value in bot.eye)
    target = (eye[0], eye[1] + 3.0, eye[2])
    now = time.monotonic()
    runtime.intent = _melee_intent(
        bot, runtime, now, target=target, expires=now + 0.25, position=target
    )
    executed: list[object] = []
    director.gateway.execute = lambda _player, action: (
        executed.append(action) or True
    )

    for tick in range(0, 60):
        director._apply_motor(runtime, now + tick / 60.0, 1.0 / 60.0)

    assert executed == []
    assert runtime.pending_action is None


def test_aligned_player_melee_executes_on_the_arrival_tick() -> None:
    """Positionless melee (claws) already aligned keeps its native cadence."""

    server, director, bot, runtime = _facing_fixture(
        class_id=int(C.CLASS_SOLDIER)
    )
    eye = tuple(float(value) for value in bot.eye)
    target = (eye[0] + 4.0, eye[1], eye[2] + 1.0)
    now = time.monotonic()
    runtime.intent = _melee_intent(
        bot,
        runtime,
        now,
        target=target,
        expires=now + 1.0,
        visible=True,
        position=None,
        tool_id=int(C.SPADE_TOOL),
    )
    executed: list[object] = []
    director.gateway.execute = lambda _player, action: (
        executed.append(action) or True
    )

    director._apply_motor(runtime, now, 1.0 / 60.0)

    assert len(executed) == 1
    assert runtime.pending_action is None


def _fire_intent(bot, runtime, now, *, frozen, target_player, expires,
                 frame_id=7, action_kind=BotActionKind.FIRE):
    return BotIntent(
        bot_id=bot.id,
        bot_generation=runtime.generation,
        frame_id=frame_id,
        map_epoch=1,
        mode_epoch=1,
        topology_version=0,
        created_at=now,
        expires_at=expires,
        movement=MovementIntent(),
        look=LookIntent(
            frozen,
            visible=True,
            target_player_id=int(target_player.id),
            target_generation=int(target_player.bot_generation),
            aim_offset_z=0.0,
        ),
        action=BotAction(action_kind, tool_id=int(bot.tool)),
    )


def _lock(runtime, target_player, now) -> None:
    runtime.lock_player_id = int(target_player.id)
    runtime.lock_generation = int(target_player.bot_generation)
    runtime.lock_confirmed_at = now
    if runtime.lock_started_at <= 0.0:
        runtime.lock_started_at = now


def test_live_lock_tracks_target_beyond_frozen_point_without_snapping() -> None:
    """Under a lease the motor chases the live target, bounded by turn_speed."""

    import math

    server, director, bot, runtime = _facing_fixture(
        class_id=int(C.CLASS_SOLDIER)
    )
    target = asyncio.run(
        director.add_bot(team=TEAM2, name="MovingMark", class_id=int(C.CLASS_SOLDIER))
    )
    assert target is not None
    eye = tuple(float(value) for value in bot.eye)
    # Frozen worker sample says +x; the target actually stands at +y.
    frozen = (eye[0] + 10.0, eye[1], eye[2])
    target.set_position(eye[0], eye[1] + 10.0, eye[2])
    now = time.monotonic()
    runtime.intent = _fire_intent(
        bot, runtime, now, frozen=frozen, target_player=target,
        expires=now + 10.0, action_kind=BotActionKind.NONE,
    )
    runtime.lock_started_at = 0.0
    _lock(runtime, target, now)

    max_step = float(runtime.profile.turn_speed) / 60.0 + 1e-6
    previous_yaw = runtime.motor.yaw
    for tick in range(1, 180):
        tick_now = now + tick / 60.0
        _lock(runtime, target, tick_now)  # worker keeps confirming
        director._apply_motor(runtime, tick_now, 1.0 / 60.0)
        delta = abs(
            (runtime.motor.yaw - previous_yaw + math.pi) % (2.0 * math.pi)
            - math.pi
        )
        assert delta <= max_step
        previous_yaw = runtime.motor.yaw

    # Live target direction is +y (yaw pi/2); the frozen point was yaw 0.
    assert abs(runtime.motor.yaw - math.pi / 2.0) < 0.25

    # Lease lapse: no more confirmations -> aim reverts to the frozen point.
    lapse_start = now + 180 / 60.0
    for tick in range(1, 180):
        director._apply_motor(runtime, lapse_start + tick / 60.0, 1.0 / 60.0)
    assert abs(runtime.motor.yaw) < 0.25


def test_sustained_fire_follows_weapon_cadence_and_stops_on_lease_lapse() -> None:
    from server.game_constants import WEAPON_PROFILES

    server, director, bot, runtime = _facing_fixture(
        class_id=int(C.CLASS_SOLDIER)
    )
    target = asyncio.run(
        director.add_bot(team=TEAM2, name="Bullseye", class_id=int(C.CLASS_SOLDIER))
    )
    assert target is not None
    eye = tuple(float(value) for value in bot.eye)
    target.set_position(eye[0] + 10.0, eye[1], eye[2])
    frozen = (eye[0] + 10.0, eye[1], eye[2])
    now = time.monotonic()
    runtime.intent = _fire_intent(
        bot, runtime, now, frozen=frozen, target_player=target, expires=now + 10.0
    )
    runtime.lock_started_at = 0.0
    _lock(runtime, target, now)
    fire_ticks: list[int] = []
    director.gateway.execute = lambda _player, action: (
        fire_ticks.append(0) or True
    )

    interval = float(WEAPON_PROFILES[int(bot.tool)].fire_interval)
    total = 0
    for tick in range(0, 120):
        tick_now = now + tick / 60.0
        _lock(runtime, target, tick_now)
        before = len(fire_ticks)
        director._apply_motor(runtime, tick_now, 1.0 / 60.0)
        if len(fire_ticks) > before:
            fire_ticks[-1] = tick
        total = len(fire_ticks)

    expected = 1 + int((119 / 60.0) / interval)
    assert 2 <= total <= expected + 1
    gaps = [b - a for a, b in zip(fire_ticks, fire_ticks[1:])]
    assert all(gap >= int(interval * 60.0) - 1 for gap in gaps)

    # Lease lapse: the final confirmation stays valid for _LOCK_LEASE plus
    # one weapon interval, then sustained fire must fall silent.
    lapse_start = now + 120 / 60.0
    for tick in range(0, 60):
        director._apply_motor(runtime, lapse_start + tick / 60.0, 1.0 / 60.0)
    drained = len(fire_ticks)
    for tick in range(60, 150):
        director._apply_motor(runtime, lapse_start + tick / 60.0, 1.0 / 60.0)
    assert len(fire_ticks) == drained
    assert runtime.pending_action is None


def test_wall_probe_blocks_sustained_fire_despite_convergence() -> None:
    server, director, bot, runtime = _facing_fixture(
        class_id=int(C.CLASS_SOLDIER)
    )
    target = asyncio.run(
        director.add_bot(team=TEAM2, name="Bunkered", class_id=int(C.CLASS_SOLDIER))
    )
    assert target is not None
    eye = tuple(float(value) for value in bot.eye)
    target.set_position(eye[0] + 10.0, eye[1], eye[2])
    frozen = (eye[0] + 10.0, eye[1], eye[2])
    server.world_manager.raycast = lambda *args, **kwargs: (
        int(eye[0]) + 2,
        int(eye[1]),
        int(eye[2]),
    )
    now = time.monotonic()
    runtime.intent = _fire_intent(
        bot, runtime, now, frozen=frozen, target_player=target, expires=now + 5.0
    )
    runtime.lock_started_at = 0.0
    _lock(runtime, target, now)
    executed: list[object] = []
    director.gateway.execute = lambda _player, action: (
        executed.append(action) or True
    )

    for tick in range(0, 60):
        tick_now = now + tick / 60.0
        _lock(runtime, target, tick_now)
        director._wall_probes_this_tick = 0
        director._apply_motor(runtime, tick_now, 1.0 / 60.0)

    assert executed == []


def test_drain_intents_refreshes_live_lock_lease_only_for_visible_targets() -> None:
    server, director, bot, runtime = _facing_fixture(
        class_id=int(C.CLASS_SOLDIER)
    )
    target = asyncio.run(
        director.add_bot(team=TEAM2, name="Spotted", class_id=int(C.CLASS_SOLDIER))
    )
    assert target is not None
    now = time.monotonic()
    eye = tuple(float(value) for value in bot.eye)

    visible_intent = BotIntent(
        bot_id=bot.id,
        bot_generation=runtime.generation,
        frame_id=11,
        map_epoch=director._map_epoch,
        mode_epoch=director._mode_epoch,
        topology_version=director._topology_version,
        created_at=now,
        expires_at=now + 0.25,
        movement=MovementIntent(),
        look=LookIntent(
            (eye[0] + 10.0, eye[1], eye[2]),
            visible=True,
            target_player_id=int(target.id),
            target_generation=int(target.bot_generation),
        ),
    )
    hidden_intent = replace(
        visible_intent,
        frame_id=12,
        look=LookIntent(
            (eye[0] + 10.0, eye[1], eye[2]),
            visible=False,
            target_player_id=int(target.id),
            target_generation=int(target.bot_generation),
        ),
    )

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.pending = [visible_intent]

        def drain_intents(self, limit: int = 12):
            drained, self.pending = self.pending[:limit], []
            return drained

    director.supervisor = _FakeSupervisor()
    director._drain_intents(now)
    assert runtime.lock_player_id == int(target.id)
    first_confirmed = runtime.lock_confirmed_at

    director.supervisor.pending = [hidden_intent]
    director._drain_intents(now + 0.1)
    # A non-visible sample must never refresh the live-tracking lease.
    assert runtime.lock_confirmed_at == first_confirmed


def _tool_of_category(category: str) -> int:
    from server.game_constants import WEAPON_PROFILES

    return next(
        int(profile.tool_id)
        for profile in WEAPON_PROFILES.values()
        if profile.category == category
    )


def test_engagement_envelopes_match_weapon_categories() -> None:
    from server.bot_ai.combat_profiles import envelope_for
    from server.game_constants import CAT_SMG, CAT_SNIPER

    sniper = envelope_for(_tool_of_category(CAT_SNIPER))
    smg = envelope_for(_tool_of_category(CAT_SMG))
    unknown = envelope_for(-123)

    assert sniper.prefers_stationary is True
    assert sniper.ideal_min >= 40.0
    assert smg.prefers_stationary is False
    assert smg.ideal_max <= 30.0
    assert unknown.ideal_min > 0.0


def test_sniper_holds_stationary_in_band_and_backs_off_up_close() -> None:
    from server.game_constants import CAT_SNIPER

    world = _SwitchableWorld()
    brain = BotBrain(world, seed=6)
    sniper_tool = _tool_of_category(CAT_SNIPER)
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        weapon_tool=sniper_tool,
        tool=sniper_tool,
        loadout=(sniper_tool,),
    )
    far_enemy = _player_snapshot(2, 3, (60.0, 0.0, 0.0))

    first = brain.decide(_frame(1, observer, far_enemy))
    assert first is not None
    state = brain._states[(1, 1)]
    state.next_reposition_at = time.monotonic() + 100.0
    state.reposition_until = 0.0

    hold = brain.decide(_frame(2, observer, far_enemy))
    assert hold is not None
    assert hold.movement.direction == (0.0, 0.0, 0.0)
    assert hold.movement.crouch is True

    close_enemy = _player_snapshot(2, 3, (15.0, 0.0, 0.0))
    retreat = brain.decide(_frame(3, observer, close_enemy))
    assert retreat is not None
    assert retreat.movement.direction[0] < -0.9


def test_smg_closes_distance_and_fires_in_bursts() -> None:
    from server.game_constants import CAT_SMG

    world = _SwitchableWorld()
    brain = BotBrain(world, seed=9)
    smg_tool = _tool_of_category(CAT_SMG)
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        weapon_tool=smg_tool,
        tool=smg_tool,
        loadout=(smg_tool,),
    )
    far_enemy = _player_snapshot(2, 3, (60.0, 0.0, 0.0))
    chase = brain.decide(_frame(1, observer, far_enemy))
    assert chase is not None
    assert chase.movement.direction[0] > 0.9
    assert chase.movement.sprint is True

    in_band = _player_snapshot(2, 3, (20.0, 0.0, 0.0))
    strike = brain.decide(_frame(2, observer, in_band))
    assert strike is not None
    assert strike.action.kind is BotActionKind.FIRE
    assert 4 <= strike.action.burst <= 8
    assert strike.action.burst_pause > 0.0


def test_medic_moves_to_wounded_teammate_and_deploys_medpack() -> None:
    world = _SwitchableWorld()
    world.visible = False
    brain = BotBrain(world, seed=13)
    medic_tool = int(C.MEDPACK_TOOL)
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        class_id=int(C.CLASS_MEDIC),
        loadout=(DEFAULT_WEAPON_TOOL, medic_tool),
    )
    wounded = replace(
        _player_snapshot(3, 2, (20.0, 0.0, 0.0), is_bot=True),
        health=35,
    )
    frame = _frame(1, observer, wounded)

    approach = brain.decide(frame)
    assert approach is not None
    assert approach.debug_role == "medic_support"
    assert approach.movement.direction[0] > 0.9

    near = replace(
        _player_snapshot(3, 2, (2.5, 0.0, 0.0), is_bot=True),
        health=35,
    )
    heal = brain.decide(_frame(2, observer, near))
    assert heal is not None
    assert heal.action.kind is BotActionKind.DEPLOY
    assert heal.action.tool_id == medic_tool
    assert heal.action.position == near.position


def test_miner_dynamites_a_stale_walled_contact_then_retreats() -> None:
    class BreachWorld(_SwitchableWorld):
        @staticmethod
        def blocking_cell(_position, _direction):
            return (3, 0, 1)

    world = BreachWorld()
    brain = BotBrain(world, seed=21)
    dynamite = int(C.DYNAMITE_TOOL)
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        class_id=int(C.CLASS_MINER),
        loadout=(DEFAULT_WEAPON_TOOL, dynamite, int(C.SUPERSPADE_TOOL)),
    )
    enemy = _player_snapshot(2, 3, (14.0, 0.0, 0.0))
    start = time.monotonic()
    seen = replace(_frame(1, observer, enemy), created_at=start)
    assert brain.decide(seen) is not None

    world.visible = False
    moved_observer = replace(observer, position=(1.0, 0.0, 0.0), eye=(1.0, 0.0, 0.0))
    stale = replace(
        _frame(2, moved_observer, enemy), created_at=start + 2.5
    )
    plant = brain.decide(stale)
    assert plant is not None
    assert plant.action.kind is BotActionKind.DEPLOY
    assert plant.action.tool_id == dynamite
    assert plant.debug_role == "miner_demolition"

    after = replace(
        _frame(3, moved_observer, enemy), created_at=start + 2.7
    )
    retreat = brain.decide(after)
    assert retreat is not None
    assert retreat.debug_role in ("blast_retreat", "blast_overwatch")
    assert retreat.action.kind is BotActionKind.NONE


def test_burst_pause_and_recoil_ride_the_sustained_fire_loop() -> None:
    from server.game_constants import WEAPON_PROFILES

    server, director, bot, runtime = _facing_fixture(
        class_id=int(C.CLASS_SOLDIER)
    )
    target = asyncio.run(
        director.add_bot(team=TEAM2, name="BurstMark", class_id=int(C.CLASS_SOLDIER))
    )
    assert target is not None
    eye = tuple(float(value) for value in bot.eye)
    target.set_position(eye[0] + 10.0, eye[1], eye[2])
    now = time.monotonic()
    interval = float(WEAPON_PROFILES[int(bot.tool)].fire_interval)
    pause = 0.6
    intent = _fire_intent(
        bot, runtime, now,
        frozen=(eye[0] + 10.0, eye[1], eye[2]),
        target_player=target,
        expires=now + 10.0,
    )
    runtime.intent = replace(
        intent,
        action=BotAction(
            BotActionKind.FIRE,
            tool_id=int(bot.tool),
            burst=1,
            burst_pause=pause,
        ),
    )
    runtime.lock_started_at = 0.0
    _lock(runtime, target, now)
    fire_ticks: list[int] = []
    director.gateway.execute = lambda _player, action: (
        fire_ticks.append(0) or True
    )
    recoil_seen = False
    for tick in range(0, 240):
        tick_now = now + tick / 60.0
        _lock(runtime, target, tick_now)
        before = len(fire_ticks)
        director._apply_motor(runtime, tick_now, 1.0 / 60.0)
        if len(fire_ticks) > before:
            fire_ticks[-1] = tick
            if runtime.motor.pitch_noise < 0.0:
                recoil_seen = True

    assert len(fire_ticks) >= 2
    gaps = [b - a for a, b in zip(fire_ticks, fire_ticks[1:])]
    # burst=1 means every shot is followed by the burst pause.
    assert all(gap >= int((interval + pause) * 60.0) - 2 for gap in gaps)
    assert recoil_seen


def test_zombie_policy_flags_fortify_for_survivors_only() -> None:
    from server.bot_ai.messages import ObjectiveSnapshot
    from server.bot_ai.policies import objective_decision_for

    observer = _player_snapshot(1, 2, (100.0, 100.0, 7.75), is_bot=True)
    anchor = ObjectiveSnapshot("team_anchor", 2, (100.0, 100.0, 7.75))
    waiting = replace(
        _frame(1, observer, _player_snapshot(2, 3, (200.0, 200.0, 7.75))),
        mode_id="zom",
        mode_phase="waiting",
        objectives=(anchor,),
    )
    prepare = objective_decision_for(waiting, observer)
    assert prepare is not None
    assert prepare.directive == "fortify"

    zombie_observer = replace(
        observer, class_id=int(C.CLASS_ZOMBIE), team=3
    )
    survivor_target = _player_snapshot(2, 2, (150.0, 100.0, 7.75))
    hunting = replace(
        _frame(2, zombie_observer, survivor_target),
        mode_id="zom",
        mode_phase="active",
        objectives=(ObjectiveSnapshot("team_anchor", 3, (100.0, 100.0, 7.75)),),
    )
    hunt = objective_decision_for(hunting, zombie_observer)
    assert hunt is not None
    assert hunt.role == "zombie_hunt_survivor"
    assert hunt.directive == ""


def _fortify_region_world(extra_solids=()):
    solids = set()
    for x in range(70, 132):
        for y in range(70, 132):
            solids.add((x, y, 10))
    solids.update(extra_solids)
    world = _fixture_voxel_world(solids)
    world._solids = solids
    world.solid = lambda x, y, z: (
        True
        if not (0 <= x < 512 and 0 <= y < 512 and 0 <= z < 240)
        else (int(x), int(y), int(z)) in world._solids
    )
    return world


def test_fortify_site_prefers_high_ground_with_few_approaches() -> None:
    from server.bot_ai.messages import ObjectiveSnapshot

    hill = {
        (x, y, z)
        for x in range(108, 119)
        for y in range(94, 107)
        for z in range(6, 11)
    }
    # A walkable one-step ramp up the west face: unreachable spires must be
    # rejected by the reachability gate, so the hill needs a real way up.
    for width_y in (99, 100, 101):
        hill.update((105, width_y, z) for z in range(9, 11))
        hill.update((106, width_y, z) for z in range(8, 11))
        hill.update((107, width_y, z) for z in range(7, 11))
    world = _fortify_region_world(hill)
    brain = BotBrain(world, seed=2)
    observer = _player_snapshot(1, 2, (100.0, 100.0, 7.75), is_bot=True)
    frame = replace(
        _frame(1, observer, _player_snapshot(2, 3, (200.0, 200.0, 7.75))),
        mode_id="zom",
        mode_phase="waiting",
        objectives=(ObjectiveSnapshot("team_anchor", 2, (100.0, 100.0, 7.75)),),
    )

    site = brain._fortify_site(frame, observer, time.monotonic())

    assert site is not None
    assert 108 <= site[0] <= 118
    assert 94 <= site[1] <= 106
    assert abs(site[2] - (6.0 - 2.25)) < 1e-6


def test_fortify_door_rule_never_seals_the_last_approach() -> None:
    assert BotBrain._sealable_approaches([0.1], keep_door=True) == []
    assert BotBrain._sealable_approaches([0.1, 0.2], keep_door=True) == [0.1]
    assert BotBrain._sealable_approaches([0.1], keep_door=False) == [0.1]


def test_fortify_builds_a_closed_two_high_perimeter_when_sealing() -> None:
    from server.bot_ai.messages import ObjectiveSnapshot
    from server.bot_ai.worker import _BrainState

    world = _fortify_region_world()
    brain = BotBrain(world, seed=4)
    site = (100.0, 100.0, 7.75)
    observer = replace(
        _player_snapshot(1, 2, site, is_bot=True),
        loadout=(DEFAULT_WEAPON_TOOL, int(C.BLOCK_TOOL)),
        blocks=400,
    )
    frame = replace(
        _frame(1, observer, _player_snapshot(2, 3, (200.0, 200.0, 7.75))),
        mode_id="zom",
        mode_phase="countdown",  # door rule off: seal fully
        objectives=(ObjectiveSnapshot("team_anchor", 2, site),),
        players=(observer,),
    )
    state = _BrainState()
    placed: list[tuple[int, int, int]] = []
    now = time.monotonic()
    for step in range(200):
        state.next_fortify_build_at = 0.0
        intent = brain._fortify_build_intent(frame, observer, state, site, now)
        if intent is None:
            break
        assert intent.action.kind is BotActionKind.BUILD
        assert intent.action.tool_id == int(C.BLOCK_TOOL)
        wx, wy, wz = (int(value) for value in intent.action.position)
        # Every wall cell sits on the ring around the site, never on it.
        assert 2 <= max(abs(wx - 100), abs(wy - 100)) <= 4
        assert wz in (8, 9)
        assert not world.solid(wx, wy, wz)
        # The cell must be face-supported when it commits: ground below or
        # the previously placed lower block.
        assert world.solid(wx, wy, wz + 1)
        world._solids.add((wx, wy, wz))
        placed.append((wx, wy, wz))
    else:
        raise AssertionError("fortify never finished sealing the perimeter")

    assert len(placed) >= 16
    site_node = world._standing_node(100, 100, site[2], vertical_span=8)
    assert site_node is not None
    assert brain._open_approaches(site_node) == []
    # Wall reaches two high somewhere on every built column.
    columns = {(x, y) for x, y, _z in placed}
    two_high = {
        (x, y)
        for x, y in columns
        if (x, y, 9) in world._solids and (x, y, 8) in world._solids
    }
    assert len(two_high) >= len(columns) * 0.8

    hold = brain._fortify_hold_intent(frame, observer, site, now)
    assert hold.debug_role == "fortify_hold"
    assert hold.movement.crouch is True


def test_fortify_waiting_phase_keeps_an_entry_for_distant_teammates() -> None:
    from server.bot_ai.messages import ObjectiveSnapshot
    from server.bot_ai.worker import _BrainState

    world = _fortify_region_world()
    brain = BotBrain(world, seed=5)
    site = (100.0, 100.0, 7.75)
    observer = replace(
        _player_snapshot(1, 2, site, is_bot=True),
        loadout=(DEFAULT_WEAPON_TOOL, int(C.BLOCK_TOOL)),
        blocks=400,
    )
    far_teammate = replace(
        _player_snapshot(3, 2, (120.0, 100.0, 7.75), is_bot=True)
    )
    frame = replace(
        _frame(1, observer, far_teammate),
        mode_id="zom",
        mode_phase="waiting",
        objectives=(ObjectiveSnapshot("team_anchor", 2, site),),
    )
    state = _BrainState()
    now = time.monotonic()
    for _step in range(200):
        state.next_fortify_build_at = 0.0
        intent = brain._fortify_build_intent(frame, observer, state, site, now)
        if intent is None:
            break
        wx, wy, wz = (int(value) for value in intent.action.position)
        world._solids.add((wx, wy, wz))

    site_node = world._standing_node(100, 100, site[2], vertical_span=8)
    assert site_node is not None
    assert len(brain._open_approaches(site_node)) >= 1


def test_compact_vxl_surface_z_is_the_topmost_solid() -> None:
    from server.bot_ai.compact_vxl import MAP_AREA, MAP_HEIGHT, CompactVoxelMap

    vxl = CompactVoxelMap.__new__(CompactVoxelMap)
    vxl._columns = [0] * MAP_AREA
    vxl.source_z_shift = 0
    vxl.set_solid(10, 20, 100, True)
    vxl.set_solid(10, 20, 150, True)

    assert vxl.surface_z(10, 20) == 100  # z-down: smaller z is higher
    assert vxl.surface_z(11, 20) == MAP_HEIGHT
    vxl.set_solid(10, 20, 40, True)
    assert vxl.surface_z(10, 20) == 40


class _SurfaceFixture:
    def __init__(self, solids) -> None:
        columns: dict[tuple[int, int], int] = {}
        for x, y, z in solids:
            key = (int(x), int(y))
            columns[key] = min(int(z), columns.get(key, 240))
        self._columns = columns

    def surface_z(self, x: int, y: int) -> int:
        return self._columns.get((int(x), int(y)), 240)


def test_tactical_map_finds_high_ground_and_tracks_terrain_changes() -> None:
    from server.bot_ai.tactical_map import TacticalMap

    solids = {(x, y, 10) for x in range(64, 192) for y in range(64, 192)}
    plateau = {
        (x, y, z)
        for x in range(128, 160)
        for y in range(96, 128)
        for z in range(4, 11)
    }
    tactical = TacticalMap()
    tactical.attach(_SurfaceFixture(solids | plateau))
    assert tactical.rebuild(4096) > 0
    assert tactical.pending_cells == 0

    spot = tactical.high_ground_near((150.0, 100.0, 7.75), radius_cells=2)
    assert spot is not None
    assert 128 <= spot[0] <= 160
    assert abs(spot[2] - (4.0 - 2.25)) < 1e-6

    # Level the plateau: dirty cells re-summarize and the pick moves down.
    tactical._vxl = _SurfaceFixture(solids)
    for x in range(128, 160, 8):
        for y in range(96, 128, 8):
            tactical.mark_dirty(x, y)
    assert tactical.rebuild(4096) > 0
    lowered = tactical.high_ground_near((150.0, 100.0, 7.75), radius_cells=2)
    assert lowered is not None
    assert abs(lowered[2] - (10.0 - 2.25)) < 1e-6


def test_tdm_assault_goal_shifts_onto_reachable_high_ground() -> None:
    from server.bot_ai.messages import ObjectiveSnapshot

    solids = {(x, y, 10) for x in range(64, 192) for y in range(64, 192)}
    hill = {
        (x, y, z)
        for x in range(128, 160)
        for y in range(80, 112)
        for z in range(6, 11)
    }
    ramp = set()
    for y in range(96, 105):
        ramp.update((160, y, z) for z in range(7, 11))
        ramp.update((161, y, z) for z in range(8, 11))
        ramp.update((162, y, z) for z in range(9, 11))
    all_solids = solids | hill | ramp
    world = _fortify_region_world()
    world._solids = all_solids
    world.tactical.attach(_SurfaceFixture(all_solids))
    world.tactical.rebuild(4096)
    brain = BotBrain(world, seed=8)

    observer = _player_snapshot(1, 2, (168.0, 100.0, 7.75), is_bot=True)
    distant_enemy = _player_snapshot(2, 3, (400.0, 400.0, 7.75))
    frame = replace(
        _frame(1, observer, distant_enemy),
        mode_id="tdm",
        objectives=(
            ObjectiveSnapshot("team_anchor", 3, (172.0, 100.0, 7.75)),
        ),
    )

    intent = brain.decide(frame)

    assert intent is not None
    state = brain._states[(1, 1)]
    assert state.tactical_goal is not None
    assert 126.0 <= state.tactical_goal[0] <= 162.0
    assert abs(state.tactical_goal[2] - (6.0 - 2.25)) < 0.75


def test_hesitation_scales_with_low_skill_and_stays_bounded() -> None:
    def acquisition_rate(skill: float, seed: int) -> float:
        world = _SwitchableWorld()
        brain = BotBrain(world, seed=seed)
        profile = replace(_profile(), skill=skill)
        hesitated = 0
        samples = 200
        for index in range(samples):
            observer = _player_snapshot(
                1000 + index, 2, (0.0, 0.0, 0.0), is_bot=True
            )
            enemy = _player_snapshot(2000 + index, 3, (10.0, 0.0, 0.0))
            frame = replace(_frame(index + 1, observer, enemy), profile=profile)
            assert brain.decide(frame) is not None
            state = brain._states[(observer.player_id, 1)]
            if state.reaction_bonus > 0.0:
                hesitated += 1
        return hesitated / samples

    assert 0.10 <= acquisition_rate(0.20, seed=41) <= 0.50
    assert acquisition_rate(0.95, seed=42) <= 0.10


def test_cover_peek_rhythm_alternates_hold_and_lean() -> None:
    class CoverWorld(_SwitchableWorld):
        @staticmethod
        def cover_direction(_position, _threat):
            return (0.7, 0.7, 0.0)

        @staticmethod
        def cover_build_cell(_position, _threat):
            return None

    world = CoverWorld()
    brain = BotBrain(world, seed=19)
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        health=35,
    )
    enemy = _player_snapshot(2, 3, (10.0, 0.0, 0.0))
    start = time.monotonic()

    hold = brain.decide(
        replace(_frame(1, observer, enemy), created_at=start)
    )
    assert hold is not None
    assert hold.movement.direction == (0.7, 0.7, 0.0)

    peek = brain.decide(
        replace(_frame(2, observer, enemy), created_at=start + 3.0)
    )
    assert peek is not None
    assert abs(peek.movement.direction[0]) < 0.1
    assert abs(abs(peek.movement.direction[1]) - 1.0) < 0.1

    tuck = brain.decide(
        replace(_frame(3, observer, enemy), created_at=start + 4.4)
    )
    assert tuck is not None
    assert tuck.movement.direction == (0.7, 0.7, 0.0)


def test_runtime_steps_bot_even_if_peerless_connection_is_marked_inactive() -> None:
    calls: list[float] = []

    async def simulate(dt: float) -> None:
        calls.append(dt)

    bot = SimpleNamespace(
        is_bot=True,
        connection=SimpleNamespace(in_game=False),
        simulate_tick=simulate,
    )
    runtime = SimulationRuntime(
        SimpleNamespace(
            players={1: bot},
            tick_interval=1.0 / 60.0,
            config=SimpleNamespace(),
        )
    )

    asyncio.run(runtime._simulate_players())

    assert calls == [1.0 / 60.0]


def test_profile_factory_is_deterministic_unique_and_wire_bounded() -> None:
    first = ProfileFactory(seed=77)
    second = ProfileFactory(seed=77)
    profiles = [first.create("mixed") for _ in range(32)]
    mirror = [second.create("mixed") for _ in range(32)]

    assert profiles == mirror
    assert len({profile.name for profile in profiles}) == len(profiles)
    assert all(3 <= len(profile.name) <= 15 for profile in profiles)
    assert all(profile.name.isascii() for profile in profiles)


def test_supervisor_queues_are_bounded_and_terrain_coalesces() -> None:
    supervisor = AIWorkerSupervisor(seed=1)
    observer = _player_snapshot(1, 2, (1.0, 1.0, 1.0), is_bot=True)
    enemy = _player_snapshot(2, 3, (4.0, 1.0, 1.0))

    accepted = [supervisor.submit_frame(_frame(index, observer, enemy)) for index in range(80)]
    supervisor.publish_world_change(
        VoxelChange(1, 2, 3, True, 0x112233),
        map_epoch=1,
        topology_version=1,
    )
    supervisor.publish_world_change(
        VoxelChange(1, 2, 3, False, 0),
        map_epoch=1,
        topology_version=2,
    )
    status = supervisor.status()

    # Strategic frames are snapshots, not an event log. Keep only the newest
    # one for a bot so worker slowdown cannot turn into seconds of stale play.
    assert all(accepted)
    assert status.queued_frames == 1
    assert status.dropped_frames == 0
    assert status.pending_terrain_cells == 1

    delivered = queue.Queue()
    supervisor._send_frames(delivered)
    assert delivered.get_nowait().frame_id == 79


def test_supervisor_frame_coalescing_remains_hard_bounded_across_bots() -> None:
    supervisor = AIWorkerSupervisor(seed=1)
    enemy = _player_snapshot(99, 3, (4.0, 1.0, 1.0))

    for index in range(80):
        observer = _player_snapshot(
            index,
            2,
            (1.0, 1.0, 1.0),
            is_bot=True,
        )
        assert supervisor.submit_frame(_frame(index, observer, enemy))

    status = supervisor.status()
    assert status.queued_frames == 64
    assert status.dropped_frames == 16


def test_worker_restart_snapshot_replays_every_committed_terrain_overlay() -> None:
    supervisor = AIWorkerSupervisor(seed=1)
    supervisor.publish_map(MapSnapshot(3, 0, b"base", "tdm", "fixture"))
    first = VoxelChange(1, 2, 3, True, 0x112233)
    second = VoxelChange(4, 5, 6, False, 0)
    supervisor.publish_world_change(first, map_epoch=3, topology_version=1)
    supervisor.publish_world_change(second, map_epoch=3, topology_version=2)

    delivered = queue.Queue()
    supervisor._send_pending_terrain(delivered)
    assert supervisor.status().pending_terrain_cells == 0

    restarted_input = queue.Queue()
    serial = supervisor._send_snapshot_if_needed(restarted_input, sent_serial=-1)
    snapshot = restarted_input.get_nowait()

    assert serial >= 0
    assert snapshot.topology_version == 2
    assert set(snapshot.changed_cells) == {first, second}


def test_worker_visibility_fails_closed_without_collision_world() -> None:
    world = WorkerVoxelWorld()

    assert world.has_line_of_sight((0.0, 0.0, 0.0), (5.0, 0.0, 0.0)) is False


def test_sound_bus_is_bounded_approximate_and_never_exact_hidden_position() -> None:
    bus = BotStimulusBus(capacity=32)
    now = time.monotonic()
    exact = (20.0, 10.0, 5.0)

    assert bus.publish(
        StimulusKind.SHOT,
        exact,
        source_id=9,
        radius=80.0,
        now=now,
    )
    heard = bus.perceive(
        (0.0, 0.0, 5.0), now=now + 0.1, rng=random.Random(7)
    )

    assert len(heard) == 1
    assert heard[0].position != exact
    assert heard[0].uncertainty >= 0.75


def _fixture_voxel_world(solids):
    world = WorkerVoxelWorld()
    world._vxl = object()
    world._native_nav = None
    solid_cells = set(solids)
    world.solid = lambda x, y, z: (
        True
        if not (0 <= x < 512 and 0 <= y < 512 and 0 <= z < 240)
        else (int(x), int(y), int(z)) in solid_cells
    )
    return world


def test_gap_navigation_requires_explicit_jump_affordance() -> None:
    world = _fixture_voxel_world({(0, 0, 10), (2, 0, 10)})
    start = (0.5, 0.5, 7.75)
    goal = (2.5, 0.5, 7.75)

    blocked = world.next_path_direction(start, goal, agent_id=1)
    jump = world.next_path_direction(
        start,
        goal,
        agent_id=1,
        abilities=frozenset({MovementAffordance.JUMP}),
    )

    assert blocked == (0.0, 0.0, 0.0)
    assert jump[0] > 0.9
    assert world.last_affordance(1) is MovementAffordance.JUMP


def test_large_vertical_edge_is_class_filtered_to_jetpack() -> None:
    world = _fixture_voxel_world({(0, 0, 10), (1, 0, 4)})
    start = (0.5, 0.5, 7.75)
    goal = (1.5, 0.5, 1.75)

    blocked = world.next_path_direction(
        start,
        goal,
        agent_id=9,
        abilities=frozenset({MovementAffordance.JUMP}),
    )
    flight = world.next_path_direction(
        start,
        goal,
        agent_id=9,
        abilities=frozenset({MovementAffordance.JETPACK}),
    )

    assert blocked == (0.0, 0.0, 0.0)
    assert flight[0] > 0.9
    assert world.last_affordance(9) is MovementAffordance.JETPACK


def test_low_health_bot_prefers_nearest_live_health_crate() -> None:
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        health=25,
    )
    frame = PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=0,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=time.monotonic(),
        mode_id="tdm",
        players=(observer,),
        entities=(
            EntitySnapshot(1, 4, -1, -1, (30.0, 0.0, 0.0)),
            EntitySnapshot(2, 4, -1, -1, (8.0, 0.0, 0.0)),
        ),
    )

    assert BotBrain._resource_goal(frame, observer) == (8.0, 0.0, 0.0)


class _SwitchableWorld:
    def __init__(self) -> None:
        self.visible = True
        self.map_epoch = 1
        self.topology_version = 0

    def has_line_of_sight(self, _origin, _target) -> bool:
        return self.visible

    def next_path_direction(self, start, goal, **_kwargs):
        dx, dy = goal[0] - start[0], goal[1] - start[1]
        return (1.0 if dx > 0 else -1.0, 1.0 if dy > 0 else 0.0, 0.0)


def test_worker_batch_discards_obsolete_frames_for_the_same_bot_life() -> None:
    world = _SwitchableWorld()
    brain = BotBrain(world, seed=2)
    observer = _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True)
    enemy = _player_snapshot(2, 3, (10.0, 0.0, 0.0))

    shutdown, intents = _process_worker_batch(
        world,
        brain,
        (
            _frame(1, observer, enemy),
            _frame(2, observer, enemy),
            _frame(9, observer, enemy),
        ),
    )

    assert shutdown is False
    assert [intent.frame_id for intent in intents] == [9]


def test_hidden_enemy_position_freezes_and_cannot_trigger_fire() -> None:
    world = _SwitchableWorld()
    brain = BotBrain(world, seed=3)
    observer = _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True)
    visible_enemy = _player_snapshot(2, 3, (10.0, 0.0, 0.0))

    visible_intent = brain.decide(_frame(1, observer, visible_enemy))
    assert visible_intent is not None
    assert visible_intent.look is not None and visible_intent.look.visible is True

    world.visible = False
    moved_hidden_enemy = _player_snapshot(2, 3, (30.0, 20.0, 0.0))
    hidden_intent = brain.decide(_frame(2, observer, moved_hidden_enemy))

    assert hidden_intent is not None
    assert hidden_intent.look is not None
    assert hidden_intent.look.target == visible_enemy.position
    assert hidden_intent.look.visible is False
    assert hidden_intent.action.kind is BotActionKind.NONE


def test_intentional_head_aim_stays_below_twenty_percent() -> None:
    world = _SwitchableWorld()
    brain = BotBrain(world, seed=91)
    head_aims = 0
    samples = 200
    for index in range(samples):
        observer = _player_snapshot(
            1000 + index, 2, (0.0, 0.0, 0.0), is_bot=True
        )
        enemy = _player_snapshot(2000 + index, 3, (10.0, 0.0, 0.0))
        intent = brain.decide(_frame(index + 1, observer, enemy))
        assert intent is not None and intent.look is not None
        if abs(intent.look.target[2] - enemy.eye[2]) < 0.2:
            head_aims += 1

    assert 0.02 <= head_aims / samples < 0.20


def test_worker_oriented_attack_uses_only_selected_positive_stock_tool() -> None:
    world = _SwitchableWorld()
    brain = BotBrain(world, seed=29)
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        weapon_tool=DEFAULT_WEAPON_TOOL,
        loadout=(DEFAULT_WEAPON_TOOL, 12),
        oriented_stock=((12, 2),),
    )
    enemy = _player_snapshot(2, 3, (30.0, 0.0, 0.0))

    oriented = None
    for frame_id in range(1, 80):
        intent = brain.decide(_frame(frame_id, observer, enemy))
        if intent is not None and intent.action.kind is BotActionKind.ORIENTED:
            oriented = intent.action
            break

    assert oriented is not None
    assert oriented.tool_id == 12


def test_empty_clip_reload_has_priority_over_oriented_equipment() -> None:
    world = _SwitchableWorld()
    brain = BotBrain(world, seed=29)
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        weapon_tool=DEFAULT_WEAPON_TOOL,
        loadout=(DEFAULT_WEAPON_TOOL, int(C.GRENADE_TOOL)),
        oriented_stock=((int(C.GRENADE_TOOL), 2),),
        ammo_clip=0,
        ammo_reserve=30,
    )
    enemy = _player_snapshot(2, 3, (30.0, 0.0, 0.0))

    for frame_id in range(1, 30):
        intent = brain.decide(_frame(frame_id, observer, enemy))
        assert intent is not None
        assert intent.action.kind is BotActionKind.RELOAD


def test_reloading_bot_does_not_cancel_reload_with_another_tool() -> None:
    world = _SwitchableWorld()
    brain = BotBrain(world, seed=7)
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        weapon_tool=DEFAULT_WEAPON_TOOL,
        loadout=(DEFAULT_WEAPON_TOOL, int(C.GRENADE_TOOL)),
        oriented_stock=((int(C.GRENADE_TOOL), 2),),
        ammo_clip=0,
        ammo_reserve=30,
        reloading=True,
    )
    enemy = _player_snapshot(2, 3, (20.0, 0.0, 0.0))

    intent = brain.decide(_frame(1, observer, enemy))

    assert intent is not None
    assert intent.action.kind is BotActionKind.NONE


def test_dry_bot_closes_for_melee_instead_of_dry_firing_forever() -> None:
    world = _SwitchableWorld()
    brain = BotBrain(world, seed=4)
    melee = int(C.SPADE_TOOL)
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        weapon_tool=DEFAULT_WEAPON_TOOL,
        loadout=(DEFAULT_WEAPON_TOOL, melee),
        ammo_clip=0,
        ammo_reserve=0,
    )
    distant = _player_snapshot(2, 3, (25.0, 0.0, 0.0))

    chase = brain.decide(_frame(1, observer, distant))

    assert chase is not None
    assert chase.movement.direction[0] > 0.9
    assert chase.action.kind is BotActionKind.NONE

    close = _player_snapshot(2, 3, (3.0, 0.0, 0.0))
    strike = brain.decide(_frame(2, observer, close))
    assert strike is not None
    assert strike.action.kind is BotActionKind.MELEE
    assert strike.action.tool_id == melee


def test_zombie_hand_never_uses_firearm_spacing_or_stock_logic() -> None:
    """A stocked Zombie hand must still close to native melee range."""

    world = _SwitchableWorld()
    brain = BotBrain(world, seed=44)
    hand = int(C.ZOMBIEHAND_TOOL)
    observer = replace(
        _player_snapshot(1, 3, (0.0, 0.0, 0.0), is_bot=True),
        class_id=int(C.CLASS_ZOMBIE),
        tool=hand,
        weapon_tool=hand,
        loadout=(hand, int(C.ZOMBIE_PREFAB_TOOL)),
        # This reproduces the live failure: melee tools can carry non-zero
        # generic stock counters, but those counters do not make them guns.
        ammo_clip=1,
        ammo_reserve=1,
    )
    distant = _player_snapshot(2, 2, (8.0, 0.0, 0.0))
    chase_frame = replace(
        _frame(1, observer, distant), mode_id="zom", mode_phase="active"
    )

    chase = brain.decide(chase_frame)

    assert chase is not None
    assert chase.movement.direction[0] > 0.9
    assert chase.movement.sprint is True
    assert chase.action.kind is BotActionKind.NONE

    close = replace(distant, position=(3.0, 0.0, 0.0), eye=(3.0, 0.0, 0.0))
    strike = brain.decide(replace(chase_frame, frame_id=2, players=(observer, close)))
    assert strike is not None
    assert strike.movement.direction[0] > 0.9
    assert strike.action.kind is BotActionKind.MELEE
    assert strike.action.tool_id == hand


def test_jump_zombie_uses_its_internal_class_mobility_in_combat() -> None:
    world = _SwitchableWorld()
    brain = BotBrain(world, seed=17)
    hand = int(C.ZOMBIEHAND_TOOL)
    observer = replace(
        _player_snapshot(1, 3, (0.0, 0.0, 0.0), is_bot=True),
        class_id=int(C.CLASS_JUMP_ZOMBIE),
        tool=hand,
        weapon_tool=hand,
        loadout=(hand, int(C.ZOMBIE_PREFAB_TOOL)),
        grounded=True,
    )
    survivor = _player_snapshot(2, 2, (12.0, 0.0, -2.0))
    frame = replace(
        _frame(1, observer, survivor), mode_id="zom", mode_phase="active"
    )

    intent = brain.decide(frame)

    assert intent is not None
    assert intent.movement.jump is True
    assert intent.movement.sprint is True


def test_zombie_repeats_native_breach_cadence_without_generic_delay() -> None:
    class BreachWorld(_SwitchableWorld):
        @staticmethod
        def blocking_cell(_position, _direction):
            return (1, 0, 1)

    world = BreachWorld()
    world.visible = False
    brain = BotBrain(world, seed=23)
    hand = int(C.ZOMBIEHAND_TOOL)
    observer = replace(
        _player_snapshot(1, 3, (0.0, 0.0, 0.0), is_bot=True),
        class_id=int(C.CLASS_ZOMBIE),
        tool=hand,
        weapon_tool=hand,
        loadout=(hand, int(C.ZOMBIE_PREFAB_TOOL)),
    )
    survivor = _player_snapshot(2, 2, (15.0, 0.0, 0.0))
    start = time.monotonic()
    frame = replace(
        _frame(1, observer, survivor),
        created_at=start,
        mode_id="zom",
        mode_phase="active",
    )

    first = brain.decide(frame)
    second = brain.decide(replace(frame, frame_id=2, created_at=start + 0.41))

    assert first is not None and first.action.kind is BotActionKind.MELEE
    assert second is not None and second.action.kind is BotActionKind.MELEE
    assert second.action.tool_id == hand


def test_stuck_zombie_builds_a_selected_native_prefab_with_tool_28() -> None:
    class BuildWorld(_SwitchableWorld):
        @staticmethod
        def blocking_cell(_position, _direction):
            return None

        @staticmethod
        def bridge_cell(_position, _direction):
            return None

    world = BuildWorld()
    world.visible = False
    brain = BotBrain(world, seed=31)
    hand = int(C.ZOMBIEHAND_TOOL)
    prefab_tool = int(C.ZOMBIE_PREFAB_TOOL)
    observer = replace(
        _player_snapshot(1, 3, (0.0, 0.0, 0.0), is_bot=True),
        class_id=int(C.CLASS_ZOMBIE),
        tool=hand,
        weapon_tool=hand,
        loadout=(hand, prefab_tool),
        prefabs=("prefab_zombiehand", "prefab_zombiebone", "prefab_zombiehead"),
        blocks=1000,
    )
    survivor = _player_snapshot(2, 2, (18.0, 0.0, -6.0))
    start = time.monotonic()
    frame = replace(
        _frame(1, observer, survivor),
        created_at=start,
        mode_id="zom",
        mode_phase="active",
    )

    assert brain.decide(frame) is not None
    build = brain.decide(replace(frame, frame_id=2, created_at=start + 0.8))

    assert build is not None
    assert build.action.kind is BotActionKind.PLACE_PREFAB
    assert build.action.tool_id == prefab_tool
    assert build.action.argument in observer.prefabs
    assert build.movement.affordance is MovementAffordance.BUILD_STEP


def test_skilled_grounded_bot_uses_bounded_combat_jump() -> None:
    world = _SwitchableWorld()
    brain = BotBrain(world, seed=11)
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        grounded=True,
    )
    enemy = _player_snapshot(2, 3, (15.0, 0.0, 0.0))
    profile = replace(_profile(), skill=0.95, aggression=0.95)
    frame = replace(_frame(1, observer, enemy), profile=profile)

    first = brain.decide(frame)
    second = brain.decide(replace(frame, frame_id=2))

    assert first is not None and first.movement.jump is True
    assert second is not None and second.movement.jump is False


def test_critical_bot_builds_replicated_block_line_cover_across_threat() -> None:
    class CoverWorld(_SwitchableWorld):
        @staticmethod
        def cover_direction(_position, _threat):
            return (0.0, 0.0, 0.0)

        @staticmethod
        def cover_build_line(_position, _threat):
            return (1, -2, 2), (1, 2, 2)

    brain = BotBrain(CoverWorld(), seed=3)
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        health=28,
        loadout=(DEFAULT_WEAPON_TOOL, int(C.BLOCK_TOOL)),
        blocks=50,
    )
    enemy = _player_snapshot(2, 3, (18.0, 0.0, 0.0))
    frame = replace(
        _frame(1, observer, enemy),
        profile=replace(_profile(), caution=0.95, creativity=0.9),
    )

    intent = brain.decide(frame)

    assert intent is not None
    assert intent.action.kind is BotActionKind.BUILD_LINE
    assert intent.action.position == (1.0, -2.0, 2.0)
    assert intent.action.end_position == (1.0, 2.0, 2.0)
    assert intent.debug_role == "combat_block_line_cover"


def test_miner_proactively_breaches_a_hidden_contact_obstruction() -> None:
    class MiningWorld(_SwitchableWorld):
        @staticmethod
        def blocking_cell(_position, _direction):
            return (1, 0, 1)

    world = MiningWorld()
    brain = BotBrain(world, seed=5)
    super_spade = int(C.SUPERSPADE_TOOL)
    observer = replace(
        _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True),
        class_id=int(C.CLASS_MINER),
        loadout=(DEFAULT_WEAPON_TOOL, super_spade),
    )
    enemy = _player_snapshot(2, 3, (12.0, 0.0, 0.0))
    assert brain.decide(_frame(1, observer, enemy)) is not None
    world.visible = False

    intent = brain.decide(_frame(2, observer, enemy))

    assert intent is not None
    assert intent.action.kind is BotActionKind.MELEE
    assert intent.action.tool_id == super_spade


def test_approximate_sound_can_be_investigated_but_never_triggers_fire() -> None:
    world = _SwitchableWorld()
    world.visible = False
    brain = BotBrain(world, seed=8)
    observer = _player_snapshot(1, 2, (0.0, 0.0, 0.0), is_bot=True)
    enemy = _player_snapshot(2, 3, (40.0, 30.0, 0.0))
    heard = BotStimulusBus()
    now = time.monotonic()
    heard.publish(
        StimulusKind.SHOT,
        enemy.position,
        source_id=enemy.player_id,
        radius=100.0,
        now=now,
    )
    frame = _frame(1, observer, enemy)
    frame = PerceptionFrame(
        frame_id=frame.frame_id,
        map_epoch=frame.map_epoch,
        mode_epoch=frame.mode_epoch,
        topology_version=frame.topology_version,
        observer_id=frame.observer_id,
        observer_generation=frame.observer_generation,
        created_at=now + 0.1,
        mode_id=frame.mode_id,
        players=frame.players,
        profile=frame.profile,
        stimuli=heard.perceive(
            observer.position, now=now + 0.1, rng=random.Random(4)
        ),
    )

    intent = brain.decide(frame)

    assert intent is not None
    assert intent.look is not None and intent.look.visible is False
    assert intent.look.target != enemy.position
    assert intent.action.kind is BotActionKind.NONE


def test_gateway_routes_fire_through_public_combat_service() -> None:
    packets = []
    combat = SimpleNamespace(
        handle_shot=lambda player, packet: (packets.append((player, packet)) or True),
        handle_weapon_reload=lambda _player: True,
    )
    server = SimpleNamespace(loop_count=123, combat=combat)
    player = SimpleNamespace(
        id=7,
        is_bot=True,
        alive=True,
        spawned=True,
        tool=DEFAULT_WEAPON_TOOL,
        orientation=(1.0, 0.0, 0.0),
        eye=(5.0, 6.0, 7.0),
    )
    gateway = BotActionGateway(server)

    accepted = gateway.execute(player, BotAction(BotActionKind.FIRE))

    assert accepted is True
    assert len(packets) == 1
    assert packets[0][1].shooter_id == player.id
    assert (packets[0][1].x, packets[0][1].y, packets[0][1].z) == player.eye


def test_gateway_routes_zombie_melee_through_public_combat_service() -> None:
    packets = []
    combat = SimpleNamespace(
        handle_shot=lambda player, packet: (packets.append((player, packet)) or True),
        handle_weapon_reload=lambda _player: True,
    )
    server = SimpleNamespace(loop_count=41, combat=combat)

    class Zombie:
        id = 9
        is_bot = True
        alive = True
        spawned = True
        tool = int(C.ZOMBIEHAND_TOOL)
        loadout = (int(C.ZOMBIEHAND_TOOL),)
        orientation = (1.0, 0.0, 0.0)
        eye = (5.0, 6.0, 7.0)

        @staticmethod
        def set_tool(tool_id, raw=True):
            Zombie.tool = int(tool_id)

        @staticmethod
        def is_spade_tool():
            return True

    accepted = BotActionGateway(server).execute(
        Zombie(),
        BotAction(
            BotActionKind.MELEE,
            tool_id=int(C.ZOMBIEHAND_TOOL),
        ),
    )

    assert accepted is True
    assert len(packets) == 1
