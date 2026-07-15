"""Regression coverage for the retail client's Snowball blast prediction.

The native client applies remote explosion impulse from ``Damage(37)``.  A
``DestroyEntity(19)`` removes the flying Snowball and plays its effect, but it
does not update player velocity.  The authoritative detonation path therefore
has to send one zero-damage prediction event before removing the projectile.
"""

from collections import deque
from types import SimpleNamespace

import pytest

import shared.constants as C
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.projectiles import Explosion, PROJECTILE_SPECS
from shared.bytes import ByteReader
from shared.packet import BlockBuildColored, Damage, DestroyEntity


class RecordingConnection:
    """Capture packets and their ENet reliability flag for one in-game peer."""

    def __init__(self, player):
        self.player = player
        self.in_game = True
        self.sent: list[tuple[bytes, bool]] = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append((bytes(data), bool(reliable)))


class JoiningConnection(RecordingConnection):
    """A peer whose MapSync catch-up journal is currently active."""

    def __init__(self, player):
        super().__init__(player)
        self.in_game = False
        self.map_mutation_watermark = 0
        self.map_mutation_overflow = False


class BlastPlayer:
    """Small real-blast participant with observable health and velocity."""

    def __init__(self, player_id, team, position):
        self.id = int(player_id)
        self.name = f"player-{player_id}"
        self.team = int(team)
        self.x, self.y, self.z = (float(value) for value in position)
        self.alive = True
        self.spawned = True
        self.input = SimpleNamespace(crouch=False)
        self.health = 100
        self._velocity = (0.0, 0.0, 0.0)
        self.damage_calls = []

    @property
    def position(self):
        return self.x, self.y, self.z

    @property
    def velocity(self):
        return self._velocity

    @velocity.setter
    def velocity(self, value):
        self._velocity = tuple(float(component) for component in value)

    def damage(self, amount, source=None, kill_type=0):
        self.damage_calls.append((int(amount), source, int(kill_type)))
        self.health -= int(amount)


def _snowball_explosion(
    server,
    impact,
    thrower_id,
    *,
    contact_block=None,
    color=None,
    source_loop=None,
):
    """Create a registered Snowball projectile already at its impact point."""
    spec = PROJECTILE_SPECS[int(C.SNOWBLOWER_TOOL)]
    projectile = server.projectile_engine.spawn(
        int(C.SNOWBLOWER_TOOL), impact, (0.0, 0.0, 0.0), 0.0,
        int(thrower_id), now=0.0,
    )
    assert projectile is not None

    entity = server.entity_registry.place(
        int(spec.entity_type), *impact, kind="projectile",
        player_id=int(thrower_id),
    )
    projectile.entity_id = entity.entity_id
    projectile.contact_block = contact_block
    projectile.block_color = color
    projectile.source_loop = source_loop
    return Explosion(projectile)


def _packet_ids(connection):
    return [data[0] for data, _reliable in connection.sent]


def test_snowball_broadcasts_one_prediction_damage_before_destroy():
    impact = (100.25, 101.5, 30.75)
    thrower = BlastPlayer(7, 0, (200.0, 200.0, 30.0))
    connection = RecordingConnection(thrower)
    server = BattleSpadesServer(ServerConfig(build_damage=False))
    server.players = {thrower.id: thrower}
    server.connections = {object(): connection}
    explosion = _snowball_explosion(server, impact, thrower.id)

    server._explode_projectile(explosion)

    assert _packet_ids(connection) == [Damage.id, DestroyEntity.id]
    damage_frames = [
        (data, reliable)
        for data, reliable in connection.sent
        if data[0] == Damage.id
    ]
    assert len(damage_frames) == 1
    damage_data, reliable = damage_frames[0]
    assert reliable is True

    packet = Damage(ByteReader(damage_data[1:]))
    assert packet.player_id == thrower.id
    assert packet.type == explosion.spec.damage_type == 20
    assert packet.damage == 0.0
    assert packet.face == 0
    assert packet.chunk_check == 0
    assert packet.seed == 0
    assert packet.causer_id == explosion.entity_id
    assert packet.causer_id == 0  # entity zero is valid on the retail wire
    assert packet.position == pytest.approx(impact, abs=1e-6)


