"""Anti-cheat checks on combat, terrain and deployable actions.

Every forged case (origin behind a wall, far melee, occluded build/dig/
placement, packet 35, skipped Blocksucker warm-up, unmounted MG) is rejected,
while the legitimate stock-client geometry next to it -- shooting through an
open doorway, shooting while hugging a wall, building against the wall the
player looks at -- keeps working. Log-only mode must change nothing.
"""

import asyncio
import math
import time

import pytest

import shared.constants as C
import server.handlers.deployables as deployable_handlers
from protocol.packet_handler import PacketHandler
from server import action_clock
from server.combat_runtime import (
    anticheat_stats,
    cell_visible,
    segment_clear,
    seed_chi_square,
)
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.oriented_actions import OrientedActionService
from server.player import Player
from shared.packet import (
    BlockBuild,
    BlockLiberate,
    BlockLine,
    BlockSuckerPacket,
    PaintBlockPacket,
    ShootPacket,
)


EYE_Z = 59.75
WALL_X = 103


class _Connection:
    def __init__(self, server):
        self.server = server
        self.player = None
        self.in_game = True
        self.sent = []
        self.reserved_player_id = None

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append((data, reliable))

    def on_disconnect(self):
        pass


def _server():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    return server


def _player(server, tool, *, player_id=0, team=TEAM1,
            position=(100.5, 100.5, EYE_Z), loadout=None, class_id=None):
    connection = _Connection(server)
    player = Player(player_id, f"AC{player_id}", team, C.RIFLE_TOOL, connection)
    connection.player = player
    player.class_id = int(C.CLASS_SOLDIER if class_id is None else class_id)
    player.loadout = list(loadout if loadout is not None else [tool])
    player.spawn(*position)
    player.set_tool(tool, raw=True)
    # Past spawn protection so authoritative damage lands.
    player.spawned_at = time.monotonic() - 60.0
    server.players[player.id] = player
    server.connections[player.id] = connection
    server.teams[team].add_player(player)
    return player


def _wall(server, x=WALL_X, *, doorway=False):
    """A solid wall plane at ``x`` (y 95..105, z 55..61) on the flat map."""
    for y in range(95, 106):
        for z in range(55, 62):
            if doorway and y == 100 and 58 <= z <= 61:
                continue
            server.world_manager.set_block(x, y, z, True, (90, 90, 90))


def _shot(player, origin=None, direction=None, *, seed=0, loop=1):
    packet = ShootPacket()
    packet.loop_count = loop
    packet.shooter_id = player.id
    packet.shot_on_world_update = 0
    packet.x, packet.y, packet.z = origin if origin is not None else player.eye
    packet.ori_x, packet.ori_y, packet.ori_z = (
        direction if direction is not None else player.orientation
    )
    packet.damage = 0.0
    packet.penetration = 0
    packet.affect_shooter = 0
    packet.secondary = 0
    packet.seed = seed
    return packet


def _aim(shooter, target, height=0.8, origin=None):
    origin = origin if origin is not None else shooter.eye
    delta = (
        target.x - origin[0],
        target.y - origin[1],
        target.z + height - origin[2],
    )
    length = math.sqrt(sum(c * c for c in delta))
    direction = tuple(c / length for c in delta)
    shooter.orientation = direction
    return direction


def _counts(player):
    return getattr(player, "anticheat_counts", {})


def _handle(server, player, packet):
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))


def _fire(server, shooter, packet):
    # These tests isolate geometry/statistics from cadence (covered separately).
    action_clock.reset(shooter, "fire")
    shooter.next_shot_time = 0.0
    shooter.last_shot_time = 0.0
    return server.combat.handle_shot(shooter, packet)


# --- voxel line of sight ---------------------------------------------------

def test_segment_clear_detects_walls_and_ignores_brushed_faces():
    server = _server()
    world = server.world_manager
    _wall(server)
    assert segment_clear(world, (100.5, 100.5, EYE_Z), (102.5, 100.5, EYE_Z))
    assert not segment_clear(world, (100.5, 100.5, EYE_Z), (104.5, 100.5, EYE_Z))
    # An eye brushing the wall (0.2 from the face) moving along it.
    assert segment_clear(world, (102.8, 99.0, EYE_Z), (102.8, 101.5, EYE_Z))
    # Endpoints touching the face are not occluded by it.
    assert segment_clear(world, (101.0, 100.5, EYE_Z), (103.05, 100.5, EYE_Z))
    # Diagonal through the wall is still caught.
    assert not segment_clear(world, (101.0, 98.0, EYE_Z), (105.0, 102.0, 58.0))


