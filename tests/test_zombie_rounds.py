"""Zombie multi-round flow, population collapse, spectators and scoring."""

import asyncio
import time
from types import SimpleNamespace

import shared.constants as C  # noqa: E402
import shared.constants_gamemode as CG  # noqa: E402
from modes.zombie import (  # noqa: E402
    SURVIVOR_TEAM,
    ZOMBIE_TEAM,
    ZombieMode,
    ZombiePhase,
)
from server.builders.state_data import build_state_data  # noqa: E402
from server.game_constants import TEAM_SPECTATOR  # noqa: E402
from shared.bytes import ByteReader  # noqa: E402
from shared.packet import LocalisedMessage, SetScore  # noqa: E402
from tests import test_zombie as fx  # noqa: E402


def _ids(rows):
    return [
        LocalisedMessage(ByteReader(data[1:])).string_id
        for data in rows
        if data and data[0] == LocalisedMessage.id
    ]


def _scores(rows):
    return [
        SetScore(ByteReader(data[1:]))
        for data in rows
        if data and data[0] == SetScore.id
    ]


def _active_mode(player_count=3, *, rounds=3):
    server = fx._Server()
    server.config.mode_settings["zom"]["score_limit"] = rounds
    server.config.mode_settings["zom"]["round_intermission"] = 0.0
    for player_id in range(1, player_count + 1):
        fx._player(server, player_id)
    respawned = []
    server.respawned = respawned

    def respawn(player):
        player.alive = player.spawned = True
        respawned.append(player.id)

    server.respawn_player = respawn
    mode = ZombieMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_tick(1))
    assert mode.phase is ZombiePhase.ACTIVE
    return server, mode


def _record_mode_end(mode):
    ended = []

    async def end(winner=None):
        ended.append(winner)
        mode.ended = True

    mode.on_mode_end = end
    return ended


def _drain(mode):
    for tick in range(10):
        asyncio.run(mode.on_tick(100 + tick))
        if mode.phase is not ZombiePhase.RESETTING:
            return


# 1. Rounds per map -----------------------------------------------------------


def test_round_limit_comes_from_retail_rule():
    server = fx._Server()
    mode = ZombieMode(server)
    assert mode.score_limit == int(
        server.config.game_rules.get("RULE_ZOMBIE_NOOF_ROUNDS")
    )
    assert int(CG.ZOM_NOOF_ROUNDS_BEFORE_NEXT_MAP) == 3


def test_non_final_round_restarts_on_the_same_map():
    server, mode = _active_mode(3, rounds=3)
    ended = _record_mode_end(mode)

    asyncio.run(mode._finish_round(ZOMBIE_TEAM, "ZOMBIE_WIN"))

    assert ended == []
    assert mode.phase is ZombiePhase.INTERMISSION
    assert mode.rounds_played == 1
    assert server.teams[ZOMBIE_TEAM].score == 1
    assert _ids(server.packets)[-1] == "ZOMBIE_WIN"

    asyncio.run(mode._begin_next_round())
    assert mode.phase is ZombiePhase.RESETTING
    _drain(mode)

    assert all(p.team == SURVIVOR_TEAM for p in server.players.values())
    assert all(p.class_id != int(C.CLASS_ZOMBIE) for p in server.players.values())
    assert sorted(server.respawned) == [1, 2, 3]
    assert mode.patient_zero_ids == set()
    # infection_delay=0 in the fixture: the next outbreak is armed again.
    assert mode.phase is ZombiePhase.COUNTDOWN
    asyncio.run(mode.on_tick(200))
    assert mode.phase is ZombiePhase.ACTIVE
    assert mode.ended is False


