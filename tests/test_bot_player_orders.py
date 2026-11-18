"""Chat orders (/follow /hold /attack /defend /protect /escort /status) steer bots."""

from __future__ import annotations

import time
from pathlib import Path
from dataclasses import replace

from server.bot_ai.messages import ObjectiveSnapshot, PerceptionFrame, PlayerSnapshot
from server.bot_ai.player_orders import (
    ATTACK, DEFEND, ESCORT, FOLLOW, HOLD, JOB_ESCORT, JOB_NONE, JOB_PICKUP, JOB_PUSH, PICKUP_CODES,
    P_JOB, PROTECT, PlayerOrder,
    PlayerOrderBook, attack_escort_count, describe_role, protect_escort_count,
)
from server.bot_ai.policies import ModePolicyMemory

BLUE, GREEN = 2, 3
HUMAN, BOT, OTHER = 1, 7, 8
BASE = (50.0, 50.0, 40.0)
ENEMY_BASE = (400.0, 400.0, 40.0)


def _player(pid, team, pos, *, carried=-1, is_bot=True, alive=True):
    return PlayerSnapshot(
        player_id=pid, generation=1, team=team, class_id=0, alive=alive, spawned=alive,
        position=pos, eye=pos, orientation=(1.0, 0.0, 0.0), velocity=(0.0, 0.0, 0.0),
        health=100, tool=6, blocks=50, ammo_clip=10, ammo_reserve=30, is_bot=is_bot,
        loadout=(2, 5, 6), carried_entity_id=carried, life_id=1,
    )


def _ctf(extra=(), *, enemy_intel_carrier=-1, own_intel_carrier=-1, own_intel_at=(52.0, 52.0, 40.0),
         players=(), bot_at=(100.0, 100.0, 40.0), bot_id=BOT,
         enemy_intel_at=(402.0, 402.0, 40.0), enemy_intel_state=0):
    bot = _player(bot_id, BLUE, bot_at)
    objectives = [
        ObjectiveSnapshot("ctf_base", BLUE, BASE),
        ObjectiveSnapshot("ctf_base", GREEN, ENEMY_BASE),
        ObjectiveSnapshot("ctf_intel", BLUE, own_intel_at, carrier_id=own_intel_carrier),
        ObjectiveSnapshot("ctf_intel", GREEN, enemy_intel_at, carrier_id=enemy_intel_carrier,
                          state=enemy_intel_state),
        *extra,
    ]
    return bot, PerceptionFrame(
        frame_id=1, map_epoch=1, mode_epoch=1, topology_version=0, observer_id=bot_id,
        observer_generation=1, created_at=time.monotonic(), mode_id="ctf",
        players=(bot, *players), objectives=tuple(objectives), mode_phase="active",
    )


def _order(code, issuer=HUMAN, anchor=(120.0, 130.0, 40.0), bot_id=BOT, params=()):
    return ObjectiveSnapshot("player_order", BLUE, anchor, carrier_id=bot_id, state=code,
                             attacker=issuer, params=params)


def _decide(order_code, params=(), **kw):
    human = _player(HUMAN, BLUE, (200.0, 210.0, 40.0), is_bot=False)
    players = (human,) + tuple(kw.pop("players", ()))
    bot, frame = _ctf((_order(order_code, params=params),), players=players, **kw)
    return ModePolicyMemory().decide(frame, bot)


def _split_roles(code, ids, count_fn, *, carrier=True, thief=False, exclude=()):
    """Every ordered bot's decided role, after the book split them into jobs.

    A friendly carrier (id 30, a human) carries the enemy intel; ``thief``
    puts our intel in an enemy's hands (id 31) as well.
    """
    book = PlayerOrderBook()
    for bot_id in ids:
        book.issue(bot_id, PlayerOrder(code, BLUE, HUMAN, (120.0, 130.0, 40.0)))
    book.sync_jobs(BLUE, code, carrier and (thief or code == ATTACK), count_fn, exclude)
    objectives = tuple(book.objectives(ids, (6.0, 40.0, 40.0)))
    carrier_player = _player(30, BLUE, (300.0, 300.0, 40.0), carried=3, is_bot=False)
    thief_player = _player(31, GREEN, (200.0, 60.0, 40.0))
    roles = {}
    for bot_id in ids:
        bot, frame = _ctf(
            objectives, bot_id=bot_id,
            enemy_intel_carrier=30 if carrier else -1,
            own_intel_carrier=31 if thief else -1,
            own_intel_at=thief_player.position if thief else (52.0, 52.0, 40.0),
            players=(carrier_player, thief_player),
        )
        roles[bot_id] = ModePolicyMemory().decide(frame, bot).role
    return roles, book


# ------------------------------------------------------------------ follow / hold

def test_follow_goes_to_the_issuer():
    d = _decide(FOLLOW)
    assert d.role == "order_follow_escort" and d.position == (200.0, 210.0, 40.0)


def test_follow_without_a_live_issuer_holds_the_ordered_spot():
    bot, frame = _ctf((_order(FOLLOW),))
    d = ModePolicyMemory().decide(frame, bot)
    assert d.role == "order_hold" and d.position == (120.0, 130.0, 40.0)


def test_hold_stays_at_the_ordered_spot():
    d = _decide(HOLD)
    assert d.role == "order_hold" and d.position == (120.0, 130.0, 40.0) and not d.sprint


def test_hold_carries_the_configured_combat_leash():
    assert _decide(HOLD).leash_radius == 6.0                       # shipped default
    assert _decide(HOLD, params=(9.5, 40.0)).leash_radius == 9.5
    assert _decide(HOLD, params=(0.0, 40.0)).leash_radius == 0.0  # stand still


def test_only_a_hold_bot_is_leashed():
    for code in (FOLLOW, ATTACK, DEFEND, PROTECT, ESCORT):
        assert _decide(code).leash_radius == 0.0


# ------------------------------------------------------------------------- attack

def test_attack_pushes_enemy_intel():
    d = _decide(ATTACK)
    assert d.role == "order_attack" and d.position == (402.0, 402.0, 40.0)


def test_attack_escort_job_follows_the_friendly_carrier():
    carrier = _player(OTHER, BLUE, (300.0, 300.0, 40.0), carried=3)
    d = _decide(ATTACK, params=(6.0, 40.0, 40.0, JOB_ESCORT), enemy_intel_carrier=OTHER, players=(carrier,))
    assert d.role == "order_attack_escort" and d.position == (300.0, 300.0, 40.0)
    assert d.posture.name == "ESCORT"


def _enemy(pid, pos, alive=True):
    return _player(pid, GREEN, pos, alive=alive)


CARRIER_AT = (300.0, 300.0, 40.0)


def _intercept(*enemies, params=(6.0, 40.0, 40.0, JOB_PUSH), bot_id=BOT):
    carrier = _player(OTHER, BLUE, CARRIER_AT, carried=3)
    bot, frame = _ctf((_order(ATTACK, params=params, bot_id=bot_id),), enemy_intel_carrier=OTHER,
                      bot_id=bot_id, players=(carrier, *enemies))
    return ModePolicyMemory().decide(frame, bot)


def test_attackers_intercept_the_enemy_nearest_the_carrier_instead_of_camping_their_base():
    near, farther = _enemy(40, (310.0, 300.0, 40.0)), _enemy(41, (330.0, 300.0, 40.0))
    d = _intercept(farther, near, bot_id=12)                                    # an even id takes the nearest
    assert d.role == "order_attack_intercept" and d.position == near.position   # nearest to the CARRIER, not the bot
    assert d.position != ENEMY_BASE and d.posture.name == "ASSAULT"


def test_attackers_only_hunt_enemies_within_the_radius_of_the_carrier():
    outside = _enemy(40, (400.0, 300.0, 40.0))                                  # 100 blocks from the carrier
    d = _intercept(outside)
    assert d.role == "order_attack_intercept_screen" and d.position == CARRIER_AT
    assert d.position != outside.position and d.position != ENEMY_BASE         # no hunting, no base camping
    assert _intercept(outside, params=(6.0, 40.0, 120.0, JOB_PUSH)).role == "order_attack_intercept"
    inside = _enemy(41, (320.0, 300.0, 40.0))
    assert _intercept(inside, params=(6.0, 40.0, 0.0, JOB_PUSH)).role == "order_attack_intercept_screen"


def test_attackers_screen_the_carrier_when_no_enemy_is_near():
    d = _intercept()
    assert d.role == "order_attack_intercept_screen" and d.position == CARRIER_AT
    assert d.arrival_radius == 10.0 and d.posture.name == "ASSAULT" and d.sprint      # far away: sprint to him
    close = _player(BOT, BLUE, (305.0, 300.0, 40.0))
    carrier = _player(OTHER, BLUE, CARRIER_AT, carried=3)
    _, frame = _ctf((_order(ATTACK, params=(6.0, 40.0, 40.0, JOB_PUSH)),), enemy_intel_carrier=OTHER, players=(carrier,))
    assert not ModePolicyMemory().decide(replace(frame, players=(close, carrier)), close).sprint


def test_attackers_ignore_dead_enemies_and_pick_up_a_live_one():
    dead = _enemy(40, (305.0, 300.0, 40.0), alive=False)
    live = _enemy(41, (330.0, 300.0, 40.0))
    d = _intercept(dead, live)
    assert d.role == "order_attack_intercept" and d.position == live.position


def test_interceptors_spread_over_the_two_nearest_enemies_by_bot_id():
    a, b, c = _enemy(40, (305.0, 300.0, 40.0)), _enemy(41, (315.0, 300.0, 40.0)), _enemy(42, (330.0, 300.0, 40.0))
    targets = {bot_id: _intercept(c, b, a, bot_id=bot_id).position for bot_id in (11, 12, 13, 14)}
    assert targets[11] == targets[13] == b.position and targets[12] == targets[14] == a.position   # never the third
    assert _intercept(a, bot_id=11).position == _intercept(a, bot_id=12).position == a.position    # one enemy: all on him


def test_every_non_escort_attacker_intercepts_whatever_its_job():
    near = _enemy(40, (310.0, 300.0, 40.0))
    for params in ((6.0, 40.0, 40.0, JOB_PUSH), (6.0, 40.0, 40.0), ()):         # push job, no job, no params
        d = _intercept(near, params=params)
        assert d.role == "order_attack_intercept" and d.position == near.position, params


def test_an_escort_job_still_escorts_even_with_enemies_near():
    d = _intercept(_enemy(40, (310.0, 300.0, 40.0)), params=(6.0, 40.0, 40.0, JOB_ESCORT))
    assert d.role == "order_attack_escort" and d.position == CARRIER_AT


def test_interceptors_find_enemies_near_a_carrier_outside_the_perception_cohort():
    far = (350.0, 120.0, 40.0)
    near = _enemy(40, (355.0, 125.0, 40.0))
    bot, frame = _ctf((_order(ATTACK, params=(6.0, 40.0, 40.0, JOB_PUSH)),))
    objectives = tuple(ObjectiveSnapshot("ctf_intel", GREEN, far, carrier_id=OTHER)
                       if o.kind == "ctf_intel" and o.team == GREEN else o for o in frame.objectives)
    d = ModePolicyMemory().decide(replace(frame, objectives=objectives, players=(bot, near)), bot)
    assert d.role == "order_attack_intercept" and d.position == near.position


def test_interceptors_push_the_objective_again_when_the_intel_resets():
    near = _enemy(40, (310.0, 300.0, 40.0))
    carrier = _player(OTHER, BLUE, CARRIER_AT, carried=3)
    carrying = _decide(ATTACK, params=(6.0, 40.0, 40.0, JOB_PUSH), enemy_intel_carrier=OTHER, players=(carrier, near))
    reset = _decide(ATTACK, params=(6.0, 40.0, 40.0, JOB_PUSH), enemy_intel_carrier=-1, players=(carrier, near))
    assert carrying.role == "order_attack_intercept"
    assert reset.role == "order_attack" and reset.position == (402.0, 402.0, 40.0)


def test_intercept_roles_track_the_live_target_and_read_clearly_in_status():
    from server.bot_ai.policies import _tracks_live_position
    assert _tracks_live_position("order_attack_intercept") and _tracks_live_position("order_attack_intercept_screen")
    assert describe_role("order_attack_intercept") == "intercepting an enemy near the carrier"
    assert describe_role("order_attack_intercept_screen") == "screening the carrier"


def test_attack_escort_finds_a_carrier_outside_the_perception_cohort():
    far = (350.0, 120.0, 40.0)
    bot, frame = _ctf((_order(ATTACK, params=(6.0, 40.0, 40.0, JOB_ESCORT)),))
    objectives = tuple(ObjectiveSnapshot("ctf_intel", GREEN, far, carrier_id=OTHER)
                       if o.kind == "ctf_intel" and o.team == GREEN else o for o in frame.objectives)
    d = ModePolicyMemory().decide(replace(frame, objectives=objectives), bot)
    assert d.role == "order_attack_escort" and d.position == far


def test_when_the_carrier_is_gone_every_attack_bot_attacks_the_intel_again():
    carrier = _player(OTHER, BLUE, (300.0, 300.0, 40.0), carried=3)
    for job in (JOB_ESCORT, JOB_PUSH):                            # a stale job changes nothing
        reset = _decide(ATTACK, params=(6.0, 40.0, 40.0, job), enemy_intel_carrier=-1, players=(carrier,))
        assert reset.role == "order_attack" and reset.position == (402.0, 402.0, 40.0)


def test_attack_with_the_intel_in_its_own_hands_still_goes_home():
    bot = _player(BOT, BLUE, (100.0, 100.0, 40.0), carried=5)
    _, frame = _ctf((_order(ATTACK, params=(6.0, 40.0, 40.0, JOB_ESCORT)),), enemy_intel_carrier=BOT)
    d = ModePolicyMemory().decide(replace(frame, players=(bot,)), bot)
    assert d.role == "order_capture" and d.position == BASE


def test_attack_bots_split_by_id_between_escort_and_attack():
    roles, _ = _split_roles(ATTACK, [11, 12, 13, 14], lambda n: attack_escort_count(n, 0.5))
    assert roles == {11: "order_attack_escort", 12: "order_attack_escort",
                     13: "order_attack_intercept_screen", 14: "order_attack_intercept_screen"}


def test_a_single_attack_bot_attacks_and_the_ratio_always_leaves_an_attacker():
    assert _split_roles(ATTACK, [11], lambda n: attack_escort_count(n, 0.5))[0] == {11: "order_attack_intercept_screen"}
    assert _split_roles(ATTACK, [11], lambda n: attack_escort_count(n, 1.0))[0] == {11: "order_attack_intercept_screen"}
    escorting = lambda ratio, ids=(11, 12, 13, 14): sorted(
        i for i, r in _split_roles(ATTACK, list(ids), lambda n: attack_escort_count(n, ratio))[0].items()
        if r.endswith("escort"))
    assert escorting(0.0) == []
    assert escorting(0.25) == [11]
    assert escorting(0.75) == [11, 12, 13]
    assert escorting(1.0) == [11, 12, 13]                         # the last bot still attacks
    assert len(escorting(0.5, (11, 12, 13))) == 1                 # three bots: one escorts


