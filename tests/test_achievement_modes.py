"""Achievements driven through the real game modes.

Each test runs the mode's own code (round start, pickups, drops, control,
detonation, round end) with an engine attached, so it pins both the mode's
hook and the rule behind it. Kills are reported the way ``Player.die`` does
(``achievements.died``) before the mode's queued death handler runs.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import shared.constants as C
import shared.constants_gamemode as CG

from modes.classic_ctf import ClassicCTFMode
from modes.ctf import CTFMode
from modes.vip import VIPPhase
from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM, ZombieMode, ZombiePhase
from server import achievements
from tests import test_demolition_fixes as dem
from tests import test_multi_hill as mh
from tests import test_score_events as se
from tests import test_vip as vfx
from tests import test_zombie as zfx
from tests.achievement_helpers import (
    MELEE, TEAM1, TEAM2, WEAPON, announcements, attach_engine, progress, unlocked,
)


async def _no_end_sequence(_winner):
    return None


def _kill(server, mode, killer, victim, kill_type=WEAPON):
    """One kill as the server delivers it: the synchronous achievement hook
    from Player.die, then the mode's queued death and kill handlers."""
    achievements.died(server, victim, killer, int(kill_type), False)
    victim.alive = victim.spawned = False
    asyncio.run(mode.on_player_death(victim, killer, int(kill_type)))


# ---------------------------------------------------------------------------
# Zombie
# ---------------------------------------------------------------------------

def _zombie(player_count=4, *, rounds=3):
    server = zfx._Server()
    server.config.mode_settings["zom"]["score_limit"] = rounds
    server.config.mode_settings["zom"]["round_intermission"] = 0.0
    for player_id in range(1, player_count + 1):
        zfx._player(server, player_id)

    def respawn(player):
        player.alive = player.spawned = True

    server.respawn_player = respawn
    attach_engine(server)
    mode = ZombieMode(server)
    server.mode = mode
    mode._run_end_sequence = _no_end_sequence
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_tick(1))
    assert mode.phase is ZombiePhase.ACTIVE
    zombies = [p for p in server.players.values() if p.team == ZOMBIE_TEAM]
    survivors = [p for p in server.players.values() if p.team == SURVIVOR_TEAM]
    return server, mode, zombies, survivors


def test_the_outbreak_names_the_teams_and_who_started_as_a_zombie():
    server, mode, zombies, survivors = _zombie(4)
    engine = server.achievements
    assert engine._zombie_teams == (ZOMBIE_TEAM, SURVIVOR_TEAM)
    assert engine._initial_zombies == zombies and len(zombies) == 1
    assert engine._is_zombie(zombies[0]) and not engine._is_zombie(survivors[0])


def test_a_zombie_that_infects_everyone_takes_three_of_a_kind_and_the_mvp():
    server, mode, (zombie,), survivors = _zombie(4)
    for index, victim in enumerate(survivors, start=1):
        assert ("zombie_kills_humans" in unlocked(server, zombie)) is False
        _kill(server, mode, zombie, victim, MELEE)
        assert victim.team == ZOMBIE_TEAM
    assert mode.phase is ZombiePhase.INTERMISSION
    assert unlocked(server, zombie) == {"zombie_kills_humans", "zombie_mvp"}
    # Nobody survived, and the infected did not start the round as zombies.
    for victim in survivors:
        assert unlocked(server, victim) == set()


def test_survivors_alive_at_the_final_whistle_get_apocalypse_later():
    server, mode, (zombie,), survivors = _zombie(4)
    _kill(server, mode, zombie, survivors[0], MELEE)
    mode.start_time = time.time() - mode.time_limit - 1.0
    asyncio.run(mode.on_tick(500))
    assert mode.phase is ZombiePhase.INTERMISSION
    assert unlocked(server, survivors[0]) == set()      # infected on the way
    assert unlocked(server, survivors[1]) == {"zombie_survivor"}
    assert unlocked(server, survivors[2]) == {"zombie_survivor"}
    assert unlocked(server, zombie) == {"zombie_mvp"}
    names = sorted(parameters[1] for _id, parameters in announcements(server))
    assert names == ["Apocalypse Later", "Apocalypse Later", "Stone Cold Killer"]


def test_the_modes_last_survivor_marker_feeds_the_last_man_clock():
    server, mode, (zombie,), (survivor,) = _zombie(2)
    assert mode.last_survivor_id == survivor.id
    for _ in range(55):
        achievements.tick(server, 0.5)
    assert "zombie_lms_one_round" not in unlocked(server, survivor)
    achievements.tick(server, 0.5)
    assert "zombie_lms_one_round" in unlocked(server, survivor)
    assert progress(server, survivor, "zombie_seconds_as_lms_count") == 28
    # The last man's zombie kills are read off the same marker.
    for _ in range(5):
        survivor.kill_streak = 0
        achievements.died(server, zombie, survivor, WEAPON, False)
    assert "lastman_kills_zombies_easy" in unlocked(server, survivor)


def test_each_zombie_round_starts_its_own_tallies():
    server, mode, (zombie,), survivors = _zombie(4)
    _kill(server, mode, zombie, survivors[0], MELEE)
    _kill(server, mode, zombie, survivors[1], MELEE)
    asyncio.run(mode._finish_round(SURVIVOR_TEAM, "SURVIVOR_WIN"))
    assert server.achievements._round_open is False
    # Between rounds nothing counts.
    achievements.died(server, survivors[2], zombie, MELEE, False)
    asyncio.run(mode._begin_next_round())
    for tick in range(10):
        asyncio.run(mode.on_tick(100 + tick))
        if mode.phase is ZombiePhase.ACTIVE:
            break
    assert mode.phase is ZombiePhase.ACTIVE
    engine = server.achievements
    assert engine._round_open is True
    (first,) = engine._initial_zombies
    assert engine.tracker(first).round == {}
    victims = [p for p in server.players.values() if p.team == SURVIVOR_TEAM]
    _kill(server, mode, first, victims[0], MELEE)
    assert "zombie_kills_humans" not in unlocked(server, first)
    assert engine.tracker(first).round["zombie_kills"] == 1


