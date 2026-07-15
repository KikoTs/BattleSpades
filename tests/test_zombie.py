"""Zombie infection lifecycle and retail snapshot regression tests."""

import asyncio
import sys
import time
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *args, **kwargs: {}))

import shared.constants as C  # noqa: E402
from modes import get_mode_class  # noqa: E402
from modes.zombie import (  # noqa: E402
    SURVIVOR_TEAM,
    ZOMBIE_TEAM,
    ZombieMode,
    ZombiePhase,
)
from server.builders.initial_info import build_initial_info  # noqa: E402
from server.builders.state_data import build_state_data  # noqa: E402
from server.class_selection import normalize_class_selection  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.round_lifecycle import RoundLifecycle  # noqa: E402
from server.team import Team  # noqa: E402
from shared.bytes import ByteReader  # noqa: E402
from shared.packet import ChangePlayer  # noqa: E402


class _Connection:
    def __init__(self):
        self.sent = []

    def send(self, data, **_kwargs):
        self.sent.append(bytes(data))


class _World:
    map_name = "CityOfChicago"
    map_file_crc = 0

    def get_spawn_point(self, team):
        if team == SURVIVOR_TEAM:
            return (64.5, 256.5, 223.75)
        return (448.5, 256.5, 223.75)


class _Server:
    def __init__(self):
        self.config = ServerConfig()
        self.config.default_mode = "zom"
        self.config.mode_settings = {"zom": {
            "infection_delay": 0.0,
            "first_infected": 1,
            "minimum_players": 2,
        }}
        self.world_manager = _World()
        self.teams = {
            SURVIVOR_TEAM: Team(SURVIVOR_TEAM, "Blue", (0, 0, 255)),
            ZOMBIE_TEAM: Team(ZOMBIE_TEAM, "Green", (0, 255, 0)),
        }
        self.players = {}
        self.packets = []
        self.mode = None

    def broadcast(self, data, **_kwargs):
        self.packets.append(bytes(data))


def _player(server, player_id, team=SURVIVOR_TEAM, class_id=C.CLASS_SOLDIER):
    player = SimpleNamespace(
        id=player_id,
        name=f"P{player_id}",
        team=team,
        alive=True,
        spawned=True,
        connection=_Connection(),
        class_id=int(class_id),
        loadout=[],
        prefabs=[],
        ugc_tools=[],
        pending_selection=None,
        pending_class_id=None,
        pending_loadout=None,
        score=0,
        death_time=0.0,
    )

    def apply(selection):
        player.class_id = int(selection.class_id)
        player.loadout = list(selection.loadout)
        player.prefabs = list(selection.prefabs)
        player.ugc_tools = list(selection.ugc_tools)

    def die(*, killer=None, kill_type=0):
        player.alive = False
        player.spawned = False
        player.death_time = time.time()

    player.apply_class_selection = apply
    player.die = die
    server.players[player_id] = player
    server.teams[team].add_player(player)
    return player


def _new_mode(player_count=0):
    server = _Server()
    for player_id in range(1, player_count + 1):
        _player(server, player_id)
    mode = ZombieMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    return server, mode


def _visibility_packets(data):
    return [
        ChangePlayer(ByteReader(packet[1:]))
        for packet in data
        if packet and packet[0] == ChangePlayer.id
    ]


def test_zombie_mode_is_registered_with_asymmetric_native_snapshot():
    server, mode = _new_mode()

    state = build_state_data(server, player_id=3)
    info = build_initial_info(server)

    assert get_mode_class("zom") is ZombieMode
    assert get_mode_class("zombie") is ZombieMode
    assert state.mode_type == 2
    assert state.team1_name == "Survivors"
    assert state.team2_name == "Zombies"
    assert int(C.CLASS_ROCKETEER) in state.team1_classes
    assert state.team2_classes == [int(C.CLASS_ZOMBIE)]
    assert state.team2_locked is True
    assert state.team2_locked_class is True
    assert state.lock_team_swap is True
    assert info.friendly_fire == 0
    assert int(C.CLASS_ROCKETEER) not in info.disabled_classes
    assert int(C.CLASS_ZOMBIE) not in info.disabled_classes
    assert int(C.CLASS_FAST_ZOMBIE) in info.disabled_classes
    assert int(C.CLASS_JUMP_ZOMBIE) in info.disabled_classes


def test_human_facing_zombie_alias_keeps_retail_mode_id_two():
    server, _mode = _new_mode()
    server.config.default_mode = "zombie"

    state = build_state_data(server, player_id=3)
    info = build_initial_info(server)

    assert state.mode_type == 2
    assert info.mode_key == 2
    assert state.team2_name == "Zombies"


def test_outbreak_keeps_one_survivor_and_assigns_melee_only_patient_zero():
    server, mode = _new_mode(player_count=3)

    asyncio.run(mode.on_tick(1))

    zombies = [p for p in server.players.values() if p.team == ZOMBIE_TEAM]
    survivors = [p for p in server.players.values() if p.team == SURVIVOR_TEAM]
    assert mode.phase is ZombiePhase.ACTIVE
    assert len(zombies) == 1
    assert len(survivors) == 2
    patient_zero = zombies[0]
    assert patient_zero.id in mode.patient_zero_ids
    assert patient_zero.class_id == int(C.CLASS_ZOMBIE)
    assert int(C.ZOMBIEHAND_TOOL) in patient_zero.loadout
    assert int(C.ZOMBIE_PREFAB_TOOL) in patient_zero.loadout
    assert set(patient_zero.prefabs) == {
        "prefab_zombiehand",
        "prefab_zombiebone",
        "prefab_zombiehead",
    }
    assert int(C.RIFLE_TOOL) not in patient_zero.loadout
    assert patient_zero.alive is False
    assert mode.respawn_time_for(patient_zero) == 0.0