def test_the_carrier_bot_itself_takes_no_job_and_does_not_shift_the_others():
    roles, book = _split_roles(ATTACK, [11, 12, 13, 14], lambda n: attack_escort_count(n, 0.5), exclude=(11,))
    assert book.job_of(11) == JOB_NONE
    assert [book.job_of(i) for i in (12, 13, 14)] == [JOB_ESCORT, JOB_PUSH, JOB_PUSH]


def test_attack_jobs_are_sticky_by_id_while_the_carrier_lives():
    count = lambda n: attack_escort_count(n, 0.5)
    book = PlayerOrderBook()
    for bot_id in (11, 12, 13, 14):
        book.issue(bot_id, PlayerOrder(ATTACK, BLUE, HUMAN, (0.0, 0.0, 0.0)))
    book.sync_jobs(BLUE, ATTACK, True, count)
    before = {i: book.job_of(i) for i in (11, 12, 13, 14)}
    assert before == {11: JOB_ESCORT, 12: JOB_ESCORT, 13: JOB_PUSH, 14: JOB_PUSH}
    # A bot dying changes nothing: it stays ordered, so a resync gives the same table.
    book.sync_jobs(BLUE, ATTACK, True, count)
    assert {i: book.job_of(i) for i in before} == before
    # Freeing one bot and ordering a newcomer with the lowest id never reshuffles the rest.
    book.clear(12)
    book.issue(5, PlayerOrder(ATTACK, BLUE, HUMAN, (0.0, 0.0, 0.0)))
    book.sync_jobs(BLUE, ATTACK, True, count)
    assert (book.job_of(11), book.job_of(13), book.job_of(14)) == (JOB_ESCORT, JOB_PUSH, JOB_PUSH)
    assert book.job_of(5) == JOB_ESCORT                           # fills the escort slot freed by 12
    assert book.job_of(12) == JOB_NONE
    # The carrier drops: the table goes and everyone attacks.
    book.sync_jobs(BLUE, ATTACK, False, count)
    assert {i: book.job_of(i) for i in (5, 11, 13, 14)} == {5: 0, 11: 0, 13: 0, 14: 0}
    # A new carrier: a fresh, deterministic split.
    book.sync_jobs(BLUE, ATTACK, True, count)
    assert [book.job_of(i) for i in (5, 11, 13, 14)] == [JOB_ESCORT, JOB_ESCORT, JOB_PUSH, JOB_PUSH]


def test_jobs_belong_to_one_team_and_one_order_kind():
    count = lambda n: attack_escort_count(n, 0.5)
    book = PlayerOrderBook()
    book.issue(1, PlayerOrder(ATTACK, BLUE, HUMAN, (0.0, 0.0, 0.0)))
    book.issue(2, PlayerOrder(ATTACK, BLUE, HUMAN, (0.0, 0.0, 0.0)))
    book.issue(3, PlayerOrder(HOLD, BLUE, HUMAN, (0.0, 0.0, 0.0)))
    book.issue(4, PlayerOrder(ATTACK, GREEN, HUMAN, (0.0, 0.0, 0.0)))
    book.sync_jobs(BLUE, ATTACK, True, count)
    assert (book.job_of(1), book.job_of(2)) == (JOB_ESCORT, JOB_PUSH)
    assert book.job_of(3) == JOB_NONE and book.job_of(4) == JOB_NONE


# ------------------------------------------------------------------------- defend

def test_defend_is_tied_to_the_base_not_the_intel():
    d = _decide(DEFEND)
    assert d.role == "order_defend" and d.position == BASE


def test_defend_chases_a_thief_near_the_base():
    thief = _player(OTHER, GREEN, (80.0, 50.0, 40.0))   # 30 blocks from base
    d = _decide(DEFEND, own_intel_carrier=OTHER, own_intel_at=(80.0, 50.0, 40.0), players=(thief,))
    assert d.role == "order_defend_hunt" and d.position == (80.0, 50.0, 40.0)


def test_defend_returns_to_base_when_the_thief_is_beyond_the_radius():
    thief = _player(OTHER, GREEN, (200.0, 50.0, 40.0))  # 150 blocks away
    d = _decide(DEFEND, own_intel_carrier=OTHER, own_intel_at=(200.0, 50.0, 40.0), players=(thief,))
    assert d.role == "order_defend" and d.position == BASE


def test_defend_chase_radius_is_configurable():
    at = (110.0, 50.0, 40.0)                            # 60 blocks from base
    thief = _player(OTHER, GREEN, at)
    kw = dict(own_intel_carrier=OTHER, own_intel_at=at, players=(thief,))
    assert _decide(DEFEND, **kw).role == "order_defend"
    assert _decide(DEFEND, params=(6.0, 80.0), **kw).role == "order_defend_hunt"
    near = _player(OTHER, GREEN, (60.0, 50.0, 40.0))
    kw = dict(own_intel_carrier=OTHER, own_intel_at=(60.0, 50.0, 40.0), players=(near,))
    assert _decide(DEFEND, params=(6.0, 0.0), **kw).role == "order_defend"  # 0 = never chase


def test_defend_chase_has_hysteresis_at_the_radius_edge():
    at = (95.0, 50.0, 40.0)                             # 45 blocks: past 40, inside 50
    thief = _player(OTHER, GREEN, at)
    kw = dict(own_intel_carrier=OTHER, own_intel_at=at, players=(thief,))
    at_base = _decide(DEFEND, bot_at=(52.0, 52.0, 40.0), **kw)      # guarding: strict radius
    chasing = _decide(DEFEND, bot_at=(120.0, 50.0, 40.0), **kw)     # already out: keeps going
    assert at_base.role == "order_defend" and chasing.role == "order_defend_hunt"


# ------------------------------------------------------------------------ protect

def test_protect_without_a_carrier_or_thief_guards_own_intel_at_base():
    d = _decide(PROTECT)
    assert d.role == "order_protect_guard" and d.position == (52.0, 52.0, 40.0)


def test_protect_escorts_a_friendly_intel_carrier_when_our_intel_is_safe():
    carrier = _player(OTHER, BLUE, (300.0, 300.0, 40.0), carried=3)
    d = _decide(PROTECT, enemy_intel_carrier=OTHER, players=(carrier,))
    assert d.role == "order_protect_escort" and d.position == (300.0, 300.0, 40.0)


def test_protect_hunts_the_thief_at_any_distance_when_nobody_carries_for_us():
    far = (480.0, 480.0, 40.0)                          # across the map, outside any leash
    thief = _player(OTHER, GREEN, far)
    d = _decide(PROTECT, own_intel_carrier=OTHER, own_intel_at=far, players=(thief,))
    assert d.role == "order_protect_hunt" and d.position == far


def test_protect_locates_a_thief_who_is_not_in_the_perception_cohort():
    far = (480.0, 480.0, 40.0)
    d = _decide(PROTECT, own_intel_carrier=OTHER, own_intel_at=far)   # no player snapshot
    assert d.role == "order_protect_hunt" and d.position == far


def _protect_roles(ids, hunt_ratio, **kw):
    return _split_roles(PROTECT, ids, lambda n: protect_escort_count(n, hunt_ratio), thief=True, **kw)[0]


def test_protect_splits_by_bot_id_between_escort_and_hunt():
    roles = _protect_roles([11, 12, 13, 14], 0.5)
    assert [roles[i] for i in (11, 12, 13, 14)] == [
        "order_protect_escort", "order_protect_escort", "order_protect_hunt", "order_protect_hunt"]


def test_protect_split_is_independent_of_the_order_bots_were_ordered_in():
    assert _protect_roles([14, 11, 13, 12], 0.5) == _protect_roles([11, 12, 13, 14], 0.5)


def test_a_single_protect_bot_escorts():
    assert _protect_roles([11], 0.5) == {11: "order_protect_escort"}
    assert _protect_roles([11], 1.0) == {11: "order_protect_escort"}


def test_protect_hunt_ratio_is_configurable_and_always_keeps_an_escort():
    ids = [11, 12, 13, 14]
    hunting = lambda ratio: sorted(i for i, r in _protect_roles(ids, ratio).items() if r.endswith("hunt"))
    assert hunting(0.0) == []                    # nobody hunts
    assert hunting(0.25) == [14]
    assert hunting(0.75) == [12, 13, 14]
    assert hunting(1.0) == [12, 13, 14]          # the first bot still escorts
    assert len(hunting(0.5)) == 2
    odd = {i for i, r in _protect_roles([11, 12, 13], 0.5).items() if r.endswith("hunt")}
    assert odd == {13}                           # three bots: the extra one escorts


def test_protect_with_no_job_runs_the_plain_version_and_escorts():
    carrier = _player(30, BLUE, (300.0, 300.0, 40.0), carried=3, is_bot=False)
    thief = _player(31, GREEN, (200.0, 60.0, 40.0))
    d = _decide(PROTECT, enemy_intel_carrier=30, own_intel_carrier=31,
                own_intel_at=thief.position, players=(carrier, thief))
    assert d.role == "order_protect_escort"


def test_protect_jobs_are_sticky_and_end_with_the_thief_or_carrier():
    count = lambda n: protect_escort_count(n, 0.5)
    book = PlayerOrderBook()
    for bot_id in (11, 12, 13, 14):
        book.issue(bot_id, PlayerOrder(PROTECT, BLUE, HUMAN, (0.0, 0.0, 0.0)))
    book.sync_jobs(BLUE, PROTECT, True, count)
    jobs = {i: book.job_of(i) for i in (11, 12, 13, 14)}
    assert jobs == {11: JOB_ESCORT, 12: JOB_ESCORT, 13: JOB_PUSH, 14: JOB_PUSH}
    book.clear(11)                                            # someone is freed mid-fight
    book.sync_jobs(BLUE, PROTECT, True, count)
    assert {i: book.job_of(i) for i in (12, 13, 14)} == {12: JOB_ESCORT, 13: JOB_PUSH, 14: JOB_PUSH}
    book.sync_jobs(BLUE, PROTECT, False, count)               # the thief is caught
    assert {book.job_of(i) for i in (12, 13, 14)} == {JOB_NONE}


# ------------------------------------------------------------------------- escort

def test_escort_follows_the_friendly_carrier():
    carrier = _player(OTHER, BLUE, (300.0, 300.0, 40.0), carried=3)
    d = _decide(ESCORT, enemy_intel_carrier=OTHER, players=(carrier,))
    assert d.role == "order_escort_carrier" and d.position == (300.0, 300.0, 40.0)


def test_escort_falls_back_to_the_issuer():
    d = _decide(ESCORT)
    assert d.role == "order_escort_issuer" and d.position == (200.0, 210.0, 40.0)


def test_escort_with_nobody_to_escort_holds_the_ordered_spot():
    bot, frame = _ctf((_order(ESCORT),))
    d = ModePolicyMemory().decide(frame, bot)
    assert d.role == "order_escort_hold" and d.position == (120.0, 130.0, 40.0)


def test_escort_never_splits_even_when_our_intel_is_stolen():
    carrier = _player(30, BLUE, (300.0, 300.0, 40.0), carried=3, is_bot=False)
    thief = _player(31, GREEN, (200.0, 60.0, 40.0))
    ids = [11, 12, 13, 14]
    orders = tuple(_order(ESCORT, bot_id=i) for i in ids)
    roles = set()
    for bot_id in ids:
        bot, frame = _ctf(orders, enemy_intel_carrier=30, own_intel_carrier=31,
                          own_intel_at=thief.position, bot_id=bot_id, players=(carrier, thief))
        roles.add(ModePolicyMemory().decide(frame, bot).role)
    assert roles == {"order_escort_carrier"}


# ----------------------------------------------------------- overrides and misc

def test_carrying_the_flag_overrides_any_order():
    human = _player(HUMAN, BLUE, (200.0, 210.0, 40.0), is_bot=False)
    bot, frame = _ctf((_order(HOLD),), players=(human,))
    bot = _player(BOT, BLUE, (100.0, 100.0, 40.0), carried=5)
    frame = replace(frame, players=(bot, human))
    d = ModePolicyMemory().decide(frame, bot)
    assert d.role == "order_capture" and d.position == BASE


def test_other_bots_orders_are_ignored_and_unordered_bots_unchanged():
    bot, frame = _ctf((ObjectiveSnapshot("player_order", BLUE, (1.0, 1.0, 1.0), carrier_id=99, state=HOLD),))
    assert not ModePolicyMemory().decide(frame, bot).role.startswith("order_")


def test_an_order_survives_the_ordered_bots_own_death_and_respawn():
    """The order is the bot's, not the life's: after respawn it walks back to the post."""
    order = _order(HOLD, anchor=(120.0, 130.0, 40.0))
    dead = _player(BOT, BLUE, (120.0, 130.0, 40.0), alive=False)
    _, frame = _ctf((order,))
    assert ModePolicyMemory().decide(replace(frame, players=(dead,)), dead) is not None
    respawned = replace(_player(BOT, BLUE, (30.0, 30.0, 40.0)), generation=2, life_id=2)
    d = ModePolicyMemory().decide(replace(frame, players=(respawned,), observer_generation=2), respawned)
    assert d.role == "order_hold" and d.position == (120.0, 130.0, 40.0)


def test_describe_role_reads_worker_roles_in_plain_words():
    assert describe_role("order_hold:arrived") == "holding position (in position)"
    assert describe_role("combat_objective:order_hold") == "fighting, holding position"
    assert describe_role("combat_pursuit:route") == "fighting"
    assert describe_role("order_protect_hunt") == "hunting the thief"
    assert describe_role("order_attack_escort") == "escorting the carrier home"
    assert describe_role("") == "idle" and describe_role("mystery_role") == "mystery_role"


# ------------------------------------------------------------------ order book

def _book(*orders):
    book = PlayerOrderBook()
    for bot_id, order in orders:
        book.issue(bot_id, order)
    return book


def test_order_book_drops_departed_bots_and_clears():
    book = PlayerOrderBook()
    book.issue(BOT, PlayerOrder(HOLD, BLUE, HUMAN, (1.0, 2.0, 3.0)))
    book.issue(99, PlayerOrder(HOLD, BLUE, HUMAN, (1.0, 2.0, 3.0)))
    assert [o.carrier_id for o in book.objectives([BOT])] == [BOT] and len(book) == 1
    assert book.clear(BOT) and not book.clear(BOT)