def test_infected_player_gets_their_human_loadout_back_next_round():
    server, mode = _active_mode(3, rounds=3)
    _record_mode_end(mode)
    victim = next(p for p in server.players.values() if p.team == SURVIVOR_TEAM)
    victim.class_id = int(C.CLASS_MINER)
    victim.alive = victim.spawned = False
    asyncio.run(mode.on_player_death(victim, None, 0))
    assert victim.class_id == int(C.CLASS_ZOMBIE)

    asyncio.run(mode._finish_round(ZOMBIE_TEAM, "ZOMBIE_WIN"))
    asyncio.run(mode._begin_next_round())
    _drain(mode)

    assert victim.team == SURVIVOR_TEAM
    assert victim.class_id == int(C.CLASS_MINER)


def test_intermission_task_starts_the_next_round():
    server, mode = _active_mode(3, rounds=3)
    _record_mode_end(mode)

    async def run():
        await mode._finish_round(SURVIVOR_TEAM, "SURVIVOR_WIN")
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(run())
    assert mode.phase is ZombiePhase.RESETTING


def test_only_the_last_round_ends_the_match():
    server, mode = _active_mode(3, rounds=3)
    ended = _record_mode_end(mode)
    winners = [ZOMBIE_TEAM, SURVIVOR_TEAM, ZOMBIE_TEAM]
    for index, winner in enumerate(winners):
        message = "ZOMBIE_WIN" if winner == ZOMBIE_TEAM else "SURVIVOR_WIN"
        asyncio.run(mode._finish_round(winner, message))
        if index < len(winners) - 1:
            assert ended == []
            asyncio.run(mode._begin_next_round())
            _drain(mode)
            asyncio.run(mode.on_tick(300 + index))
            assert mode.phase is ZombiePhase.ACTIVE

    assert ended == [ZOMBIE_TEAM]
    assert mode.rounds_played == 3
    assert server.teams[ZOMBIE_TEAM].score == 2
    assert server.teams[SURVIVOR_TEAM].score == 1

    # A new match (the end sequence's restart) resets the round counter.
    mode.on_mode_end = ZombieMode.on_mode_end.__get__(mode)
    asyncio.run(mode.on_mode_start())
    assert mode.rounds_played == 0
    assert server.teams[ZOMBIE_TEAM].score == 0


def test_single_round_setting_ends_immediately():
    _server, mode = _active_mode(3, rounds=1)
    ended = _record_mode_end(mode)
    asyncio.run(mode._finish_round(SURVIVOR_TEAM, "SURVIVOR_WIN"))
    assert ended == [SURVIVOR_TEAM]


def test_finish_round_is_idempotent_during_intermission():
    server, mode = _active_mode(3, rounds=3)
    _record_mode_end(mode)
    asyncio.run(mode._finish_round(ZOMBIE_TEAM, "ZOMBIE_WIN"))
    asyncio.run(mode._finish_round(ZOMBIE_TEAM, "ZOMBIE_WIN"))
    assert mode.rounds_played == 1
    assert server.teams[ZOMBIE_TEAM].score == 1


def test_non_final_round_timeout_does_not_open_the_map_vote():
    server, mode = _active_mode(3, rounds=3)
    _record_mode_end(mode)
    votes = []
    server.vote_manager = SimpleNamespace(
        ensure_map_vote=lambda now: votes.append(now)
    )
    # Inside the stock 10 s map-vote window (TIME_AFTER_MAP_VOTE_START_BEFORE_END).
    mode.start_time = time.time() - mode.time_limit + 5.0

    asyncio.run(mode.on_tick(400))
    assert votes == []

    mode.rounds_played = mode.score_limit - 1
    asyncio.run(mode.on_tick(401))
    assert votes


def test_non_final_round_timeout_finishes_the_round():
    server, mode = _active_mode(3, rounds=3)
    ended = _record_mode_end(mode)
    mode.start_time = time.time() - mode.time_limit - 1.0

    asyncio.run(mode.on_tick(500))

    assert ended == []
    assert mode.phase is ZombiePhase.INTERMISSION
    assert "SURVIVOR_WIN" in _ids(server.packets)


# 2. Loadout edits never infect -----------------------------------------------