# ---------------------------------------------------------------------------
# VIP
# ---------------------------------------------------------------------------

def _vip():
    server, mode = vfx._new_mode()
    attach_engine(server)
    mode._run_end_sequence = _no_end_sequence
    players = {
        "blue": [vfx._player(server, 1, TEAM1), vfx._player(server, 3, TEAM1)],
        "green": [vfx._player(server, 2, TEAM2), vfx._player(server, 4, TEAM2)],
    }
    asyncio.run(mode.on_tick(1))
    assert mode.phase is VIPPhase.ACTIVE
    blue_vip, green_vip = mode.vips[TEAM1], mode.vips[TEAM2]
    blue_guard = next(p for p in players["blue"] if p is not blue_vip)
    green_guard = next(p for p in players["green"] if p is not green_vip)
    return server, mode, blue_vip, blue_guard, green_vip, green_guard


def _cancel_round_task(mode):
    task = getattr(mode, "_round_task", None)
    if task is not None:
        task.cancel()


def test_a_vip_who_kills_nobody_and_lives_is_the_passenger():
    server, mode, blue_vip, blue_guard, green_vip, green_guard = _vip()
    assert server.achievements._vips == {TEAM1: blue_vip, TEAM2: green_vip}
    _kill(server, mode, green_guard, blue_vip)
    _kill(server, mode, green_guard, blue_guard)
    assert mode.phase is VIPPhase.INTERMISSION
    _cancel_round_task(mode)
    assert unlocked(server, green_vip) == {"vip_pacifist"}
    assert unlocked(server, blue_vip) == set()        # dead
    assert unlocked(server, green_guard) == set()     # most kills, not a VIP
    assert server.achievements._vips is None


def test_a_vip_with_the_most_kills_is_the_main_man_not_the_passenger():
    server, mode, blue_vip, blue_guard, green_vip, green_guard = _vip()
    _kill(server, mode, green_vip, blue_vip)
    _kill(server, mode, green_vip, blue_guard)
    _cancel_round_task(mode)
    assert unlocked(server, green_vip) == {"vip_mvp"}


def test_the_main_man_needs_the_highest_tally():
    server, mode, blue_vip, blue_guard, green_vip, green_guard = _vip()
    _kill(server, mode, blue_guard, green_guard)
    green_guard.alive = green_guard.spawned = True
    _kill(server, mode, blue_guard, green_guard)
    green_guard.alive = green_guard.spawned = True
    _kill(server, mode, green_vip, blue_vip)
    _kill(server, mode, green_guard, blue_guard)
    _cancel_round_task(mode)
    # The blue guard out-killed the green VIP, who did kill someone.
    assert unlocked(server, green_vip) == set()


def test_a_vip_alive_when_the_match_clock_runs_out_is_still_judged():
    server, mode, blue_vip, blue_guard, green_vip, green_guard = _vip()
    _kill(server, mode, blue_vip, green_guard)
    asyncio.run(mode.on_mode_end(None))
    assert unlocked(server, blue_vip) == {"vip_mvp"}
    assert unlocked(server, green_vip) == {"vip_pacifist"}
    assert server.achievements._round_open is False


def test_a_successor_did_not_start_the_round_as_vip():
    server, mode, blue_vip, blue_guard, green_vip, green_guard = _vip()
    # The green VIP leaves; its guard is promoted mid-round.
    mode.vips[TEAM2] = green_guard
    _kill(server, mode, green_guard, blue_vip)
    _kill(server, mode, green_guard, blue_guard)
    _cancel_round_task(mode)
    assert unlocked(server, green_guard) == set()
    assert unlocked(server, green_vip) == set()


# ---------------------------------------------------------------------------
# Diamond Mine
# ---------------------------------------------------------------------------

def _dia(monkeypatch, now):
    server, mode = se._dia(monkeypatch, now)
    attach_engine(server)
    mode._run_end_sequence = _no_end_sequence
    return server, mode


def _cash(mode, player, tick):
    player.set_position(mode.active_dropoffs[0].zone.center)
    asyncio.run(mode.on_tick(tick))
    assert int(player.id) not in mode.carriers


@pytest.mark.parametrize("delay,unlocks", [(5.0, True), (5.1, False)])
def test_diamond_picked_up_within_five_seconds_and_carried_home(monkeypatch, delay, unlocks):
    now = [100.0]
    server, mode = _dia(monkeypatch, now)
    miner = se._add(server, 1, TEAM1, (300.0, 300.0, 60.0))
    mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0], uncovered_by=miner)
    now[0] += delay
    asyncio.run(mode.on_tick(1))
    assert int(miner.id) in mode.carriers
    _cash(mode, miner, 2)
    assert ("diamond_hotfoot" in unlocked(server, miner)) is unlocks


def test_dropping_the_diamond_on_the_way_spoils_the_hotfoot(monkeypatch):
    now = [100.0]
    server, mode = _dia(monkeypatch, now)
    miner = se._add(server, 1, TEAM1, (300.0, 300.0, 60.0))
    mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0], uncovered_by=miner)
    asyncio.run(mode.on_tick(1))
    asyncio.run(mode._drop_carried_diamond(miner))
    now[0] += 1.0
    asyncio.run(mode.on_tick(2))
    assert int(miner.id) not in mode.carriers          # still inside the re-pickup delay
    now[0] += 3.0                                       # 4 s after it first appeared
    asyncio.run(mode.on_tick(3))
    assert int(miner.id) in mode.carriers
    _cash(mode, miner, 4)
    assert "diamond_hotfoot" not in unlocked(server, miner)
    # It was still the miner's own find.
    assert server.achievements.tracker(miner).match["diamonds_found"] == 1