def test_order_book_publishes_the_tunables_on_every_order():
    book = _book((1, PlayerOrder(HOLD, BLUE, HUMAN, (0.0, 0.0, 0.0))),
                 (2, PlayerOrder(DEFEND, BLUE, HUMAN, (0.0, 0.0, 0.0))))
    assert {o.params for o in book.objectives([1, 2], (7.0, 33.0))} == {(7.0, 33.0, 0.0)}   # last = job


def test_orphaned_follow_becomes_hold_where_the_bot_stands():
    book = _book(
        (1, PlayerOrder(FOLLOW, BLUE, HUMAN, (0.0, 0.0, 0.0))),
        (2, PlayerOrder(FOLLOW, BLUE, 55, (0.0, 0.0, 0.0))),
        (3, PlayerOrder(HOLD, BLUE, HUMAN, (9.0, 9.0, 9.0))),
        (4, PlayerOrder(ESCORT, BLUE, HUMAN, (8.0, 8.0, 8.0))),
        (5, PlayerOrder(FOLLOW, BLUE, HUMAN, (0.0, 0.0, 0.0))),
    )
    positions = {1: (10.0, 11.0, 12.0), 2: (20.0, 21.0, 22.0), 3: (1.0, 1.0, 1.0), 4: (2.0, 2.0, 2.0)}
    converted = book.release_orphaned_follows(lambda issuer, team: issuer == 55, positions)
    assert converted == [1]                                     # 2's issuer is alive; 5 has no position
    assert book.get(1) == PlayerOrder(HOLD, BLUE, HUMAN, (10.0, 11.0, 12.0))
    assert book.get(2).code == FOLLOW and book.get(5).code == FOLLOW
    assert book.get(3).anchor == (9.0, 9.0, 9.0) and book.get(4).code == ESCORT


def test_order_book_forgets_a_bot_that_changed_team():
    book = _book((1, PlayerOrder(HOLD, BLUE, HUMAN, (0.0, 0.0, 0.0))),
                 (2, PlayerOrder(HOLD, BLUE, HUMAN, (0.0, 0.0, 0.0))))
    assert book.drop_team_changes({1: GREEN, 2: BLUE, 77: GREEN}) == 1
    assert book.get(1) is None and book.get(2) is not None


# ------------------------------------------------------------ director + commands

import asyncio
from types import SimpleNamespace

from commands.command_handler import handle_command
from server.bot_ai.director import BotDirector
from server.game_constants import TEAM1, TEAM2


def _bot(pid, name, team, pos=(1.0, 2.0, 3.0), alive=True):
    return SimpleNamespace(id=pid, name=name, team=team, position=pos, alive=alive)


def _director(*bots, players=None, config=None):
    director = BotDirector.__new__(BotDirector)
    director.bots = list(bots)
    if players is not None:
        director.server = SimpleNamespace(
            players={p.id: p for p in players},
            config=SimpleNamespace(bots=config or SimpleNamespace()),
        )
    return director


def _human(pid=9, team=TEAM1, alive=True):
    return SimpleNamespace(id=pid, team=team, alive=alive, spawned=alive, name="Paul", admin=False)


def test_give_order_targets_only_the_issuers_team_and_named_bots():
    a, b, c = _bot(1, "Rex", TEAM1), _bot(2, "Ada", TEAM1), _bot(3, "Eve", TEAM2)
    director = _director(a, b, c)
    issuer = SimpleNamespace(id=9, team=TEAM1)
    assert director.give_order("hold", issuer)[0] == 2
    assert director.give_order("follow", issuer, ("rex",))[0] == 1
    objectives = director._objectives_player_orders(None)
    assert {o.carrier_id: o.state for o in objectives} == {1: FOLLOW, 2: HOLD}
    assert director.give_order("free", issuer)[0] == 2
    assert director._objectives_player_orders(None) == []
    assert director.give_order("hold", SimpleNamespace(id=9, team=TEAM2), ("rex",))[0] == 0


def test_director_publishes_the_configured_tunables():
    config = SimpleNamespace(order_hold_leash=9.0, order_defend_chase_radius=55.0,
                             order_protect_hunt_ratio=0.25)
    director = _director(_bot(1, "Rex", TEAM1), players=[_human()], config=config)
    director.give_order("hold", _human())
    assert director._objectives_player_orders(None)[0].params == (9.0, 55.0, 40.0, 0.0)


def test_director_without_config_falls_back_to_the_defaults():
    director = _director(_bot(1, "Rex", TEAM1))
    director.give_order("hold", SimpleNamespace(id=9, team=TEAM1))
    assert director._objectives_player_orders(None)[0].params == (6.0, 40.0, 40.0, 0.0)


def _jobs_of(director):
    return {o.carrier_id: int(o.params[-1]) for o in director._objectives_player_orders(None)}


def _team1_director(config=None, holders=None):
    bots = [_bot(i, f"B{i}", TEAM1) for i in (1, 2, 3, 4)]
    director = _director(*bots, players=[_human()], config=config)
    director.server.mode = SimpleNamespace(intel_holder=holders if holders is not None else {})
    return director, bots


def test_director_splits_attack_bots_while_a_friendly_carrier_holds_the_enemy_intel():
    director, _ = _team1_director(holders={TEAM2: SimpleNamespace(id=30)})   # TEAM2's intel is held by one of ours
    director.give_order("attack", _human())
    assert _jobs_of(director) == {1: JOB_ESCORT, 2: JOB_ESCORT, 3: JOB_PUSH, 4: JOB_PUSH}
    director.server.mode.intel_holder = {}                                   # the carrier died
    assert set(_jobs_of(director).values()) == {JOB_NONE}
    director.server.mode.intel_holder = {TEAM2: SimpleNamespace(id=31)}      # a new carrier
    assert _jobs_of(director) == {1: JOB_ESCORT, 2: JOB_ESCORT, 3: JOB_PUSH, 4: JOB_PUSH}


def test_director_excludes_a_bot_carrier_from_the_attack_split():
    director, _ = _team1_director(holders={TEAM2: SimpleNamespace(id=1)})    # bot 1 carries
    director.give_order("attack", _human())
    assert _jobs_of(director) == {1: JOB_NONE, 2: JOB_ESCORT, 3: JOB_PUSH, 4: JOB_PUSH}


def test_director_reads_the_attack_and_protect_ratios_from_config():
    config = SimpleNamespace(order_attack_escort_ratio=0.75, order_protect_hunt_ratio=0.25)
    holders = {TEAM2: SimpleNamespace(id=30), TEAM1: SimpleNamespace(id=31)}  # carrier AND thief
    director, _ = _team1_director(config, holders)
    director.give_order("attack", _human(), ("b1", "b2", "b3", "b4"))
    assert sorted(_jobs_of(director).values()) == [JOB_ESCORT, JOB_ESCORT, JOB_ESCORT, JOB_PUSH]
    director.give_order("protect", _human())
    assert sorted(_jobs_of(director).values()) == [JOB_ESCORT, JOB_ESCORT, JOB_ESCORT, JOB_PUSH]


def test_protect_only_splits_when_both_a_carrier_and_a_thief_exist():
    director, _ = _team1_director(holders={TEAM2: SimpleNamespace(id=30)})   # carrier, no thief
    director.give_order("protect", _human())
    assert set(_jobs_of(director).values()) == {JOB_NONE}
    director.server.mode.intel_holder = {TEAM2: SimpleNamespace(id=30), TEAM1: SimpleNamespace(id=31)}
    assert _jobs_of(director) == {1: JOB_ESCORT, 2: JOB_ESCORT, 3: JOB_PUSH, 4: JOB_PUSH}


def test_a_bot_keeps_its_job_through_its_own_death_and_respawn():
    director, bots = _team1_director(holders={TEAM2: SimpleNamespace(id=30)})
    director.give_order("attack", _human())
    before = _jobs_of(director)
    bots[2].alive = False                                    # bot 3 dies...
    assert _jobs_of(director) == before
    bots[2].alive = True                                     # ...and respawns
    assert _jobs_of(director) == before


def test_follow_becomes_hold_at_the_bots_spot_when_the_issuer_dies():
    rex = _bot(1, "Rex", TEAM1, pos=(10.0, 10.0, 10.0))
    issuer = _human()
    director = _director(rex, players=[issuer])
    director.give_order("follow", issuer)
    assert director._objectives_player_orders(None)[0].state == FOLLOW      # issuer alive
    rex.position = (40.0, 41.0, 42.0)                                        # bot moved with him
    issuer.alive = issuer.spawned = False                                    # he dies
    order = director._objectives_player_orders(None)[0]
    assert order.state == HOLD and order.position == (40.0, 41.0, 42.0)
    issuer.alive = issuer.spawned = True                                     # he respawns elsewhere
    rex.position = (60.0, 60.0, 60.0)
    order = director._objectives_player_orders(None)[0]
    assert order.state == HOLD and order.position == (40.0, 41.0, 42.0)     # no chasing the respawn


def test_follow_becomes_hold_when_the_issuer_changes_team():
    rex = _bot(1, "Rex", TEAM1, pos=(5.0, 5.0, 5.0))
    issuer = _human()
    director = _director(rex, players=[issuer])
    director.give_order("follow", issuer)
    issuer.team = TEAM2
    order = director._objectives_player_orders(None)[0]
    assert order.state == HOLD and order.position == (5.0, 5.0, 5.0)


def _six_ordered_bots(*issuers):
    bots = [_bot(i, n, TEAM1, pos=(float(i), 0.0, 0.0))
            for i, n in enumerate(("Ada", "Bea", "Cy", "Dee", "Eli", "Fay"), start=1)]
    return bots, _director(*bots, players=list(issuers))


def test_every_order_records_the_player_who_issued_it():
    anna = _human(9)
    bots, director = _six_ordered_bots(anna)
    for bot, action in zip(bots, ("follow", "hold", "attack", "defend", "protect", "escort")):
        director.give_order(action, anna, (bot.name,))
    book = director.__dict__["_order_book"]
    assert [book.get(b.id).issuer_id for b in bots] == [9] * 6
    assert [o.attacker for o in director._objectives_player_orders(None)] == [9] * 6


def test_an_issuer_who_leaves_frees_every_order_type_they_gave():
    anna, ben = _human(9), _human(10)
    bots, director = _six_ordered_bots(anna, ben)
    for bot, action in zip(bots[:5], ("follow", "hold", "attack", "defend", "protect")):
        director.give_order(action, anna, (bot.name,))
    director.give_order("escort", ben, (bots[5].name,))
    assert len(director._objectives_player_orders(None)) == 6
    del director.server.players[anna.id]                      # Anna disconnects
    remaining = director._objectives_player_orders(None)
    assert {o.carrier_id: o.state for o in remaining} == {6: ESCORT}      # Ben's order stays


def test_a_bot_whose_order_is_replaced_by_someone_else_belongs_to_the_new_issuer():
    anna, ben = _human(9), _human(10)
    bots, director = _six_ordered_bots(anna, ben)
    director.give_order("hold", anna, ("ada",))
    director.give_order("attack", ben, ("ada",))              # Ben re-orders Anna's bot
    assert director.__dict__["_order_book"].get(1).issuer_id == 10
    del director.server.players[anna.id]
    assert [o.state for o in director._objectives_player_orders(None)] == [ATTACK]
    del director.server.players[ben.id]                       # now the owner leaves
    assert director._objectives_player_orders(None) == []


def test_an_issuer_who_only_died_or_is_spectating_keeps_their_non_follow_orders():
    anna = _human(9)
    bots, director = _six_ordered_bots(anna)
    director.give_order("hold", anna, ("ada",))
    anna.alive = anna.spawned = False                         # died: still connected
    assert [o.state for o in director._objectives_player_orders(None)] == [HOLD]
    anna.team = 0                                             # moved to spectators: still connected
    assert [o.state for o in director._objectives_player_orders(None)] == [HOLD]


def test_any_teammate_can_free_bots_someone_else_ordered(monkeypatch):
    sent = _commands(monkeypatch)
    anna, ben = _human(9), _human(10)
    bots, director = _six_ordered_bots(anna, ben)
    director.give_order("hold", anna)
    assert len(director._objectives_player_orders(None)) == 6
    server = SimpleNamespace(bots=director, config=SimpleNamespace(log_commands=False))
    asyncio.run(handle_command(server, ben, "free ada bea"))   # Ben frees two of Anna's bots
    assert sorted(o.carrier_id for o in director._objectives_player_orders(None)) == [3, 4, 5, 6]
    asyncio.run(handle_command(server, ben, "free"))
    assert director._objectives_player_orders(None) == []
    assert sent == ["2 bot(s) released", "4 bot(s) released"]


def test_other_orders_do_not_depend_on_the_issuer_staying_alive():
    issuer = _human()
    director = _director(_bot(1, "Rex", TEAM1), _bot(2, "Ada", TEAM1), players=[issuer])
    director.give_order("defend", issuer, ("rex",))
    director.give_order("hold", issuer, ("ada",))
    issuer.alive = issuer.spawned = False
    assert {o.carrier_id: o.state for o in director._objectives_player_orders(None)} == {1: DEFEND, 2: HOLD}


def _escort_director(holders=None):
    rex = _bot(1, "Rex", TEAM1, pos=(10.0, 10.0, 10.0))
    issuer = _human()
    director = _director(rex, players=[issuer])
    director.server.mode = SimpleNamespace(intel_holder=holders if holders is not None else {})
    director.give_order("escort", issuer)
    return director, rex, issuer


def test_escort_becomes_hold_at_the_bots_spot_when_the_issuer_dies():
    director, rex, issuer = _escort_director()
    assert director._objectives_player_orders(None)[0].state == ESCORT      # issuer alive
    rex.position = (40.0, 41.0, 42.0)                                        # the bot moved with him
    issuer.alive = issuer.spawned = False                                    # he dies
    order = director._objectives_player_orders(None)[0]
    assert order.state == HOLD and order.position == (40.0, 41.0, 42.0)
    issuer.alive = issuer.spawned = True                                     # he respawns elsewhere
    rex.position = (60.0, 60.0, 60.0)
    order = director._objectives_player_orders(None)[0]
    assert order.state == HOLD and order.position == (40.0, 41.0, 42.0)     # no chasing the respawn


def test_escort_keeps_escorting_a_carrier_when_the_issuer_dies_and_converts_when_he_is_gone():
    carrier = SimpleNamespace(id=30)
    director, rex, issuer = _escort_director(holders={TEAM2: carrier})      # a teammate holds the enemy intel
    issuer.alive = issuer.spawned = False
    assert director._objectives_player_orders(None)[0].state == ESCORT      # escorting a different carrier
    director.server.mode.intel_holder = {}                                   # the carrier drops or dies
    rex.position = (7.0, 8.0, 9.0)
    order = director._objectives_player_orders(None)[0]
    assert order.state == HOLD and order.position == (7.0, 8.0, 9.0)        # the issuer is still dead: now it converts