def test_round_clock_starts_at_outbreak_not_idle_server_uptime():
    server, mode = _new_mode()
    mode.start_time = time.time() - mode.time_limit - 10.0

    asyncio.run(mode.on_tick(1))

    assert mode.phase is ZombiePhase.WAITING
    assert mode.ended is False

    first = _player(server, 1)
    second = _player(server, 2)
    asyncio.run(mode.on_player_join(first))
    asyncio.run(mode.on_player_join(second))
    asyncio.run(mode.on_tick(2))

    assert mode.phase is ZombiePhase.ACTIVE
    assert mode.ended is False
    assert mode.elapsed_time < 1.0
    assert time.time() - mode.start_time < 1.0


def test_survivor_death_converts_before_respawn_and_reveals_last_man():
    server, mode = _new_mode(player_count=3)
    asyncio.run(mode.on_tick(1))
    zombie = next(p for p in server.players.values() if p.team == ZOMBIE_TEAM)
    victim = next(p for p in server.players.values() if p.team == SURVIVOR_TEAM)
    victim.alive = victim.spawned = False

    asyncio.run(mode.on_player_death(victim, zombie, 0))

    survivor = next(p for p in server.players.values() if p.team == SURVIVOR_TEAM)
    assert victim.team == ZOMBIE_TEAM
    assert victim.class_id == int(C.CLASS_ZOMBIE)
    assert mode.last_survivor_id == survivor.id
    assert zombie.score == 100
    visible = _visibility_packets(server.packets)
    assert (survivor.id, 1) in {
        (packet.player_id, packet.high_minimap_visibility)
        for packet in visible
    }


def test_departed_only_zombie_is_replaced_without_soft_lock():
    server, mode = _new_mode(player_count=3)
    asyncio.run(mode.on_tick(1))
    departed = next(p for p in server.players.values() if p.team == ZOMBIE_TEAM)
    server.players.pop(departed.id)
    server.teams[ZOMBIE_TEAM].remove_player(departed)

    asyncio.run(mode.on_player_leave(departed))

    assert len([p for p in server.players.values() if p.team == ZOMBIE_TEAM]) == 1
    assert len([p for p in server.players.values() if p.team == SURVIVOR_TEAM]) == 1


def test_join_and_class_selection_cannot_escape_infection_role():
    _server, mode = _new_mode(player_count=2)
    asyncio.run(mode.on_tick(1))
    forged = normalize_class_selection(int(C.CLASS_MINER))

    team = mode.prepare_join_team(SURVIVOR_TEAM)
    selection = mode.prepare_join_selection(team, forged)

    assert team == ZOMBIE_TEAM
    assert selection.class_id == int(C.CLASS_ZOMBIE)
    assert int(C.ZOMBIEHAND_TOOL) in selection.loadout
    fake_player = SimpleNamespace(team=ZOMBIE_TEAM)
    assert mode.allows_class_selection(fake_player, selection) is True
    assert mode.allows_class_selection(fake_player, forged) is False
    assert mode.allows_team_change(fake_player, SURVIVOR_TEAM) is False


def test_same_role_damage_is_blocked_but_environmental_damage_remains():
    _server, mode = _new_mode()
    survivor_a = SimpleNamespace(team=SURVIVOR_TEAM)
    survivor_b = SimpleNamespace(team=SURVIVOR_TEAM)

    assert mode.modify_incoming_damage(survivor_a, 70, survivor_b, 0) == 0
    assert mode.modify_incoming_damage(survivor_a, 70, None, 0) == 70


def test_round_lifecycle_uses_mode_specific_zero_respawn_delay():
    player = SimpleNamespace(
        id=9,
        alive=False,
        spawned=False,
        death_time=time.time() - 0.01,
        team=ZOMBIE_TEAM,
        _grave_entity_id=12,
    )
    server = SimpleNamespace(
        config=SimpleNamespace(respawn_time=5.0),
        players={player.id: player},
        mode=SimpleNamespace(
            can_player_respawn=lambda target: True,
            respawn_time_for=lambda target: 0.0,
        ),
    )
    lifecycle = RoundLifecycle(server)
    lifecycle.respawn_player = lambda target: setattr(target, "alive", True)

    asyncio.run(lifecycle.process_respawns())

    assert player.alive is True
    assert player._grave_entity_id is None


def test_time_limit_awards_survivors_when_one_is_still_alive():
    _server, mode = _new_mode(player_count=3)
    asyncio.run(mode.on_tick(1))
    winners = []

    async def finish(winner, message):
        winners.append((winner, message))

    mode._finish_round = finish
    asyncio.run(mode._end_by_time())

    assert winners == [(SURVIVOR_TEAM, "The survivors endured the outbreak!")]
