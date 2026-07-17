"""VIP mode state-machine and native marker regression tests."""

import asyncio
import sys
import time
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
import shared.constants_gamemode as CG  # noqa: E402
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
from shared.packet import ChangePlayer, SetScore  # noqa: E402


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


def _player(server, player_id, team, *, alive=True, position=(0.0, 0.0, 0.0)):
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
        position=position,
    )

    def apply(selection):
        player.class_id = selection.class_id
        player.loadout = list(selection.loadout)
        player.prefabs = list(selection.prefabs)
        player.ugc_tools = list(selection.ugc_tools)

    def die(*, killer=None, kill_type=0):
        player.alive = False
        player.spawned = False
        player.last_kill_type = int(kill_type)

    player.apply_class_selection = apply
    player.die = die
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


def _score_packets(data):
    return [
        SetScore(ByteReader(packet[1:]))
        for packet in data
        if packet and packet[0] == SetScore.id
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


def test_vip_uses_the_two_shipped_gangster_maps_for_default_votes():
    config = ServerConfig(default_mode="vip")
    from server.main import BattleSpadesServer

    server = BattleSpadesServer(config)
    server.mode = VIPMode(server)

    assert server.mode.stock_maps == ("Alcatraz", "CityOfChicago")
    assert server.vote_manager._mode_available_maps() == server.mode.stock_maps


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
    assert blue.last_kill_type == int(C.KILL.CLASS_CHANGE_KILL)
    assert green.last_kill_type == int(C.KILL.CLASS_CHANGE_KILL)
    assert server.respawned == [
        (blue.id, int(C.MAFIA_VIPS[TEAM1])),
        (green.id, int(C.MAFIA_VIPS[TEAM2])),
    ]
    visible = _visibility_packets(server.packets)
    assert {(packet.player_id, packet.high_minimap_visibility) for packet in visible} >= {
        (blue.id, 1), (green.id, 1),
    }


def test_live_vip_death_uses_native_boss_kill_and_zero_respawn_timer():
    server, mode = _new_mode()
    blue = _player(server, 1, TEAM1)
    green = _player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    blue_vip = mode.vips[TEAM1]
    green_vip = mode.vips[TEAM2]

    assert mode.death_kill_type_for(
        blue_vip,
        green_vip,
        int(C.KILL.WEAPON_KILL),
    ) == int(C.KILL.VIP_MODE_KILL)
    assert mode.respawn_time_for(blue_vip) == 0.0
    ordinary = green if green is not green_vip else blue
    if ordinary is not blue_vip:
        assert mode.death_kill_type_for(
            ordinary,
            blue_vip,
            int(C.KILL.WEAPON_KILL),
        ) == int(C.KILL.WEAPON_KILL)


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


def test_vip_subround_respawns_are_bounded_per_gameplay_tick():
    server, mode = _new_mode()
    for player_id in range(1, 11):
        team = TEAM1 if player_id % 2 else TEAM2
        _player(server, player_id, team)
    mode.round_respawns_per_tick = 3
    server.respawned.clear()

    asyncio.run(mode._begin_round(reset_players=True))

    assert mode.phase is VIPPhase.RESETTING
    assert server.respawned == []
    previous = 0
    for tick in range(1, 5):
        asyncio.run(mode.on_tick(tick))
        assert len(server.respawned) - previous <= 3
        previous = len(server.respawned)

    assert len(server.respawned) == 10
    assert mode.phase is VIPPhase.SELECTING


def test_vip_active_roster_audit_is_not_a_60_hz_player_scan():
    server, mode = _new_mode()
    _player(server, 1, TEAM1)
    _player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    assert mode.phase is VIPPhase.ACTIVE

    calls = []

    async def record_audit():
        calls.append(True)

    mode._check_team_elimination = record_audit
    mode._next_roster_audit = time.monotonic() + 60.0
    for tick in range(2, 122):
        asyncio.run(mode.on_tick(tick))
    assert calls == []

    mode._next_roster_audit = 0.0
    asyncio.run(mode.on_tick(122))
    assert calls == [True]


def test_vip_periodic_survival_and_escort_scores_use_retail_reasons():
    server, mode = _new_mode()
    blue_a = _player(server, 1, TEAM1)
    blue_b = _player(server, 3, TEAM1)
    green_a = _player(server, 2, TEAM2)
    green_b = _player(server, 4, TEAM2)
    asyncio.run(mode.on_tick(1))

    blue_vip = mode.vips[TEAM1]
    blue_guard = blue_b if blue_vip is blue_a else blue_a
    green_vip = mode.vips[TEAM2]
    green_guard = green_b if green_vip is green_a else green_a
    blue_vip.position = (100.0, 100.0, 100.0)
    blue_guard.position = (114.0, 100.0, 100.0)
    green_vip.position = (300.0, 300.0, 100.0)
    green_guard.position = (316.0, 300.0, 100.0)
    server.packets.clear()
    mode._next_vip_survival_score = 0.0
    mode._next_escort_score = 0.0

    asyncio.run(mode.on_tick(2))

    assert blue_vip.score == int(CG.VIP_SCORE_LIVEVIP_SCORE)
    assert green_vip.score == int(CG.VIP_SCORE_LIVEVIP_SCORE)
    assert blue_guard.score == int(CG.VIP_SCORE_ESCORT_SCORE)
    assert green_guard.score == 0
    score_packets = _score_packets(server.packets)
    assert [packet.reason for packet in score_packets].count(
        int(C.SCORE_REASON.VIP_SURVIVE_SCORE_REASON)
    ) == 2
    assert [packet.reason for packet in score_packets].count(
        int(C.SCORE_REASON.VIP_ESCORT_SCORE_REASON)
    ) == 1

    # A second tick cannot replay missed intervals or duplicate reliable HUD
    # score packets; deadlines are re-armed from the current monotonic time.
    packet_count = len(score_packets)
    asyncio.run(mode.on_tick(3))
    assert len(_score_packets(server.packets)) == packet_count


def test_live_vip_kill_bonus_is_separate_from_enemy_vip_percentage():
    server, mode = _new_mode()
    blue = _player(server, 1, TEAM1)
    green = _player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    blue_vip = mode.vips[TEAM1]
    green_vip = mode.vips[TEAM2]
    green_vip.score = 250
    green_vip.alive = green_vip.spawned = False
    server.packets.clear()

    asyncio.run(mode.on_player_death(green_vip, blue_vip, 0))

    assert blue_vip.score == int(CG.VIP_SCORE_KILL_AS_VIP) + 25
    reasons = [
        packet.reason
        for packet in _score_packets(server.packets)
        if packet.type == int(C.SCORE.PLAYER)
    ]
    assert reasons == [
        int(C.SCORE_REASON.VIP_KILL_SCORE_REASON),
        int(C.SCORE_REASON.VIP_KILLENEMYVIP_SCORE_REASON),
    ]