def test_escort_stays_when_the_carrier_is_gone_but_the_issuer_is_back():
    carrier = SimpleNamespace(id=30)
    director, rex, issuer = _escort_director(holders={TEAM2: carrier})
    issuer.alive = issuer.spawned = False
    director._objectives_player_orders(None)
    issuer.alive = issuer.spawned = True                                     # respawned while the carrier lives
    director.server.mode.intel_holder = {}
    assert director._objectives_player_orders(None)[0].state == ESCORT      # escorts him again


def test_a_bot_carrying_the_intel_itself_is_not_escorting_a_carrier():
    director, rex, issuer = _escort_director(holders={TEAM2: SimpleNamespace(id=1)})   # the bot is the carrier
    issuer.alive = issuer.spawned = False
    assert director._objectives_player_orders(None)[0].state == HOLD


def test_escort_ordered_from_outside_the_team_never_counts_as_an_issuer_who_died():
    boss = _admin(team=TEAM2)                                                # an admin on green orders blue
    bots, director = _two_teams(boss)
    director.server.mode = SimpleNamespace(intel_holder={})
    director.give_order("escort", boss, team=TEAM1)
    boss.alive = boss.spawned = False                                        # he dies...
    assert {o.state for o in director._objectives_player_orders(None)} == {ESCORT}
    boss.team = 0                                                            # ...or spectates
    assert {o.state for o in director._objectives_player_orders(None)} == {ESCORT}


def test_an_escort_issuer_who_changes_team_does_not_convert_the_order():
    director, rex, issuer = _escort_director()
    issuer.team = TEAM2                                                      # left the bots' team, still alive
    assert director._objectives_player_orders(None)[0].state == ESCORT


def test_book_converts_only_escorts_with_a_dead_issuer_and_no_carrier_and_keeps_the_half():
    book = _book(
        (1, PlayerOrder(ESCORT, BLUE, HUMAN, (0.0, 0.0, 0.0), half=1)),
        (2, PlayerOrder(ESCORT, BLUE, HUMAN, (0.0, 0.0, 0.0))),
        (3, PlayerOrder(ESCORT, BLUE, 55, (0.0, 0.0, 0.0))),
        (4, PlayerOrder(DEFEND, BLUE, HUMAN, (9.0, 9.0, 9.0))),
    )
    positions = {1: (1.0, 1.0, 1.0), 2: (2.0, 2.0, 2.0), 3: (3.0, 3.0, 3.0), 4: (4.0, 4.0, 4.0)}
    converted = book.release_orphaned_follows(
        lambda issuer, team: issuer == 55,                                   # only 55 is alive
        positions,
        issuer_dead=lambda issuer, team: issuer == HUMAN,
        has_carrier=lambda team, bot_id: bot_id == 2,                        # bot 2 escorts a carrier
    )
    assert converted == [1]
    assert book.get(1) == PlayerOrder(HOLD, BLUE, HUMAN, (1.0, 1.0, 1.0), 1)
    assert book.get(2).code == ESCORT and book.get(3).code == ESCORT and book.get(4).code == DEFEND
    assert book.release_orphaned_follows(lambda i, t: True, positions) == []   # no dead-issuer hook: escorts stay


def test_orders_persist_while_the_ordered_bot_is_dead():
    rex = _bot(1, "Rex", TEAM1, pos=(7.0, 7.0, 7.0))
    director = _director(rex, players=[_human()])
    director.give_order("hold", _human())
    rex.alive = False
    rex.position = (99.0, 99.0, 99.0)                        # body where it fell
    rex.alive = True                                          # respawned at the spawn
    order = director._objectives_player_orders(None)[0]
    assert order.state == HOLD and order.position == (7.0, 7.0, 7.0)


def test_a_bot_that_switches_team_forgets_its_order():
    rex = _bot(1, "Rex", TEAM1)
    director = _director(rex, players=[_human()])
    director.give_order("hold", _human())
    rex.team = TEAM2
    assert director._objectives_player_orders(None) == []


def test_order_status_reports_order_and_what_the_bot_is_doing():
    rex, ada, eve = _bot(1, "[BOT]Rex", TEAM1), _bot(2, "[BOT]Ada", TEAM1, alive=False), _bot(3, "[BOT]Eve", TEAM2)
    director = _director(rex, ada, eve, players=[_human()], config=SimpleNamespace(name_prefix="[BOT]"))
    director._runtime = {1: SimpleNamespace(intent=SimpleNamespace(debug_role="order_hold:arrived")),
                         2: SimpleNamespace(intent=None)}
    director.give_order("hold", _human(), ("rex",))
    director.give_order("protect", _human(), ("ada",))
    assert director.order_status(_human()) == [
        ("Rex", "hold", 0, "holding position (in position)"),
        ("Ada", "protect", 0, "dead, waiting to respawn"),
    ]
    assert director.order_status(_human(), ("zzz",)) == []


def _commands(monkeypatch):
    sent = []

    async def fake_send(server, player, message):
        sent.append(message)

    monkeypatch.setattr("commands.bot_orders.send_message", fake_send)
    monkeypatch.setattr("commands.command_handler.send_message", fake_send)
    return sent


def test_chat_command_issues_order_and_replies(monkeypatch):
    sent = _commands(monkeypatch)
    director = _director(_bot(1, "Rex", TEAM1), _bot(2, "Ada", TEAM1))
    server = SimpleNamespace(bots=director, config=SimpleNamespace(log_commands=False))
    player = SimpleNamespace(id=9, team=TEAM1, name="Paul", admin=False)
    asyncio.run(handle_command(server, player, "protect intel"))
    asyncio.run(handle_command(server, player, "hold Rex"))
    assert sent == ["2 bot(s) protecting the intel", "1 bot(s) holding position"]
    assert {o.carrier_id: o.state for o in director._objectives_player_orders(None)} == {1: HOLD, 2: PROTECT}
    spectator = SimpleNamespace(id=10, team=255, name="Spec", admin=False)
    asyncio.run(handle_command(server, spectator, "attack"))
    assert sent[-1] == REFUSED_SPECTATOR                         # a spectator who is not an admin gets nothing
    assert {o.carrier_id for o in director._objectives_player_orders(None)} == {1, 2}   # and orders nobody


def test_escort_is_its_own_command_and_protect_no_longer_aliases_it(monkeypatch):
    sent = _commands(monkeypatch)
    director = _director(_bot(1, "Rex", TEAM1), _bot(2, "Ada", TEAM1))
    server = SimpleNamespace(bots=director, config=SimpleNamespace(log_commands=False))
    player = SimpleNamespace(id=9, team=TEAM1, name="Paul", admin=False)
    asyncio.run(handle_command(server, player, "escort"))
    assert sent == ["2 bot(s) escorting"]
    assert {o.state for o in director._objectives_player_orders(None)} == {ESCORT}
    asyncio.run(handle_command(server, player, "protectintel"))      # remaining alias still works
    assert {o.state for o in director._objectives_player_orders(None)} == {PROTECT}


def test_status_command_lists_each_bot(monkeypatch):
    sent = _commands(monkeypatch)
    director = _director(_bot(1, "Rex", TEAM1), _bot(2, "Ada", TEAM1), _bot(3, "Eve", TEAM2))
    director._runtime = {1: SimpleNamespace(intent=SimpleNamespace(debug_role="order_defend"))}
    server = SimpleNamespace(bots=director, config=SimpleNamespace(log_commands=False))
    player = SimpleNamespace(id=9, team=TEAM1, name="Paul", admin=False)
    asyncio.run(handle_command(server, player, "defend rex"))
    sent.clear()
    asyncio.run(handle_command(server, player, "status"))
    assert sent == ["Rex [defend]: guarding the base", "Ada [free]: idle"]
    sent.clear()
    asyncio.run(handle_command(server, player, "orders ada"))          # alias + name filter
    assert sent == ["Ada [free]: idle"]
    sent.clear()
    asyncio.run(handle_command(server, player, "status nobody"))
    assert sent == ["No matching bots on your team"]
    spectator = SimpleNamespace(id=10, team=255, name="Spec", admin=False)
    sent.clear()
    asyncio.run(handle_command(server, spectator, "status"))       # not an admin: refused
    assert sent == [REFUSED_SPECTATOR]
    spectator.admin = True
    sent.clear()
    asyncio.run(handle_command(server, spectator, "status"))       # an admin spectating sees both teams
    assert sent == ["Team 2 defend: Rex | free: Ada", "Team 3 free: Eve"]        # grouped, one line per group set
    sent.clear()
    asyncio.run(handle_command(server, spectator, "status full"))
    assert sent == ["Team 2: Rex [defend]: guarding the base", "Team 2: Ada [free]: idle",
                    "Team 3: Eve [free]: idle"]                                   # "full" gives the per-bot lines
    asyncio.run(handle_command(SimpleNamespace(bots=None, config=server.config), player, "status"))
    assert sent[-1] == "There are no bots to command"


# ------------------------------------------------------------ hold leash (worker)

import math

from server.bot_ai.messages import MovementAffordance
from server.bot_ai.policies import ModeBotDecision, ModeBotPosture
from server.bot_ai.simple_navigation import RouteStep
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState
from tests.test_bot_combat_tactics import _HEAD, _fighter
from tests.test_simple_bot_tactics import _TacticalWorld, _frame, _profile

_POST = (10.5, 10.5, _HEAD)


def _held_fight(observer_x, *, leash, enemy_x=30.5):
    """One combat frame for a bot holding ``_POST`` against an enemy to the east.

    The bot carries an SMG (ideal range 8-30 blocks), so it uses the strafing
    footwork; a planted minigun relocates only when hit, with or without a leash.
    """
    import shared.constants as C
    smg = dict(weapon_tool=int(C.SMG_TOOL), tool=int(C.SMG_TOOL),
               loadout=(int(C.SMG_TOOL), int(C.SPADE_TOOL)))
    observer = _fighter(1, BLUE, observer_x, 10.5, **smg)
    enemy = _fighter(2, GREEN, enemy_x, 10.5)
    world = _TacticalWorld(route_step=RouteStep(_POST, MovementAffordance.WALK))
    decision = ModeBotDecision(
        _POST, "order_hold", sprint=False, arrival_radius=2.0, posture=ModeBotPosture.DEFEND,
        objective_priority=0.95, engagement_radius=32.0, leash_radius=leash)
    state = _BotState(1, 1, observer.life_id)
    return SimpleBotBrain(world)._combat_intent(
        _frame(observer, enemy), observer, enemy, state, _profile(), 100.0, decision)


def _xy(intent):
    return intent.movement.direction[0], intent.movement.direction[1]


def test_a_leashed_hold_bot_strafes_inside_the_leash():
    intent = _held_fight(10.5, leash=6.0)
    assert intent.debug_role == "combat_objective:order_hold"
    x, y = _xy(intent)
    assert math.hypot(x, y) > 0.5 and abs(y) > abs(x)       # a dodge across the enemy's line
    assert not intent.movement.sprint


def test_a_leashed_hold_bot_beyond_the_leash_is_walked_back_to_the_post():
    intent = _held_fight(19.5, leash=6.0, enemy_x=33.5)      # 9 blocks out, enemy still in range
    assert intent.debug_role == "combat_objective:order_hold"
    assert _xy(intent)[0] < -0.9                             # straight back, not sideways


def test_a_leashed_step_never_ends_outside_the_leash_or_leads_further_out():
    leash, step = 6.0, 1.5
    for tenth in range(0, 90, 5):                            # post .. 9 blocks east of it
        x = 10.5 + tenth / 10.0
        dx, dy = _xy(_held_fight(x, leash=leash, enemy_x=x + 14.0))
        norm = math.hypot(dx, dy)
        if norm < 1e-6:
            continue                                         # standing still is always legal
        end = math.hypot(x - 10.5 + dx / norm * step, dy / norm * step)
        assert end <= leash + 1e-6 or end <= (x - 10.5) + 1e-6, (x, dx, dy, end)


def test_zero_leash_keeps_the_stand_still_behaviour():
    intent = _held_fight(10.5, leash=0.0)
    x, y = _xy(intent)
    assert abs(y) < 0.5                                      # no strafing off the post
    assert intent.debug_role == "combat_objective:order_hold"


def test_leashed_bot_keeps_shooting_while_it_dodges():
    from server.bot_ai.messages import BotActionKind
    intent = _held_fight(10.5, leash=6.0)
    assert intent.action.kind is BotActionKind.FIRE and intent.look.target_player_id == 2


# ------------------------------------------------------------------------ /split

def _squad(count=5, *, issuers=()):
    """``count`` TEAM1 bots (ids 1..count) plus one TEAM2 bot, and the humans given."""
    bots = [_bot(i, f"Bot{i}", TEAM1, pos=(float(i), 0.0, 0.0)) for i in range(1, count + 1)]
    bots.append(_bot(90, "Enemy", TEAM2))
    return bots, _director(*bots, players=list(issuers) or [_human()])


def _state(director):
    book = director.__dict__["_order_book"]
    return {b.id: (NAMES_OF[book.get(b.id).code], book.get(b.id).half) if book.get(b.id) else None
            for b in director.bots if b.team == TEAM1}


from server.bot_ai.player_orders import NAMES as NAMES_OF


def test_split_one_action_keeps_the_old_order_on_the_larger_half():
    anna, ben = _human(9), _human(10)
    bots, director = _squad(5, issuers=[anna, ben])
    director.give_order("hold", anna)
    count, message = director.give_split("attack", None, ben)
    assert (count, message) == (5, "3 bot(s) keep their orders, 2 now attacking")
    assert _state(director) == {1: ("hold", 1), 2: ("hold", 1), 3: ("hold", 1),
                                4: ("attack", 2), 5: ("attack", 2)}
    book = director.__dict__["_order_book"]
    assert [book.get(i).issuer_id for i in (1, 2, 3)] == [9, 9, 9]          # kept: Anna's
    assert [book.get(i).issuer_id for i in (4, 5)] == [10, 10]              # new half: Ben's


def test_split_one_action_leaves_bots_without_orders_alone():
    bots, director = _squad(6)
    director.give_order("hold", _human(), ("bot1", "bot2", "bot3", "bot4"))
    count, message = director.give_split("defend", None, _human())
    assert count == 4 and message == "2 bot(s) keep their orders, 2 now defending"
    state = _state(director)
    assert state[5] is None and state[6] is None
    assert {state[i][1] for i in (1, 2, 3, 4)} == {1, 2}


def test_split_one_action_with_no_active_order_splits_all_bots_into_free_and_the_action():
    bots, director = _squad(4)
    count, message = director.give_split("attack", None, _human())
    assert (count, message) == (4, "2 bot(s) free, 2 attacking")
    assert _state(director) == {1: ("free", 1), 2: ("free", 1), 3: ("attack", 2), 4: ("attack", 2)}
    assert [o.carrier_id for o in director._objectives_player_orders(None)] == [3, 4]   # free halves steer nothing
    # After /free (or a map change, or a fresh start) the same thing happens again.
    director.give_order("free", _human())
    assert director.give_split("defend", None, _human())[0] == 4
    # An odd count gives the larger half to free.
    bots, director = _squad(5)
    assert director.give_split("attack", None, _human())[1] == "3 bot(s) free, 2 attacking"


