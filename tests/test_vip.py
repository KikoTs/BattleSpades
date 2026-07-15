"""VIP mode state-machine and native marker regression tests."""

import asyncio
import sys
import time
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from modes import get_mode_class  # noqa: E402
from modes.vip import VIPMode, VIPPhase  # noqa: E402
from server.builders.initial_info import build_initial_info  # noqa: E402
from server.builders.state_data import build_state_data  # noqa: E402
from server.class_selection import normalize_class_selection  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.game_constants import TEAM1, TEAM2  # noqa: E402
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

    def team_base_anchor(self, team):
        return (64.5, 256.5, 223.75) if team == TEAM1 else (448.5, 256.5, 223.75)

    def get_spawn_point(self, team):
        return self.team_base_anchor(team)


class _Server:
    def __init__(self):
        self.config = ServerConfig()
        self.config.default_mode = "vip"
        self.config.mode_settings = {"vip": {
            "selection_delay": 0.0,
            "round_intermission": 0.0,
        }}
        self.tick_rate = 60
        self.world_manager = _World()
        self.teams = {
            TEAM1: Team(TEAM1, "Blue", (0, 0, 255)),
            TEAM2: Team(TEAM2, "Green", (0, 255, 0)),
        }
        self.players = {}
        self.packets = []
        self.respawned = []
        self.mode = None

    def broadcast(self, data, **_kwargs):
        self.packets.append(bytes(data))

    def respawn_player(self, player):
        player.alive = True
        player.spawned = True
        self.respawned.append((player.id, player.class_id))


def _player(server, player_id, team, *, alive=True):
    connection = _Connection()
    player = SimpleNamespace(
        id=player_id,
        name=f"P{player_id}",
        team=team,
        alive=alive,
        spawned=alive,
        connection=connection,
        class_id=int(C.CLASS_GANGSTER_1),
        loadout=[],
        prefabs=[],
        ugc_tools=[],
        score=0,
        health=100,
    )

    def apply(selection):
        player.class_id = selection.class_id
        player.loadout = list(selection.loadout)
        player.prefabs = list(selection.prefabs)
        player.ugc_tools = list(selection.ugc_tools)

    player.apply_class_selection = apply
    player.send = connection.send
    server.players[player_id] = player
    server.teams[team].add_player(player)
    return player


def _new_mode():
    server = _Server()
    mode = VIPMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    return server, mode


def _visibility_packets(data):
    return [
        ChangePlayer(ByteReader(packet[1:]))
        for packet in data
        if packet and packet[0] == ChangePlayer.id
    ]


def test_vip_is_registered_and_join_state_bypasses_class_picker():
    server, _mode = _new_mode()

    state = build_state_data(server, player_id=3)
    info = build_initial_info(server)

    assert get_mode_class("vip") is VIPMode
    assert state.team1_locked_class is True
    assert state.team2_locked_class is True
    assert state.team1_classes == list(C.MAFIA_TEAM_CLASSES)
    assert state.team2_classes == list(C.MAFIA_TEAM_CLASSES)
    assert info.texture_skin == "mafia"
    assert state.score_limit == 3


def test_vip_join_selection_rejects_non_gangster_class_and_loadout():
    _server, mode = _new_mode()
    forged = normalize_class_selection(int(C.CLASS_MINER))

    selection = mode.prepare_join_selection(TEAM1, forged)

    assert selection.class_id in {int(value) for value in C.MAFIA_TEAM_CLASSES}
    assert int(C.TOMMYGUN_TOOL) in selection.loadout
    assert int(C.SNUB_PISTOL_TOOL) in selection.loadout
    assert int(C.MOLOTOV_TOOL) in selection.loadout
    assert int(C.CROWBAR_TOOL) in selection.loadout
    assert int(C.DYNAMITE_TOOL) not in selection.loadout


def test_vip_selection_promotes_one_boss_per_team_and_tracks_them():
    server, mode = _new_mode()
    blue = _player(server, 1, TEAM1)
    green = _player(server, 2, TEAM2)

    asyncio.run(mode.on_tick(1))

    assert mode.phase is VIPPhase.ACTIVE
    assert mode.vips == {TEAM1: blue, TEAM2: green}
    assert blue.class_id == int(C.MAFIA_VIPS[TEAM1])
    assert green.class_id == int(C.MAFIA_VIPS[TEAM2])
    assert server.respawned == [
        (blue.id, int(C.MAFIA_VIPS[TEAM1])),
        (green.id, int(C.MAFIA_VIPS[TEAM2])),
    ]
    visible = _visibility_packets(server.packets)
    assert {(packet.player_id, packet.high_minimap_visibility) for packet in visible} >= {
        (blue.id, 1), (green.id, 1),
    }


