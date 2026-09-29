"""KillAction presentation fields pinned to the stock client.

Evidence (docs/KILLFEED_RETAIL.md): GameScene.process_packet_kill_action
(gameScene.pyd 0x10194940) shows KILL2..KILL5/KILLM from ``kill_count`` and
the domination/revenge banners from the two flag bytes; HUD.add_kill
(hud.pyd 0x1008CD70) picks the feed icon from ``kill_type``.
MULTIKILLMAXTIMEGAP (6.0) is referenced by no client binary, so it is the
server's multikill window.
"""

import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *args, **kwargs: {}))

import pytest

import shared.constants as C
import server.player as player_module
from server import kill_feed
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2, WEAPON_CATALOG
from server.player import Player
from server.world_manager import WorldManager
from shared.bytes import ByteReader
from shared.packet import KillAction, SetHP


class _Connection:
    def __init__(self, server):
        self.server = server
        self.player = None
        self.sent_packets = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent_packets.append(bytes(data))


class _Server:
    def __init__(self):
        self.config = ServerConfig()
        self.config.log_suppress_packets = set()
        self.loop_count = 1
        self.players = {}
        self.connections = {}
        self.broadcast_packets = []
        self.world_manager = WorldManager(self.config)
        self.world_manager.generate_flat_map()

    def broadcast(self, data, exclude=None, reliable=True):
        self.broadcast_packets.append(bytes(data))

    def queue_mode_event(self, *args):
        pass


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(player_module.time, "monotonic", fake)
    return fake


def _player(server, player_id, team):
    connection = _Connection(server)
    player = Player(player_id, f"P{player_id}", team, C.RIFLE_TOOL, connection)
    connection.player = player
    player.spawn(100.5 + player_id, 100.5, 60.0)
    server.players[player_id] = player
    server.connections[player_id] = connection
    return player


def _last_kill(server):
    data = [d for d in server.broadcast_packets if d[0] == KillAction.id][-1]
    return KillAction(ByteReader(data[1:]))


def _kill(server, killer, victim, kill_type=0):
    if not victim.alive:
        victim.spawn(100.5 + victim.id, 100.5, 60.0)
    victim.die(killer=killer, kill_type=kill_type)
    return _last_kill(server)


def test_kill_count_is_the_six_second_multikill_chain(clock):
    server = _Server()
    killer = _player(server, 1, TEAM1)
    victims = [_player(server, pid, TEAM2) for pid in (2, 3, 4, 5)]

    assert _kill(server, killer, victims[0]).kill_count == 1
    clock.now += 6.0  # a gap of exactly MULTIKILLMAXTIMEGAP still chains
    assert _kill(server, killer, victims[1]).kill_count == 2
    clock.now += 1.0
    assert _kill(server, killer, victims[2]).kill_count == 3
    clock.now += 6.01
    assert _kill(server, killer, victims[3]).kill_count == 1
    # The life streak (profile/award stats) keeps counting separately.
    assert killer.kill_streak == 4


def test_multikill_chain_ends_with_the_killers_death(clock):
    server = _Server()
    killer = _player(server, 1, TEAM1)
    enemy = _player(server, 2, TEAM2)
    other = _player(server, 3, TEAM2)

    assert _kill(server, killer, enemy).kill_count == 1
    _kill(server, other, killer)
    killer.spawn(101.5, 100.5, 60.0)
    clock.now += 1.0
    assert _kill(server, killer, enemy).kill_count == 1


def test_uncredited_deaths_carry_no_banner_fields(clock):
    server = _Server()
    killer = _player(server, 1, TEAM1)
    mate = _player(server, 2, TEAM1)

    team_kill = _kill(server, killer, mate)
    fall = _kill(server, None, killer, int(C.FALL_KILL))
    for packet in (team_kill, fall):
        assert packet.kill_count == 0
        assert not packet.isDominationKill
        assert not packet.isRevengeKill
    assert fall.killer_id == killer.id  # world death: killer is the victim


def test_fourth_unanswered_kill_is_a_domination_once(clock):
    server = _Server()
    killer = _player(server, 1, TEAM1)
    victim = _player(server, 2, TEAM2)

    flags = []
    for _ in range(6):
        clock.now += 30.0
        packet = _kill(server, killer, victim)
        flags.append(bool(packet.isDominationKill))
    assert flags == [False, False, False, True, False, False]
    assert kill_feed.is_dominating(killer, victim)


def test_killing_your_dominator_is_revenge_and_ends_the_domination(clock):
    server = _Server()
    bully = _player(server, 1, TEAM1)
    victim = _player(server, 2, TEAM2)
    for _ in range(4):
        _kill(server, bully, victim)
    assert kill_feed.is_dominating(bully, victim)

    victim.spawn(102.5, 100.5, 60.0)
    revenge = _kill(server, victim, bully)
    assert revenge.isRevengeKill and not revenge.isDominationKill
    assert not kill_feed.is_dominating(bully, victim)

    # The chain restarts: three more kills are not yet a domination and a
    # later kill by the old victim is no longer revenge.
    bully.spawn(101.5, 100.5, 60.0)
    for _ in range(3):
        assert not _kill(server, bully, victim).isDominationKill
    victim.spawn(102.5, 100.5, 60.0)
    assert not _kill(server, victim, bully).isRevengeKill