def test_split_free_alone_with_no_orders_would_free_everyone_so_it_is_refused():
    bots, director = _squad(4)
    count, message = director.give_split("free", None, _human())
    assert count == 0 and message.startswith("No bot has an order, so /split free would leave everyone free")
    assert _state(director) == {1: None, 2: None, 3: None, 4: None}


def test_split_with_no_orders_still_needs_two_bots():
    bots, director = _squad(1)
    assert director.give_split("attack", None, _human()) == (0, "A split needs at least two bots")


def test_free_is_a_valid_action_in_both_split_forms():
    bots, director = _squad(5)
    assert director.give_split("hold", "free", _human()) == (5, "3 bot(s) holding position, 2 free")
    assert _state(director) == {1: ("hold", 1), 2: ("hold", 1), 3: ("hold", 1), 4: ("free", 2), 5: ("free", 2)}
    bots, director = _squad(4)
    assert director.give_split("free", "attack", _human()) == (4, "2 bot(s) free, 2 attacking")
    assert director.give_split("free", "free", _human())[0] == 0          # already split, and equal
    bots, director = _squad(4)
    assert director.give_split("free", "free", _human()) == (0, "Pick two different actions to split between")


def test_split_free_with_orders_keeps_half_and_frees_the_other_half():
    bots, director = _squad(5)
    director.give_order("hold", _human())
    count, message = director.give_split("free", None, _human())
    assert (count, message) == (5, "3 bot(s) keep their orders, 2 now free")
    assert _state(director) == {1: ("hold", 1), 2: ("hold", 1), 3: ("hold", 1), 4: ("free", 2), 5: ("free", 2)}
    assert sorted(o.carrier_id for o in director._objectives_player_orders(None)) == [1, 2, 3]


def test_a_free_half_shows_in_status_and_survives_death():
    bots, director = _squad(4)
    director.give_split("hold", "free", _human())
    assert [(r[1], r[2]) for r in director.order_status(_human())] == [
        ("hold", 1), ("hold", 1), ("free", 2), ("free", 2)]
    before = _state(director)
    for bot in bots[:4]:
        bot.alive = False
    director._objectives_player_orders(None)
    for bot in bots[:4]:
        bot.alive = True
    director._objectives_player_orders(None)
    assert _state(director) == before


def test_a_plain_order_ends_a_split_with_a_free_half_and_leaves_nothing_behind():
    bots, director = _squad(4)
    director.give_split("hold", "free", _human())
    director.give_order("follow", _human(), ("bot1",))
    state = _state(director)
    assert state[1] == ("follow", 0) and state[2] == ("hold", 0)
    assert state[3] is None and state[4] is None                         # the free half is simply free again
    assert not director.__dict__["_order_book"].split_active(TEAM1)


def test_free_releases_a_free_half_too_and_a_split_that_lost_a_half_dissolves():
    bots, director = _squad(4)
    director.give_split("hold", "free", _human())
    director.give_order("free", _human(), ("bot1", "bot2"))               # the whole ordered half
    director._objectives_player_orders(None)
    assert _state(director) == {1: None, 2: None, 3: None, 4: None}      # the free half dissolved with it
    bots, director = _squad(4)
    director.give_split("hold", "free", _human())
    director.give_order("free", _human())
    assert len(director.__dict__["_order_book"]) == 0


def test_a_split_issuers_disconnect_clears_the_free_half_as_well():
    anna = _human(9)
    bots, director = _squad(4, issuers=[anna])
    director.give_split("hold", "free", anna)
    del director.server.players[anna.id]
    director._objectives_player_orders(None)
    assert len(director.__dict__["_order_book"]) == 0


def test_split_free_through_chat_including_aliases_and_the_team_form(monkeypatch):
    sent = _commands(monkeypatch)
    boss = _admin(team=TEAM2)
    bots, director = _two_teams(boss)
    server = _server(director)
    run = lambda text: asyncio.run(handle_command(server, boss, text))
    run("team blue split free attack")
    assert sent[-1] == "2 bot(s) free, 2 attacking"
    run("team blue free")
    run("team blue split hold release")
    assert sent[-1] == "2 bot(s) holding position, 2 free"
    run("team green split resume defend")
    assert sent[-1] == "2 bot(s) free, 2 defending"
    run("team green status")
    assert sent[-1].endswith("idle") and "free, half 1" in sent[-4]


def test_split_needs_at_least_two_bots():
    bots, director = _squad(3)
    director.give_order("hold", _human(), ("bot1",))
    assert director.give_split("attack", None, _human()) == (0, "A split needs at least two bots with orders")
    assert director.give_split("attack", "hold", _human(), ("bot2",)) == (0, "A split needs at least two bots")


def test_split_two_actions_stops_everything_and_gives_the_larger_half_to_the_first():
    bots, director = _squad(5)
    director.give_order("follow", _human(), ("bot1",))                       # replaced by the split
    count, message = director.give_split("hold", "attack", _human())
    assert (count, message) == (5, "3 bot(s) holding position, 2 attacking")
    assert _state(director) == {1: ("hold", 1), 2: ("hold", 1), 3: ("hold", 1),
                                4: ("attack", 2), 5: ("attack", 2)}


def test_split_two_actions_even_count_and_name_order():
    bots, director = _squad(4)
    director.give_split("attack", "defend", _human())
    assert _state(director) == {1: ("attack", 1), 2: ("attack", 1), 3: ("defend", 2), 4: ("defend", 2)}
    bots, director = _squad(5)
    director.give_split("defend", "attack", _human())                        # swapped names, odd count
    assert [v[0] for v in _state(director).values()] == ["defend"] * 3 + ["attack"] * 2


def test_split_two_actions_can_be_limited_to_named_bots():
    bots, director = _squad(5)
    count, _ = director.give_split("hold", "attack", _human(), ("bot2", "bot3", "bot4"))
    assert count == 3
    assert _state(director) == {1: None, 2: ("hold", 1), 3: ("hold", 1), 4: ("attack", 2), 5: None}


def test_split_refuses_two_equal_actions_and_unknown_ones():
    bots, director = _squad(4)
    assert director.give_split("hold", "hold", _human())[0] == 0
    assert director.give_split("fly", None, _human()) == (0, "Unknown action 'fly'")
    assert director.give_split("free", None, _human())[0] == 0               # /free is not a split action


def test_an_already_split_team_cannot_be_split_again_either_way():
    bots, director = _squad(5)
    director.give_split("hold", "attack", _human())
    before = _state(director)
    for first, second in (("defend", None), ("defend", "protect")):
        count, message = director.give_split(first, second, _human())
        assert count == 0 and message.startswith("Your team is already split. Give a plain order")
    assert _state(director) == before


def test_a_plain_order_replaces_the_whole_split():
    bots, director = _squad(5)
    director.give_split("hold", "attack", _human())
    director.give_order("follow", _human())
    assert set(_state(director).values()) == {("follow", 0)}
    assert director.give_split("hold", "attack", _human())[0] == 5            # splittable again


def test_a_plain_order_to_some_bots_ends_the_split_for_all_but_keeps_their_orders():
    bots, director = _squad(5)
    director.give_split("hold", "attack", _human())
    director.give_order("defend", _human(), ("bot1",))
    state = _state(director)
    assert state[1] == ("defend", 0)
    assert state[2] == ("hold", 0) and state[4] == ("attack", 0)              # no longer a split
    assert not director.__dict__["_order_book"].split_active(TEAM1)


def test_split_halves_survive_death_and_respawn():
    bots, director = _squad(5)
    director.give_split("hold", "attack", _human())
    before = _state(director)
    for bot in bots[:5]:
        bot.alive = False
    director._objectives_player_orders(None)
    for bot in bots[:5]:
        bot.alive = True
    director._objectives_player_orders(None)
    assert _state(director) == before


def test_freeing_a_whole_half_leaves_no_split_behind():
    bots, director = _squad(5)
    director.give_split("hold", "attack", _human())
    director.give_order("free", _human(), ("bot4", "bot5"))                   # the whole second half
    director._objectives_player_orders(None)
    assert _state(director) == {1: ("hold", 0), 2: ("hold", 0), 3: ("hold", 0), 4: None, 5: None}
    assert director.give_split("defend", None, _human())[0] == 3              # a fresh split works


def test_split_attack_bots_run_the_plain_order_with_no_automatic_job_split():
    carrier = SimpleNamespace(id=30)
    bots, director = _squad(6)
    director.server.mode = SimpleNamespace(intel_holder={TEAM2: carrier})     # a friendly carrier exists
    director.give_order("attack", _human())
    assert sorted(_jobs_of(director).values()) == [JOB_ESCORT] * 3 + [JOB_PUSH] * 3   # automatic split
    director.give_split("attack", "hold", _human())                           # now a manual split
    assert set(_jobs_of(director).values()) == {JOB_NONE}                     # plain attack + plain hold


def test_split_protect_bots_run_the_plain_version_and_escort():
    bots, director = _squad(4)
    director.server.mode = SimpleNamespace(intel_holder={TEAM2: SimpleNamespace(id=30), TEAM1: SimpleNamespace(id=31)})
    director.give_order("protect", _human())
    assert JOB_PUSH in _jobs_of(director).values()                            # carrier + thief: it splits
    director.give_split("protect", "defend", _human())
    assert set(_jobs_of(director).values()) == {JOB_NONE}


def test_status_shows_each_bots_half():
    bots, director = _squad(4)
    director.give_split("hold", "attack", _human())
    rows = director.order_status(_human())
    assert [(r[1], r[2]) for r in rows] == [("hold", 1), ("hold", 1), ("attack", 2), ("attack", 2)]


def test_an_orphaned_follow_inside_a_split_keeps_its_half():
    book = _book((1, PlayerOrder(FOLLOW, BLUE, HUMAN, (0.0, 0.0, 0.0), half=2)))
    book.release_orphaned_follows(lambda issuer, team: False, {1: (4.0, 5.0, 6.0)})
    assert book.get(1) == PlayerOrder(HOLD, BLUE, HUMAN, (4.0, 5.0, 6.0), 2)


def test_a_split_issuers_disconnect_frees_the_halves_they_gave():
    anna, ben = _human(9), _human(10)
    bots, director = _squad(4, issuers=[anna, ben])
    director.give_split("hold", "attack", anna)                               # Anna's two-action split
    del director.server.players[anna.id]
    assert director._objectives_player_orders(None) == []                     # all of it freed
    bots, director = _squad(4, issuers=[anna, ben])
    director.give_order("hold", anna)
    director.give_split("attack", None, ben)                                  # Ben re-orders half
    del director.server.players[ben.id]                                       # Ben leaves
    director._objectives_player_orders(None)                                  # the director's next pass
    state = _state(director)
    assert state[1] == ("hold", 0) and state[2] == ("hold", 0)                # Anna's half stays, no split
    assert state[3] is None and state[4] is None


def test_split_commands_reply_and_refuse_through_chat(monkeypatch):
    sent = _commands(monkeypatch)
    bots, director = _squad(5)
    server = SimpleNamespace(bots=director, config=SimpleNamespace(log_commands=False))
    player = _human(9)
    run = lambda text: asyncio.run(handle_command(server, player, text))
    run("split attack")                                                      # nothing ordered yet
    run("split hold attack")                                                 # now split already
    run("split defend")
    run("split")
    run("split fly")
    assert sent[0] == "3 bot(s) free, 2 attacking"
    assert sent[1].startswith("Your team is already split") and sent[2].startswith("Your team is already split")
    assert sent[3].startswith("Usage: /split <action> [<action>]")
    assert sent[4] == "Unknown action 'fly'. Actions: follow, hold, attack, defend, protect, escort, free"
    sent.clear()
    run("hold")                                                              # a plain order ends the split
    run("split hold attack")
    assert sent == ["5 bot(s) holding position", "3 bot(s) holding position, 2 attacking"]
    sent.clear()
    run("status bot1 bot5")
    assert sent == ["Bot1 [hold, half 1]: idle", "Bot5 [attack, half 2]: idle"]
    spectator = SimpleNamespace(id=11, team=0, name="Spec", admin=False)
    sent.clear()
    asyncio.run(handle_command(server, spectator, "split hold attack"))
    assert sent == [REFUSED_SPECTATOR]
    spectator.admin = True                                        # an admin with no team of their own is pointed at /team
    sent.clear()
    asyncio.run(handle_command(server, spectator, "split hold attack"))
    assert sent == ["Join a team to command bots, or use /team <color|all> <command>"]


def test_split_command_accepts_aliases_names_and_the_intel_word(monkeypatch):
    sent = _commands(monkeypatch)
    bots, director = _squad(5)
    server = SimpleNamespace(bots=director, config=SimpleNamespace(log_commands=False))
    asyncio.run(handle_command(server, _human(9), "split stay assault bot1 bot2"))
    assert sent == ["1 bot(s) holding position, 1 attacking"]
    assert _state(director)[1] == ("hold", 1) and _state(director)[2] == ("attack", 2)
    sent.clear()
    bots, director = _squad(4)
    server = SimpleNamespace(bots=director, config=SimpleNamespace(log_commands=False))
    asyncio.run(handle_command(server, _human(9), "split protect intel hold"))
    assert sent == ["2 bot(s) protecting the intel, 2 holding position"]


def test_free_with_nothing_to_release_says_so(monkeypatch):
    sent = _commands(monkeypatch)
    bots, director = _squad(3)
    server = SimpleNamespace(bots=director, config=SimpleNamespace(log_commands=False))
    asyncio.run(handle_command(server, _human(9), "free"))
    assert sent == ["None of those bots had an order"]


def test_split_protect_intel_word_is_dropped_before_and_after_the_second_action(monkeypatch):
    sent = _commands(monkeypatch)
    bots, director = _squad(4)
    server = SimpleNamespace(bots=director, config=SimpleNamespace(log_commands=False))
    asyncio.run(handle_command(server, _human(9), "split hold protect intel bot1 bot2"))
    assert sent == ["1 bot(s) holding position, 1 protecting the intel"]


# ------------------------------------------------------- /team <color> <command>

from commands.bot_orders import REFUSED_OTHER_TEAM, REFUSED_SPECTATOR, team_tokens


def _server(director, *, names=("TEAM1_COLOR", "TEAM2_COLOR")):
    config = SimpleNamespace(log_commands=False, team1_name=names[0], team2_name=names[1])
    return SimpleNamespace(bots=director, config=config)