def test_three_found_diamonds_dropped_off_in_one_match(monkeypatch):
    now = [100.0]
    server, mode = _dia(monkeypatch, now)
    miner = se._add(server, 1, TEAM1, (300.0, 300.0, 60.0))
    mate = se._add(server, 2, TEAM1, (10.0, 10.0, 60.0))
    tick = 0
    for found in range(1, 4):
        miner.set_position((300.0, 300.0, 60.0))
        now[0] += 30.0
        mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0], uncovered_by=miner)
        tick += 1
        asyncio.run(mode.on_tick(tick))
        tick += 1
        _cash(mode, miner, tick)
        assert ("diamond_collector" in unlocked(server, miner)) is (found == 3)
    # Cashing in a teammate's find is not finding it.
    mate.set_position((300.0, 300.0, 60.0))
    miner.set_position((10.0, 10.0, 60.0))
    now[0] += 30.0
    mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0], uncovered_by=miner)
    asyncio.run(mode.on_tick(tick + 1))
    assert int(mate.id) in mode.carriers
    _cash(mode, mate, tick + 2)
    assert "diamonds_found" not in server.achievements.tracker(mate).match


def test_found_diamonds_do_not_carry_into_the_next_match(monkeypatch):
    now = [100.0]
    server, mode = _dia(monkeypatch, now)
    miner = se._add(server, 1, TEAM1, (300.0, 300.0, 60.0))
    tick = 0
    for round_index in range(2):
        for _ in range(2):
            miner.set_position((300.0, 300.0, 60.0))
            now[0] += 30.0
            mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0], uncovered_by=miner)
            tick += 1
            asyncio.run(mode.on_tick(tick))
            tick += 1
            _cash(mode, miner, tick)
        asyncio.run(mode.on_mode_start())
    assert "diamond_collector" not in unlocked(server, miner)


def _steal_one(server, mode, now, thief, finder, tick):
    """``finder`` uncovers a diamond; ``thief`` takes it first and cashes in."""
    thief.set_position((300.0, 300.0, 60.0))
    finder.set_position((340.0, 340.0, 60.0))
    now[0] += 30.0
    mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0], uncovered_by=finder)
    asyncio.run(mode.on_tick(tick))
    assert int(thief.id) in mode.carriers
    _cash(mode, thief, tick + 1)


def test_five_diamonds_stolen_from_under_the_finders_nose(monkeypatch):
    now = [100.0]
    server, mode = _dia(monkeypatch, now)
    server.config.mode_settings["dia"]["score_limit"] = 50
    mode.score_limit = 50
    thief = se._add(server, 1, TEAM1, (300.0, 300.0, 60.0))
    finder = se._add(server, 2, TEAM2, (340.0, 340.0, 60.0))
    mate = se._add(server, 3, TEAM1, (360.0, 360.0, 60.0))
    for stolen in range(1, 6):
        _steal_one(server, mode, now, thief, finder, stolen * 10)
        assert progress(server, thief, "diamond_thief_count") == stolen
        assert ("diamond_thief" in unlocked(server, thief)) is (stolen == 5)
    # A teammate's find is not stolen from the enemy.
    _steal_one(server, mode, now, thief, mate, 100)
    assert progress(server, thief, "diamond_thief_count") == 5
    assert progress(server, thief, "diamond_interceptor_count") == 0


def test_a_diamond_the_enemy_already_carried_is_intercepted_not_stolen(monkeypatch):
    now = [100.0]
    server, mode = _dia(monkeypatch, now)
    raider = se._add(server, 1, TEAM1, (10.0, 10.0, 60.0))
    carrier = se._add(server, 2, TEAM2, (300.0, 300.0, 60.0))
    mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0], uncovered_by=carrier)
    asyncio.run(mode.on_tick(1))
    assert int(carrier.id) in mode.carriers
    # The raider kills the carrier, takes the diamond and cashes it in.
    _kill(server, mode, raider, carrier)
    carrier.set_position((10.0, 10.0, 60.0))
    raider.set_position((300.0, 300.0, 60.0))
    now[0] += 10.0
    asyncio.run(mode.on_tick(2))
    assert int(raider.id) in mode.carriers
    _cash(mode, raider, 3)
    assert progress(server, raider, "diamond_interceptor_count") == 1
    assert progress(server, raider, "diamond_thief_count") == 0
    assert "diamond_hotfoot" not in unlocked(server, raider)


def test_an_interception_ends_when_the_interceptor_drops_it(monkeypatch):
    now = [100.0]
    server, mode = _dia(monkeypatch, now)
    raider = se._add(server, 1, TEAM1, (10.0, 10.0, 60.0))
    mate = se._add(server, 3, TEAM1, (20.0, 20.0, 60.0))
    carrier = se._add(server, 2, TEAM2, (300.0, 300.0, 60.0))
    mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0], uncovered_by=carrier)
    asyncio.run(mode.on_tick(1))
    asyncio.run(mode._drop_carried_diamond(carrier))
    carrier.set_position((10.0, 10.0, 60.0))
    raider.set_position((300.0, 300.0, 60.0))
    now[0] += 10.0
    asyncio.run(mode.on_tick(2))
    assert int(raider.id) in mode.carriers
    # The raider drops it; a teammate carries it home.
    asyncio.run(mode._drop_carried_diamond(raider))
    raider.set_position((10.0, 10.0, 60.0))
    mate.set_position((300.0, 300.0, 60.0))
    now[0] += 10.0
    asyncio.run(mode.on_tick(3))
    assert int(mate.id) in mode.carriers
    _cash(mode, mate, 4)
    assert progress(server, raider, "diamond_interceptor_count") == 0
    assert progress(server, mate, "diamond_interceptor_count") == 0