def test_snowball_impulse_waits_for_three_subsequent_observed_frames():
    """Prediction phase follows ClientData observations, not server clock."""

    impact = (100.0, 100.0, 30.0)
    thrower = BlastPlayer(7, TEAM1, (200.0, 200.0, 30.0))
    target = BlastPlayer(8, TEAM2, (101.0, 100.0, 30.0))
    queued = []
    target.queue_explosion_impulse = (
        lambda after_frames, origin, radius, minimum, maximum:
        queued.append((after_frames, origin, radius, minimum, maximum))
    )
    server = BattleSpadesServer(ServerConfig(build_damage=False))
    server.loop_count = 100
    server.players = {thrower.id: thrower, target.id: target}
    server.connections = {object(): RecordingConnection(target)}
    explosion = _snowball_explosion(server, impact, thrower.id)

    server._explode_projectile(explosion)

    assert len(queued) == 1
    assert queued[0][0] == 3
    assert queued[0][1] == impact
    assert target.velocity == (0.0, 0.0, 0.0)


def test_stale_projectile_entity_does_not_emit_prediction_or_destroy():
    """A Damage causer must still exist when the retail client resolves it."""
    impact = (100.25, 101.5, 30.75)
    thrower = BlastPlayer(7, 0, (200.0, 200.0, 30.0))
    connection = RecordingConnection(thrower)
    server = BattleSpadesServer(ServerConfig(build_damage=False))
    server.players = {thrower.id: thrower}
    server.connections = {object(): connection}
    explosion = _snowball_explosion(server, impact, thrower.id)
    assert server.entity_registry.remove(explosion.entity_id) is not None

    server._explode_projectile(explosion)

    assert connection.sent == []


def test_disconnect_cancels_owned_projectile_before_player_id_reuse():
    """An old projectile must never attribute damage to a replacement id."""
    thrower = BlastPlayer(0, TEAM1, (100.0, 100.0, 30.0))
    observer_player = BlastPlayer(1, TEAM2, (110.0, 100.0, 30.0))
    observer = RecordingConnection(observer_player)
    disconnected = []
    peer = object()
    owner_connection = SimpleNamespace(
        player=thrower,
        reserved_player_id=None,
        on_disconnect=lambda: disconnected.append(True),
    )
    server = BattleSpadesServer(ServerConfig(build_damage=False))
    server.players = {thrower.id: thrower, observer_player.id: observer_player}
    server.connections = {peer: owner_connection, object(): observer}
    server.teams[thrower.team].add_player(thrower)
    projectile = server.projectile_engine.spawn(
        int(C.SNOWBLOWER_TOOL), thrower.position, (50.0, 0.0, 0.0),
        0.0, thrower.id, now=0.0,
    )
    entity = server.entity_registry.place(
        int(projectile.spec.entity_type), *thrower.position,
        kind="projectile", player_id=thrower.id,
    )
    projectile.entity_id = entity.entity_id
    server._pending_ingame_packets = deque(
        [(owner_connection, b"stale-use-oriented-item")]
    )

    server._on_disconnect_sync(peer)

    assert server.projectile_engine.projectiles == []
    assert list(server._pending_ingame_packets) == []
    assert server.entity_registry.get(entity.entity_id) is None
    assert DestroyEntity.id in _packet_ids(observer)
    assert disconnected == [True]


def test_snowball_prediction_is_not_replayed_as_a_map_mutation():
    """An ephemeral blast cannot hit a joiner after its MapSync finishes."""
    impact = (100.25, 101.5, 30.75)
    thrower = BlastPlayer(7, 0, (200.0, 200.0, 30.0))
    observer = RecordingConnection(thrower)
    joiner = JoiningConnection(None)
    server = BattleSpadesServer(ServerConfig(build_damage=False))
    server.players = {thrower.id: thrower}
    server.connections = {object(): observer, object(): joiner}
    explosion = _snowball_explosion(server, impact, thrower.id)

    server._explode_projectile(explosion)

    assert _packet_ids(observer) == [Damage.id, DestroyEntity.id]
    assert joiner.sent == []
    assert server._map_mutation_sequence == 0
    assert list(server._map_mutation_journal) == []