def test_vip_damage_is_halved_and_only_dead_vip_team_loses_respawns():
    server, mode = _new_mode()
    blue_a = _player(server, 1, TEAM1)
    blue_b = _player(server, 3, TEAM1)
    _player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    blue_vip = mode.vips[TEAM1]
    blue_guard = blue_b if blue_vip is blue_a else blue_a
    green_vip = mode.vips[TEAM2]

    assert mode.modify_incoming_damage(blue_vip, 41, green_vip, 0) == 20
    blue_vip.alive = False
    blue_vip.spawned = False
    asyncio.run(mode.on_player_death(blue_vip, green_vip, 0))

    assert mode.vip_alive[TEAM1] is False
    assert mode.can_player_respawn(blue_vip) is False
    assert mode.can_player_respawn(blue_guard) is False
    assert mode.can_player_respawn(green_vip) is True
    assert _visibility_packets(server.packets)[-1].high_minimap_visibility == 0


def test_vip_disconnect_counts_as_vip_death_and_late_join_gets_survivor_marker():
    server, mode = _new_mode()
    _player(server, 1, TEAM1)
    _player(server, 3, TEAM1)
    _player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    blue_vip = mode.vips[TEAM1]
    green_vip = mode.vips[TEAM2]

    server.players.pop(blue_vip.id)
    server.teams[TEAM1].remove_player(blue_vip)
    asyncio.run(mode.on_player_leave(blue_vip))
    joining = _Connection()
    mode.reveal_to(joining)

    assert mode.vip_alive[TEAM1] is False
    assert mode.respawn_enabled[TEAM1] is False
    visible = _visibility_packets(joining.sent)
    assert [(packet.player_id, packet.high_minimap_visibility) for packet in visible] == [
        (green_vip.id, 1)
    ]


def test_full_match_restart_demotes_old_bosses_before_outer_respawn():
    server, mode = _new_mode()
    blue = _player(server, 1, TEAM1)
    green = _player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))

    assert blue.class_id == int(C.MAFIA_VIPS[TEAM1])
    assert green.class_id == int(C.MAFIA_VIPS[TEAM2])

    asyncio.run(mode.on_mode_start())

    assert blue.class_id in {int(value) for value in C.MAFIA_TEAM_CLASSES}
    assert green.class_id in {int(value) for value in C.MAFIA_TEAM_CLASSES}
    assert mode.phase is VIPPhase.SELECTING


def test_disabling_sudden_death_scores_immediately_on_vip_death():
    server, mode = _new_mode()
    mode.sudden_death_enabled = False
    _player(server, 1, TEAM1)
    _player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    blue_vip = mode.vips[TEAM1]
    green_vip = mode.vips[TEAM2]

    blue_vip.alive = blue_vip.spawned = False
    asyncio.run(mode.on_player_death(blue_vip, green_vip, 0))

    assert server.teams[TEAM2].score == 1
    assert mode.phase is VIPPhase.INTERMISSION
    if mode._round_task is not None:
        mode._round_task.cancel()


def test_vip_round_scores_when_vipless_team_is_eliminated():
    server, mode = _new_mode()
    blue_vip = _player(server, 1, TEAM1)
    blue_guard = _player(server, 3, TEAM1)
    green_vip = _player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))

    blue_vip.alive = blue_vip.spawned = False
    asyncio.run(mode.on_player_death(blue_vip, green_vip, 0))
    blue_guard.alive = blue_guard.spawned = False
    asyncio.run(mode.on_player_death(blue_guard, green_vip, 0))

    assert server.teams[TEAM2].score == 1
    assert mode.phase is VIPPhase.INTERMISSION
    task = mode._round_task
    if task is not None:
        task.cancel()


def test_round_lifecycle_does_not_respawn_sudden_death_casualty(monkeypatch):
    player = SimpleNamespace(
        id=5, alive=False, spawned=False, death_time=time.time() - 30.0,
        team=TEAM1, _grave_entity_id=7,
    )
    server = SimpleNamespace(
        config=SimpleNamespace(respawn_time=5.0),
        players={player.id: player},
        mode=SimpleNamespace(can_player_respawn=lambda target: False),
    )
    lifecycle = RoundLifecycle(server)
    lifecycle.respawn_player = lambda target: setattr(target, "alive", True)

    asyncio.run(lifecycle.process_respawns())

    assert player.alive is False
    assert player._grave_entity_id == 7