def test_cell_visible_accepts_the_face_cell_and_rejects_behind_the_wall():
    server = _server()
    _wall(server)
    eyes = [(100.5, 100.5, EYE_Z)]
    world = server.world_manager
    assert cell_visible(world, eyes, (WALL_X - 1, 100, 60))  # air in front
    assert cell_visible(world, eyes, (WALL_X, 100, 60))  # the wall face voxel
    assert not cell_visible(world, eyes, (WALL_X + 1, 100, 60))


# --- 1. shooting / knifing / digging through walls -------------------------

def test_rifle_with_forged_origin_behind_a_wall_is_rejected():
    server = _server()
    _wall(server)
    shooter = _player(server, C.RIFLE_TOOL)
    target = _player(server, C.RIFLE_TOOL, player_id=1, team=TEAM2,
                     position=(106.5, 100.5, EYE_Z))
    forged_origin = (104.5, 100.5, EYE_Z)  # 4 blocks, on the far side
    direction = _aim(shooter, target, origin=forged_origin)

    assert _fire(server, shooter, _shot(shooter, forged_origin, direction)) is False
    assert target.health == 100
    assert shooter.anticheat_counts["shot_origin_occluded"] == 1


def test_legit_shot_from_the_eye_through_an_open_doorway_hits():
    server = _server()
    _wall(server, doorway=True)
    shooter = _player(server, C.RIFLE_TOOL)
    target = _player(server, C.RIFLE_TOOL, player_id=1, team=TEAM2,
                     position=(106.5, 100.5, EYE_Z))
    direction = _aim(shooter, target)

    assert _fire(server, shooter, _shot(shooter, None, direction)) is True
    assert target.health < 100
    assert not _counts(shooter)


def test_shot_while_hugging_a_wall_with_lagging_eye_still_hits():
    server = _server()
    _wall(server)
    # Eye 0.45 from the wall face (body radius), strafing along it: the
    # client's origin is 0.5 ahead of the lagging server eye.
    shooter = _player(server, C.RIFLE_TOOL, position=(102.55, 100.0, EYE_Z))
    shooter.velocity = (0.0, 0.25, 0.0)
    target = _player(server, C.RIFLE_TOOL, player_id=1, team=TEAM2,
                     position=(96.5, 100.5, EYE_Z))
    origin = (102.55, 100.5, EYE_Z)
    direction = _aim(shooter, target, origin=origin)

    assert _fire(server, shooter, _shot(shooter, origin, direction)) is True
    assert target.health < 100
    assert _counts(shooter).get("shot_origin_occluded", 0) == 0


def test_knife_through_wall_with_forged_origin_is_rejected():
    server = _server()
    _wall(server)
    attacker = _player(server, C.KNIFE_TOOL, loadout=[C.KNIFE_TOOL])
    target = _player(server, C.RIFLE_TOOL, player_id=1, team=TEAM2,
                     position=(107.5, 100.5, EYE_Z))
    forged_origin = (105.5, 100.5, EYE_Z)
    direction = _aim(attacker, target, origin=forged_origin)

    _fire(server, attacker, _shot(attacker, forged_origin, direction))

    assert target.health == 100
    assert attacker.anticheat_counts["shot_origin_occluded"] == 1


def test_knife_with_open_air_origin_offset_cannot_reach_beyond_melee_range():
    server = _server()
    attacker = _player(server, C.KNIFE_TOOL, loadout=[C.KNIFE_TOOL])
    target = _player(server, C.RIFLE_TOOL, player_id=1, team=TEAM2,
                     position=(109.5, 100.5, EYE_Z))
    # Origin 6 blocks ahead in open air (inside the coarse 8-block cap).
    forged_origin = (106.5, 100.5, EYE_Z)
    direction = _aim(attacker, target, origin=forged_origin)

    _fire(server, attacker, _shot(attacker, forged_origin, direction))

    assert target.health == 100
    assert attacker.anticheat_counts["melee_out_of_reach"] == 1


def test_knife_from_the_eye_does_not_connect_through_a_wall():
    server = _server()
    _wall(server, x=102)
    attacker = _player(server, C.KNIFE_TOOL, loadout=[C.KNIFE_TOOL])
    target = _player(server, C.RIFLE_TOOL, player_id=1, team=TEAM2,
                     position=(103.5, 100.5, EYE_Z))
    direction = _aim(attacker, target)

    _fire(server, attacker, _shot(attacker, None, direction))

    assert target.health == 100


