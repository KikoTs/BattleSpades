"""Round-2 mode fixes: generic retail scoring edge cases.

Objective blasts are not suicides, departed ids are never paid, Tutorial/UGC
never see generic combat scores, Zombie never pays the generic assist, and
CTF/Classic CTF keep the retail 100/150 kill score.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import shared.constants as C
import shared.constants_gamemode as CG

from modes.base_mode import BaseMode
from modes.classic_ctf import ClassicCTFMode
from modes.ctf import CTFMode
from modes.tutorial import TutorialMode
from modes.ugc import UGCMode
from modes.zombie import ZombieMode
from server import combat_scores
from server.game_constants import KILL_HEADSHOT, KILL_MELEE, TEAM1, TEAM2
from shared.bytes import ByteReader
from shared.packet import SetScore
from tests.test_mode_lifecycle_contracts import _player, _scoring_server


class _PlainMode(BaseMode):
    name = "Plain"


def _mode(**config):
    server = _scoring_server()
    for key, value in config.items():
        setattr(server.config, key, value)
    return _PlainMode(server)


def _score_rows(server):
    return [
        SetScore(ByteReader(data[1:]))
        for data in server.broadcast_packets
        if data and data[0] == SetScore.id
    ]


# -- 1. death penalty -----------------------------------------------------


@pytest.mark.parametrize("kill_type", [int(C.AIRSTRIKE_KILL), int(C.BOMB_KILL)])
def test_ownerless_objective_blast_is_not_a_suicide(kill_type):
    """Multi-Hill/Demolition airstrikes (owner 0xFF) and Occupation bombs
    (thrower=None) used to charge every victim GENERIC_SCORE_SUICIDE."""
    mode = _mode()
    victim = _player(0, TEAM1, mode.server)
    asyncio.run(mode.on_player_death(victim, None, kill_type))
    assert victim.score == 0
    assert _score_rows(mode.server) == []


def test_bomb_carrier_caught_in_own_blast_and_teammates_are_not_penalised():
    mode = _mode()
    carrier = _player(0, TEAM1, mode.server)
    mate = _player(1, TEAM1, mode.server)
    asyncio.run(mode.on_player_death(carrier, carrier, int(C.BOMB_KILL)))
    asyncio.run(mode.on_player_death(mate, carrier, int(C.BOMB_KILL)))
    assert carrier.score == 0 and mate.score == 0


def test_unattributed_world_death_is_not_a_suicide():
    mode = _mode()
    victim = _player(0, TEAM1, mode.server)
    # An ownerless mine / server blast: no killing player, not self-inflicted.
    asyncio.run(mode.on_player_death(victim, None, int(C.MINE_KILL)))
    asyncio.run(mode.on_player_death(victim, None, int(C.WEAPON_KILL)))
    assert victim.score == 0


def test_real_suicides_and_falls_still_cost_the_retail_penalty():
    mode = _mode()
    player = _player(0, TEAM1, mode.server)
    asyncio.run(mode.on_player_death(player, None, int(C.FALL_KILL)))
    assert player.score == int(CG.GENERIC_SCORE_SUICIDE)
    asyncio.run(mode.on_player_death(player, player, int(C.GRENADE_KILL)))
    assert player.score == 2 * int(CG.GENERIC_SCORE_SUICIDE)
    teammate = _player(1, TEAM1, mode.server)
    asyncio.run(mode.on_player_death(teammate, player, int(C.WEAPON_KILL)))
    assert player.score == 2 * int(CG.GENERIC_SCORE_SUICIDE) + int(
        CG.GENERIC_SCORE_TEAMKILL
    )


def test_departed_or_reused_id_gets_no_generic_score():
    mode = _mode()
    killer = _player(0, TEAM1, mode.server)
    victim = _player(1, TEAM2, mode.server)
    # The killer left and a newcomer took compact id 0 before the queued
    # kill event drained.
    newcomer = _player(0, TEAM2, mode.server)
    asyncio.run(mode.on_player_kill(killer, victim, int(C.WEAPON_KILL)))
    assert killer.score == 0 and newcomer.score == 0
    assert _score_rows(mode.server) == []
    # A departed team killer is not charged either.
    mode.server.players.pop(0)
    mate = _player(2, TEAM1, mode.server)
    asyncio.run(mode.on_player_death(mate, killer, int(C.WEAPON_KILL)))
    assert killer.score == 0


# -- 3. Tutorial / UGC / Zombie -------------------------------------------


def test_tutorial_ugc_and_zombie_opt_out_of_generic_scoring():
    assert TutorialMode.generic_scoring_enabled is False
    assert UGCMode.generic_scoring_enabled is False
    assert ZombieMode.generic_scoring_enabled is False
    assert BaseMode.generic_scoring_enabled is True


@pytest.mark.parametrize(
    "config", [{"default_mode": "tut"}, {"default_mode": "ugc"}, {"ugc_runtime": True}]
)
def test_generic_scores_are_blocked_on_tutorial_and_ugc_runtimes(config):
    mode = _mode(**config)
    killer = _player(0, TEAM1, mode.server)
    victim = _player(1, TEAM2, mode.server)
    asyncio.run(mode.on_player_kill(killer, victim, int(C.WEAPON_KILL)))
    asyncio.run(mode.on_player_death(killer, killer, int(C.WEAPON_KILL)))
    assert killer.score == 0
    assert _score_rows(mode.server) == []


def _assist_server(mode_code: str, *, generic: bool):
    packets = []
    server = SimpleNamespace(
        mode=SimpleNamespace(ended=False, generic_scoring_enabled=generic),
        config=SimpleNamespace(default_mode=mode_code, ugc_runtime=False),
        players={},
        broadcast=lambda data, **_k: packets.append(bytes(data)),
        packets=packets,
    )
    return server


def _body(server, pid, team):
    body = SimpleNamespace(
        id=pid, team=team, score=0, name=f"P{pid}", kill_streak=1,
        replication_generation=0, damage_contributions={},
    )
    server.players[pid] = body
    return body


@pytest.mark.parametrize(
    "mode_code, generic, paid",
    [("zom", False, False), ("zombie", True, False), ("tdm", True, True)],
)
def test_zombie_never_pays_the_generic_assist(mode_code, generic, paid):
    server = _assist_server(mode_code, generic=generic)
    killer = _body(server, 1, TEAM1)
    assistant = _body(server, 2, TEAM1)
    victim = _body(server, 3, TEAM2)
    combat_scores.record_damage(server, victim, assistant, 60, now=10.0)
    combat_scores.record_death(server, victim, killer, int(C.WEAPON_KILL), now=11.0)
    expected = int(CG.GENERIC_SCORE_ASSIST) if paid else 0
    assert assistant.score == expected
    # The per-round award tally still counts the assist.
    assert combat_scores.round_stats(assistant).awards.get(C.MOST_ASSISTS) == 1


# -- 11. CTF / Classic CTF keep the retail kill score --------------------


@pytest.mark.parametrize("mode_class", [CTFMode, ClassicCTFMode])
@pytest.mark.parametrize(
    "kill_type, expected",
    [(int(C.WEAPON_KILL), 100), (int(KILL_HEADSHOT), 150), (int(KILL_MELEE), 150)],
)
def test_ctf_and_classic_ctf_keep_generic_retail_kill_score(mode_class, kill_type, expected):
    from tests.test_ctf_entities import _Server

    server = _Server()
    mode = mode_class(server)
    killer = SimpleNamespace(id=1, team=TEAM1, score=0, name="K")
    victim = SimpleNamespace(id=2, team=TEAM2, score=0, name="V")
    server.players.update({1: killer, 2: victim})
    asyncio.run(mode.on_player_kill(killer, victim, kill_type))
    assert killer.score == expected
    assert int(CG.GENERIC_SCORE_KILL) == 100
    assert int(CG.GENERIC_SCORE_HEADSHOT) == int(CG.GENERIC_SCORE_MELEE) == 150


@pytest.mark.parametrize(
    "killer_is_self, kill_type, counted",
    [
        (False, int(C.BOMB_KILL), False),
        (False, int(C.AIRSTRIKE_KILL), False),
        (True, int(C.BOMB_KILL), False),
        (False, int(C.WEAPON_KILL), False),
        (False, int(C.FALL_KILL), True),
        (True, int(C.GRENADE_KILL), True),
    ],
)
def test_most_suicides_award_matches_the_penalty_rule(killer_is_self, kill_type, counted):
    server = _assist_server("tdm", generic=True)
    victim = _body(server, 3, TEAM2)
    killer = victim if killer_is_self else None
    combat_scores.record_death(server, victim, killer, kill_type, now=1.0)
    awards = combat_scores.round_stats(victim).awards
    assert awards.get(C.MOST_SUICIDES, 0) == (1 if counted else 0)


def test_server_forced_recovery_death_is_not_a_suicide_penalty():
    from types import SimpleNamespace as _NS
    from modes.base_mode import BaseMode
    import shared.constants as _C

    mode = BaseMode.__new__(BaseMode)
    player = _NS(death_penalty_exempt=True, team=2)
    assert mode.apply_generic_death_penalty(player, None, int(_C.FALL_KILL)) == 0
    assert player.death_penalty_exempt is False


def test_medpack_heals_a_boosted_vip_below_their_max():
    from types import SimpleNamespace as _NS
    from server.entities.behaviors import MedpackBehavior

    healed = []
    player = _NS(team=2, health=150, max_health=200, heal=healed.append)
    medpack = MedpackBehavior.__new__(MedpackBehavior)
    medpack.team, medpack.heal_amount, medpack.uses = 2, 50, 3
    medpack.on_touch(None, player, None)
    assert healed == [50]