# ---------------------------------------------------------------------------
# Multi-Hill
# ---------------------------------------------------------------------------

def _hills(monkeypatch, now):
    server, mode = mh._started(monkeypatch, now)
    attach_engine(server)
    server.mode = mode
    mode._run_end_sequence = _no_end_sequence
    # Holding one hill for its whole window must not win the match first.
    mode.score_limit = 100000
    return server, mode, mode.active_zones[0]


def _outside(zone):
    return (zone.center[0] + 200.0, zone.center[1], zone.center[2])


def _expire(mode, now, tick=900):
    now[0] = mode._next_rotation_at + 0.1
    asyncio.run(mode.on_tick(tick))
    assert mode.phase == "intermission"


def test_the_only_player_to_score_from_a_hill(monkeypatch):
    now = [100.0]
    server, mode, zone = _hills(monkeypatch, now)
    blue = mh._mh_player(server, 1, TEAM1, zone.center)
    now[0] = 100.5
    asyncio.run(mode.on_tick(1))
    assert blue.score > 0
    assert set(server.achievements._hill_scorers[zone.index]) == {id(blue)}
    _expire(mode, now)
    assert "hill_greedy" in unlocked(server, blue)
    assert zone.index not in server.achievements._hill_scorers


def test_a_second_scorer_on_the_hill_denies_one_man_army(monkeypatch):
    now = [100.0]
    server, mode, zone = _hills(monkeypatch, now)
    blue = mh._mh_player(server, 1, TEAM1, zone.center)
    bot = mh._mh_player(server, 2, TEAM2, _outside(zone))
    bot.is_bot = True
    now[0] = 100.5
    asyncio.run(mode.on_tick(1))
    # A bot steps in and is paid for contesting: it is a player too.
    bot.position = tuple(zone.center)
    now[0] = 100.0 + float(CG.MH_SCORE_OCCUPY_INTERVAL) + 0.1
    asyncio.run(mode.on_tick(2))
    assert bot.score > 0
    _expire(mode, now)
    assert "hill_greedy" not in unlocked(server, blue)


def test_surviving_the_strike_on_the_hill_you_emptied(monkeypatch):
    now = [100.0]
    server, mode, zone = _hills(monkeypatch, now)
    stayer = mh._mh_player(server, 1, TEAM1, zone.center)
    runner = mh._mh_player(server, 2, TEAM1, zone.center)
    casualty = mh._mh_player(server, 3, TEAM1, zone.center)
    passer_by = mh._mh_player(server, 4, TEAM1, _outside(zone))
    now[0] = 100.5
    asyncio.run(mode.on_tick(1))
    _expire(mode, now)
    assert len(server.achievements._strikes) == 1
    # While the shells fall: one runs off the hill, one is killed.
    runner.position = _outside(zone)
    casualty.alive = False
    achievements.tick(server, 0.5)
    casualty.alive = True
    assert progress(server, stayer, "hill_strike_survivor_count") == 0
    achievements.tick(server, 0.5)
    assert server.achievements._strikes == []
    assert progress(server, stayer, "hill_strike_survivor_count") == 1
    assert progress(server, runner, "hill_strike_survivor_count") == 1
    assert progress(server, casualty, "hill_strike_survivor_count") == 0
    assert progress(server, passer_by, "hill_strike_survivor_count") == 0
    assert "hill_strike_stay_put" in unlocked(server, stayer)
    assert "hill_strike_stay_put" not in unlocked(server, runner)


def test_a_strike_is_not_judged_while_its_shells_are_in_the_air(monkeypatch):
    from modes.airstrike import AIRSTRIKE_SPEC

    now = [100.0]
    server, mode, zone = _hills(monkeypatch, now)
    blue = mh._mh_player(server, 1, TEAM1, zone.center)
    now[0] = 100.5
    asyncio.run(mode.on_tick(1))
    shell = SimpleNamespace(spec=AIRSTRIKE_SPEC)
    server.projectile_engine = SimpleNamespace(projectiles=[shell])
    _expire(mode, now)
    for _ in range(6):
        achievements.tick(server, 0.5)
    assert progress(server, blue, "hill_strike_survivor_count") == 0
    server.projectile_engine.projectiles.clear()
    achievements.tick(server, 0.5)
    assert progress(server, blue, "hill_strike_survivor_count") == 1


def test_five_strikes_survived(monkeypatch):
    now = [100.0]
    server, mode, zone = _hills(monkeypatch, now)
    blue = mh._mh_player(server, 1, TEAM1, zone.center)
    engine = server.achievements
    engine.add(blue, "hill_strike_survivor_count", 4)
    now[0] = 100.5
    asyncio.run(mode.on_tick(1))
    _expire(mode, now)
    assert "hill_strike_survivor" not in unlocked(server, blue)
    achievements.tick(server, 1.0)
    assert "hill_strike_survivor" in unlocked(server, blue)