def test_answering_kill_resets_the_unanswered_count(clock):
    server = _Server()
    a = _player(server, 1, TEAM1)
    b = _player(server, 2, TEAM2)
    for _ in range(3):
        _kill(server, a, b)
    b.spawn(102.5, 100.5, 60.0)
    _kill(server, b, a)  # not a revenge: a was not dominating yet
    a.spawn(101.5, 100.5, 60.0)
    for _ in range(3):
        assert not _kill(server, a, b).isDominationKill
    assert _kill(server, a, b).isDominationKill


def test_team_change_kill_clears_relations_like_the_client(clock):
    """Client lines 3671-3678 reset both flags on A429/A430 kills only."""

    server = _Server()
    bully = _player(server, 1, TEAM1)
    victim = _player(server, 2, TEAM2)
    for _ in range(4):
        _kill(server, bully, victim)

    victim.spawn(102.5, 100.5, 60.0)
    victim.die(kill_type=int(C.CLASS_CHANGE_KILL))
    assert kill_feed.is_dominating(bully, victim)

    victim.spawn(102.5, 100.5, 60.0)
    victim.die(kill_type=int(C.TEAM_CHANGE_KILL))
    assert not kill_feed.is_dominating(bully, victim)


def test_departing_id_leaves_no_relation_for_the_next_owner(clock):
    from server.round_lifecycle import RoundLifecycle

    server = _Server()
    server.entity_registry = SimpleNamespace(remove=lambda *_: None)
    bully = _player(server, 1, TEAM1)
    victim = _player(server, 2, TEAM2)
    for _ in range(4):
        _kill(server, bully, victim)
    lifecycle = RoundLifecycle(server)
    lifecycle.remove_owned_deployables = lambda player: None
    lifecycle.forget_player(victim)
    assert not kill_feed.is_dominating(bully, victim)
    assert bully._unanswered_kills == {}


def test_replayed_death_never_reannounces_banners(clock):
    server = _Server()
    bully = _player(server, 1, TEAM1)
    victim = _player(server, 2, TEAM2)
    for _ in range(4):
        clock.now += 1.0
        broadcast = _kill(server, bully, victim)
    assert broadcast.isDominationKill and broadcast.kill_count == 4

    replay = KillAction(ByteReader(victim.last_kill_action_data[1:]))
    assert (replay.player_id, replay.killer_id, replay.kill_type) == (
        broadcast.player_id,
        broadcast.killer_id,
        broadcast.kill_type,
    )
    assert replay.respawn_time == broadcast.respawn_time
    assert replay.kill_count == 0
    assert not replay.isDominationKill and not replay.isRevengeKill


def test_fractional_respawn_delay_rounds_up(clock):
    server = _Server()
    server.mode = SimpleNamespace(respawn_time_for=lambda player: 4.5)
    player = _player(server, 1, TEAM1)
    player.die(kill_type=int(C.FALL_KILL))
    assert _last_kill(server).respawn_time == 5

    server.mode = SimpleNamespace(respawn_time_for=lambda player: 7.0)
    player.spawn(101.5, 100.5, 60.0)
    player.die(kill_type=int(C.FALL_KILL))
    assert _last_kill(server).respawn_time == 7


def test_round_reset_clears_chains_and_dominations(clock):
    server = _Server()
    bully = _player(server, 1, TEAM1)
    victim = _player(server, 2, TEAM2)
    for _ in range(4):
        _kill(server, bully, victim)
    kill_feed.reset_all(server)
    assert not kill_feed.is_dominating(bully, victim)
    victim.spawn(102.5, 100.5, 60.0)
    assert _kill(server, bully, victim).kill_count == 1


def test_every_catalog_weapon_kill_type_has_a_client_icon():
    """HUD.add_kill has no branch for unknown types: the feed row would be
    iconless. UGC-only types (27-29) are the client's own gap."""

    ugc_only = {
        int(C.UGC_ROCKET2_KILL),
        int(C.UGC_DRILL_KILL),
        int(C.UGC_SNOWBALL_KILL),
    }
    missing = {
        name: int(profile.kill_type)
        for name, profile in WEAPON_CATALOG.items()
        if profile.kill_type is not None
        and int(profile.kill_type) not in kill_feed.HUD_KILL_ICONS
        and int(profile.kill_type) not in ugc_only
    }
    assert missing == {}


def test_burn_damage_uses_the_client_burn_hp_type():
    from server.fire import BURN_HP_DAMAGE_TYPE, FireController

    assert BURN_HP_DAMAGE_TYPE == 3
    server = _Server()
    owner = _player(server, 1, TEAM1)
    target = _player(server, 2, TEAM2)
    target.spawn_protection_remaining = lambda: 0.0
    controller = FireController(server)
    controller.ignite_player(target, owner.id, now=10.0)
    target.connection.sent_packets.clear()
    controller.update(now=10.31)

    hp = [
        SetHP(ByteReader(d[1:]))
        for d in target.connection.sent_packets
        if d[0] == SetHP.id
    ]
    assert hp and all(packet.damage_type == 3 for packet in hp)
    assert target.health < 100


def test_hit_and_world_damage_keep_their_hp_types():
    server = _Server()
    attacker = _player(server, 1, TEAM1)
    target = _player(server, 2, TEAM2)
    target.spawn_protection_remaining = lambda: 0.0

    target.damage(10, source=attacker, kill_type=0)
    target.damage(10, source=None, kill_type=int(C.FALL_KILL))
    types = [
        SetHP(ByteReader(d[1:])).damage_type
        for d in target.connection.sent_packets
        if d[0] == SetHP.id
    ]
    assert types[-2:] == [1, 0]
