"""Retail objective score events: CTF / Diamond Mine / Occupation kill
events, carry/escort payouts, carrier minimap exposure, blast survival and
teabagging (server.combat_scores helpers)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants as C
import shared.constants_gamemode as CG

from modes.ctf import CTFMode
from modes.diamond_mine import DiamondMineMode
from modes.occupation import OccupationMode
from server import combat_scores as cs
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL
from shared.packet import ChangePlayer, SetScore
from tests.test_recovered_objective_modes import _decode, _Player, _Server, _zone

R = C.SCORE_REASON
WEAPON = int(C.KILL.WEAPON_KILL)


def _reasons(server, player=None):
    rows = _decode(server.packets, SetScore)
    return [
        (row.reason, row.value) for row in rows
        if row.type == int(C.SCORE.PLAYER)
        and (player is None or row.specifier == player.id)
    ]


def _add(server, player_id, team, position):
    player = _Player(player_id, team, position)
    server.players[player.id] = player
    return player


# ---------------------------------------------------------------------------
# CTF
# ---------------------------------------------------------------------------


def _ctf(monkeypatch=None):
    server = _Server()
    mode = CTFMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    server.packets.clear()
    return server, mode


def _far(mode, team):
    """A spot far from every CTF objective."""
    return (256.0, 20.0 if team == TEAM1 else 490.0, 50.0)


def test_ctf_intercept_pays_killer_of_own_intel_carrier():
    server, mode = _ctf()
    runner = _add(server, 1, TEAM2, (250.0, 256.0, 50.0))
    defender = _add(server, 2, TEAM1, (260.0, 256.0, 50.0))
    asyncio.run(mode._pickup_intel(runner, TEAM1))
    runner.alive = False

    asyncio.run(mode.on_player_death(runner, defender, WEAPON))

    assert (int(R.CTF_INTERCEPT_SCORE_REASON), int(CG.CTF_SCORE_INTERCEPT)) in _reasons(
        server, defender
    )
    assert defender.score == int(CG.CTF_SCORE_INTERCEPT)
    # One event per kill: no defend/assault on top.
    assert len(_reasons(server, defender)) == 1


def test_ctf_carrier_defend_needs_enemy_near_own_carrier():
    server, mode = _ctf()
    carrier = _add(server, 1, TEAM1, (250.0, 256.0, 50.0))
    guard = _add(server, 2, TEAM1, (240.0, 256.0, 50.0))
    attacker = _add(server, 3, TEAM2, (255.0, 256.0, 50.0))
    far_attacker = _add(server, 4, TEAM2, (250.0, 300.0, 50.0))
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    carrier.score = 0  # the home-grab "First to Claim Flag" 100 is tested separately

    attacker.alive = False
    asyncio.run(mode.on_player_death(attacker, guard, WEAPON))
    assert guard.score == int(CG.CTF_SCORE_CARRIER_DEFEND)
    assert _reasons(server, guard) == [
        (int(R.CTF_CARRIER_DEFEND_SCORE_REASON), int(CG.CTF_SCORE_CARRIER_DEFEND))
    ]
    assert cs.round_stats(guard).awards[C.MOST_DEFENDS] == 1

    far_attacker.alive = False
    asyncio.run(mode.on_player_death(far_attacker, guard, WEAPON))
    assert guard.score == int(CG.CTF_SCORE_CARRIER_DEFEND)  # unchanged


def test_ctf_carrier_killing_its_attacker_is_not_carrier_defend():
    server, mode = _ctf()
    carrier = _add(server, 1, TEAM1, (250.0, 256.0, 50.0))
    attacker = _add(server, 3, TEAM2, (255.0, 256.0, 50.0))
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    carrier.score = 0  # the home-grab "First to Claim Flag" 100 is tested separately
    attacker.alive = False

    asyncio.run(mode.on_player_death(attacker, carrier, WEAPON))

    assert carrier.score == 0


def test_ctf_defend_assault_and_assault_enemy_geometry():
    server, mode = _ctf()
    blue_intel = mode.intel_positions[TEAM1]
    green_intel = mode.intel_positions[TEAM2]
    # Defend: Green threat next to the Blue intel, killed by a Blue player.
    blue = _add(server, 1, TEAM1, _far(mode, TEAM1))
    threat = _add(server, 2, TEAM2, blue_intel)
    threat.alive = False
    asyncio.run(mode.on_player_death(threat, blue, WEAPON))
    assert _reasons(server, blue) == [
        (int(R.CTF_DEFEND_SCORE_REASON), int(CG.CTF_SCORE_DEFEND))
    ]

    # Close to Flag: Blue killer standing at the Green intel.
    server.packets.clear()
    raider = _add(server, 3, TEAM1, green_intel)
    victim = _add(server, 4, TEAM2, _far(mode, TEAM2))
    victim.alive = False
    asyncio.run(mode.on_player_death(victim, raider, WEAPON))
    assert _reasons(server, raider) == [
        (int(R.CTF_ASSAULT_SCORE_REASON), int(CG.CTF_SCORE_ASSAULT))
    ]

    # Flag Assault: Blue sniper far away kills a defender at the Green intel.
    server.packets.clear()
    sniper = _add(server, 5, TEAM1, _far(mode, TEAM1))
    guard = _add(server, 6, TEAM2, green_intel)
    guard.alive = False
    asyncio.run(mode.on_player_death(guard, sniper, WEAPON))
    assert _reasons(server, sniper) == [
        (int(R.CTF_ASSAULT_ENEMY_SCORE_REASON), int(CG.CTF_SCORE_ASSAULT_ENEMY))
    ]


def test_ctf_midfield_kill_team_kill_and_suicide_earn_no_objective_event():
    server, mode = _ctf()
    a = _add(server, 1, TEAM1, (256.0, 150.0, 50.0))
    b = _add(server, 2, TEAM2, (256.0, 160.0, 50.0))
    mate = _add(server, 3, TEAM1, mode.intel_positions[TEAM1])
    b.alive = False
    asyncio.run(mode.on_player_death(b, a, WEAPON))
    mate.alive = False
    asyncio.run(mode.on_player_death(mate, a, WEAPON))
    asyncio.run(mode.on_player_death(a, a, WEAPON))
    assert not [row for row in _reasons(server) if row[0] >= int(R.CTF_CAPTURE_SCORE_REASON)]


def test_ctf_distraction_pays_victim_near_own_carrier():
    server, mode = _ctf()
    carrier = _add(server, 1, TEAM1, (250.0, 256.0, 50.0))
    decoy = _add(server, 2, TEAM1, (260.0, 256.0, 50.0))
    loner = _add(server, 3, TEAM1, (250.0, 400.0, 50.0))
    enemy = _add(server, 4, TEAM2, (300.0, 256.0, 50.0))
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    carrier.score = 0  # the home-grab "First to Claim Flag" 100 is tested separately

    decoy.alive = False
    asyncio.run(mode.on_player_death(decoy, enemy, WEAPON))
    assert _reasons(server, decoy) == [
        (int(R.CTF_DISTRACT_SCORE_REASON), int(CG.CTF_SCORE_DISTRACT))
    ]
    assert cs.round_stats(decoy).awards[C.MOST_DISTRACTIONS] == 1

    loner.alive = False
    asyncio.run(mode.on_player_death(loner, enemy, WEAPON))
    assert loner.score == 0
    # A fall/world death is no distraction either.
    decoy.alive = True
    decoy.score = 0
    asyncio.run(mode.on_player_death(decoy, None, int(C.FALL_KILL)))
    assert decoy.score <= 0


def test_ctf_carry_and_escort_pay_every_interval_with_hysteresis():
    server, mode = _ctf()
    carrier = _add(server, 1, TEAM1, (250.0, 256.0, 50.0))
    escort = _add(server, 2, TEAM1, (250.0 + CG.CTF_ESCORT_RADIUS - 1, 256.0, 50.0))
    straggler = _add(server, 3, TEAM1, (250.0 + CG.CTF_ESCORT_RADIUS + 5, 256.0, 50.0))
    enemy = _add(server, 4, TEAM2, (252.0, 256.0, 50.0))
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    carrier.score = 0  # the home-grab "First to Claim Flag" 100 is tested separately
    interval = float(CG.CTF_SCORE_CARRY_INTERVAL)

    mode._tick_carriers(1000.0)  # arms the carry clock
    mode._tick_carriers(1000.0 + interval - 0.1)
    assert carrier.score == 0
    mode._tick_carriers(1000.0 + interval)
    assert carrier.score == int(CG.CTF_SCORE_CARRY_SCORE)
    assert _reasons(server, carrier)[-1][0] == int(R.CTF_CARRY_SCORE_REASON)
    assert escort.score == int(CG.CTF_SCORE_ESCORT_SCORE)
    assert _reasons(server, escort)[-1][0] == int(R.CTF_ESCORT_SCORE_REASON)
    assert straggler.score == 0 and enemy.score == 0

    # Hysteresis: a current escort stays in until radius + hysteresis.
    escort.set_position((250.0 + CG.CTF_ESCORT_RADIUS + CG.CTF_ESCORT_HYSTERESIS * 0.5,
                         256.0, 50.0))
    mode._tick_carriers(1000.0 + 2 * interval)
    assert escort.score == 2 * int(CG.CTF_SCORE_ESCORT_SCORE)
    escort.set_position((250.0 + CG.CTF_ESCORT_RADIUS + CG.CTF_ESCORT_HYSTERESIS + 1,
                         256.0, 50.0))
    mode._tick_carriers(1000.0 + 3 * interval)
    assert escort.score == 2 * int(CG.CTF_SCORE_ESCORT_SCORE)

    # A stalled tick pays once, not a catch-up burst.
    before = carrier.score
    mode._tick_carriers(1000.0 + 20 * interval)
    assert carrier.score == before + int(CG.CTF_SCORE_CARRY_SCORE)


def test_ctf_carry_stops_on_drop_and_when_round_ended():
    server, mode = _ctf()
    carrier = _add(server, 1, TEAM1, (250.0, 256.0, 50.0))
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    carrier.score = 0  # the home-grab "First to Claim Flag" 100 is tested separately
    mode._tick_carriers(1000.0)
    mode.ended = True
    mode._tick_carriers(1010.0)
    assert carrier.score == 0
    mode.ended = False
    asyncio.run(mode._drop_intel(carrier, TEAM2))
    mode._tick_carriers(1020.0)
    mode._tick_carriers(1030.0)
    assert carrier.score == 0


def test_ctf_departed_carrier_is_never_paid():
    server, mode = _ctf()
    carrier = _add(server, 1, TEAM1, (250.0, 256.0, 50.0))
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    carrier.score = 0  # the home-grab "First to Claim Flag" 100 is tested separately
    claimed = list(_reasons(server))
    mode._tick_carriers(1000.0)
    _add(server, 1, TEAM2, (0.0, 0.0, 50.0))  # id reused by a joiner
    mode._tick_carriers(1010.0)
    assert carrier.score == 0
    assert _reasons(server) == claimed


def test_ctf_carrier_exposed_on_minimap_only_after_retail_delay():
    server, mode = _ctf()
    carrier = _add(server, 1, TEAM1, (250.0, 256.0, 50.0))
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    carrier.score = 0  # the home-grab "First to Claim Flag" 100 is tested separately
    exposure = float(C.INTEL_MINIMAP_EXPOSURE_TIME)
    assert exposure == 30

    mode._tick_carriers(1000.0)
    mode._tick_carriers(1000.0 + exposure - 0.5)
    assert not _decode(server.packets, ChangePlayer)
    assert not mode.mode_marks_player(carrier)
    assert mode.escape_watch_objective_player(carrier)

    mode._tick_carriers(1000.0 + exposure)
    markers = _decode(server.packets, ChangePlayer)
    assert len(markers) == 1
    assert markers[0].type == int(C.SET_HIGH_MINIMAP_VISIBILITY)
    assert markers[0].high_minimap_visibility == 1
    assert mode.mode_marks_player(carrier)
    mode._tick_carriers(1000.0 + exposure + 5)
    assert len(_decode(server.packets, ChangePlayer)) == 1  # sent once

    asyncio.run(mode._drop_intel(carrier, TEAM2))
    markers = _decode(server.packets, ChangePlayer)
    assert markers[-1].high_minimap_visibility == 0
    assert not mode.mode_marks_player(carrier)


def test_ctf_quick_capture_sends_no_marker_at_all():
    server, mode = _ctf()
    carrier = _add(server, 1, TEAM1, (250.0, 256.0, 50.0))
    carrier.captures = 0
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    carrier.score = 0  # the home-grab "First to Claim Flag" 100 is tested separately
    mode._tick_carriers(1000.0)
    asyncio.run(mode._capture_intel(carrier, TEAM2))
    assert not _decode(server.packets, ChangePlayer)
    assert carrier.score == int(CG.CTF_INDIVIDUAL_SCORE_FOR_CAPTURED_INTEL)


# ---------------------------------------------------------------------------
# Diamond Mine
# ---------------------------------------------------------------------------


def _dia(monkeypatch, now):
    monkeypatch.setattr("modes.diamond_mine.time.time", lambda: now[0])
    server = _Server({"dia": {
        "score_limit": 5, "max_active_bases": 1, "max_active_diamonds": 2,
    }})
    server.world_manager.map_metadata.diamond_base_zones.append(_zone(TEAM_NEUTRAL, 150))
    server.world_manager.map_metadata.diamond_base_capacities.append(3)
    mode = DiamondMineMode(server)
    mode._rng = SimpleNamespace(random=lambda: 0.0, randrange=lambda _n: 0)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    return server, mode


def test_dia_intercept_carrier_defend_and_distract(monkeypatch):
    now = [100.0]
    server, mode = _dia(monkeypatch, now)
    carrier = _add(server, 1, TEAM1, (300.0, 300.0, 60.0))
    buddy = _add(server, 2, TEAM1, (305.0, 300.0, 60.0))
    raider = _add(server, 3, TEAM2, (308.0, 300.0, 60.0))
    mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0])
    asyncio.run(mode.on_tick(1))
    assert int(carrier.id) in mode.carriers

    # Raider shoots the escorting buddy: distraction for the buddy.
    buddy.alive = False
    asyncio.run(mode.on_player_death(buddy, raider, WEAPON))
    assert _reasons(server, buddy) == [
        (int(R.DIA_DISTRACT_SCORE_REASON), int(CG.DIA_SCORE_DISTRACT))
    ]
    buddy.alive = True

    # Buddy kills the raider next to the carrier: carrier defend.
    raider.alive = False
    asyncio.run(mode.on_player_death(raider, buddy, WEAPON))
    assert _reasons(server, buddy)[-1] == (
        int(R.DIA_CARRIER_DEFEND_SCORE_REASON),
        int(CG.DIA_SCORE_DISTRACT) + int(CG.DIA_SCORE_CARRIER_DEFEND),
    )
    raider.alive = True

    # Raider kills the carrier: intercept.
    carrier.alive = False
    asyncio.run(mode.on_player_death(carrier, raider, WEAPON))
    assert _reasons(server, raider)[-1] == (
        int(R.DIA_INTERCEPT_SCORE_REASON), int(CG.DIA_SCORE_INTERCEPT)
    )


def test_dia_defend_near_ground_diamond_and_assault_at_dropoff(monkeypatch):
    now = [100.0]
    server, mode = _dia(monkeypatch, now)
    mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0])
    guard = _add(server, 1, TEAM1, (400.0, 400.0, 60.0))
    thief = _add(server, 2, TEAM2, (303.0, 300.0, 60.0))
    thief.alive = False
    asyncio.run(mode.on_player_death(thief, guard, WEAPON))
    assert _reasons(server, guard) == [
        (int(R.DIA_DEFEND_SCORE_REASON), int(CG.DIA_SCORE_DEFEND))
    ]

    dropoff = mode.active_dropoffs[0].zone.center
    stormer = _add(server, 3, TEAM2, dropoff)
    victim = _add(server, 4, TEAM1, (dropoff[0], dropoff[1] + 60.0, dropoff[2]))
    victim.alive = False
    asyncio.run(mode.on_player_death(victim, stormer, WEAPON))
    assert _reasons(server, stormer) == [
        (int(R.DIA_ASSAULT_SCORE_REASON), int(CG.DIA_SCORE_ASSAULT))
    ]

    # Nowhere near anything: nothing.
    loner = _add(server, 5, TEAM1, (20.0, 20.0, 60.0))
    target = _add(server, 6, TEAM2, (25.0, 20.0, 60.0))
    target.alive = False
    asyncio.run(mode.on_player_death(target, loner, WEAPON))
    assert loner.score == 0


def test_dia_escort_uses_hysteresis(monkeypatch):
    now = [100.0]
    server, mode = _dia(monkeypatch, now)
    carrier = _add(server, 1, TEAM1, (300.0, 300.0, 60.0))
    escort = _add(server, 2, TEAM1, (300.0 + CG.DIA_ESCORT_RADIUS - 1, 300.0, 60.0))
    mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0])
    asyncio.run(mode.on_tick(1))
    mode._award_carry_and_escort(1)
    assert escort.score == int(CG.DIA_SCORE_ESCORT_SCORE)
    escort.set_position((300.0 + CG.DIA_ESCORT_RADIUS + CG.DIA_ESCORT_HYSTERESIS - 1,
                         300.0, 60.0))
    mode._award_carry_and_escort(1)
    assert escort.score == 2 * int(CG.DIA_SCORE_ESCORT_SCORE)
    escort.set_position((300.0 + CG.DIA_ESCORT_RADIUS + CG.DIA_ESCORT_HYSTERESIS + 1,
                         300.0, 60.0))
    mode._award_carry_and_escort(1)
    assert escort.score == 2 * int(CG.DIA_SCORE_ESCORT_SCORE)
    # Walking back to just outside the entry radius does not re-join.
    escort.set_position((300.0 + CG.DIA_ESCORT_RADIUS + 1, 300.0, 60.0))
    mode._award_carry_and_escort(1)
    assert escort.score == 2 * int(CG.DIA_SCORE_ESCORT_SCORE)


def test_dia_kill_events_stop_after_round_end(monkeypatch):
    now = [100.0]
    server, mode = _dia(monkeypatch, now)
    mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0])
    guard = _add(server, 1, TEAM1, (400.0, 400.0, 60.0))
    thief = _add(server, 2, TEAM2, (303.0, 300.0, 60.0))
    mode.ended = True
    thief.alive = False
    asyncio.run(mode.on_player_death(thief, guard, WEAPON))
    assert guard.score == 0


# ---------------------------------------------------------------------------
# Occupation
# ---------------------------------------------------------------------------


def _occ(monkeypatch, now):
    monkeypatch.setattr("modes.occupation.time.time", lambda: now[0])
    server = _Server({"oc": {"score_limit": 30, "max_active_bombs": 1, "bomb_fuse_time": 10}})
    server.config.friendly_fire = False
    metadata = server.world_manager.map_metadata
    metadata.occupation_base_zone = _zone(TEAM2, 400)
    metadata.occupation_bomb_points.append((100.0, 100.0, 60.0))
    mode = OccupationMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    return server, mode


def test_occ_carrier_defend_distract_and_intercept_stay_single(monkeypatch):
    now = [100.0]
    server, mode = _occ(monkeypatch, now)
    carrier = _add(server, 1, TEAM1, (100.0, 100.0, 60.0))
    buddy = _add(server, 2, TEAM1, (105.0, 100.0, 60.0))
    green = _add(server, 3, TEAM2, (108.0, 100.0, 60.0))
    asyncio.run(mode.on_tick(1))
    assert int(carrier.id) in mode.carriers

    buddy.alive = False
    asyncio.run(mode.on_player_death(buddy, green, WEAPON))
    assert _reasons(server, buddy) == [
        (int(R.OCC_DISTRACT_SCORE_REASON), int(CG.OC_SCORE_DISTRACT))
    ]
    buddy.alive = True

    green.alive = False
    asyncio.run(mode.on_player_death(green, buddy, WEAPON))
    assert _reasons(server, buddy)[-1] == (
        int(R.OCC_CARRIER_DEFEND_SCORE_REASON),
        int(CG.OC_SCORE_DISTRACT) + int(CG.OC_SCORE_CARRIER_DEFEND),
    )
    green.alive = True

    server.packets.clear()
    carrier.alive = False
    asyncio.run(mode.on_player_death(carrier, green, WEAPON))
    # Only the existing intercept pays; the classifier adds nothing on top.
    assert _reasons(server, green) == [
        (int(R.OCC_INTERCEPT_SCORE_REASON), int(CG.OC_SCORE_INTERCEPT))
    ]


def test_occ_bomb_defend_and_close_to_bomb(monkeypatch):
    now = [100.0]
    server, mode = _occ(monkeypatch, now)
    carrier = _add(server, 1, TEAM1, (100.0, 100.0, 60.0))
    asyncio.run(mode.on_tick(1))
    asyncio.run(mode._drop_bomb(carrier))  # lights the fuse at (100,100)
    bomb = next(iter(mode.bombs.values()))
    assert bomb.armed

    # Blue protects its lit bomb: Bomb Defend.
    defender = _add(server, 2, TEAM1, (150.0, 150.0, 60.0))
    defuser = _add(server, 3, TEAM2, (103.0, 100.0, 60.0))
    defuser.alive = False
    asyncio.run(mode.on_player_death(defuser, defender, WEAPON))
    assert _reasons(server, defender) == [
        (int(R.OCC_DEFEND_SCORE_REASON), int(CG.OC_SCORE_DEFEND))
    ]

    # Green fights at the enemy's lit bomb: Close to Bomb.
    rusher = _add(server, 4, TEAM2, (104.0, 100.0, 60.0))
    blue = _add(server, 5, TEAM1, (160.0, 160.0, 60.0))
    blue.alive = False
    asyncio.run(mode.on_player_death(blue, rusher, WEAPON))
    assert _reasons(server, rusher) == [
        (int(R.OCC_ASSAULT_SCORE_REASON), int(CG.OC_SCORE_ASSAULT))
    ]


def test_occ_blast_survivors_get_survive_score(monkeypatch):
    now = [100.0]
    server, mode = _occ(monkeypatch, now)
    carrier = _add(server, 1, TEAM1, (100.0, 100.0, 60.0))
    asyncio.run(mode.on_tick(1))
    asyncio.run(mode._drop_bomb(carrier))
    carrier.set_position((300.0, 300.0, 60.0))
    near = _add(server, 2, TEAM2, (100.0 + C.BOMB_EXPLOSION_RADIUS - 1, 100.0, 60.0))
    far = _add(server, 3, TEAM2, (100.0 + C.BOMB_EXPLOSION_RADIUS + 5, 100.0, 60.0))
    dead = _add(server, 4, TEAM2, (100.0, 100.0 + C.BOMB_EXPLOSION_RADIUS - 2, 60.0))

    def blast(*args, **kwargs):
        dead.alive = False  # killed by the blast

    server._apply_blast = blast
    now[0] = 111.0
    asyncio.run(mode.on_tick(2))

    assert _reasons(server, near) == [
        (int(R.OCC_SURVIVE_SCORE_REASON), int(CG.OC_SCORE_SURVIVE))
    ]
    assert far.score == 0 and dead.score == 0


# ---------------------------------------------------------------------------
# Generic helpers + teabag
# ---------------------------------------------------------------------------


def test_award_score_event_skips_spectators_departed_ended_and_retiring():
    server = _Server()
    mode = SimpleNamespace(ended=False, retiring=False)
    server.mode = mode
    player = _add(server, 1, TEAM1, (0.0, 0.0, 0.0))
    bot = _add(server, 2, TEAM2, (0.0, 0.0, 0.0))
    bot.is_bot = True
    spectator = _add(server, 3, TEAM_NEUTRAL, (0.0, 0.0, 0.0))

    assert cs.award_score_event(server, player, 10, int(R.CTF_CARRY_SCORE_REASON))
    assert cs.award_score_event(server, bot, 10, int(R.CTF_CARRY_SCORE_REASON))
    assert not cs.award_score_event(server, spectator, 10, int(R.CTF_CARRY_SCORE_REASON))
    assert not cs.award_score_event(server, player, 0, int(R.CTF_CARRY_SCORE_REASON))
    mode.retiring = True
    assert not cs.award_score_event(server, player, 10, int(R.CTF_CARRY_SCORE_REASON))
    mode.retiring, mode.ended = False, True
    assert not cs.award_score_event(server, player, 10, int(R.CTF_CARRY_SCORE_REASON))
    mode.ended = False
    server.players[1] = _Player(1, TEAM1, (0.0, 0.0, 0.0))
    assert not cs.award_score_event(server, player, 10, int(R.CTF_CARRY_SCORE_REASON))
    assert player.score == 10 and bot.score == 10


def _teabag_server(points: bool):
    server = _Server()
    server.mode = SimpleNamespace(ended=False, retiring=False, mode_code="tdm",
                                  death_representation="grave")
    server.config.game_rules = {"RULE_POINTS_FROM_TEABAGGING": points}
    return server


def _rules(monkeypatch, enabled: bool):
    monkeypatch.setattr(cs, "_teabag_points_enabled", lambda _server: enabled)


def test_teabag_three_quick_crouches_over_fresh_enemy_corpse(monkeypatch):
    _rules(monkeypatch, True)
    server = _teabag_server(True)
    player = _add(server, 1, TEAM1, (100.0, 100.0, 58.0))
    victim = _add(server, 2, TEAM2, (101.0, 100.0, 60.0))
    victim.alive, victim.death_time = False, 50.0

    assert not cs.record_teabag_crouch(server, player, now=10.0)
    assert not cs.record_teabag_crouch(server, player, now=10.3)
    assert cs.record_teabag_crouch(server, player, now=10.6)
    assert player.score == int(CG.NORMAL_SCORE_TEABAG)
    assert _reasons(server, player) == [(int(C.TEABAG_SCORE_REASON), 2)]
    assert cs.round_stats(player).awards[C.MOST_TEABAGS] == 1
    # The same corpse only counts once.
    for stamp in (11.0, 11.2, 11.4):
        assert not cs.record_teabag_crouch(server, player, now=stamp)
    assert player.score == 2


def test_teabag_rejects_slow_far_friendly_and_living(monkeypatch):
    _rules(monkeypatch, True)
    server = _teabag_server(True)
    player = _add(server, 1, TEAM1, (100.0, 100.0, 58.0))
    far = _add(server, 2, TEAM2, (100.0 + C.TEABAG_MAX_DISTANCE + 0.5, 100.0, 60.0))
    far.alive, far.death_time = False, 50.0
    mate = _add(server, 3, TEAM1, (100.5, 100.0, 60.0))
    mate.alive, mate.death_time = False, 50.0
    alive = _add(server, 4, TEAM2, (100.2, 100.0, 60.0))

    for stamp in (1.0, 1.2, 1.4):
        cs.record_teabag_crouch(server, player, now=stamp)
    assert player.score == 0

    # Slow crouches over a valid corpse never reach the count.
    victim = _add(server, 5, TEAM2, (100.5, 100.0, 60.0))
    victim.alive, victim.death_time = False, 60.0
    for stamp in (5.0, 5.6, 6.2, 6.8):
        assert not cs.record_teabag_crouch(server, player, now=stamp)
    assert player.score == 0
    assert alive.alive


def test_teabag_counts_award_but_no_points_when_rule_off(monkeypatch):
    _rules(monkeypatch, False)
    server = _teabag_server(False)
    player = _add(server, 1, TEAM1, (100.0, 100.0, 58.0))
    victim = _add(server, 2, TEAM2, (100.5, 100.0, 60.0))
    victim.alive, victim.death_time = False, 50.0
    for stamp in (1.0, 1.2):
        cs.record_teabag_crouch(server, player, now=stamp)
    assert cs.record_teabag_crouch(server, player, now=1.4)
    assert player.score == 0 and not _reasons(server)
    assert cs.round_stats(player).awards[C.MOST_TEABAGS] == 1


def test_classify_objective_kill_priority():
    killer = SimpleNamespace(x=0.0, y=0.0, z=0.0, team=TEAM1, alive=True)
    victim = SimpleNamespace(x=5.0, y=0.0, z=0.0, team=TEAM2, alive=False)
    carrier = SimpleNamespace(x=6.0, y=0.0, z=0.0, team=TEAM1, alive=True)
    kwargs = dict(carrier_threat_radius=10.0, threat_radius=20.0)
    assert cs.classify_objective_kill(killer, victim, victim_carrying=True,
                                      killer_team_carriers=[carrier], **kwargs) == "intercept"
    assert cs.classify_objective_kill(killer, victim, killer_team_carriers=[carrier],
                                      defend_points=[(5.0, 0.0, 0.0)], **kwargs) == "carrier_defend"
    assert cs.classify_objective_kill(killer, victim, defend_points=[(5.0, 0.0, 0.0)],
                                      attack_points=[(0.0, 0.0, 0.0)], **kwargs) == "defend"
    assert cs.classify_objective_kill(killer, victim, **kwargs) is None