def _two_teams(*humans):
    bots = ([_bot(i, f"Blu{i}", TEAM1, pos=(float(i), 0.0, 0.0)) for i in range(1, 5)]
            + [_bot(i, f"Grn{i}", TEAM2, pos=(float(i), 0.0, 0.0)) for i in range(11, 15)])
    return bots, _director(*bots, players=list(humans))


def _admin(pid=20, team=TEAM2):
    return SimpleNamespace(id=pid, team=team, alive=True, spawned=True, name="Boss", admin=True)


def _spectator(pid=21, *, admin=False):
    return SimpleNamespace(id=pid, team=0, alive=False, spawned=False, name="Spec", admin=admin)


def _teams_of(director, team):
    book = director.__dict__.get("_order_book")
    return {b.id: (NAMES_OF[book.get(b.id).code], book.get(b.id).half)
            for b in director.bots if b.team == team and book is not None and book.get(b.id)}


def test_team_names_resolve_to_the_servers_real_colours_and_ids():
    tokens = team_tokens(_server(None))
    assert tokens["blue"] == TEAM1 and tokens["green"] == TEAM2
    assert tokens["team1"] == TEAM1 and tokens["team2"] == TEAM2
    assert tokens["team1_color"] == TEAM1 and tokens["team2_color"] == TEAM2
    custom = team_tokens(_server(None, names=("TEAM_RED", "TEAM_GOLD")))
    assert custom["team_red"] == TEAM1 and custom["team_gold"] == TEAM2
    assert "blue" not in custom and "green" not in custom        # the server's own names, not stock ones


def test_an_admin_orders_the_other_teams_bots_without_changing_their_own_team(monkeypatch):
    sent = _commands(monkeypatch)
    boss = _admin(team=TEAM2)
    bots, director = _two_teams(boss)
    server = _server(director)
    asyncio.run(handle_command(server, boss, "team blue protect"))
    asyncio.run(handle_command(server, boss, "team blue split attack"))        # already ordered: keep half
    assert sent[0] == "4 bot(s) protecting the intel"
    assert sent[1] == "2 bot(s) keep their orders, 2 now attacking"
    assert _teams_of(director, TEAM1) == {1: ("protect", 1), 2: ("protect", 1),
                                          3: ("attack", 2), 4: ("attack", 2)}
    assert _teams_of(director, TEAM2) == {}                                  # their own team untouched
    assert boss.team == TEAM2
    book = director.__dict__["_order_book"]
    assert {book.get(i).issuer_id for i in (1, 2, 3, 4)} == {20}
    assert {book.get(i).team for i in (1, 2, 3, 4)} == {TEAM1}


def test_team_command_accepts_every_command_and_split_form(monkeypatch):
    sent = _commands(monkeypatch)
    boss = _admin()
    bots, director = _two_teams(boss)
    server = _server(director)
    run = lambda text: asyncio.run(handle_command(server, boss, text))
    run("team green split hold attack")
    assert sent[-1] == "2 bot(s) holding position, 2 attacking"
    assert _teams_of(director, TEAM2) == {11: ("hold", 1), 12: ("hold", 1), 13: ("attack", 2), 14: ("attack", 2)}
    run("team green status grn11")
    assert sent[-1] == "Grn11 [hold, half 1]: idle"
    run("team green free")
    assert sent[-1] == "4 bot(s) released" and _teams_of(director, TEAM2) == {}
    for text, said in (
        ("team blue follow", "4 bot(s) following you"),
        ("team blue hold blu1", "1 bot(s) holding position"),
        ("team blue stay blu2", "1 bot(s) holding position"),
        ("team blue assault", "4 bot(s) attacking"),
        ("team blue defend", "4 bot(s) defending"),
        ("team blue protect intel blu1", "1 bot(s) protecting the intel"),
        ("team blue protectintel", "4 bot(s) protecting the intel"),
        ("team blue escort", "4 bot(s) escorting"),
        ("team blue release blu1", "1 bot(s) released"),
        ("team team1 split defend hold", "2 bot(s) defending, 2 holding position"),
    ):
        if text == "team blue follow":
            boss.team = TEAM1               # an admin playing on blue may /follow blue's bots
        elif text == "team blue hold blu1":
            boss.team = TEAM2
        run(text)
        assert sent[-1] == said, (text, sent[-1])


def test_a_spectator_is_refused_unless_an_admin(monkeypatch):
    sent = _commands(monkeypatch)
    spec = _spectator()
    bots, director = _two_teams(spec)
    asyncio.run(handle_command(_server(director), spec, "team blue hold"))
    assert sent == [REFUSED_SPECTATOR]
    assert _teams_of(director, TEAM1) == {}
    sent.clear()
    boss = _spectator(22, admin=True)
    asyncio.run(handle_command(_server(director), boss, "team blue hold"))
    assert sent == ["4 bot(s) holding position"]
    asyncio.run(handle_command(_server(director), boss, "team green split attack defend"))
    assert sent[-1] == "2 bot(s) attacking, 2 defending"
    assert boss.team == 0                                                       # still a spectator


def test_a_team_member_may_use_team_for_their_own_colour_only(monkeypatch):
    sent = _commands(monkeypatch)
    member = _human(9, team=TEAM1)
    bots, director = _two_teams(member)
    asyncio.run(handle_command(_server(director), member, "team green hold"))
    assert sent == [REFUSED_OTHER_TEAM]
    assert _teams_of(director, TEAM2) == {}
    sent.clear()
    asyncio.run(handle_command(_server(director), member, "team blue hold"))     # their own colour is just the long form
    assert sent == ["4 bot(s) holding position"]
    assert _teams_of(director, TEAM1) == {i: ("hold", 0) for i in (1, 2, 3, 4)}


def test_team_command_rejects_unknown_teams_and_commands(monkeypatch):
    sent = _commands(monkeypatch)
    boss = _admin()
    bots, director = _two_teams(boss)
    server = _server(director)
    asyncio.run(handle_command(server, boss, "team purple hold"))
    asyncio.run(handle_command(server, boss, "team blue dance"))
    assert sent[0] == "Unknown team 'purple'. Use blue, green or all (team1 or team2)"
    assert sent[1] == "Unknown command 'dance'. Use: follow, hold, attack, defend, protect, escort, free, status, split"
    assert _teams_of(director, TEAM1) == {}


def test_follow_from_outside_the_team_is_refused_because_nobody_can_be_followed(monkeypatch):
    sent = _commands(monkeypatch)
    spec = _spectator(admin=True)
    bots, director = _two_teams(spec)
    server = _server(director)
    asyncio.run(handle_command(server, spec, "team blue follow"))
    asyncio.run(handle_command(server, spec, "team blue split follow attack"))
    assert sent == ["You are not on that team, so there is nobody for /follow to follow"] * 2
    assert _teams_of(director, TEAM1) == {}


def test_status_of_an_empty_match_from_outside_says_that_team(monkeypatch):
    sent = _commands(monkeypatch)
    boss = _admin()
    bots, director = _two_teams(boss)
    asyncio.run(handle_command(_server(director), boss, "team blue status nobody"))
    assert sent == ["No matching bots on that team"]


def test_team_members_keep_their_own_orders_working_beside_an_admin_spectators(monkeypatch):
    sent = _commands(monkeypatch)
    spec, member = _spectator(admin=True), _human(9, team=TEAM1)
    bots, director = _two_teams(spec, member)
    server = _server(director)
    asyncio.run(handle_command(server, spec, "team blue attack"))
    asyncio.run(handle_command(server, member, "hold blu1 blu2"))             # the member re-orders two
    book = director.__dict__["_order_book"]
    assert [(book.get(i).code, book.get(i).issuer_id) for i in (1, 2, 3, 4)] == [
        (HOLD, 9), (HOLD, 9), (ATTACK, 21), (ATTACK, 21)]
    asyncio.run(handle_command(server, member, "free blu3"))                  # any teammate can free
    assert book.get(3) is None and book.get(4) is not None
    del director.server.players[spec.id]                                      # the spectator disconnects
    assert sorted(o.carrier_id for o in director._objectives_player_orders(None)) == [1, 2]   # the member's stay


# ----------------------------------------------------------- /team all <command>

def test_team_all_applies_a_command_to_both_teams_under_labels(monkeypatch):
    sent = _commands(monkeypatch)
    boss = _admin(team=TEAM2)
    bots, director = _two_teams(boss)
    server = _server(director)
    run = lambda text: asyncio.run(handle_command(server, boss, text))
    run("team all hold")
    assert sent == ["Blue: 4 bot(s) holding position", "Green: 4 bot(s) holding position"]
    assert _teams_of(director, TEAM1) == {i: ("hold", 0) for i in (1, 2, 3, 4)}
    assert _teams_of(director, TEAM2) == {i: ("hold", 0) for i in (11, 12, 13, 14)}
    sent.clear()
    run("team all free")
    assert sent == ["Blue: 4 bot(s) released", "Green: 4 bot(s) released"]
    assert _teams_of(director, TEAM1) == {} and _teams_of(director, TEAM2) == {}
    assert boss.team == TEAM2


def test_team_all_split_forms_including_free_work_for_both_teams(monkeypatch):
    sent = _commands(monkeypatch)
    boss = _admin()
    bots, director = _two_teams(boss)
    server = _server(director)
    run = lambda text: asyncio.run(handle_command(server, boss, text))
    run("team all split free attack")
    assert sent == ["Blue: 2 bot(s) free, 2 attacking", "Green: 2 bot(s) free, 2 attacking"]
    sent.clear()
    run("team all hold")                                                     # a plain order ends both splits
    run("team all split hold attack")
    assert sent[-2:] == ["Blue: 2 bot(s) holding position, 2 attacking",
                         "Green: 2 bot(s) holding position, 2 attacking"]
    sent.clear()
    run("team all split defend")                                             # both already split
    assert [m.split(":")[0] for m in sent] == ["Blue", "Green"]
    assert all("is already split" in m for m in sent)


def test_team_all_keeps_going_when_one_team_refuses(monkeypatch):
    sent = _commands(monkeypatch)
    boss = _admin()
    bots, director = _two_teams(boss)
    server = _server(director)
    run = lambda text: asyncio.run(handle_command(server, boss, text))
    run("team blue split hold attack")                                       # only blue is split
    sent.clear()
    run("team all split defend")
    assert sent[0].startswith("Blue: That team is already split")
    assert sent[1] == "Green: 2 bot(s) free, 2 defending"                    # green still proceeds
    sent.clear()
    boss.team = TEAM2
    run("team all follow")                                                   # an admin on green may /follow only green
    assert sent[0] == "Blue: You are not on that team, so there is nobody for /follow to follow"
    assert sent[1] == "Green: 4 bot(s) following you"


def test_team_all_status_lists_both_teams_with_labels(monkeypatch):
    sent = _commands(monkeypatch)
    boss = _admin()
    bots, director = _two_teams(boss)
    server = _server(director)
    asyncio.run(handle_command(server, boss, "team blue split hold free"))
    sent.clear()
    asyncio.run(handle_command(server, boss, "team all status"))
    assert sent == ["Blue hold 1: Blu1, Blu2 | free 2: Blu3, Blu4", "Green free: Grn11, Grn12, Grn13, Grn14"]
    sent.clear()
    asyncio.run(handle_command(server, boss, "team all status blu1 grn14"))   # names filter both teams
    assert sent == ["Blue hold 1: Blu1", "Green free: Grn14"]
    sent.clear()
    asyncio.run(handle_command(server, boss, "team all status full"))         # full per-bot detail on request
    assert len(sent) == 8
    assert sent[0] == "Blue: Blu1 [hold, half 1]: idle" and sent[3] == "Blue: Blu4 [free, half 2]: idle"
    assert sent[4] == "Green: Grn11 [free]: idle"
    sent.clear()
    asyncio.run(handle_command(server, boss, "t a st full blu1 grn14"))       # short forms, any order of words
    assert sent == ["Blue: Blu1 [hold, half 1]: idle", "Green: Grn14 [free]: idle"]
    sent.clear()
    asyncio.run(handle_command(server, boss, "team blue status"))              # a single team keeps full detail
    assert len(sent) == 4 and sent[0] == "Blu1 [hold, half 1]: idle"


def test_team_all_is_for_admins_only(monkeypatch):
    sent = _commands(monkeypatch)
    spec, member, boss = _spectator(), _human(9, team=TEAM1), _spectator(22, admin=True)
    bots, director = _two_teams(spec, member, boss)
    asyncio.run(handle_command(_server(director), spec, "team all hold"))
    assert sent == [REFUSED_SPECTATOR]
    sent.clear()
    asyncio.run(handle_command(_server(director), member, "team all hold"))
    assert sent == [REFUSED_OTHER_TEAM]
    assert _teams_of(director, TEAM1) == {} and _teams_of(director, TEAM2) == {}
    sent.clear()
    asyncio.run(handle_command(_server(director), boss, "team all hold"))
    assert sent == ["Blue: 4 bot(s) holding position", "Green: 4 bot(s) holding position"]
    assert boss.team == 0


def test_team_all_with_no_bots_or_a_bad_command_answers_once(monkeypatch):
    sent = _commands(monkeypatch)
    boss = _admin()
    asyncio.run(handle_command(SimpleNamespace(bots=None, config=_server(None).config), boss, "team all hold"))
    assert sent == ["There are no bots to command"]
    sent.clear()
    bots, director = _two_teams(boss)
    asyncio.run(handle_command(_server(director), boss, "team all dance"))
    assert sent == ["Unknown command 'dance'. Use: follow, hold, attack, defend, protect, escort, free, status, split"]


# ------------------------------------------------- /status for an admin spectating

def test_an_admin_spectators_status_shows_both_teams(monkeypatch):
    sent = _commands(monkeypatch)
    spec = _spectator(admin=True)
    bots, director = _two_teams(spec)
    server = _server(director)
    asyncio.run(handle_command(server, _admin(team=TEAM2), "team blue split hold attack"))
    sent.clear()
    asyncio.run(handle_command(server, spec, "status"))
    assert sent == ["Blue hold 1: Blu1, Blu2 | attack 2: Blu3, Blu4", "Green free: Grn11, Grn12, Grn13, Grn14"]
    sent.clear()
    asyncio.run(handle_command(server, spec, "st full"))                            # "/st full" = full detail
    assert len(sent) == 8
    assert sent[0] == "Blue: Blu1 [hold, half 1]: idle" and sent[3] == "Blue: Blu4 [attack, half 2]: idle"
    assert sent[4] == "Green: Grn11 [free]: idle"
    sent.clear()
    asyncio.run(handle_command(server, spec, "orders blu2 grn12"))                  # alias + name filter
    assert sent == ["Blue hold 1: Blu2", "Green free: Grn12"]
    sent.clear()
    asyncio.run(handle_command(server, spec, "status zzz"))
    assert sent == ["Blue: No matching bots on that team", "Green: No matching bots on that team"]