def test_legit_knife_hit_in_reach_still_lands():
    server = _server()
    attacker = _player(server, C.KNIFE_TOOL, loadout=[C.KNIFE_TOOL])
    target = _player(server, C.RIFLE_TOOL, player_id=1, team=TEAM2,
                     position=(102.0, 100.5, EYE_Z))
    direction = _aim(attacker, target)

    _fire(server, attacker, _shot(attacker, None, direction))

    assert target.health < 100


def test_spade_dig_with_forged_origin_behind_wall_is_rejected():
    server = _server()
    _wall(server)
    _wall(server, x=106)
    digger = _player(server, C.SPADE_TOOL, loadout=[C.SPADE_TOOL])
    forged_origin = (104.5, 100.5, EYE_Z)
    digger.orientation = (1.0, 0.0, 0.0)

    _fire(server, digger, _shot(digger, forged_origin, (1.0, 0.0, 0.0)))

    assert server.world_manager.get_solid(106, 100, 60)
    assert digger.anticheat_counts["shot_origin_occluded"] == 1


def test_spade_dig_from_open_air_offset_cannot_reach_far_cells():
    server = _server()
    _wall(server, x=109)
    digger = _player(server, C.SPADE_TOOL, loadout=[C.SPADE_TOOL])
    digger.orientation = (1.0, 0.0, 0.0)
    forged_origin = (106.5, 100.5, EYE_Z)  # open air, 6 blocks ahead

    _fire(server, digger, _shot(digger, forged_origin, (1.0, 0.0, 0.0)))

    assert server.world_manager.get_solid(109, 100, 59)
    assert digger.anticheat_counts["dig_out_of_reach"] == 1


def test_bot_shots_from_the_server_eye_are_never_rejected():
    server = _server()
    _wall(server)
    bot = _player(server, C.RIFLE_TOOL, position=(102.55, 100.5, EYE_Z))
    bot.is_bot = True
    target = _player(server, C.RIFLE_TOOL, player_id=1, team=TEAM2,
                     position=(96.5, 100.5, EYE_Z))
    direction = _aim(bot, target)
    packet = _shot(bot, None, direction, loop=int(server.loop_count))

    assert _fire(server, bot, packet) is True
    assert target.health < 100
    assert not _counts(bot)


# --- 1b/2. soft origin + aim checks: log-only by default -------------------

def _open_air_drift_case(server):
    shooter = _player(server, C.RIFLE_TOOL)
    target = _player(server, C.RIFLE_TOOL, player_id=1, team=TEAM2,
                     position=(110.5, 100.5, EYE_Z))
    origin = (100.5, 97.5, EYE_Z)  # 3 blocks sideways, clear air
    direction = _aim(shooter, target, origin=origin)
    return shooter, target, _shot(shooter, origin, direction)


def test_origin_drift_is_log_only_by_default():
    server = _server()
    shooter, target, packet = _open_air_drift_case(server)

    assert _fire(server, shooter, packet) is True
    assert target.health < 100
    assert shooter.anticheat_counts["shot_origin_drift:observed"] == 1
    assert "shot_origin_drift" not in _counts(shooter)
    assert sum(anticheat_stats(shooter)["origin_error_fallback"].values()) == 1


def test_origin_drift_is_rejected_when_enforced():
    server = _server()
    server.config.anticheat.enforce_shot_origin = True
    shooter, target, packet = _open_air_drift_case(server)

    assert _fire(server, shooter, packet) is False
    assert target.health == 100
    assert shooter.anticheat_counts["shot_origin_drift"] == 1


def test_origin_drift_uses_per_loop_eye_when_available(monkeypatch):
    server = _server()
    server.config.anticheat.enforce_shot_origin = True
    shooter, target, packet = _open_air_drift_case(server)
    claimed = (packet.x, packet.y, packet.z)
    # The shot's own frame put the eye exactly at the claimed origin.
    monkeypatch.setattr(
        shooter, "eye_at_loop",
        lambda loop: claimed if loop == packet.loop_count else None,
    )

    assert _fire(server, shooter, packet) is True
    assert target.health < 100
    assert sum(anticheat_stats(shooter)["origin_error"].values()) == 1