def test_keeping_and_taking_a_contested_hill(monkeypatch):
    now = [100.0]
    server, mode, zone = _hills(monkeypatch, now)
    blue = mh._mh_player(server, 1, TEAM1, zone.center)
    green = mh._mh_player(server, 2, TEAM2, _outside(zone))

    def step(seconds=0.5):
        now[0] += seconds
        mode._update_control(now[0])

    step()
    assert mode.zone_owner[zone.index] == TEAM1
    # Green walks in (contested) and is driven off: Blue retained it.
    green.position = tuple(zone.center)
    step()
    assert mode.zone_contested[zone.index]
    green.position = _outside(zone)
    step()
    assert progress(server, blue, "hill_defender_count") == 1
    assert progress(server, blue, "hill_interceptor_count") == 0

    # An uncontested takeover is neither.
    blue.position = _outside(zone)
    green.position = tuple(zone.center)
    step()
    assert mode.zone_owner[zone.index] == TEAM2
    assert progress(server, green, "hill_interceptor_count") == 0

    # Blue contests Green's hill and clears it: Blue took a contested hill.
    blue.position = tuple(zone.center)
    step()
    green.position = _outside(zone)
    step()
    assert mode.zone_owner[zone.index] == TEAM1
    assert progress(server, blue, "hill_interceptor_count") == 1
    assert progress(server, blue, "hill_defender_count") == 1
    assert progress(server, green, "hill_defender_count") == 0

    # A contest both sides walk away from pays nobody.
    green.position = tuple(zone.center)
    step()
    blue.position = green.position = _outside(zone)
    step()
    assert progress(server, blue, "hill_defender_count") == 1


def test_ten_contested_hills_kept(monkeypatch):
    now = [100.0]
    server, mode, zone = _hills(monkeypatch, now)
    blue = mh._mh_player(server, 1, TEAM1, zone.center)
    green = mh._mh_player(server, 2, TEAM2, _outside(zone))
    now[0] += 0.5
    mode._update_control(now[0])
    for kept in range(1, 11):
        green.position = tuple(zone.center)
        now[0] += 0.5
        mode._update_control(now[0])
        green.position = _outside(zone)
        now[0] += 0.5
        mode._update_control(now[0])
        assert progress(server, blue, "hill_defender_count") == kept
        assert ("hill_defender" in unlocked(server, blue)) is (kept == 10)


def test_kills_from_and_into_an_active_hill(monkeypatch):
    now = [100.0]
    server, mode, zone = _hills(monkeypatch, now)
    blue = mh._mh_player(server, 1, TEAM1, zone.center)
    green = mh._mh_player(server, 2, TEAM2, _outside(zone))
    now[0] = 100.5
    asyncio.run(mode.on_tick(1))
    asyncio.run(mode.on_player_kill(blue, green, 0))          # shooting out
    assert (progress(server, blue, "hill_kill_shooting_out_count"),
            progress(server, blue, "hill_kill_shooting_in_count")) == (1, 0)
    asyncio.run(mode.on_player_kill(green, blue, 0))          # shooting in
    assert (progress(server, green, "hill_kill_shooting_out_count"),
            progress(server, green, "hill_kill_shooting_in_count")) == (0, 1)
    green.position = tuple(zone.center)
    asyncio.run(mode.on_player_kill(blue, green, 0))          # both inside
    assert (progress(server, blue, "hill_kill_shooting_out_count"),
            progress(server, blue, "hill_kill_shooting_in_count")) == (2, 1)
    # No hill is active between rotations.
    _expire(mode, now)
    asyncio.run(mode.on_player_kill(blue, green, 0))
    assert progress(server, blue, "hill_kill_shooting_out_count") == 2


def test_hill_kills_of_bots_follow_the_switch(monkeypatch):
    now = [100.0]
    server, mode, zone = _hills(monkeypatch, now)
    server.achievements.config = SimpleNamespace(enabled=True, count_bot_kills=False)
    blue = mh._mh_player(server, 1, TEAM1, zone.center)
    bot = mh._mh_player(server, 2, TEAM2, zone.center)
    bot.is_bot = True
    now[0] = 100.5
    asyncio.run(mode.on_tick(1))
    asyncio.run(mode.on_player_kill(blue, bot, 0))
    assert progress(server, blue, "hill_kill_shooting_out_count") == 0


# ---------------------------------------------------------------------------
# Demolition
# ---------------------------------------------------------------------------

def _demolition(monkeypatch, now):
    server, mode = dem._active_mode(monkeypatch, now)
    attach_engine(server)
    server.mode = mode
    return server, mode


def _destroy(server, player, *cells):
    for cell in cells:
        server.world_manager.mutate(cell, False)
    achievements.blocks_destroyed(server, player, list(cells), [], [])


def test_the_mode_counts_only_enemy_objective_cells(monkeypatch):
    now = [100.0]
    server, mode = _demolition(monkeypatch, now)
    blue = dem._player(1, TEAM1, server)
    green = dem._player(2, TEAM2, server)
    cells = [(40, 50, 30), (41, 50, 30), (10, 20, 30), (99, 99, 99)]
    assert mode.enemy_objective_damage(blue, cells) == (TEAM2, 2)
    assert mode.enemy_objective_damage(green, cells) == (TEAM1, 1)
    assert mode.enemy_objective_damage(blue, [(10, 20, 30)]) is None   # its own base
    assert mode.enemy_objective_damage(None, cells) is None
    spectator = SimpleNamespace(id=9, team=int(C.TEAM_SPECTATOR))
    assert mode.enemy_objective_damage(spectator, cells) is None
    mode.phase = "airstrike"
    assert mode.enemy_objective_damage(blue, cells) is None


def test_base_damage_reaches_the_counters_through_the_block_hook(monkeypatch):
    now = [100.0]
    server, mode = _demolition(monkeypatch, now)
    blue = dem._player(1, TEAM1, server)
    _destroy(server, blue, (40, 50, 30))
    assert progress(server, blue, "demolition_damage_many_rounds_count") == 1
    assert server.achievements.tracker(blue).match["demolition_damage"] == 1
    # A drill bore never reaches the mode's queued hook but is counted here.
    with achievements.block_cause(server, blue, int(C.DRILL_KILL)):
        _destroy(server, blue, (41, 50, 30))
    assert progress(server, blue, "drillgun_demolition_count") == 1
    assert progress(server, blue, "demolition_damage_many_rounds_count") == 2