def test_a_plain_spectator_cannot_read_any_status_but_an_admin_one_can(monkeypatch):
    sent = _commands(monkeypatch)
    spec, boss = _spectator(), _spectator(22, admin=True)
    bots, director = _two_teams(spec, boss)
    for text in ("status", "st full", "team blue status", "team all status", "team all st full blu1"):
        sent.clear()
        asyncio.run(handle_command(_server(director), spec, text))
        assert sent == [REFUSED_SPECTATOR], text
    sent.clear()
    asyncio.run(handle_command(_server(director), boss, "team blue status blu3"))
    assert sent == ["Blu3 [free]: idle"]                                              # one team, unlabeled


def test_players_and_admins_on_a_team_still_see_only_their_own_team_with_plain_status(monkeypatch):
    sent = _commands(monkeypatch)
    member, boss = _human(9, team=TEAM1), _admin(team=TEAM2)
    bots, director = _two_teams(member, boss)
    server = _server(director)
    asyncio.run(handle_command(server, member, "status"))
    assert [m.split(" ")[0] for m in sent] == ["Blu1", "Blu2", "Blu3", "Blu4"]
    sent.clear()
    asyncio.run(handle_command(server, boss, "status"))
    assert [m.split(" ")[0] for m in sent] == ["Grn11", "Grn12", "Grn13", "Grn14"]


def test_an_admin_spectators_status_with_no_bots_answers_once(monkeypatch):
    sent = _commands(monkeypatch)
    spec = _spectator(admin=True)
    asyncio.run(handle_command(SimpleNamespace(bots=None, config=_server(None).config), spec, "status"))
    assert sent == ["There are no bots to command"]


# ------------------------------------------------ who may command which team's bots

_BLUE, _GREEN = "blue", "green"
_OK, _SPEC, _OTHER, _NOTEAM = "ok", "spectator", "other-team", "no-team"
_NOTEAM_TEXT = "Join a team to command bots, or use /team <color|all> <command>"
_MATRIX_ROLES = {
    "player": lambda: _human(9, team=TEAM1),
    "spectator": lambda: _spectator(21),
    "admin-player": lambda: _admin(20, team=TEAM1),
    "admin-spectator": lambda: _spectator(22, admin=True),
}
# command -> the team (or "both") it orders, and the outcome for each role.
_MATRIX = [
    # plain commands act on the player's own team
    ("hold",               "own",   dict(player=_OK, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _NOTEAM})),
    ("split hold attack",  "own",   dict(player=_OK, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _NOTEAM})),
    ("free",               "own",   dict(player=_OK, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _NOTEAM})),
    ("status",             "own",   dict(player=_OK, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _OK})),
    # /team <color> <command>
    ("team blue hold",     _BLUE,   dict(player=_OK, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _OK})),
    ("team blue split hold attack", _BLUE,
                                    dict(player=_OK, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _OK})),
    ("team blue status",   _BLUE,   dict(player=_OK, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _OK})),
    ("team green hold",    _GREEN,  dict(player=_OTHER, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _OK})),
    ("team green split hold attack", _GREEN,
                                    dict(player=_OTHER, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _OK})),
    ("team green status",  _GREEN,  dict(player=_OTHER, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _OK})),
    ("team green free",    _GREEN,  dict(player=_OTHER, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _OK})),
    # /team all <command>
    ("team all hold",      "both",  dict(player=_OTHER, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _OK})),
    ("team all split hold attack", "both",
                                    dict(player=_OTHER, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _OK})),
    ("team all status",    "both",  dict(player=_OTHER, spectator=_SPEC, **{"admin-player": _OK, "admin-spectator": _OK})),
]


def _matrix_ids():
    return [f"{role}:{text}" for text, _, outcomes in _MATRIX for role in outcomes]


import pytest


@pytest.mark.parametrize("role,text,target,outcome", [
    (role, text, target, outcome)
    for text, target, outcomes in _MATRIX for role, outcome in outcomes.items()
], ids=_matrix_ids())
def test_who_may_command_which_teams_bots(monkeypatch, role, text, target, outcome):
    sent = _commands(monkeypatch)
    who = _MATRIX_ROLES[role]()
    bots, director = _two_teams(who)
    asyncio.run(handle_command(_server(director), who, text))
    if outcome == _OK:
        assert sent and not any(m in (REFUSED_SPECTATOR, REFUSED_OTHER_TEAM, _NOTEAM_TEXT) for m in sent), sent
        if text.split()[-1] != "status" and not text.endswith("free"):
            acted = {t for t in (TEAM1, TEAM2) if _teams_of(director, t)}
            expected = {TEAM1, TEAM2} if target == "both" else {TEAM1 if target in ("own", _BLUE) else TEAM2}
            assert acted == expected, (acted, expected)
        return
    expected_text = {_SPEC: REFUSED_SPECTATOR, _OTHER: REFUSED_OTHER_TEAM, _NOTEAM: _NOTEAM_TEXT}[outcome]
    assert sent == [expected_text]
    assert _teams_of(director, TEAM1) == {} and _teams_of(director, TEAM2) == {}      # nothing was ordered


def test_the_refusals_read_clearly():
    assert REFUSED_SPECTATOR == "Spectators cannot command bots or read their status"
    assert REFUSED_OTHER_TEAM == "You can only command your own team's bots; ask an admin to use /team"


# ------------------------------------------------------------------ short forms

from commands.command_handler import _commands as _registry

_SHORT_COMMANDS = {"t": "team", "sp": "split", "fo": "follow", "ho": "hold", "at": "attack",
                   "de": "defend", "pr": "protect", "es": "escort", "fr": "free", "st": "status"}


def test_every_short_command_form_maps_to_its_command():
    for short, name in _SHORT_COMMANDS.items():
        assert _registry[short].name == name, short


def test_no_registered_command_name_or_alias_is_shadowed_by_another():
    """The registry silently overwrites duplicates, so check every declared key still
    resolves to the command that declared it (a short form must not hide /ban, /k, /w...)."""
    seen = {}
    for command in _registry.values():
        seen[id(command)] = command
    for command in seen.values():
        for key in (command.name, *command.aliases):
            assert _registry[key.lower()] is command, f"/{key} is shadowed by /{_registry[key.lower()].name}"
    assert _registry["b"].name == "ban"                          # the existing /b was not taken
    assert "a" not in _registry and "g" not in _registry         # team arguments, never commands


def test_top_level_short_commands_work_in_any_case(monkeypatch):
    sent = _commands(monkeypatch)
    bots, director = _squad(4)
    server = SimpleNamespace(bots=director, config=SimpleNamespace(log_commands=False))
    run = lambda text: asyncio.run(handle_command(server, _human(9), text))
    for text, said in (("ho", "4 bot(s) holding position"), ("AT bot1", "1 bot(s) attacking"),
                       ("De", "4 bot(s) defending"), ("PR intel", "4 bot(s) protecting the intel"),
                       ("Es bot2 bot3", "2 bot(s) escorting"), ("fO", "4 bot(s) following you"),
                       ("FR bot4", "1 bot(s) released")):
        run(text)
        assert sent[-1] == said, (text, sent[-1])
    run("st bot1")
    assert sent[-1].startswith("Bot1 [protect]") or sent[-1].startswith("Bot1 [")
    sent.clear()
    run("sp ho at")                                              # split, short action names
    assert sent == ["2 bot(s) holding position, 2 attacking"]
    run("FR")
    assert sent[-1] == "4 bot(s) released"


def test_short_forms_mix_with_long_ones_inside_team_and_split(monkeypatch):
    sent = _commands(monkeypatch)
    boss = _admin(team=TEAM2)
    bots, director = _two_teams(boss)
    server = _server(director)
    run = lambda text: asyncio.run(handle_command(server, boss, text))
    run("t b sp at pr")                                          # the example from the notes
    assert sent[-1] == "2 bot(s) attacking, 2 protecting the intel"
    assert _teams_of(director, TEAM1) == {1: ("attack", 1), 2: ("attack", 1), 3: ("protect", 2), 4: ("protect", 2)}
    run("T G SPLIT FR Attack")                                   # any case, any mix
    assert sent[-1] == "2 bot(s) free, 2 attacking"
    run("team b hold")
    assert sent[-1] == "4 bot(s) holding position"               # a plain order replaces the split
    run("t g ho")
    assert sent[-1] == "4 bot(s) holding position"
    run("t a st blu1")
    assert sent[-2:] == ["Blue hold: Blu1", "Green: No matching bots on your team"]          # the admin plays on green
    run("t a sp ho fr")
    assert sent[-2:] == ["Blue: 2 bot(s) holding position, 2 free", "Green: 2 bot(s) holding position, 2 free"]
    run("t all fr")
    assert sent[-2:] == ["Blue: 4 bot(s) released", "Green: 4 bot(s) released"]
    run("t B es")
    assert sent[-1] == "4 bot(s) escorting"
    run("t g de blu1")                                           # bot names still work after a short action
    assert sent[-1] == "No matching bots on your team"
    run("t g de grn11")
    assert sent[-1] == "1 bot(s) defending"
    run("t b sp pr intel de")                                    # the "intel" word after a short protect
    assert sent[-1] == "2 bot(s) protecting the intel, 2 defending"


def test_b_and_g_also_name_the_teams_for_a_plain_team_switch_but_a_does_not():
    tokens = team_tokens(_server(None))
    assert tokens["b"] == TEAM1 and tokens["g"] == TEAM2 and "a" not in tokens


def test_short_actions_in_a_status_and_split_with_an_admin_spectator(monkeypatch):
    sent = _commands(monkeypatch)
    spec = _spectator(admin=True)
    bots, director = _two_teams(spec)
    server = _server(director)
    run = lambda text: asyncio.run(handle_command(server, spec, text))
    run("t a sp ho at")
    assert sent[-2:] == ["Blue: 2 bot(s) holding position, 2 attacking",
                         "Green: 2 bot(s) holding position, 2 attacking"]
    run("t g st grn13")
    assert sent[-1] == "Grn13 [attack, half 2]: idle"