def test_survivor_loadout_edit_during_outbreak_does_not_infect():
    server, mode = _active_mode(3)
    survivor = next(p for p in server.players.values() if p.team == SURVIVOR_TEAM)
    for kill_type in (
        C.KILL.CLASS_CHANGE_KILL,
        C.KILL.TEAM_CHANGE_KILL,
        C.KILL.FORCED_TEAM_CHANGE_KILL,
    ):
        survivor.alive = survivor.spawned = False
        asyncio.run(mode.on_player_death(survivor, None, int(kill_type)))
        assert survivor.team == SURVIVOR_TEAM
        assert survivor.class_id != int(C.CLASS_ZOMBIE)
    assert mode.phase is ZombiePhase.ACTIVE


def test_survivor_between_lives_does_not_trigger_last_man_cue():
    server, mode = _active_mode(3)
    a, b = [p for p in server.players.values() if p.team == SURVIVOR_TEAM]
    server.packets.clear()
    a.alive = a.spawned = False  # loadout-edit respawn in flight
    asyncio.run(mode.on_tick(600))
    assert mode.last_survivor_id is None
    assert "LAST_MAN_STANDING" not in _ids(server.packets)


# 3. Population collapse ------------------------------------------------------


def test_patient_zero_leaving_a_two_player_round_aborts_to_waiting():
    server, mode = _active_mode(2)
    ended = _record_mode_end(mode)
    zombie = next(p for p in server.players.values() if p.team == ZOMBIE_TEAM)
    survivor = next(p for p in server.players.values() if p.team == SURVIVOR_TEAM)
    server.players.pop(zombie.id)
    server.teams[ZOMBIE_TEAM].remove_player(zombie)
    server.packets.clear()

    asyncio.run(mode.on_player_leave(zombie))

    assert ended == []
    assert mode.phase is ZombiePhase.WAITING
    assert mode.rounds_played == 0
    assert survivor.team == SURVIVOR_TEAM
    assert "ZOMBIE_WIN" not in _ids(server.packets)
    assert mode.patient_zero_ids == set()


def test_survivor_leaving_a_two_player_round_returns_zombie_to_survivors():
    server, mode = _active_mode(2)
    ended = _record_mode_end(mode)
    zombie = next(p for p in server.players.values() if p.team == ZOMBIE_TEAM)
    zombie.alive = zombie.spawned = True
    survivor = next(p for p in server.players.values() if p.team == SURVIVOR_TEAM)
    server.players.pop(survivor.id)
    server.teams[SURVIVOR_TEAM].remove_player(survivor)

    asyncio.run(mode.on_player_leave(survivor))

    assert ended == []
    assert mode.phase is ZombiePhase.WAITING
    assert zombie.team == SURVIVOR_TEAM
    assert zombie.class_id != int(C.CLASS_ZOMBIE)
    assert zombie.alive is False  # KillAction model swap, then normal respawn


def test_empty_server_does_not_hand_the_first_joiner_a_zombie_win():
    server, mode = _active_mode(2)
    ended = _record_mode_end(mode)
    for player in list(server.players.values()):
        server.players.pop(player.id)
        server.teams[int(player.team)].remove_player(player)
        asyncio.run(mode.on_player_leave(player))
    assert mode.phase is ZombiePhase.WAITING

    first = fx._player(server, 7)
    asyncio.run(mode.on_player_join(first))
    asyncio.run(mode.on_tick(700))
    assert ended == []
    assert mode.phase is ZombiePhase.WAITING
    assert first.team == SURVIVOR_TEAM

    second = fx._player(server, 8)
    asyncio.run(mode.on_player_join(second))
    asyncio.run(mode.on_tick(701))
    assert mode.phase is ZombiePhase.ACTIVE
    assert ended == []


def test_underpopulated_active_round_aborts_on_tick_too():
    server, mode = _active_mode(2)
    ended = _record_mode_end(mode)
    zombie = next(p for p in server.players.values() if p.team == ZOMBIE_TEAM)
    zombie.connection = None  # vanished without a leave hook

    asyncio.run(mode.on_tick(800))

    assert ended == []
    assert mode.phase is ZombiePhase.WAITING