def test_repairing_the_base_counts_blocks_the_mode_credits(monkeypatch):
    now = [100.0]
    server, mode = _demolition(monkeypatch, now)
    blue = dem._player(1, TEAM1, server)
    green = dem._player(2, TEAM2, server)
    world = server.world_manager
    world.mutate((10, 20, 30), False)
    asyncio.run(mode.on_blocks_destroyed(green, ((10, 20, 30),), True))
    world.mutate((10, 20, 30), True)
    asyncio.run(mode.on_blocks_built(blue, ((10, 20, 30),)))
    assert progress(server, blue, "demolition_repair_count") == 1
    # Digging out and refilling its own wall is not a repair.
    world.mutate((11, 20, 30), False)
    asyncio.run(mode.on_blocks_destroyed(blue, ((11, 20, 30),), True))
    world.mutate((11, 20, 30), True)
    asyncio.run(mode.on_blocks_built(blue, ((11, 20, 30),)))
    assert progress(server, blue, "demolition_repair_count") == 1
    # A block placed outside the base is not a repair either.
    asyncio.run(mode.on_blocks_built(blue, ((200, 200, 30),)))
    assert progress(server, blue, "demolition_repair_count") == 1
    server.achievements.add(blue, "demolition_repair_count", 98)
    assert "demolition_repair" not in unlocked(server, blue)
    world.mutate((10, 20, 30), False)
    asyncio.run(mode.on_blocks_destroyed(green, ((10, 20, 30),), True))
    world.mutate((10, 20, 30), True)
    asyncio.run(mode.on_blocks_built(blue, ((10, 20, 30),)))
    assert "demolition_repair" in unlocked(server, blue)


def test_the_last_attacker_of_the_fallen_base_puts_the_boot_in(monkeypatch):
    from modes import demolition as demolition_module

    now = [100.0]
    server, mode = _demolition(monkeypatch, now)
    monkeypatch.setattr(demolition_module, "trigger_airstrike", lambda *_args: 5)
    first = dem._player(1, TEAM1, server)
    last = dem._player(2, TEAM1, server)
    _destroy(server, first, (40, 50, 30))
    _destroy(server, last, (41, 50, 30))
    asyncio.run(mode.on_tick(2))
    assert mode.phase == "airstrike"
    # Rubble kicked after the base fell is not damage to it.
    achievements.blocks_destroyed(server, first, [(40, 50, 30)], [], [])
    now[0] += float(CG.DEM_TIME_TO_WAIT_FOR_AIRSTRIKE)
    asyncio.run(mode.on_tick(3))
    now[0] += demolition_module.AIRSTRIKE_IMPACT_DELAY
    asyncio.run(mode.on_tick(4))
    assert mode.ended and mode.winner == TEAM1
    assert unlocked(server, last) == {"demolition_final_damage"}
    assert unlocked(server, first) == set()


def test_a_timeout_win_also_credits_the_final_damage(monkeypatch):
    now = [100.0]
    server, mode = _demolition(monkeypatch, now)
    blue = dem._player(1, TEAM1, server)
    _destroy(server, blue, (40, 50, 30))
    asyncio.run(mode._end_by_time())
    assert mode.winner == TEAM1
    assert unlocked(server, blue) == {"demolition_final_damage"}


# ---------------------------------------------------------------------------
# Occupation
# ---------------------------------------------------------------------------

def _occupation(monkeypatch, now):
    server, mode = se._occ(monkeypatch, now)
    attach_engine(server)
    mode._run_end_sequence = _no_end_sequence
    return server, mode


def _pick_up_from_spawn(server, mode, player, tick=1):
    asyncio.run(mode.on_tick(tick))
    assert int(player.id) in mode.carriers
    return next(iter(mode.bombs.values()))


def _plant(mode, player):
    player.set_position(mode.target_zone.center)
    asyncio.run(mode.handle_drop_pickup(player, player.position, (0.0, 0.0, 0.0)))


def test_a_bomb_carried_from_its_spawn_into_the_base_and_survived(monkeypatch):
    now = [100.0]
    server, mode = _occupation(monkeypatch, now)
    bomber = se._add(server, 1, TEAM1, (100.0, 100.0, 60.0))
    bomb = _pick_up_from_spawn(server, mode, bomber)
    _plant(mode, bomber)
    assert unlocked(server, bomber) == {"bomb_hotfoot"}
    bomber.set_position((100.0, 100.0, 60.0))
    now[0] = bomb.explode_at + 0.1
    asyncio.run(mode.on_tick(2))
    assert server.teams[TEAM1].score > 0
    assert unlocked(server, bomber) == {"bomb_hotfoot", "bomb_survivor"}
    assert server.achievements._last_bomb_planter is bomber


def test_a_bomber_killed_by_the_blast_did_not_survive(monkeypatch):
    now = [100.0]
    server, mode = _occupation(monkeypatch, now)
    bomber = se._add(server, 1, TEAM1, (100.0, 100.0, 60.0))
    bomb = _pick_up_from_spawn(server, mode, bomber)
    _plant(mode, bomber)

    def blast(*_args, **_kwargs):
        bomber.alive = False

    server._apply_blast = blast
    now[0] = bomb.explode_at + 0.1
    asyncio.run(mode.on_tick(2))
    assert unlocked(server, bomber) == {"bomb_hotfoot"}


def test_a_bomb_dropped_on_the_way_is_no_hotfoot(monkeypatch):
    now = [100.0]
    server, mode = _occupation(monkeypatch, now)
    bomber = se._add(server, 1, TEAM1, (100.0, 100.0, 60.0))
    _pick_up_from_spawn(server, mode, bomber)
    asyncio.run(mode.handle_drop_pickup(bomber, bomber.position, (0.0, 0.0, 0.0)))
    now[0] += 4.0
    asyncio.run(mode.on_tick(2))
    assert int(bomber.id) in mode.carriers
    _plant(mode, bomber)
    assert unlocked(server, bomber) == set()


