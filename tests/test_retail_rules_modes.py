"""Retail mode rules that had constants but no server behaviour.

* VIP sudden-death damage over time (CG.VIP_SUDDEN_DEATH_*).
* VIP Defend / Distraction / Close to VIP / VIP Assault score events.
* Zombie patient zero's C.FIRST_ZOMBIE_SPAWN_PROTECTION_TIME window.
* Capture-point resupply (C.CAPTURE_POINT_REFILL_TIME) on owned TC
  territories and held Multi-Hill hills.
* Multi-Hill "First to Hill" / "Claim Hill" split.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants as C
import shared.constants_gamemode as CG

from modes.vip import SUDDEN_DEATH_DAMAGE_TYPE, VIP_SCORE_ASSAULT, VIP_SCORE_ASSAULT_ENEMY, VIPPhase
from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM, ZombieMode
from server.game_constants import TEAM1, TEAM2
from shared.bytes import ByteReader
from shared.packet import LocalisedMessage, SetHP, SetScore

from tests import test_multi_hill as mh_tests
from tests import test_territory_control as tc_tests
from tests.test_vip import _new_mode, _player


def _decode(rows, packet_type):
    return [
        packet_type(ByteReader(data[1:]))
        for data in rows
        if data and data[0] == packet_type.id
    ]


def _clock(monkeypatch, module: str, start: float = 1000.0):
    now = [start]
    monkeypatch.setattr(f"{module}.time.monotonic", lambda: now[0])
    return now


# --------------------------------------------------------------- VIP


def _vip_round(monkeypatch):
    monkeypatch.setattr("server.profile_stats.score_changed", lambda *_a: None)
    now = _clock(monkeypatch, "modes.vip")
    server, mode = _new_mode()
    for player_id, team in ((1, TEAM1), (3, TEAM1), (5, TEAM1), (2, TEAM2), (4, TEAM2)):
        _player(server, player_id, team)
    asyncio.run(mode.on_tick(1))
    assert mode.phase is VIPPhase.ACTIVE
    return server, mode, now


def _kill_blue_vip(server, mode):
    blue_vip = mode.vips[TEAM1]
    green_vip = mode.vips[TEAM2]
    blue_vip.alive = blue_vip.spawned = False
    asyncio.run(mode.on_player_death(blue_vip, green_vip, 0))
    return blue_vip, green_vip


def test_sudden_death_announces_after_delay_then_drains_one_hp_per_second(monkeypatch):
    server, mode, now = _vip_round(monkeypatch)
    blue_vip, green_vip = _kill_blue_vip(server, mode)
    blue = [p for p in server.players.values() if p.team == TEAM1 and p is not blue_vip]
    green = [p for p in server.players.values() if p.team == TEAM2]
    server.packets.clear()

    now[0] += float(CG.VIP_SUDDEN_DEATH_DELAY_AFTER_VIP_KILL) - 0.1
    asyncio.run(mode.on_tick(2))
    assert not any(m.string_id == "VIP_SUDDEN_DEATH_ACTIVATED"
                   for m in _decode(server.packets, LocalisedMessage))

    now[0] += 0.2
    asyncio.run(mode.on_tick(3))
    activated = [m for m in _decode(server.packets, LocalisedMessage)
                 if m.string_id == "VIP_SUDDEN_DEATH_ACTIVATED"]
    assert len(activated) == 1
    assert list(activated[0].parameters) == [server.teams[TEAM1].name]

    # No damage during the VIP_SUDDEN_DEATH_TIME grace.
    now[0] += float(CG.VIP_SUDDEN_DEATH_TIME) - 0.5
    asyncio.run(mode.on_tick(4))
    assert all(p.health == 100 for p in blue + green)

    now[0] += 0.5
    asyncio.run(mode.on_tick(5))
    assert [p.health for p in blue] == [100 - int(CG.VIP_SUDDEN_DEATH_DAMAGE)] * len(blue)
    assert all(p.health == 100 for p in green)
    rows = _decode(blue[0].connection.sent, SetHP)
    assert rows[-1].damage_type == SUDDEN_DEATH_DAMAGE_TYPE == 4
    assert rows[-1].hp == 99

    # Same second: no second step. A long stall pays exactly one step.
    asyncio.run(mode.on_tick(6))
    assert blue[0].health == 99
    now[0] += 30.0
    asyncio.run(mode.on_tick(7))
    assert blue[0].health == 98
    assert green_vip.health == 100


def test_sudden_death_kills_with_vip_mode_kill_and_stops_at_round_end(monkeypatch):
    server, mode, now = _vip_round(monkeypatch)
    blue_vip, _green_vip = _kill_blue_vip(server, mode)
    blue = [p for p in server.players.values() if p.team == TEAM1 and p is not blue_vip]
    now[0] += float(CG.VIP_SUDDEN_DEATH_DELAY_AFTER_VIP_KILL)
    asyncio.run(mode.on_tick(2))
    now[0] += float(CG.VIP_SUDDEN_DEATH_TIME)
    blue[0].health = 1
    asyncio.run(mode.on_tick(3))
    assert blue[0].alive is False
    assert blue[0].last_kill_type == int(C.KILL.VIP_MODE_KILL)

    asyncio.run(mode._finish_round(TEAM2))
    assert mode._sudden_death_damage_at == {TEAM1: None, TEAM2: None}
    health = blue[1].health
    now[0] += 10.0
    asyncio.run(mode.on_tick(4))
    assert blue[1].health == health


def test_sudden_death_disabled_rule_schedules_no_damage(monkeypatch):
    server, mode, now = _vip_round(monkeypatch)
    mode.sudden_death_enabled = False
    blue_vip = mode.vips[TEAM1]
    blue_vip.alive = blue_vip.spawned = False
    asyncio.run(mode.on_player_death(blue_vip, mode.vips[TEAM2], 0))
    assert mode._sudden_death_activate_at == {TEAM1: None, TEAM2: None}


def _reasons(server):
    return [(row.specifier, row.reason, row.value) for row in _decode(server.packets, SetScore)]


def test_vip_defend_pays_the_killer_and_distract_pays_the_vip(monkeypatch):
    server, mode, _now = _vip_round(monkeypatch)
    blue_vip = mode.vips[TEAM1]
    guard = next(p for p in server.players.values() if p.team == TEAM1 and p is not blue_vip)
    attacker = next(p for p in server.players.values() if p.team == TEAM2 and p is not mode.vips[TEAM2])
    blue_vip.position = (100.0, 100.0, 50.0)
    attacker.position = (110.0, 100.0, 50.0)  # inside VIP_THREAT_RADIUS (20)
    mode.vips[TEAM2].position = (400.0, 400.0, 50.0)
    guard.score = blue_vip.score = 0
    server.packets.clear()
    asyncio.run(mode.on_player_kill(guard, attacker, 0))
    reasons = {(pid, reason) for pid, reason, _value in _reasons(server)}
    assert (guard.id, int(C.SCORE_REASON.VIP_DEFEND_SCORE_REASON)) in reasons
    assert (blue_vip.id, int(C.SCORE_REASON.VIP_DISTRACT_SCORE_REASON)) in reasons
    assert blue_vip.score == int(CG.VIP_SCORE_DISTRACT)
    assert guard.score == int(CG.GENERIC_SCORE_KILL) + int(CG.VIP_SCORE_DEFEND)

    # Out of the threat radius: only the generic kill score.
    attacker.position = (130.0, 100.0, 50.0)
    guard.score = 0
    asyncio.run(mode.on_player_kill(guard, attacker, 0))
    assert guard.score == int(CG.GENERIC_SCORE_KILL)


def test_close_to_vip_pays_for_killing_a_bodyguard(monkeypatch):
    server, mode, _now = _vip_round(monkeypatch)
    green_vip = mode.vips[TEAM2]
    bodyguard = next(p for p in server.players.values() if p.team == TEAM2 and p is not green_vip)
    killer = next(p for p in server.players.values() if p.team == TEAM1 and p is not mode.vips[TEAM1])
    mode.vips[TEAM1].position = (0.0, 0.0, 0.0)
    green_vip.position = (300.0, 300.0, 50.0)
    bodyguard.position = (305.0, 300.0, 50.0)
    killer.score = 0
    asyncio.run(mode.on_player_kill(killer, bodyguard, 0))
    assert killer.score == int(CG.GENERIC_SCORE_KILL) + VIP_SCORE_ASSAULT


def test_vip_assault_pays_recent_damagers_of_the_killed_vip(monkeypatch):
    server, mode, now = _vip_round(monkeypatch)
    green_vip = mode.vips[TEAM2]
    blue = [p for p in server.players.values() if p.team == TEAM1 and p is not mode.vips[TEAM1]]
    helper, killer = blue[0], blue[1]
    stale = mode.vips[TEAM1]
    mode.modify_incoming_damage(green_vip, 40, stale, 0)
    now[0] += 11.0  # older than VIP_ASSAULT_ENEMY_WINDOW
    mode.modify_incoming_damage(green_vip, 40, helper, 0)
    mode.modify_incoming_damage(green_vip, 40, killer, 0)
    helper.score = killer.score = stale.score = 0
    green_vip.alive = green_vip.spawned = False
    asyncio.run(mode.on_player_death(green_vip, killer, 0))
    assert helper.score == VIP_SCORE_ASSAULT_ENEMY
    reasons = {(pid, reason) for pid, reason, _value in _reasons(server)}
    assert (helper.id, int(C.SCORE_REASON.VIP_ASSAULT_ENEMY_SCORE_REASON)) in reasons
    assert (killer.id, int(C.SCORE_REASON.VIP_ASSAULT_ENEMY_SCORE_REASON)) not in reasons
    assert stale.score == 0


def test_new_vip_reasons_feed_the_retail_commendations():
    totals = C.SCORE_REASONS_FOR_TOTALS
    assert int(C.SCORE_REASON.VIP_DEFEND_SCORE_REASON) in totals[C.COM_VIP_DEFEND]
    assert int(C.SCORE_REASON.VIP_DISTRACT_SCORE_REASON) in totals[C.COM_VIP_DEFEND]
    assert int(C.SCORE_REASON.VIP_ASSAULT_SCORE_REASON) in totals[C.COM_VIP_ASSAULT]
    assert int(C.SCORE_REASON.VIP_ASSAULT_ENEMY_SCORE_REASON) in totals[C.COM_VIP_ASSAULT]
    assert int(C.SCORE_REASON.MH_CLAIM_SCORE_REASON) in totals[C.COM_MH_CONTROL]


# ------------------------------------------------------------ Zombie


class _ProtectedPlayer(SimpleNamespace):
    """Player.spawn_protection_remaining semantics with a fixed clock."""

    def spawn_protection_remaining(self):
        if not self.alive:
            return 0.0
        duration = self.rule
        cap = getattr(self, "spawn_protection_cap", None)
        if cap is not None:
            duration = min(duration, cap)
        return max(0.0, duration - (self.now[0] - self.spawned_at))


def test_patient_zero_first_life_gets_exactly_the_retail_window():
    now = [50.0]
    player = _ProtectedPlayer(id=7, team=ZOMBIE_TEAM, alive=True, spawned_at=50.0, rule=3.0, now=now)
    mode = ZombieMode.__new__(ZombieMode)
    mode._patient_zero_protection_pending = {7: player}
    asyncio.run(mode.on_player_spawn(player))
    assert player.spawn_protection_remaining() == float(C.FIRST_ZOMBIE_SPAWN_PROTECTION_TIME) == 0.5
    now[0] += 0.5
    assert player.spawn_protection_remaining() == 0.0

    # One use only: the next life keeps the ordinary rule window (Player.spawn
    # resets the per-life cap).
    player.spawned_at = now[0]
    player.spawn_protection_cap = None
    asyncio.run(mode.on_player_spawn(player))
    assert player.spawn_protection_remaining() == 3.0


def test_patient_zero_window_never_extends_a_shorter_rule():
    now = [50.0]
    player = _ProtectedPlayer(id=7, team=ZOMBIE_TEAM, alive=True, spawned_at=50.0, rule=0.0, now=now)
    ZombieMode.limit_spawn_protection(player, 0.5)
    assert player.spawned_at == 50.0
    assert player.spawn_protection_remaining() == 0.0


def test_infecting_patient_zero_marks_the_one_use_window():
    mode = ZombieMode.__new__(ZombieMode)
    mode.patient_zero_ids = set()
    mode._patient_zero_protection_pending = {}
    mode._survivor_selection_by_player = {}
    mode._infection_spawn_by_player = {}
    mode._move_to_team = lambda player, team: setattr(player, "team", team)
    mode._zombie_selection = lambda class_id: SimpleNamespace(class_id=class_id)
    mode._zombie_class_for = lambda player: int(C.CLASS_ZOMBIE)
    mode.announce_localised_to_player = lambda *_a, **_k: None
    player = SimpleNamespace(
        id=4, team=SURVIVOR_TEAM, class_id=-1, alive=False, position=None,
        apply_class_selection=lambda selection: None,
    )
    asyncio.run(mode._infect(player, patient_zero=True))
    assert mode._patient_zero_protection_pending == {4: player}
    asyncio.run(mode._infect(SimpleNamespace(id=5, team=ZOMBIE_TEAM), patient_zero=True))
    assert 5 not in mode._patient_zero_protection_pending


# ---------------------------------------------------- Capture points


def _supplied(player):
    player.supplies = []
    player.max_health = 100
    player.health = 40
    player.heal = lambda amount: (player.supplies.append(("heal", amount)),
                                  setattr(player, "health", 100))
    player.restock_ammo = lambda kind=0: player.supplies.append(("ammo", kind))
    player.restock_blocks = lambda: player.supplies.append(("blocks",))
    return player


def test_owned_territory_resupplies_its_team_every_refill_interval(monkeypatch):
    now = [100.0]
    server, mode = tc_tests._mode(monkeypatch, now)
    owned = mode.territories[0]
    assert owned.owner == TEAM1
    holder = _supplied(tc_tests._player(server, 1, TEAM1, owned.zone.center))
    asyncio.run(mode._capture_tick(0.5))
    assert holder.supplies == [("heal", 100), ("ammo", int(C.AMMO_CRATE)), ("blocks",)]

    now[0] += float(C.CAPTURE_POINT_REFILL_TIME) - 0.5
    asyncio.run(mode._capture_tick(0.5))
    assert len(holder.supplies) == 3
    now[0] += 0.5
    holder.health = 100
    asyncio.run(mode._capture_tick(0.5))
    # Full health is not re-healed; ammo and blocks refill again.
    assert holder.supplies[3:] == [("ammo", int(C.AMMO_CRATE)), ("blocks",)]


def test_contested_or_enemy_territory_does_not_resupply(monkeypatch):
    now = [100.0]
    server, mode = tc_tests._mode(monkeypatch, now)
    owned = mode.territories[0]
    intruder = _supplied(tc_tests._player(server, 2, TEAM2, owned.zone.center))
    asyncio.run(mode._capture_tick(0.5))
    assert intruder.supplies == []
    holder = _supplied(tc_tests._player(server, 1, TEAM1, owned.zone.center))
    asyncio.run(mode._capture_tick(0.5))
    assert holder.supplies == [] and intruder.supplies == []


def test_capture_point_resupply_can_be_disabled(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("modes.territory_control.time.time", lambda: now[0])
    monkeypatch.setattr("server.profile_stats.score_changed", lambda *_a: None)
    server = tc_tests._Server()
    server.config.mode_settings["tc"]["capture_point_resupply"] = False
    mode = tc_tests.TerritoryControlMode(server)
    asyncio.run(mode.on_mode_start())
    holder = _supplied(tc_tests._player(server, 1, TEAM1, mode.territories[0].zone.center))
    asyncio.run(mode._capture_tick(0.5))
    assert holder.supplies == []


def test_held_hill_resupplies_its_owner(monkeypatch):
    now = [100.0]
    server, mode = mh_tests._started(monkeypatch, now)
    zone = mode.active_zones[0]
    blue = _supplied(mh_tests._mh_player(server, 1, TEAM1, zone.center))
    mode._update_control(now[0])  # claims the hill
    assert mode.zone_owner[zone.index] == TEAM1
    mode._update_control(now[0])
    assert blue.supplies == [("heal", 100), ("ammo", int(C.AMMO_CRATE)), ("blocks",)]
    mode._update_control(now[0] + 1.0)
    assert len(blue.supplies) == 3


# -------------------------------------------------------- Multi-Hill


def test_first_to_hill_and_claim_hill_are_separate_awards(monkeypatch):
    now = [100.0]
    server, mode = mh_tests._started(monkeypatch, now)
    zone = mode.active_zones[0]
    far = (0.0, 0.0, 0.0)
    green = mh_tests._mh_player(server, 4, TEAM2, zone.center)
    blue_a = mh_tests._mh_player(server, 1, TEAM1, zone.center)
    blue_b = mh_tests._mh_player(server, 2, TEAM1, zone.center)
    mode._update_control(now[0])
    # Lowest id reached the fresh hill first; a contested hill is held, so
    # nobody claims it yet (rules audit 2026-09-27 #19).
    assert blue_a.score == int(CG.MH_SCORE_FIRST)
    assert green.score == 0 and blue_b.score == 0
    # Green leaves: blue holds it alone and EVERY blue occupant claims it.
    green.position = far
    mode._update_control(now[0])
    assert blue_a.score == int(CG.MH_SCORE_FIRST) + int(CG.MH_SCORE_CLAIM)
    assert blue_b.score == int(CG.MH_SCORE_CLAIM)
    # First to Hill is paid once per activation.
    blue_a.position = blue_b.position = far
    mode._update_control(now[0])
    blue_a.position = zone.center
    mode._update_control(now[0])
    assert blue_a.score == int(CG.MH_SCORE_FIRST) + int(CG.MH_SCORE_CLAIM)