def test_leave_after_match_end_does_nothing():
    server, mode = _active_mode(2)
    mode.ended = True
    zombie = next(p for p in server.players.values() if p.team == ZOMBIE_TEAM)
    survivor = next(p for p in server.players.values() if p.team == SURVIVOR_TEAM)
    asyncio.run(mode.on_player_leave(zombie))
    assert survivor.team == SURVIVOR_TEAM
    assert mode.phase is ZombiePhase.ACTIVE


# 4. StateData team names -----------------------------------------------------


def test_state_data_team_names_are_client_string_ids():
    server, _mode = fx._new_mode()
    state = build_state_data(server, player_id=3)
    assert state.team1_name == "ZOMBIE_TEAM"
    assert state.team2_name == "SURVIVOR_TEAM"


# 5. Spectators ---------------------------------------------------------------


def _spectator(server, player_id):
    player = fx._player(server, player_id)
    server.teams[SURVIVOR_TEAM].remove_player(player)
    player.team = TEAM_SPECTATOR
    player.alive = player.spawned = False
    return player


def test_spectators_do_not_count_toward_minimum_players():
    server = fx._Server()
    fx._player(server, 1)
    spectator = _spectator(server, 2)
    mode = ZombieMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_player_join(spectator))
    asyncio.run(mode.on_tick(1))

    assert spectator.team == TEAM_SPECTATOR
    assert mode.phase is ZombiePhase.WAITING


def test_spectator_may_join_the_role_a_fresh_join_would_get():
    server, mode = fx._new_mode()
    spectator = _spectator(server, 5)

    assert mode.allows_team_change(spectator, SURVIVOR_TEAM) is True
    assert mode.allows_team_change(spectator, ZOMBIE_TEAM) is False

    mode.phase = ZombiePhase.ACTIVE
    assert mode.allows_team_change(spectator, ZOMBIE_TEAM) is True
    assert mode.allows_team_change(spectator, SURVIVOR_TEAM) is False

    # Players already in the round still cannot escape their role.
    player = fx._player(server, 6)
    assert mode.allows_team_change(player, ZOMBIE_TEAM) is False
    assert mode.allows_team_change(player, TEAM_SPECTATOR) is False


def test_spectator_entering_during_outbreak_gets_the_zombie_kit():
    server, mode = _active_mode(3)
    spectator = _spectator(server, 9)
    spectator.class_id = int(C.CLASS_MINER)
    # The team handler moves the player before queueing the mode event.
    spectator.team = ZOMBIE_TEAM
    server.teams[ZOMBIE_TEAM].add_player(spectator)

    asyncio.run(mode.on_player_team_change(spectator, TEAM_SPECTATOR, ZOMBIE_TEAM))

    assert spectator.class_id == int(C.CLASS_ZOMBIE)
    assert spectator.pending_selection is None


def test_spectator_entering_survivors_arms_the_countdown():
    server = fx._Server()
    fx._player(server, 1)
    spectator = _spectator(server, 2)
    mode = ZombieMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    assert mode.phase is ZombiePhase.WAITING

    spectator.team = SURVIVOR_TEAM
    server.teams[SURVIVOR_TEAM].add_player(spectator)
    asyncio.run(mode.on_player_team_change(spectator, TEAM_SPECTATOR, SURVIVOR_TEAM))

    assert mode.phase is ZombiePhase.COUNTDOWN


# 6. Survival bonus -----------------------------------------------------------


def test_survivor_win_awards_the_retail_individual_bonus():
    server, mode = _active_mode(3)
    _record_mode_end(mode)
    survivors = [p for p in server.players.values() if p.team == SURVIVOR_TEAM]
    zombies = [p for p in server.players.values() if p.team == ZOMBIE_TEAM]
    for player in server.players.values():
        player.score = 0
    server.packets.clear()

    asyncio.run(mode._finish_round(SURVIVOR_TEAM, "SURVIVOR_WIN"))

    bonus = int(CG.ZOM_EXTRA_INDIVIDUAL_SCORE_FOR_SURVIVAL)
    assert bonus == 200
    assert all(p.score == bonus for p in survivors)
    assert all(p.score == 0 for p in zombies)
    reasons = {s.reason for s in _scores(server.packets)}
    assert int(C.ZOM_SURVIVE_SCORE_REASON) in reasons