def test_the_docs_list_every_short_form():
    root = Path(__file__).resolve().parents[1]
    guide = (root / "docs" / "BOT_CHAT_ORDERS.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    for short, name in _SHORT_COMMANDS.items():
        assert f"| `/{name}` | `/{short}` |" in guide, (name, short)
        assert f"`{short}`={name}" in readme, (name, short)
    for color, short in (("blue", "b"), ("green", "g"), ("all", "a")):
        assert f"`{short}`={color}" in readme and f"`{short}`" in guide


# ------------------------------------------------------ dropped enemy intel pickup

def _jobp(job):
    """Order params carrying ``job``, whatever the params layout is."""
    return ((6.0, 40.0, 40.0, 1.0)[:P_JOB] + (float(job),))


DROP_AT = (150.0, 150.0, 40.0)


def _dropped(code, job=JOB_PICKUP, **kw):
    return _decide(code, params=_jobp(job), enemy_intel_at=DROP_AT, enemy_intel_state=1, **kw)


def test_ordered_bots_chosen_for_a_dropped_intel_run_and_grab_it_sprinting():
    for code in (ATTACK, ESCORT, PROTECT, DEFEND, FOLLOW):
        d = _dropped(code)
        assert d.role == "order_pickup" and d.position == DROP_AT, code
        assert d.sprint and d.arrival_radius <= 1.5 and d.posture.name == "ASSAULT"
        assert d.objective_priority > 0.9


def test_hold_bots_never_grab_even_if_a_job_were_stamped():
    d = _dropped(HOLD)
    assert d.role == "order_hold" and d.position == (120.0, 130.0, 40.0)
    assert HOLD not in PICKUP_CODES


def test_the_grab_only_applies_while_the_intel_really_lies_dropped():
    home = _decide(DEFEND, params=_jobp(JOB_PICKUP), enemy_intel_at=DROP_AT, enemy_intel_state=0)
    assert home.role == "order_defend"                                       # state 0: at home, not dropped
    carried = _decide(DEFEND, params=_jobp(JOB_PICKUP), enemy_intel_at=DROP_AT, enemy_intel_state=2,
                      enemy_intel_carrier=OTHER)
    assert carried.role == "order_defend"                                    # somebody holds it now
    assert _dropped(DEFEND, job=JOB_NONE).role == "order_defend"            # not one of the chosen


def test_a_bot_that_grabs_the_intel_takes_it_home_first_then_resumes_its_order():
    human = _player(HUMAN, BLUE, (200.0, 210.0, 40.0), is_bot=False)
    holding = _player(BOT, BLUE, (140.0, 140.0, 40.0), carried=5)
    _, frame = _ctf((_order(ATTACK, params=_jobp(JOB_PICKUP)),), enemy_intel_carrier=BOT,
                    enemy_intel_at=DROP_AT, enemy_intel_state=2)
    d = ModePolicyMemory().decide(replace(frame, players=(holding, human)), holding)
    assert d.role == "order_capture" and d.position == BASE and d.sprint        # the carrier rule wins
    after = _decide(DEFEND, enemy_intel_state=0)                                # captured: intel home, no job
    assert after.role == "order_defend" and after.position == BASE              # the order resumes


def test_unchosen_orders_keep_their_roles_while_the_intel_lies_dropped():
    for code, role in ((DEFEND, "order_defend"), (PROTECT, "order_protect_guard"), (HOLD, "order_hold")):
        assert _dropped(code, job=JOB_NONE).role == role, code
    assert _dropped(FOLLOW, job=JOB_NONE).role == "order_follow_escort"


def test_attack_bots_not_sent_still_head_for_the_dropped_intel():
    d = _dropped(ATTACK, job=JOB_NONE)
    assert d.role == "order_attack" and d.position == DROP_AT                   # the intel is their objective anyway


def test_escorts_head_for_the_dropped_intel_when_their_carrier_falls():
    d = _dropped(ESCORT, job=JOB_NONE)
    assert d.role == "order_escort_dropped" and d.position == DROP_AT
    assert d.position != (200.0, 210.0, 40.0)                                   # not the issuer
    assert d.posture.name == "ESCORT"


def test_escorts_never_target_a_dead_carriers_body():
    body = (300.0, 300.0, 40.0)
    dead = _player(OTHER, BLUE, body, carried=3, alive=False)                   # still listed as the holder this frame
    for code, params in ((ESCORT, ()), (ATTACK, _jobp(JOB_ESCORT)), (PROTECT, ())):
        d = _decide(code, params=params, enemy_intel_carrier=OTHER, players=(dead,))
        assert d.position != body, code
    live = _player(OTHER, BLUE, body, carried=3)
    assert _decide(ESCORT, enemy_intel_carrier=OTHER, players=(live,)).position == body   # a living carrier is escorted


def test_escorts_do_not_follow_the_issuers_respawn_either_while_intel_lies_dropped():
    d = _dropped(ESCORT, job=JOB_NONE)
    assert d.role != "order_escort_issuer"


def test_describe_role_reads_the_new_pickup_roles():
    assert describe_role("order_pickup") == "grabbing the dropped intel"
    assert describe_role("order_escort_dropped") == "heading for the dropped intel"


# --- the book's selection

def _near(bot_id, distance):
    return (DROP_AT[0] + distance, DROP_AT[1], DROP_AT[2])


def _pick_book(orders, **kw):
    """A book plus the chosen grabbers for ``orders`` ({bot id: (code, half)}) at given distances."""
    book = PlayerOrderBook()
    for bot_id, (code, half, _distance) in orders.items():
        book.issue(bot_id, PlayerOrder(code, BLUE, HUMAN, (0.0, 0.0, 0.0), half))
    positions = {bot_id: _near(bot_id, d) for bot_id, (_c, _h, d) in orders.items()}
    kw.setdefault("alive", lambda bot_id: True)
    kw.setdefault("count", 2)
    kw.setdefault("radius", 30.0)
    book.sync_pickups(BLUE, kw.pop("dropped_at", DROP_AT), positions, **kw)
    return book, positions


def _chosen(book, ids):
    return sorted(i for i in ids if book.job_of(i) == JOB_PICKUP)


def test_the_nearest_count_bots_within_the_radius_are_chosen_ties_by_id():
    orders = {1: (ATTACK, 0, 25.0), 2: (DEFEND, 0, 5.0), 3: (PROTECT, 0, 10.0), 4: (ESCORT, 0, 5.0),
              5: (FOLLOW, 0, 12.0), 6: (ATTACK, 0, 40.0)}
    book, _ = _pick_book(orders)
    assert _chosen(book, orders) == [2, 4]                                  # 2 and 4 tie at 5 blocks
    book, _ = _pick_book(orders, count=4)
    assert _chosen(book, orders) == [2, 3, 4, 5]                            # then 10 and 12; 25 is 5th, 40 is out
    book, _ = _pick_book(orders, count=9)
    assert _chosen(book, orders) == [1, 2, 3, 4, 5]                         # 6 is beyond the 30-block radius
    book, _ = _pick_book(orders, radius=8.0)
    assert _chosen(book, orders) == [2, 4]


def test_hold_free_dead_and_other_orders_are_never_chosen():
    from server.bot_ai.player_orders import FREE
    orders = {1: (HOLD, 0, 1.0), 2: (FREE, 1, 2.0), 3: (DEFEND, 0, 3.0), 4: (ATTACK, 0, 4.0), 5: (ATTACK, 0, 6.0)}
    book, _ = _pick_book(orders, alive=lambda bot_id: bot_id != 3)
    assert _chosen(book, orders) == [4, 5]                                  # hold, free and the dead bot are skipped


def test_nothing_is_chosen_when_nothing_is_dropped_or_the_count_is_zero():
    orders = {1: (ATTACK, 0, 1.0), 2: (ATTACK, 0, 2.0)}
    assert _chosen(*[_pick_book(orders, dropped_at=None)[0], orders]) == []
    assert _chosen(*[_pick_book(orders, count=0)[0], orders]) == []


def test_each_split_half_picks_its_own_grabbers():
    orders = {1: (ATTACK, 1, 4.0), 2: (ATTACK, 1, 5.0), 3: (ATTACK, 1, 6.0),
              4: (DEFEND, 2, 2.0), 5: (DEFEND, 2, 3.0), 6: (DEFEND, 2, 9.0), 7: (HOLD, 2, 1.0)}
    book, _ = _pick_book(orders)
    assert _chosen(book, orders) == [1, 2, 4, 5]                            # two from each half, hold excluded
    plain = {1: (ATTACK, 0, 4.0), 2: (DEFEND, 0, 5.0), 3: (ATTACK, 0, 6.0)}
    assert len(_chosen(*[_pick_book(plain)[0], plain])) == 2                # an unsplit team is one group


def test_grabbers_stay_chosen_while_eligible_and_are_replaced_when_they_die():
    orders = {1: (ATTACK, 0, 5.0), 2: (ATTACK, 0, 6.0), 3: (ATTACK, 0, 20.0)}
    book, positions = _pick_book(orders)
    assert _chosen(book, orders) == [1, 2]
    positions[3] = _near(3, 1.0)                                            # a newcomer is now nearest of all
    book.sync_pickups(BLUE, DROP_AT, positions, lambda i: True, 2, 30.0)
    assert _chosen(book, orders) == [1, 2]                                  # the runners keep their job
    book.sync_pickups(BLUE, DROP_AT, positions, lambda i: i != 1, 2, 30.0)  # bot 1 dies
    assert _chosen(book, orders) == [2, 3]                                  # the next nearest takes over
    positions[2] = _near(2, 60.0)                                           # bot 2 is thrown far away
    book.sync_pickups(BLUE, DROP_AT, positions, lambda i: i != 1, 2, 30.0)
    assert _chosen(book, orders) == [3]


def test_the_job_is_published_on_the_order_objective_and_cleared_with_the_drop():
    orders = {1: (ATTACK, 0, 5.0), 2: (ATTACK, 0, 6.0), 3: (ATTACK, 0, 7.0)}
    book, positions = _pick_book(orders)
    jobs = {o.carrier_id: int(o.params[P_JOB]) for o in book.objectives(orders, (6.0, 40.0, 40.0, 1.0)[:P_JOB])}
    assert jobs == {1: JOB_PICKUP, 2: JOB_PICKUP, 3: JOB_NONE}
    book.sync_pickups(BLUE, None, positions, lambda i: True, 2, 30.0)       # picked up / returned
    assert _chosen(book, orders) == []


# --- the director

def _drop_director(*bot_specs, config=None, drop_team=TEAM2, holders=None):
    """TEAM1 bots as (id, distance from the drop, order) and the enemy (TEAM2) intel lying dropped."""
    bots = [_bot(i, f"Bot{i}", TEAM1, pos=_near(i, d)) for i, d, _o in bot_specs]
    human = _human(9)
    director = _director(*bots, players=[human], config=config)
    director.server.mode = SimpleNamespace(
        intel_holder=holders if holders is not None else {}, intel_positions={drop_team: DROP_AT},
        intel_drop_time={drop_team: 5.0})
    for (i, _d, order) in bot_specs:
        if order:
            director.give_order(order, human, (f"bot{i}",))
    return director, bots


def _grabbers(director):
    return sorted(o.carrier_id for o in director._objectives_player_orders(None) if int(o.params[P_JOB]) == JOB_PICKUP)


def test_the_director_sends_the_nearest_two_eligible_bots_within_30_blocks():
    director, _ = _drop_director((1, 4.0, "hold"), (2, 5.0, "attack"), (3, 9.0, "defend"), (4, 12.0, "protect"),
                                 (5, 20.0, "follow"), (6, 31.0, "attack"), (7, 6.0, None))
    assert _grabbers(director) == [2, 3]                      # hold (1) and the unordered bot (7) are skipped
    director.give_order("free", _human(9), ("bot2",))        # one is released: the next nearest takes over
    assert _grabbers(director) == [3, 4]


def test_the_director_reads_count_and_radius_from_config():
    config = SimpleNamespace(order_pickup_count=3, order_pickup_radius=15.0)
    director, _ = _drop_director((1, 4.0, "attack"), (2, 8.0, "attack"), (3, 14.0, "attack"), (4, 16.0, "attack"), config=config)
    assert _grabbers(director) == [1, 2, 3]
    off, _ = _drop_director((1, 4.0, "attack"), config=SimpleNamespace(order_pickup_count=0))
    assert _grabbers(off) == []


def test_only_the_enemys_dropped_intel_sends_bots_never_our_own():
    director, _ = _drop_director((1, 4.0, "attack"), (2, 5.0, "defend"), drop_team=TEAM1)   # OUR intel is dropped
    assert _grabbers(director) == []
    director, _ = _drop_director((1, 4.0, "attack"), (2, 5.0, "defend"), holders={TEAM2: SimpleNamespace(id=30)})
    assert _grabbers(director) == []                          # picked up: nobody is sent any more
    director.server.mode.intel_holder = {}
    assert _grabbers(director) == [1, 2]                      # dropped again
    director.server.mode.intel_drop_time = {TEAM2: 0.0}       # returned home
    assert _grabbers(director) == []


def test_a_split_sends_two_from_each_half_and_never_a_free_or_hold_half():
    director, _ = _drop_director((1, 3.0, None), (2, 4.0, None), (3, 5.0, None), (4, 6.0, None),
                                 (5, 7.0, None), (6, 8.0, None))
    director.give_split("attack", "defend", _human(9))
    assert _grabbers(director) == [1, 2, 4, 5]                # halves {1,2,3} and {4,5,6}: two each
    director.give_order("free", _human(9))
    director.give_split("hold", "attack", _human(9))
    assert _grabbers(director) == [4, 5]                      # the hold half is skipped, the attack half grabs
    director.give_order("free", _human(9))
    director.give_split("free", "defend", _human(9))
    assert _grabbers(director) == [4, 5]                      # a free half is skipped too


def test_a_dead_bot_is_not_sent_and_a_grabber_that_dies_is_replaced():
    director, bots = _drop_director((1, 4.0, "attack"), (2, 5.0, "attack"), (3, 6.0, "attack"))
    assert _grabbers(director) == [1, 2]
    bots[0].alive = False
    assert _grabbers(director) == [2, 3]


def test_the_other_team_goes_for_a_dropped_team1_intel():
    bots = [_bot(i, f"Grn{i}", TEAM2, pos=_near(i, 4.0 + i)) for i in range(11, 14)]
    director = _director(*bots, players=[_human(9, team=TEAM2)])
    director.server.mode = SimpleNamespace(intel_holder={}, intel_positions={TEAM1: DROP_AT}, intel_drop_time={TEAM1: 5.0})
    director.give_order("attack", _human(9, team=TEAM2))
    assert _grabbers(director) == [11, 12]


# --- the worker dives on the grab

def test_a_chat_ordered_grab_dives_for_the_intel_from_any_distance_even_beside_a_defender():
    from tests.test_bot_player_orders import _fighter as fighter
    for distance in (6.0, 25.0, 45.0):
        intel = (10.5 + distance, 10.5, _HEAD)
        me = fighter(1, BLUE, 10.5, 10.5)
        enemy = fighter(2, GREEN, intel[0] + 3.0, 14.5)
        world = _TacticalWorld(route_step=RouteStep(intel, MovementAffordance.WALK))
        decision = ModeBotDecision(intel, "order_pickup", sprint=True, arrival_radius=1.0,
                                   posture=ModeBotPosture.ASSAULT, objective_priority=0.97, engagement_radius=40.0)
        intent = SimpleBotBrain(world)._combat_intent(
            _frame(me, enemy), me, enemy, _BotState(1, 1, me.life_id), _profile(), 100.0, decision)
        assert intent.debug_role == "combat_objective:order_pickup", distance
        assert intent.movement.direction[0] > 0.9, distance                 # straight at the intel


# -------------------------------------------------------------- compact status format

from commands.bot_orders import compact_status, parse_full


def _rows(*groups):
    """Status rows from (order, half, [names]) groups, in the order given."""
    return [(name, order, half, "idle") for order, half, names in groups for name in names]


def test_compact_status_groups_by_order_and_half_one_segment_each():
    rows = _rows(("hold", 0, ["Rex", "Ann"]), ("attack", 1, ["Zed", "Moe"]))
    assert compact_status("Blue", rows) == ["Blue hold: Rex, Ann | attack 1: Zed, Moe"]       # the example from the notes


def test_compact_status_gathers_scattered_bots_and_puts_free_last():
    rows = [("A", "free", 0, ""), ("B", "hold", 0, ""), ("C", "attack", 2, ""), ("D", "hold", 0, ""), ("E", "free", 0, "")]
    assert compact_status("Green", rows) == ["Green hold: B, D | attack 2: C | free: A, E"]


def test_compact_status_wraps_at_the_chat_width_and_repeats_the_label():
    names = [f"Bot{n:02d}" for n in range(1, 16)]
    rows = _rows(("hold", 0, names[:8]), ("attack", 1, names[8:12]), ("protect", 2, names[12:]))
    lines = compact_status("Blue", rows)
    assert all(len(line) <= 90 for line in lines) and all(line.startswith("Blue ") for line in lines)
    joined = " ".join(lines)
    assert all(name in joined for name in names)                                              # nobody is dropped
    assert 2 <= len(lines) <= 3


def test_a_group_too_long_for_one_line_is_split_across_lines_with_its_header():
    names = [f"LongBotName{n:02d}" for n in range(1, 13)]                                     # ~13 chars each
    lines = compact_status("Blue", _rows(("hold", 0, names)))
    assert len(lines) >= 2 and all(len(line) <= 90 for line in lines)
    assert all(line.startswith("Blue hold: ") for line in lines)
    assert sum(line.count("LongBotName") for line in lines) == 12


def test_two_teams_of_fifteen_bots_fit_in_about_ten_chat_lines_even_with_many_groups():
    worst = []
    for label, base in (("Blue", 0), ("Green", 100)):
        orders = ["hold", "attack", "defend", "protect", "escort", "follow", "free"]
        rows = [(f"B{base + n}x", orders[n % 7], (n % 3), "") for n in range(15)]            # 15 bots, up to 15 groups
        worst += compact_status(label, rows)
    assert len(worst) <= 10, len(worst)
    best = compact_status("Blue", _rows(("hold", 0, [f"Pancake{n:02d}" for n in range(15)])))
    assert len(best) <= 3                                   # 15 long names fill three 90-character lines


def test_parse_full_takes_the_word_out_of_the_names():
    assert parse_full(["full"]) == (True, [])
    assert parse_full(["FULL", "rex"]) == (True, ["rex"])
    assert parse_full(["rex", "full", "ann"]) == (True, ["rex", "ann"])
    assert parse_full(["rex"]) == (False, ["rex"])
    assert parse_full([]) == (False, [])


def test_a_team_member_always_gets_full_detail_and_full_changes_nothing_for_them(monkeypatch):
    sent = _commands(monkeypatch)
    member = _human(9, team=TEAM1)
    bots, director = _two_teams(member)
    server = _server(director)
    asyncio.run(handle_command(server, member, "status"))
    first = list(sent)
    sent.clear()
    asyncio.run(handle_command(server, member, "st full"))
    assert sent == first and len(first) == 4 and first[0] == "Blu1 [free]: idle"


def test_the_docs_describe_the_compact_status():
    guide = (Path(__file__).resolve().parents[1] / "docs" / "BOT_CHAT_ORDERS.md").read_text(encoding="utf-8")
    assert "/status full" in guide and "/st full" in guide and "Blue hold: Rex, Ann | attack 1: Zed, Moe" in guide
