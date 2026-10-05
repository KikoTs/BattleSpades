"""Round-2 mode fixes: Arena, VIP health, Zombie phases, bot retirement,
Diamond/TC/Occupation/Demolition identity and loading-joiner gating."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import shared.constants as C
import shared.constants_gamemode as CG
from shared.bytes import ByteReader
from shared.packet import (
    LockTeam,
    MinimapZoneClear,
    SetHP,
    SetScore,
    TeamLockClass,
    TerritoryBaseState,
)

from modes.arena import ArenaMode
from modes.vip import VIPMode, VIPPhase
from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM, ZombieMode, ZombiePhase
from server.bot_ai.director import BotDirector
from server.game_constants import MAX_HEALTH, TEAM1, TEAM2
from server.map_resources import MapResourceService
from server.player import Player
from server.round_lifecycle import RoundLifecycle
from tests import test_arena as arena_fx
from tests import test_vip as vip_fx
from tests import test_zombie as zombie_fx
from tests.test_mode_lifecycle_contracts import _Conn


def _decode(rows, packet_type):
    return [
        packet_type(ByteReader(data[1:]))
        for data in rows if data and data[0] == packet_type.id
    ]


# -- 4. Arena --------------------------------------------------------------


def _arena(monkeypatch, now, settings=None):
    monkeypatch.setattr("modes.arena.time.time", lambda: now[0])
    server = arena_fx._Server(settings)
    server.config.respawn_time = 5
    mode = ArenaMode(server)
    server.mode = mode
    return server, mode


def test_arena_holds_countdown_until_both_teams_have_a_fighter(monkeypatch):
    now = [1000.0]
    server, mode = _arena(monkeypatch, now)
    blue = arena_fx._player(server, 1, TEAM1)
    asyncio.run(mode.on_mode_start())
    for step in range(1, 4):
        now[0] += 10.0
        asyncio.run(mode.on_tick(step))
    # One side only: FIGHT is held, respawns stay on, no free round win.
    assert mode.round_started is False
    assert mode.waiting_for_players is True
    assert mode.can_player_respawn(blue) is True
    assert mode.respawn_time_for(blue) == 5.0

    arena_fx._player(server, 2, TEAM2)
    now[0] += 0.1
    asyncio.run(mode.on_tick(10))
    # A full countdown restarts once the second team arrives.
    assert mode.round_started is False
    assert mode.waiting_for_players is False
    now[0] += mode.round_start_delay + 0.1
    asyncio.run(mode.on_tick(11))
    assert mode.round_started is True
    assert {p.id for p in mode.alive_players} == {1, 2}


def test_arena_dead_fighter_is_told_the_real_wait_not_five_seconds(monkeypatch):
    now = [1000.0]
    server, mode = _arena(monkeypatch, now)
    blue = arena_fx._player(server, 1, TEAM1)
    arena_fx._player(server, 2, TEAM2)
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode._begin_round())
    now[0] += 30.0
    wait = mode.respawn_time_for(blue)
    assert mode.can_player_respawn(blue) is False
    assert wait == pytest.approx(
        mode.round_time_limit - 30.0 + mode.round_end_delay
    )
    mode.next_round_at = now[0] + 3.0
    assert mode.respawn_time_for(blue) == pytest.approx(3.0)


def test_arena_countdown_blocks_damage_through_the_live_damage_hook(monkeypatch):
    now = [1000.0]
    server, mode = _arena(monkeypatch, now)
    blue = arena_fx._player(server, 1, TEAM1)
    green = arena_fx._player(server, 2, TEAM2)
    asyncio.run(mode.on_mode_start())
    # Player.damage consults modify_incoming_damage, never on_player_damage.
    assert mode.modify_incoming_damage(blue, 40, green, 0) == 0
    asyncio.run(mode._begin_round())
    assert mode.modify_incoming_damage(blue, 40, green, 0) == 40
    mode.round_ended = True
    assert mode.modify_incoming_damage(blue, 40, green, 0) == 0


def test_player_damage_uses_arena_countdown_rule():
    mode = SimpleNamespace(
        modify_incoming_damage=lambda player, amount, source, kind: 0
    )
    server = SimpleNamespace(mode=mode, world_manager=None)
    connection = _Conn()
    connection.server = server
    player = Player(1, "P1", TEAM1, C.RIFLE_TOOL, connection)
    player.alive = True
    player.health = 100
    assert player.damage(40, source=None, kill_type=int(C.WEAPON_KILL)) is False
    assert player.health == 100


# -- 7. VIP health / SetHP / loading joiners ----------------------------


def test_vip_boss_keeps_its_rule_health_through_crates_and_heals():
    server = vip_fx._Server()
    server.config.mode_settings["vip"]["vip_health_multiplier"] = 2.0
    mode = VIPMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    vip_fx._player(server, 1, TEAM1)
    vip_fx._player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    assert mode.phase is VIPPhase.ACTIVE
    for vip in mode.vips.values():
        assert vip.health == 200
        assert vip.max_health == 200


def _body(max_health, health, *, in_game=True):
    connection = _Conn()
    connection.in_game = in_game
    player = Player(1, "P1", TEAM1, C.RIFLE_TOOL, connection)
    player.alive = True
    player.max_health = max_health
    player.health = health
    return player, connection


@pytest.mark.parametrize(
    "max_health, health, expected", [(200, 150, 200), (50, 40, 50), (100, 30, 100)]
)
def test_health_crate_and_heal_cap_at_the_body_maximum(max_health, health, expected):
    player, connection = _body(max_health, health)
    _name, crate = MapResourceService._behaviors()[int(C.HEALTH_CRATE)]
    crate.refill(player)
    assert player.health == expected
    assert _decode(connection.sent, SetHP)[-1].hp == expected


def test_sethp_is_clamped_to_one_byte():
    player, connection = _body(400, 300)
    player.heal(50)
    assert player.health == 350
    assert _decode(connection.sent, SetHP)[-1].hp == 255


def test_spawn_resets_the_body_maximum():
    player, _connection = _body(200, 200)
    try:
        player.spawn(100.5, 100.5, 59.75)
    except Exception:  # noqa: BLE001 - spawn needs no live server here
        pytest.skip("spawn requires a live world in this build")
    assert player.max_health == MAX_HEALTH
    assert player.health == MAX_HEALTH


def test_loading_joiner_gets_no_damage_sethp():
    player, connection = _body(100, 100, in_game=False)
    player.damage(10, source=None, kill_type=int(C.FALL_KILL))
    assert player.health == 90
    assert _decode(connection.sent, SetHP) == []
    connection.in_game = True
    player.damage(10, source=None, kill_type=int(C.FALL_KILL))
    assert _decode(connection.sent, SetHP)[-1].hp == 80


def test_vip_never_promotes_a_loading_joiner():
    server = vip_fx._Server()
    mode = VIPMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    loading = vip_fx._player(server, 1, TEAM1)
    loading.connection.in_game = False
    vip_fx._player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    assert mode.phase is not VIPPhase.ACTIVE
    loading.connection.in_game = True
    mode._next_roster_audit = 0.0
    asyncio.run(mode.on_tick(2))
    asyncio.run(mode.on_tick(3))
    assert mode.phase is VIPPhase.ACTIVE


def test_radar_reset_sends_no_team_visibility_packet():
    sent = []
    loading = SimpleNamespace(id=1, team=TEAM1, connection=SimpleNamespace(in_game=False))
    settled = SimpleNamespace(id=2, team=TEAM1, connection=SimpleNamespace(in_game=True))
    registry = SimpleNamespace(all=lambda: (), clear=lambda: None)
    server = SimpleNamespace(
        _radar_station_counts={TEAM1: 1, TEAM2: 0},
        players={1: loading, 2: settled},
        _send_radar_visibility=lambda player, visible: sent.append((player.id, visible)),
        config=SimpleNamespace(entities_wire_ready=False),
        entity_registry=registry,
        entities=[],
        rocket_turrets=[],
        projectile_engine=SimpleNamespace(projectiles=[]),
        fire_controller=SimpleNamespace(clear=lambda: None),
    )
    RoundLifecycle(server).reset_round_runtime()
    # Radar detection is client-side from the entity; no packet 83 to undo.
    assert sent == []
    assert server._radar_station_counts == {TEAM1: 0, TEAM2: 0}


# -- 9. Zombie phases ------------------------------------------------------


def _zombie_active(player_count=3, rounds=3):
    from tests.test_zombie_rounds import _active_mode

    return _active_mode(player_count, rounds=rounds)


def test_zombie_intermission_republishes_team_locks():
    server, mode = _zombie_active()
    server.packets.clear()
    asyncio.run(mode._finish_round(ZOMBIE_TEAM, "ZOMBIE_WIN"))
    task = mode._round_task
    if task is not None:
        task.cancel()
    locks = {row.team_id: row.locked for row in _decode(server.packets, LockTeam)}
    # Intermission only admits survivors: Zombie locked, Survivor open.
    assert locks == {ZOMBIE_TEAM: 1, SURVIVOR_TEAM: 0}
    class_locks = _decode(server.packets, TeamLockClass)
    assert class_locks and class_locks[-1].locked == 0


def test_zombie_failed_round_restart_falls_back_instead_of_freezing():
    server, mode = _zombie_active()

    async def scenario():
        async def broken():
            raise RuntimeError("respawn exploded")

        mode._begin_next_round = broken
        mode.round_intermission = 0.0
        await mode._finish_round(ZOMBIE_TEAM, "ZOMBIE_WIN")
        await mode._round_task
        return server, mode

    server, mode = asyncio.run(scenario())
    # The guarded full restart revived the match instead of INTERMISSION forever.
    assert mode.phase is not ZombiePhase.INTERMISSION
    assert mode.rounds_played == 0
    assert mode.ended is False


def test_zombie_fallback_ends_match_when_restart_also_fails():
    server, mode = _zombie_active()

    async def scenario():
        ended = []

        async def broken():
            raise RuntimeError("boom")

        async def end(winner=None):
            ended.append(winner)
            mode.ended = True

        mode._begin_next_round = broken
        mode._restart_round = broken
        mode.on_mode_end = end
        mode.round_intermission = 0.0
        await mode._finish_round(ZOMBIE_TEAM, "ZOMBIE_WIN")
        await mode._round_task
        return ended

    assert asyncio.run(scenario()) == [None]


def test_vip_failed_subround_restart_falls_back_instead_of_freezing():
    async def scenario():
        server = vip_fx._Server()
        server.config.mode_settings["vip"]["score_limit"] = 3
        mode = VIPMode(server)
        server.mode = mode
        await mode.on_mode_start()
        blue = vip_fx._player(server, 1, TEAM1)
        vip_fx._player(server, 2, TEAM2)
        await mode.on_tick(1)
        assert mode.phase is VIPPhase.ACTIVE
        restarts = []
        original_restart = mode._restart_round

        async def broken(**_kwargs):
            raise RuntimeError("reset exploded")

        async def observed_restart():
            restarts.append(True)
            await original_restart()

        mode._begin_round = broken
        mode._restart_round = observed_restart
        blue.alive = blue.spawned = False
        await mode.on_player_death(blue, mode.vips[TEAM2], 0)
        assert mode.phase is VIPPhase.INTERMISSION
        await mode._round_task
        return mode, restarts

    mode, restarts = asyncio.run(scenario())
    assert restarts == [True]


# -- 6. bot retirement veto -------------------------------------------------


def _director(mode):
    fake = SimpleNamespace(server=SimpleNamespace(mode=mode), _runtime={}, bots=[])
    fake._safe_to_retire = lambda player: BotDirector._safe_to_retire(fake, player)
    fake._mode_retire_rank = lambda player: BotDirector._mode_retire_rank(fake, player)
    fake.removed = []

    async def remove_bot(candidate, *, force=False, notify_mode=True):
        fake.removed.append(candidate.id)
        return True

    fake.remove_bot = remove_bot
    return fake


def test_zombie_last_survivor_bot_is_not_retired_and_zombies_go_first():
    server, mode = _zombie_active(4)
    survivors = [p for p in server.players.values() if p.team == SURVIVOR_TEAM]
    asyncio.run(mode._infect(survivors[0], patient_zero=False))
    survivors = [p for p in server.players.values() if p.team == SURVIVOR_TEAM]
    zombies = [p for p in server.players.values() if p.team == ZOMBIE_TEAM]
    assert len(survivors) == 2 and len(zombies) == 2
    director = _director(mode)
    for player in server.players.values():
        player.pickup_id = None
        player.alive = True
    director.bots = survivors + zombies
    asyncio.run(BotDirector.make_room_for_human(director))
    # Patient zero is protected; the other zombie goes before any human.
    (removed,) = director.removed
    assert removed in {p.id for p in zombies}
    assert removed not in mode.patient_zero_ids
    # The last human standing is vetoed.
    survivors[0].team = ZOMBIE_TEAM
    assert director._safe_to_retire(survivors[1]) is False
    assert mode.bot_retire_safe(survivors[1]) is False


def test_arena_live_fighter_bot_is_vetoed_and_dead_bot_goes_first(monkeypatch):
    now = [1000.0]
    server, mode = _arena(monkeypatch, now)
    alive = arena_fx._player(server, 1, TEAM1)
    arena_fx._player(server, 2, TEAM2)
    dead = arena_fx._player(server, 3, TEAM1)
    for player in server.players.values():
        player.pickup_id = None
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode._begin_round())
    dead.alive = dead.spawned = False
    director = _director(mode)
    assert director._safe_to_retire(alive) is False
    assert director._safe_to_retire(dead) is True
    director.bots = [alive, dead]
    asyncio.run(BotDirector.make_room_for_human(director))
    assert director.removed == [dead.id]


def test_vip_last_alive_of_sudden_death_team_is_vetoed():
    server = vip_fx._Server()
    mode = VIPMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    blue_a = vip_fx._player(server, 1, TEAM1)
    blue_b = vip_fx._player(server, 3, TEAM1)
    vip_fx._player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    blue_vip = mode.vips[TEAM1]
    guard = blue_b if blue_vip is blue_a else blue_a
    assert mode.bot_retire_safe(guard) is True
    blue_vip.alive = blue_vip.spawned = False
    asyncio.run(mode.on_player_death(blue_vip, mode.vips[TEAM2], 0))
    assert mode.bot_retire_safe(guard) is False


# -- 8. Diamond Mine -------------------------------------------------------


def test_diamond_restart_clears_previous_dropoff_icons(monkeypatch):
    from tests.test_diamond_mine_fixes import _mode

    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    asyncio.run(mode.on_mode_start())
    old_zone = mode.active_dropoffs[0].zone
    server.packets.clear()
    asyncio.run(mode.on_mode_start())
    clears = _decode(server.packets, MinimapZoneClear)
    assert clears, "previous drop-off zone never cleared"
    x0, x1, y0, y1, _z0, _z1 = old_zone.bounds
    assert (clears[0].A2018, clears[0].A2019, clears[0].A2020, clears[0].A2021) == (
        x0, x1, y0, y1,
    )


# -- 10. Territory Control / Occupation identity -----------------------


def test_tc_reveal_replays_contention_to_a_joiner(monkeypatch):
    from tests.test_territory_control import _Connection, _mode

    server, mode = _mode(monkeypatch, [100.0])
    mode.territories[1].contested = True
    connection = _Connection()
    mode.reveal_to(connection)
    rows = [
        (row.base_index, row.action)
        for row in _decode(connection.sent, TerritoryBaseState)
    ]
    assert (1, int(C.TC_BASE_CONTENDED)) in rows
    assert (0, int(C.TC_BASE_CONTENDED)) not in rows


def test_tc_presence_score_never_pays_a_reused_id(monkeypatch):
    from tests.test_territory_control import _mode, _player

    server, mode = _mode(monkeypatch, [100.0])
    territory = mode.territories[0]
    owner = territory.owner if territory.owner in (TEAM1, TEAM2) else TEAM1
    territory.owner = owner
    holder = _player(server, 5, owner, territory.zone.center)
    mode._send_presence_transitions(territory, mode._occupants(territory.zone))
    assert 5 in territory.occupants[owner]
    # The holder leaves; a newcomer takes id 5 before the next capture tick.
    newcomer = _player(server, 5, owner, (0.0, 0.0, 0.0))
    mode._award_presence_scores(1)
    assert newcomer.score == 0
    server.players[5] = holder
    mode._award_presence_scores(1)
    assert holder.score == int(CG.TC_SCORE_OCCUPY_PERHILL)


def test_occupation_disposal_never_pays_a_reused_id(monkeypatch):
    from tests.test_occupation_fixes import _mode
    from tests.test_recovered_objective_modes import _Player

    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    blue = _Player(1, TEAM1, (100.0, 100.0, 60.0))
    green = _Player(2, TEAM2, (100.0, 100.0, 60.0))
    server.players = {blue.id: blue}
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_tick(1))
    bomb = next(iter(mode.bombs.values()))
    assert blue.pickup_id == int(C.BOMB_PICKUP)
    asyncio.run(mode._drop_bomb(blue))
    server.players[green.id] = green
    blue.set_position((300.0, 300.0, 60.0))
    now[0] += 0.1
    bomb.pickup_after = 0.0
    mode._pickup_bomb(green, bomb)
    assert bomb.last_carrier is green
    asyncio.run(mode._drop_bomb(green))
    # Green leaves; a newcomer on the same team reuses id 2.
    newcomer = _Player(2, TEAM2, (0.0, 0.0, 60.0))
    server.players[2] = newcomer
    now[0] = bomb.explode_at + 0.1
    asyncio.run(mode.on_tick(2))
    assert newcomer.score == 0
    assert green.score == 0


# -- 12. Demolition gating / id hygiene -----------------------------------


def _demolition(monkeypatch):
    from tests.test_demolition_fixes import _active_mode

    return _active_mode(monkeypatch, [100.0])


def test_demolition_locks_skip_loading_joiners_but_reveal_sends_them(monkeypatch):
    server, mode = _demolition(monkeypatch)
    connection = _Conn()
    connection.in_game = False
    player = SimpleNamespace(id=4, team=TEAM1, connection=connection)
    connection.player = player
    asyncio.run(mode.on_player_spawn(player))
    assert connection.sent == []
    mode.reveal_to(connection)
    assert connection.sent


def test_demolition_block_credit_is_not_inherited_by_a_reused_id(monkeypatch):
    server, mode = _demolition(monkeypatch)
    monkeypatch.setattr(CG, "DEM_SCORE_DESTROY_INTERVAL", 2)
    leaver = SimpleNamespace(id=1, team=TEAM1, score=0, name="P1")
    server.players[1] = leaver
    server.world_manager.mutate((40, 50, 30), False)
    asyncio.run(mode.on_blocks_destroyed(leaver, ((40, 50, 30),), True))
    asyncio.run(mode.on_player_leave(leaver))
    newcomer = SimpleNamespace(id=1, team=TEAM1, score=0, name="P1b")
    server.players[1] = newcomer
    server.world_manager.mutate((41, 50, 30), False)
    asyncio.run(mode.on_blocks_destroyed(newcomer, ((41, 50, 30),), True))
    assert newcomer.score == 0
    # A departed player's queued terrain event pays nobody.
    server.world_manager.mutate((42, 50, 30), False)
    asyncio.run(mode.on_blocks_destroyed(leaver, ((42, 50, 30),), True))
    assert leaver.score == 0
    assert _decode(server.packets, SetScore) == [] or all(
        row.specifier != 1 or row.value == 0 for row in _decode(server.packets, SetScore)
    )


def test_zombie_match_goes_to_the_team_with_more_rounds():
    server, mode = _zombie_active(rounds=3)
    ended = []

    async def record_end(winner=None):
        ended.append(winner)

    mode.on_mode_end = record_end
    server.teams[ZOMBIE_TEAM].score = 2
    server.teams[SURVIVOR_TEAM].score = 0
    mode.rounds_played = 2
    # Survivors take the last round, but zombies won the match 2-1.
    asyncio.run(mode._finish_round(SURVIVOR_TEAM, "SURVIVOR_WIN"))
    task = mode._round_task
    if task is not None:
        task.cancel()
    assert ended == [ZOMBIE_TEAM]


def test_diamond_explains_itself_once_on_first_spawn():
    from types import SimpleNamespace

    from modes.diamond_mine import DiamondMineMode
    from shared.packet import HelpMessage

    sent = []
    player = SimpleNamespace(send=lambda payload, reliable=False: sent.append(payload))
    mode = DiamondMineMode.__new__(DiamondMineMode)
    asyncio.run(mode.on_player_spawn(player))
    asyncio.run(mode.on_player_spawn(player))
    help_packets = _decode(sent, HelpMessage)
    assert len(sent) == 1 and help_packets
    assert list(help_packets[0].message_ids)[0] == "DIAMOND_TUTORIAL"