def _aim_off_case(server):
    shooter = _player(server, C.RIFLE_TOOL)
    target = _player(server, C.RIFLE_TOOL, player_id=1, team=TEAM2,
                     position=(106.5, 100.5, EYE_Z))
    direction = _aim(shooter, target)
    # Reported view is 30 degrees away from the fired direction.
    angle = math.radians(30.0)
    shooter.orientation = (
        direction[0] * math.cos(angle) - direction[1] * math.sin(angle),
        direction[0] * math.sin(angle) + direction[1] * math.cos(angle),
        direction[2],
    )
    return shooter, target, _shot(shooter, None, direction)


def test_aim_mismatch_is_log_only_by_default_and_measured():
    server = _server()
    shooter, target, packet = _aim_off_case(server)

    assert _fire(server, shooter, packet) is True
    assert target.health < 100
    assert shooter.anticheat_counts["aim_direction_mismatch:observed"] == 1
    assert anticheat_stats(shooter)["aim_angle_fallback"]["<=45"] == 1


def test_aim_mismatch_is_rejected_when_enforced():
    server = _server()
    server.config.anticheat.enforce_aim_direction = True
    shooter, target, packet = _aim_off_case(server)

    assert _fire(server, shooter, packet) is False
    assert target.health == 100
    assert shooter.anticheat_counts["aim_direction_mismatch"] == 1


def test_aim_compares_against_the_shots_own_frame(monkeypatch):
    server = _server()
    server.config.anticheat.enforce_aim_direction = True
    shooter, target, packet = _aim_off_case(server)
    fired = (packet.ori_x, packet.ori_y, packet.ori_z)
    monkeypatch.setattr(shooter, "orientation_at_loop", lambda loop: fired)

    assert _fire(server, shooter, packet) is True
    assert anticheat_stats(shooter)["aim_angle"]["<=0.5"] == 1


# --- 7/8. crouch hitbox + per-player aggregates ----------------------------

def test_hit_resolution_uses_simulated_crouch_not_the_input_bit(monkeypatch):
    server = _server()
    target = _player(server, C.RIFLE_TOOL, player_id=1, team=TEAM2,
                     position=(106.5, 100.5, 60.0))
    target.set_orientation_vector(0.0, 1.0, 0.0)
    target.input.crouch = True  # raw bit says crouched...
    monkeypatch.setattr(type(target), "hitbox_crouched",
                        property(lambda self: False), raising=False)
    # ...but the simulated body stands: the standing centre-gap ray (which
    # misses the two crouched leg boxes) must use the standing parts.
    standing = server.combat._ray_hits_target(
        (target.x - 5.0, target.y + 0.3, target.z + 1.2),
        (1.0, 0.0, 0.0), 10.0, target,
    )
    monkeypatch.setattr(type(target), "hitbox_crouched",
                        property(lambda self: True), raising=False)
    crouched = server.combat._ray_hits_target(
        (target.x - 5.0, target.y + 0.3, target.z + 1.2),
        (1.0, 0.0, 0.0), 10.0, target,
    )
    assert (standing is None) != (crouched is None) or standing != crouched


def test_accepted_shots_fold_into_per_weapon_aggregates():
    server = _server()
    shooter = _player(server, C.RIFLE_TOOL)
    target = _player(server, C.RIFLE_TOOL, player_id=1, team=TEAM2,
                     position=(106.5, 100.5, EYE_Z))
    hit_dir = _aim(shooter, target)
    _fire(server, shooter, _shot(shooter, None, hit_dir))
    miss_dir = _aim(shooter, target, height=-6.0)
    _fire(server, shooter, _shot(shooter, None, miss_dir))

    stats = anticheat_stats(shooter)
    weapon = stats["weapons"][int(C.RIFLE_TOOL)]
    assert stats["shots"] == weapon["shots"] == 2
    assert stats["hits"] == weapon["hits"] == 1
    assert sum(stats["origin_error_fallback"].values()) == 2


def test_bots_do_not_pollute_aggregates():
    server = _server()
    bot = _player(server, C.RIFLE_TOOL)
    bot.is_bot = True
    bot.orientation = (1.0, 0.0, 0.0)
    _fire(server, bot, _shot(bot, None, (1.0, 0.0, 0.0)))
    assert getattr(bot, "anticheat_stats", None) is None


# --- 5. shotgun seed skew ---------------------------------------------------

def test_repeated_pellet_seed_is_flagged_log_only():
    server = _server()
    shooter = _player(server, C.SHOTGUN_TOOL, loadout=[C.SHOTGUN_TOOL])
    shooter.orientation = (1.0, 0.0, 0.0)
    for _ in range(40):
        shooter.ammo_clip = 5
        shooter.reloading = False
        _fire(server, shooter, _shot(shooter, None, (1.0, 0.0, 0.0), seed=7))

    seeds = anticheat_stats(shooter)["pellet_seeds"]
    assert seeds[7] == 40
    assert shooter.anticheat_counts["pellet_seed_skew:observed"] >= 1
    assert seed_chi_square(seeds) > 1000.0