def test_a_bomb_that_goes_off_outside_the_base_helps_nobody(monkeypatch):
    now = [100.0]
    server, mode = _occupation(monkeypatch, now)
    bomber = se._add(server, 1, TEAM1, (100.0, 100.0, 60.0))
    bomb = _pick_up_from_spawn(server, mode, bomber)
    asyncio.run(mode.handle_drop_pickup(bomber, bomber.position, (0.0, 0.0, 0.0)))
    now[0] = bomb.explode_at + 0.1
    asyncio.run(mode.on_tick(2))
    assert unlocked(server, bomber) == set()
    assert server.achievements._last_bomb_planter is None


def test_the_last_successful_bomber_wins_the_round(monkeypatch):
    now = [100.0]
    server, mode = _occupation(monkeypatch, now)
    bomber = se._add(server, 1, TEAM1, (100.0, 100.0, 60.0))
    bomb = _pick_up_from_spawn(server, mode, bomber)
    _plant(mode, bomber)
    bomber.set_position((100.0, 100.0, 60.0))
    now[0] = bomb.explode_at + 0.1
    asyncio.run(mode.on_tick(2))
    assert "bomb_final_bomber" not in unlocked(server, bomber)
    asyncio.run(mode._end_by_score(TEAM1))
    assert "bomb_final_bomber" in unlocked(server, bomber)


def test_the_final_bomber_needs_its_team_to_win(monkeypatch):
    now = [100.0]
    server, mode = _occupation(monkeypatch, now)
    bomber = se._add(server, 1, TEAM1, (100.0, 100.0, 60.0))
    bomb = _pick_up_from_spawn(server, mode, bomber)
    _plant(mode, bomber)
    now[0] = bomb.explode_at + 0.1
    asyncio.run(mode.on_tick(2))
    asyncio.run(mode._end_by_score(TEAM2))
    assert "bomb_final_bomber" not in unlocked(server, bomber)


def test_intercepting_a_carrier_and_holding_the_bomb_until_it_blows(monkeypatch):
    now = [100.0]
    server, mode = _occupation(monkeypatch, now)
    carrier = se._add(server, 1, TEAM1, (100.0, 100.0, 60.0))
    guard = se._add(server, 2, TEAM2, (108.0, 100.0, 60.0))
    bomb = _pick_up_from_spawn(server, mode, carrier)
    _kill(server, mode, guard, carrier)
    assert bomb.armed and int(carrier.id) not in mode.carriers
    assert progress(server, guard, "bomb_interceptor_count") == 0
    now[0] = bomb.explode_at + 0.1
    asyncio.run(mode.on_tick(2))
    assert progress(server, guard, "bomb_interceptor_count") == 1
    server.achievements.add(guard, "bomb_interceptor_count", 3)
    assert "bomb_interceptor" not in unlocked(server, guard)


def test_an_interception_fails_when_the_attackers_take_the_bomb_back(monkeypatch):
    now = [100.0]
    server, mode = _occupation(monkeypatch, now)
    carrier = se._add(server, 1, TEAM1, (100.0, 100.0, 60.0))
    mate = se._add(server, 3, TEAM1, (300.0, 300.0, 60.0))
    guard = se._add(server, 2, TEAM2, (108.0, 100.0, 60.0))
    bomb = _pick_up_from_spawn(server, mode, carrier)
    _kill(server, mode, guard, carrier)
    carrier.set_position((300.0, 300.0, 60.0))
    guard.set_position((300.0, 310.0, 60.0))
    mate.set_position(bomb.position)
    now[0] += 4.0
    asyncio.run(mode.on_tick(2))
    assert int(mate.id) in mode.carriers
    now[0] = bomb.explode_at + 0.1
    asyncio.run(mode.on_tick(3))
    assert progress(server, guard, "bomb_interceptor_count") == 0


def test_killing_a_defender_who_carries_the_bomb_is_not_an_interception(monkeypatch):
    now = [100.0]
    server, mode = _occupation(monkeypatch, now)
    defender = se._add(server, 2, TEAM2, (100.0, 100.0, 60.0))
    attacker = se._add(server, 1, TEAM1, (108.0, 100.0, 60.0))
    attacker.set_position((300.0, 300.0, 60.0))
    bomb = _pick_up_from_spawn(server, mode, defender)
    attacker.set_position((108.0, 100.0, 60.0))
    _kill(server, mode, attacker, defender)
    now[0] = bomb.explode_at + 0.1
    asyncio.run(mode.on_tick(2))
    assert progress(server, attacker, "bomb_interceptor_count") == 0


# ---------------------------------------------------------------------------
# CTF and Classic CTF
# ---------------------------------------------------------------------------

def _ctf(mode_class=CTFMode):
    from tests.test_recovered_objective_modes import _Server

    server = _Server()
    attach_engine(server)
    mode = mode_class(server)
    server.mode = mode
    mode._run_end_sequence = _no_end_sequence
    asyncio.run(mode.on_mode_start())
    return server, mode


def _intercept(server, mode, runner, defender):
    asyncio.run(mode._pickup_intel(runner, TEAM1))
    _kill(server, mode, defender, runner)
    runner.alive = runner.spawned = True


