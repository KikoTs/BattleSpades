"""Damage-based assist accounting across concrete player lives and rounds."""
from types import SimpleNamespace

import shared.constants as C
from server import combat_scores, scoreboard
from shared.bytes import ByteReader
from shared.packet import SetScore


def _world():
    server = SimpleNamespace(players={}, sent=[], mode=SimpleNamespace(ended=False),
                             config=SimpleNamespace(default_mode="tdm"))
    server.broadcast = lambda data: server.sent.append(data)
    for player_id, team in ((0, 2), (1, 2), (2, 3)):
        server.players[player_id] = SimpleNamespace(
            id=player_id, team=team, score=0, replication_generation=1,
            kill_streak=1, connection=SimpleNamespace(server=server),
        )
    return server, server.players[0], server.players[1], server.players[2]


def test_assist_threshold_is_retail_half_health_and_fifty_points():
    server, assistant, killer, victim = _world()
    combat_scores.record_damage(server, victim, assistant, 25, now=1)
    combat_scores.record_damage(server, victim, assistant, 25, now=2)
    combat_scores.record_damage(server, victim, killer, 50, now=3)
    combat_scores.record_death(server, victim, killer, C.WEAPON_KILL, now=3)
    assert assistant.score == 50 and killer.score == 0
    assert combat_scores.round_stats(assistant).awards[C.MOST_ASSISTS] == 1
    packet = SetScore(ByteReader(server.sent[0][1:]))
    assert (packet.type, packet.specifier, packet.reason, packet.value) == (1, 0, 5, 50)
    assert assistant.profile_stats.values[C.KILL_SCORE_ASSIST_REASON] == [1, 50]
    # The accepted death handler consumes history before sending. A repeated
    # callback cannot pay the same damage twice (Player.die also guards alive).
    combat_scores.record_death(server, victim, killer, C.WEAPON_KILL, now=3)
    assert assistant.score == 50


def test_assist_below_threshold_and_expired_damage_do_not_score():
    for damage, death_time in ((49, 2), (50, 12)):
        server, assistant, killer, victim = _world()
        combat_scores.record_damage(server, victim, assistant, damage, now=1)
        combat_scores.record_death(server, victim, killer, C.WEAPON_KILL, now=death_time)
        assert assistant.score == 0 and not server.sent


def test_expired_contribution_is_not_refreshed_by_a_small_new_hit():
    server, assistant, killer, victim = _world()
    combat_scores.record_damage(server, victim, assistant, 50, now=1)
    combat_scores.record_damage(server, victim, assistant, 1, now=12)
    combat_scores.record_death(server, victim, killer, C.WEAPON_KILL, now=12)
    assert assistant.score == 0


def test_healing_removes_old_damage_credit():
    server, assistant, killer, victim = _world()
    combat_scores.record_damage(server, victim, assistant, 60, now=1)
    combat_scores.record_healing(victim, 11)
    combat_scores.record_death(server, victim, killer, C.WEAPON_KILL, now=2)
    assert assistant.score == 0


def test_self_team_and_transition_deaths_clear_assists_without_awards():
    for cause in ("self", "environment", "friendly", "transition"):
        server, assistant, killer, victim = _world()
        combat_scores.record_damage(server, victim, assistant, 50, now=1)
        if cause == "self":
            killer = victim
        elif cause == "environment":
            killer = None
        elif cause == "friendly":
            killer.team = victim.team
        kind = C.CLASS_CHANGE_KILL if cause == "transition" else C.WEAPON_KILL
        combat_scores.record_death(server, victim, killer, kind, now=2)
        assert assistant.score == 0 and victim.damage_contributions == {}


def test_disconnected_reused_identity_changed_team_and_respawn_are_rejected():
    for mutation in ("disconnect", "reuse", "team", "respawn"):
        server, assistant, killer, victim = _world()
        combat_scores.record_damage(server, victim, assistant, 50, now=1)
        if mutation == "disconnect":
            del server.players[assistant.id]
        elif mutation == "reuse":
            server.players[assistant.id] = SimpleNamespace(id=assistant.id)
        elif mutation == "team":
            assistant.team = 3
        else:
            assistant.replication_generation += 1
        combat_scores.record_death(server, victim, killer, C.WEAPON_KILL, now=2)
        assert assistant.score == 0


def test_round_reset_clears_damage_and_resets_profile_score_baseline_only():
    server, assistant, killer, victim = _world()
    combat_scores.record_damage(server, victim, assistant, 50, now=1)
    assistant.score = 50
    scoreboard.send_player_score(server, assistant, reason=C.KILL_SCORE_ASSIST_REASON)
    reset = []
    server.revival_master = SimpleNamespace(reset_scoreboard_baselines=lambda: reset.append(True))
    scoreboard.reset_round_scores(server)
    assert assistant.score == 0 and victim.damage_contributions == {}
    assert assistant.profile_stats.last_score == 0
    assert assistant.profile_stats.values[C.KILL_SCORE_ASSIST_REASON] == [1, 50]
    assert reset == [True]
    combat_scores.record_death(server, victim, killer, C.WEAPON_KILL, now=2)
    assert assistant.score == 0


def test_post_round_and_editor_damage_do_not_create_rewards():
    for ended, mode in ((True, "tdm"), (False, "ugc"), (False, "tutorial")):
        server, assistant, killer, victim = _world()
        server.mode.ended, server.config.default_mode = ended, mode
        combat_scores.record_damage(server, victim, assistant, 90, now=1)
        combat_scores.record_death(server, victim, killer, C.WEAPON_KILL, now=2)
        assert assistant.score == 0 and not server.sent