def test_uniform_seeds_are_not_flagged():
    server = _server()
    shooter = _player(server, C.SHOTGUN_TOOL, loadout=[C.SHOTGUN_TOOL])
    shooter.orientation = (1.0, 0.0, 0.0)
    for index in range(64):
        shooter.ammo_clip = 5
        shooter.reloading = False
        _fire(server, shooter,
              _shot(shooter, None, (1.0, 0.0, 0.0), seed=1 + (index * 37) % 255))

    assert "pellet_seed_skew:observed" not in _counts(shooter)


# --- building / digging / painting reach + LOS -----------------------------

def _build_packet(cell, loop=1):
    packet = BlockBuild()
    packet.loop_count = loop
    packet.player_id = 0
    packet.x, packet.y, packet.z = cell
    packet.block_type = 0
    return packet


def test_building_against_the_wall_you_look_at_works(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("server.combat_runtime.time.monotonic", lambda: clock[0])
    server = _server()
    _wall(server)
    builder = _player(server, C.BLOCK_TOOL, loadout=[C.BLOCK_TOOL])
    builder.blocks = 10
    cells = [(WALL_X - 1, 100, 60), (WALL_X - 1, 97, 57), (WALL_X - 1, 103, 55)]
    for cell in cells:
        # The stock BlockTool fires every 0.5 s, beyond MIN_BLOCK_INTERVAL.
        assert server.combat.handle_block_build(builder, _build_packet(cell))
        clock[0] += 0.5
    assert builder.blocks == 10 - len(cells)  # reserved for commit
    assert not _counts(builder)


def test_building_behind_a_wall_is_rejected():
    server = _server()
    _wall(server)
    builder = _player(server, C.BLOCK_TOOL, loadout=[C.BLOCK_TOOL])
    builder.blocks = 10
    hidden = (WALL_X + 1, 100, 60)  # face-supported by the wall's back

    assert not server.combat.handle_block_build(builder, _build_packet(hidden))
    assert builder.blocks == 10
    assert builder.anticheat_counts["build_occluded"] == 1


def test_block_line_behind_a_wall_is_rejected_but_in_front_is_accepted():
    server = _server()
    _wall(server)
    builder = _player(server, C.BLOCK_TOOL, loadout=[C.BLOCK_TOOL])
    builder.blocks = 20

    def line(a, b):
        packet = BlockLine()
        packet.loop_count = 1
        packet.player_id = 0
        packet.x1, packet.y1, packet.z1 = a
        packet.x2, packet.y2, packet.z2 = b
        return packet

    assert not server.combat.handle_block_line(
        builder, line((WALL_X + 1, 98, 61), (WALL_X + 1, 102, 61)))
    assert server.combat.handle_block_line(
        builder, line((WALL_X - 1, 98, 61), (WALL_X - 1, 102, 61)))


def test_paint_behind_a_wall_is_rejected():
    server = _server()
    _wall(server)
    _wall(server, x=WALL_X + 2)
    painter = _player(server, C.BLOCK_TOOL, loadout=[C.BLOCK_TOOL])

    def paint(cell):
        packet = PaintBlockPacket()
        packet.loop_count = 1
        packet.x, packet.y, packet.z = cell
        packet.color = (1, 2, 3)
        return packet

    assert not server.combat.handle_paint_packet(painter, paint((WALL_X + 2, 100, 60)))
    assert server.combat.handle_paint_packet(painter, paint((WALL_X, 100, 60)))


# --- 3. BlockLiberate(35) ---------------------------------------------------

def _liberate(cell):
    packet = BlockLiberate()
    packet.loop_count = 1
    packet.player_id = 0
    packet.x, packet.y, packet.z = cell
    return packet


def test_block_liberate_outside_ugc_is_a_protocol_violation(monkeypatch):
    server = _server()
    player = _player(server, C.BLOCK_TOOL, loadout=[C.BLOCK_TOOL])
    kicked = []
    monkeypatch.setattr(player, "disconnect", lambda reason: kicked.append(reason))

    _handle(server, player, _liberate((100, 100, 62)))

    assert server.world_manager.get_solid(100, 100, 62)
    assert kicked == [int(C.DISCONNECT.ERROR_KICK_HACKING)]
    assert player.anticheat_counts["protocol:block_liberate"] == 1


def test_block_liberate_in_ugc_requires_line_of_sight():
    server = _server()
    server.config.ugc_runtime = True
    _wall(server)
    _wall(server, x=WALL_X + 2)
    player = _player(server, C.BLOCK_TOOL, loadout=[C.BLOCK_TOOL])

    assert not server.combat.handle_block_destroy(player, _liberate((WALL_X + 2, 100, 60)))
    assert player.anticheat_counts["liberate_occluded"] == 1
    assert server.combat.handle_block_destroy(player, _liberate((WALL_X, 100, 60)))


# --- oriented launches -------------------------------------------------------

def test_launch_point_behind_a_wall_or_far_away_is_rejected():
    server = _server()
    _wall(server, x=101)
    player = _player(server, C.GRENADE_TOOL, loadout=[C.GRENADE_TOOL])
    service = OrientedActionService(server)
    validate = service._validated_launch
    world = server.world_manager
    velocity = (10.0, 0.0, 0.0)

    # 2 blocks away, on the far side of the 1-block wall.
    assert validate(player, C.GRENADE_TOOL, (102.5, 100.5, EYE_Z), velocity,
                    2.0, world=world) is None
    # 5 blocks away in open air (was accepted up to 10).
    assert validate(player, C.GRENADE_TOOL, (100.5, 105.5, EYE_Z), velocity,
                    2.0, world=world) is None
    # Muzzle point nudged into the hugged wall is still a legal throw.
    assert validate(player, C.GRENADE_TOOL, (101.1, 100.5, EYE_Z), velocity,
                    2.0, world=world) is not None
    # Ordinary muzzle offset in open air.
    assert validate(player, C.GRENADE_TOOL, (100.5, 99.9, EYE_Z), velocity,
                    2.0, world=world) is not None


# --- deployables --------------------------------------------------------------

def test_c4_behind_a_wall_is_rejected_and_on_the_visible_face_accepted():
    server = _server()
    _wall(server)
    _wall(server, x=WALL_X + 2)
    miner = _player(server, C.C4_TOOL, loadout=[C.C4_TOOL],
                    class_id=C.CLASS_MINER, position=(101.5, 100.5, EYE_Z))
    service = server.deployable_actions

    assert not service.place_c4(miner, (WALL_X + 2, 100, 60), face=0)
    assert miner.anticheat_counts["placement_occluded"] == 1
    assert service.place_c4(miner, (WALL_X, 100, 60), face=0)


# --- 6. Blocksucker warm-up --------------------------------------------------

def test_block_sucker_full_power_requires_the_retail_warm_up(monkeypatch):
    server = _server()
    player = _player(server, C.BLOCK_SUCKER_TOOL, loadout=[C.BLOCK_SUCKER_TOOL],
                     class_id=C.CLASS_MINER)
    player.orientation = (0.0, 0.0, 1.0)
    clock = [5000.0]
    monkeypatch.setattr(deployable_handlers.time, "monotonic", lambda: clock[0])
    hits = []
    monkeypatch.setattr(server.combat, "_apply_block_damage",
                        lambda *args, **kwargs: hits.append(args) or True)

    def send(state, shot, loop):
        packet = BlockSuckerPacket()
        packet.loop_count = loop
        packet.shooter_id = player.id
        packet.state = int(state)
        packet.shot = shot
        _handle(server, player, packet)

    send(C.BLOCK_SUCKER_STATE_WARMING_UP, 0, 100)
    clock[0] += 0.1
    send(C.BLOCK_SUCKER_STATE_FULL_POWER, 1, 106)  # skipped the spin-up
    assert hits == []
    assert player.anticheat_counts["block_sucker_warmup"] == 1

    clock[0] += 1.0
    send(C.BLOCK_SUCKER_STATE_FULL_POWER, 1, 166)
    assert len(hits) == 1

    # Release, then a compressed arrival (lost/retransmitted state=1) is
    # still accepted when the client's own loop stamps show the full delay.
    send(C.BLOCK_SUCKER_STATE_INACTIVE, 0, 170)
    clock[0] += 2.0
    send(C.BLOCK_SUCKER_STATE_WARMING_UP, 0, 300)
    clock[0] += 0.4
    player._block_sucker_next_shot = 0.0
    send(C.BLOCK_SUCKER_STATE_FULL_POWER, 1, 360)
    assert len(hits) == 2