def test_zombie_win_awards_no_survival_bonus():
    server, mode = _active_mode(3)
    _record_mode_end(mode)
    for player in server.players.values():
        player.score = 0
    asyncio.run(mode._finish_round(ZOMBIE_TEAM, "ZOMBIE_WIN"))
    assert all(p.score == 0 for p in server.players.values())


# 7. Departed players / scoring hygiene --------------------------------------


def test_departed_killer_is_not_scored():
    server, mode = _active_mode(3)
    zombie = next(p for p in server.players.values() if p.team == ZOMBIE_TEAM)
    victim = next(p for p in server.players.values() if p.team == SURVIVOR_TEAM)
    server.players.pop(zombie.id)
    zombie.score = 0
    victim.alive = victim.spawned = False

    asyncio.run(mode.on_player_death(victim, zombie, 0))

    assert zombie.score == 0
    assert victim.team == ZOMBIE_TEAM


def test_death_of_departed_survivor_is_ignored():
    server, mode = _active_mode(3)
    victim = next(p for p in server.players.values() if p.team == SURVIVOR_TEAM)
    server.players.pop(victim.id)
    asyncio.run(mode.on_player_death(victim, None, 0))
    assert victim.team == SURVIVOR_TEAM


def test_zombie_kill_does_not_add_generic_kill_score():
    server, mode = _active_mode(3)
    zombie = next(p for p in server.players.values() if p.team == ZOMBIE_TEAM)
    survivor = next(p for p in server.players.values() if p.team == SURVIVOR_TEAM)
    survivor.score = 0
    asyncio.run(mode.on_player_kill(survivor, zombie, int(C.KILL.WEAPON_KILL)))
    # Not the last man: no ZOM award, and no generic per-kill score either.
    assert survivor.score == 0
    assert "on_player_kill" in ZombieMode.__dict__


def test_outbreak_retry_does_not_reannounce_every_tick():
    server = fx._Server()
    a = fx._player(server, 1)
    fx._player(server, 2)
    mode = ZombieMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    a.alive = a.spawned = False
    server.packets.clear()

    for tick in range(5):
        asyncio.run(mode.on_tick(tick))

    assert mode.phase is ZombiePhase.COUNTDOWN
    assert _ids(server.packets).count("ZOMBIE_VIRUS_RELEASED") == 0


def test_pre_outbreak_joiner_drained_after_outbreak_stays_human():
    server, mode = _active_mode(3)
    late = fx._player(server, 9)  # joined as a survivor before the outbreak
    asyncio.run(mode.on_player_join(late))
    assert late.team == SURVIVOR_TEAM
    assert late.alive is True


def test_zombie_joiner_drained_after_round_abort_gets_a_model_swap():
    server, mode = fx._new_mode(player_count=0)
    joiner = fx._player(server, 4, team=ZOMBIE_TEAM, class_id=C.CLASS_ZOMBIE)
    asyncio.run(mode.on_player_join(joiner))
    assert joiner.team == SURVIVOR_TEAM
    assert joiner.alive is False
    assert joiner.class_id != int(C.CLASS_ZOMBIE)


def test_bot_survivor_selection_is_a_human_kit():
    from server.class_selection import normalize_class_selection

    _server, mode = fx._new_mode()
    forged = normalize_class_selection(int(C.CLASS_ZOMBIE))
    selection = mode.prepare_bot_selection(SURVIVOR_TEAM, forged, player_id=3)
    assert selection.class_id == int(C.CLASS_SOLDIER)
    legal = normalize_class_selection(int(C.CLASS_MINER))
    assert mode.prepare_bot_selection(SURVIVOR_TEAM, legal, player_id=3) is legal