@pytest.mark.parametrize("mode_class", [CTFMode, ClassicCTFMode])
def test_intercepting_the_thief_and_seeing_the_intel_home(mode_class):
    server, mode = _ctf(mode_class)
    runner = se._add(server, 1, TEAM2, (250.0, 256.0, 50.0))
    defender = se._add(server, 2, TEAM1, (260.0, 256.0, 50.0))
    for done in range(1, 11):
        _intercept(server, mode, runner, defender)
        assert progress(server, defender, "intel_defence_count") == done - 1
        # Home by the timer on odd rounds, by a teammate's touch on even ones.
        returned_by = None if done % 2 else se._add(server, 9, TEAM1, (0.0, 0.0, 50.0))
        asyncio.run(mode._return_intel(TEAM1, returned_by=returned_by))
        assert progress(server, defender, "intel_defence_count") == done
        expected = set()
        if done >= 5:
            expected.add("intel_defence_easy")
        if done >= 10:
            expected.add("intel_defence_hard")
        assert unlocked(server, defender) == expected
    assert unlocked(server, runner) == set()


def test_an_interception_is_lost_when_the_enemy_takes_the_intel_again():
    server, mode = _ctf()
    runner = se._add(server, 1, TEAM2, (250.0, 256.0, 50.0))
    second = se._add(server, 3, TEAM2, (250.0, 256.0, 50.0))
    defender = se._add(server, 2, TEAM1, (260.0, 256.0, 50.0))
    _intercept(server, mode, runner, defender)
    asyncio.run(mode._pickup_intel(second, TEAM1))
    assert server.achievements._intel_interceptors == {}
    # Whoever stops the second thief is the one who defended it.
    other = se._add(server, 4, TEAM1, (262.0, 256.0, 50.0))
    _kill(server, mode, other, second)
    asyncio.run(mode._return_intel(TEAM1))
    assert progress(server, defender, "intel_defence_count") == 0
    assert progress(server, other, "intel_defence_count") == 1


def test_only_the_intels_own_team_can_defend_it():
    server, mode = _ctf()
    runner = se._add(server, 1, TEAM2, (250.0, 256.0, 50.0))
    defender = se._add(server, 2, TEAM1, (260.0, 256.0, 50.0))
    asyncio.run(mode._pickup_intel(runner, TEAM1))
    # The carrier falls to its death: nobody intercepted it.
    achievements.died(server, runner, None, int(C.FALL_KILL), False)
    runner.alive = False
    asyncio.run(mode.on_player_death(runner, None, int(C.FALL_KILL)))
    asyncio.run(mode._return_intel(TEAM1))
    assert progress(server, defender, "intel_defence_count") == 0
    # A return with nothing intercepted (round reset, guard) is nothing.
    asyncio.run(mode._return_intel(TEAM2))
    assert server.achievements.faults == {}


def test_a_team_kill_of_the_thief_is_not_a_defence():
    server, mode = _ctf()
    runner = se._add(server, 1, TEAM2, (250.0, 256.0, 50.0))
    mate = se._add(server, 3, TEAM2, (252.0, 256.0, 50.0))
    asyncio.run(mode._pickup_intel(runner, TEAM1))
    # Friendly fire: the thief's own teammate kills it.
    _kill(server, mode, mate, runner)
    assert server.achievements._intel_interceptors == {}
    asyncio.run(mode._return_intel(TEAM1))
    assert progress(server, mate, "intel_defence_count") == 0


def test_a_defender_who_left_is_not_credited():
    server, mode = _ctf()
    runner = se._add(server, 1, TEAM2, (250.0, 256.0, 50.0))
    defender = se._add(server, 2, TEAM1, (260.0, 256.0, 50.0))
    _intercept(server, mode, runner, defender)
    del server.players[defender.id]
    asyncio.run(mode._return_intel(TEAM1))
    server.players[defender.id] = defender
    assert progress(server, defender, "intel_defence_count") == 0


@pytest.mark.parametrize("mode_class,counts", [(ClassicCTFMode, True), (CTFMode, False)])
def test_kills_while_carrying_the_intel_count_in_classic_only(mode_class, counts):
    server, mode = _ctf(mode_class)
    carrier = se._add(server, 1, TEAM2, (250.0, 256.0, 50.0))
    victim = se._add(server, 2, TEAM1, (260.0, 256.0, 50.0))
    achievements.died(server, victim, carrier, WEAPON, False)
    assert progress(server, carrier, "classic_kills_with_intel_count") == 0
    asyncio.run(mode._pickup_intel(carrier, TEAM1))
    for done in range(1, 6):
        achievements.died(server, victim, carrier, WEAPON, False)
        assert progress(server, carrier, "classic_kills_with_intel_count") == (done if counts else 0)
    assert ("classic_kills_with_intel" in unlocked(server, carrier)) is counts


# ---------------------------------------------------------------------------
# Match boundaries
# ---------------------------------------------------------------------------

def test_every_mode_start_opens_a_fresh_match_and_its_end_closes_it(monkeypatch):
    now = [100.0]
    server, mode = _demolition(monkeypatch, now)
    blue = dem._player(1, TEAM1, server)
    engine = server.achievements
    _destroy(server, blue, (40, 50, 30))
    engine.tracker(blue).round["kills"] = 3
    asyncio.run(mode.on_mode_end(None))
    assert engine._round_open is False
    # A restart is a new match: the one-round tallies are gone.
    asyncio.run(mode.on_mode_start())
    assert engine._round_open is True
    assert engine.tracker(blue).match == {} and engine.tracker(blue).round == {}
    assert engine._demolition_last_damager == {}
    assert progress(server, blue, "demolition_damage_many_rounds_count") == 1


def test_a_retiring_mode_ends_no_match(monkeypatch):
    now = [100.0]
    server, mode = _demolition(monkeypatch, now)
    blue = dem._player(1, TEAM1, server)
    _destroy(server, blue, (40, 50, 30))
    mode.begin_retirement()
    asyncio.run(mode.on_mode_end(TEAM1))
    # A map/mode rollover is not a win.
    assert unlocked(server, blue) == set()
