"""Dynamic bot difficulty balance (server/bot_ai/skill_balance.py)."""

from __future__ import annotations

from types import SimpleNamespace

from server.bot_ai.profiles import ProfileFactory, _BANDS
from server.bot_ai.skill_balance import (
    BotSkillBalancer,
    band_limits,
    human_edge,
    scale_profile,
)
from server.game_constants import TEAM1, TEAM2


def _human(player_id, team, kills=0, deaths=0):
    return SimpleNamespace(id=player_id, team=team, is_bot=False, kills=kills, deaths=deaths)


def _server(difficulty="mixed", **bot_config):
    bots = SimpleNamespace(difficulty=difficulty, **bot_config)
    server = SimpleNamespace(
        config=SimpleNamespace(bots=bots),
        players={},
        teams={TEAM1: SimpleNamespace(score=0), TEAM2: SimpleNamespace(score=0)},
        mode=SimpleNamespace(ended=False),
        bots=SimpleNamespace(_runtime={}),
    )
    return server


def _add_bot(server, player_id, team, difficulty="normal", seed=0):
    profile = ProfileFactory(seed=seed + player_id).create(difficulty)
    player = SimpleNamespace(id=player_id, team=team, is_bot=True, kills=0, deaths=0)
    server.players[player_id] = player
    runtime = SimpleNamespace(player=player, generation=1, profile=profile)
    server.bots._runtime[player_id] = runtime
    return runtime


def test_close_game_changes_nothing():
    server = _server()
    server.players[1] = _human(1, TEAM1, kills=5, deaths=6)
    assert human_edge(server, TEAM1) == 0.0


def test_losing_humans_soften_enemy_bots_and_sharpen_allies():
    server = _server()
    server.players[1] = _human(1, TEAM1, kills=1, deaths=12)
    server.teams[TEAM2].score = 20
    enemy = _add_bot(server, 10, TEAM2)
    ally = _add_bot(server, 11, TEAM1)
    enemy_base, ally_base = enemy.profile, ally.profile
    balancer = BotSkillBalancer(server)
    targets = balancer.compute_targets()
    assert targets[TEAM2] < -0.5
    assert 0.0 < targets[TEAM1] <= -targets[TEAM2] * 0.5 + 1e-9
    for second in range(0, 120):
        balancer.update(float(second))
    assert enemy.profile.aim_noise > enemy_base.aim_noise
    assert enemy.profile.reaction_time > enemy_base.reaction_time
    assert enemy.profile.skill < enemy_base.skill
    assert ally.profile.aim_noise < ally_base.aim_noise
    assert ally.profile.skill > ally_base.skill


def test_stomping_humans_sharpen_enemy_bots():
    server = _server()
    server.players[1] = _human(1, TEAM1, kills=20, deaths=2)
    server.teams[TEAM1].score = 30
    enemy = _add_bot(server, 10, TEAM2)
    base = enemy.profile
    balancer = BotSkillBalancer(server)
    for second in range(0, 120):
        balancer.update(float(second))
    assert enemy.profile.aim_noise < base.aim_noise
    assert enemy.profile.skill > base.skill


def test_changes_are_smooth_and_bounded_by_rate():
    server = _server(skill_balance_rate=0.05)
    server.players[1] = _human(1, TEAM1, kills=0, deaths=20)
    _add_bot(server, 10, TEAM2)
    balancer = BotSkillBalancer(server)
    balancer.update(0.0)
    assert abs(balancer.factors[TEAM2]) <= 0.05 + 1e-9
    balancer.update(1.0)
    assert abs(balancer.factors[TEAM2]) <= 0.10 + 1e-9
    # A long stall is capped at five seconds of movement.
    balancer.update(100.0)
    assert abs(balancer.factors[TEAM2]) <= 0.35 + 1e-9


def test_profiles_stay_inside_the_configured_band():
    limits = band_limits(("normal",))
    band = _BANDS["normal"]
    profile = ProfileFactory(seed=3).create("normal")
    for strength in (-0.9, 0.9):
        scaled = scale_profile(profile, strength, limits)
        assert band.skill[0] <= scaled.skill <= band.skill[1]
        assert band.aim_noise[0] <= scaled.aim_noise <= band.aim_noise[1]
        assert band.reaction[0] <= scaled.reaction_time <= band.reaction[1]
        assert band.turn_speed[0] <= scaled.turn_speed <= band.turn_speed[1]
        assert abs(scaled.recoil_control - (0.30 + scaled.skill * 0.65)) < 1e-9


def test_normal_server_never_gets_hard_bots_even_when_stomped():
    server = _server(difficulty="normal", skill_balance_max_shift=0.9)
    server.players[1] = _human(1, TEAM1, kills=40, deaths=0)
    server.teams[TEAM1].score = 100
    enemy = _add_bot(server, 10, TEAM2, difficulty="normal")
    balancer = BotSkillBalancer(server)
    for second in range(0, 200):
        balancer.update(float(second))
    assert enemy.profile.skill <= _BANDS["normal"].skill[1] + 1e-9
    assert enemy.profile.aim_noise >= _BANDS["normal"].aim_noise[0] - 1e-9


def test_no_humans_or_disabled_returns_bots_to_their_base_profile():
    server = _server()
    server.players[1] = _human(1, TEAM1, kills=0, deaths=20)
    enemy = _add_bot(server, 10, TEAM2)
    base = enemy.profile
    balancer = BotSkillBalancer(server)
    for second in range(0, 60):
        balancer.update(float(second))
    assert enemy.profile != base
    server.config.bots.skill_balance = False
    for second in range(60, 200):
        balancer.update(float(second))
    assert balancer.factors[TEAM2] == 0.0
    assert enemy.profile == base


def test_bot_changing_team_picks_up_the_new_team_factor():
    server = _server()
    server.players[1] = _human(1, TEAM1, kills=0, deaths=20)
    bot = _add_bot(server, 10, TEAM1)
    base = bot.profile
    balancer = BotSkillBalancer(server)
    for second in range(0, 60):
        balancer.update(float(second))
    assert bot.profile.skill > base.skill  # ally of losing humans
    bot.player.team = TEAM2
    balancer.update(60.0)
    assert bot.profile.skill < base.skill  # now their opponent


def test_retired_bots_are_forgotten():
    server = _server()
    _add_bot(server, 10, TEAM1)
    balancer = BotSkillBalancer(server)
    balancer.update(0.0)
    assert balancer.base_profile(10, 1) is not None
    del server.bots._runtime[10]
    balancer.update(1.0)
    assert balancer.base_profile(10, 1) is None


def test_missing_director_is_harmless():
    server = _server()
    server.bots = None
    BotSkillBalancer(server).update(0.0)