def test_snowball_prediction_reaches_friendly_and_enemy_without_changing_policy():
    impact = (100.0, 100.0, 30.0)
    thrower = BlastPlayer(1, 0, (200.0, 200.0, 30.0))
    teammate = BlastPlayer(2, 0, (102.0, 100.0, 30.0))
    enemy = BlastPlayer(3, 1, (102.0, 100.0, 30.0))
    teammate_connection = RecordingConnection(teammate)
    enemy_connection = RecordingConnection(enemy)
    server = BattleSpadesServer(
        ServerConfig(build_damage=False, friendly_fire=False)
    )
    server.players = {
        thrower.id: thrower,
        teammate.id: teammate,
        enemy.id: enemy,
    }
    server.connections = {
        object(): teammate_connection,
        object(): enemy_connection,
    }
    server._blocked_los = lambda *_args: False
    explosion = _snowball_explosion(server, impact, thrower.id)

    server._explode_projectile(explosion)

    assert teammate.health == 100
    assert teammate.damage_calls == []
    assert teammate.velocity == pytest.approx((0.3, 0.0, 0.0))
    assert enemy.health == 90
    assert enemy.damage_calls == [(10, thrower, explosion.spec.kill_type)]
    assert enemy.velocity == pytest.approx((0.3, 0.0, 0.0))

    for connection in (teammate_connection, enemy_connection):
        damage_frames = [
            (data, reliable)
            for data, reliable in connection.sent
            if data[0] == Damage.id
        ]
        assert len(damage_frames) == 1
        assert damage_frames[0][1] is True


def test_block_cannon_world_contact_builds_colored_voxel_and_journals_it():
    """A terrain hit is a persistent colored build, not only a weak blast."""
    impact = (100.75, 100.25, 61.25)
    build_cell = (100, 100, 61)
    contact_cell = (101, 100, 61)
    selected_color = 0x2468AC
    thrower = BlastPlayer(7, TEAM1, (90.0, 100.0, 61.0))
    observer = RecordingConnection(thrower)
    joiner = JoiningConnection(None)
    server = BattleSpadesServer(ServerConfig(build_damage=False))
    server.world_manager.generate_flat_map()
    server.world_manager.set_block(*contact_cell, True, 0x777777)
    server.players = {thrower.id: thrower}
    server.connections = {object(): observer, object(): joiner}
    explosion = _snowball_explosion(
        server,
        impact,
        thrower.id,
        contact_block=contact_cell,
        color=selected_color,
        source_loop=321,
    )

    server._explode_projectile(explosion)

    assert server.world_manager.get_solid(*build_cell)
    assert server.world_manager.get_color(*build_cell) & 0xFFFFFF == selected_color
    assert _packet_ids(observer) == [
        BlockBuildColored.id,
        Damage.id,
        DestroyEntity.id,
    ]
    build = BlockBuildColored(ByteReader(observer.sent[0][0][1:]))
    assert (build.x, build.y, build.z) == build_cell
    assert build.color == selected_color
    assert build.loop_count == 321
    assert build.player_id == thrower.id
    assert joiner.sent == []
    assert server._map_mutation_sequence == 1
    assert len(server._map_mutation_journal) == 1


def test_block_cannon_player_contact_does_not_create_unsupported_voxel():
    """A shot stopped by a player remains a blast and never builds in air."""
    impact = (100.75, 100.25, 61.25)
    thrower = BlastPlayer(7, TEAM1, (90.0, 100.0, 61.0))
    observer = RecordingConnection(thrower)
    server = BattleSpadesServer(ServerConfig(build_damage=False))
    server.world_manager.generate_flat_map()
    server.players = {thrower.id: thrower}
    server.connections = {object(): observer}
    explosion = _snowball_explosion(
        server,
        impact,
        thrower.id,
        contact_block=None,
        color=0x123456,
        source_loop=111,
    )

    server._explode_projectile(explosion)

    assert not server.world_manager.get_solid(100, 100, 61)
    assert _packet_ids(observer) == [Damage.id, DestroyEntity.id]
    assert server._map_mutation_sequence == 0
