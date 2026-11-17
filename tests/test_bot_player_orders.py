"""Chat orders (/follow /hold /attack /defend /protect /escort /status) steer bots."""

from __future__ import annotations

import time
from dataclasses import replace

from server.bot_ai.messages import ObjectiveSnapshot, PerceptionFrame, PlayerSnapshot
from server.bot_ai.player_orders import (
    ATTACK, DEFEND, ESCORT, FOLLOW, HOLD, JOB_ESCORT, JOB_NONE, JOB_PUSH, PROTECT, PlayerOrder,
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
         players=(), bot_at=(100.0, 100.0, 40.0), bot_id=BOT):
    bot = _player(bot_id, BLUE, bot_at)
    objectives = [
        ObjectiveSnapshot("ctf_base", BLUE, BASE),
        ObjectiveSnapshot("ctf_base", GREEN, ENEMY_BASE),
        ObjectiveSnapshot("ctf_intel", BLUE, own_intel_at, carrier_id=own_intel_carrier),
        ObjectiveSnapshot("ctf_intel", GREEN, (402.0, 402.0, 40.0), carrier_id=enemy_intel_carrier),
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
    objectives = tuple(book.objectives(ids, (6.0, 40.0)))
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
    d = _decide(ATTACK, params=(6.0, 40.0, JOB_ESCORT), enemy_intel_carrier=OTHER, players=(carrier,))
    assert d.role == "order_attack_escort" and d.position == (300.0, 300.0, 40.0)
    assert d.posture.name == "ESCORT"


def test_attack_push_job_keeps_attacking_the_enemy_base_not_the_carrier():
    carrier = _player(OTHER, BLUE, (300.0, 300.0, 40.0), carried=3)
    for params in ((6.0, 40.0, JOB_PUSH), (6.0, 40.0), ()):       # no job = plain attack
        d = _decide(ATTACK, params=params, enemy_intel_carrier=OTHER, players=(carrier,))
        assert d.role == "order_attack" and d.position == ENEMY_BASE, params
        assert d.posture.name == "ASSAULT"


def test_attack_escort_finds_a_carrier_outside_the_perception_cohort():
    far = (350.0, 120.0, 40.0)
    bot, frame = _ctf((_order(ATTACK, params=(6.0, 40.0, JOB_ESCORT)),))
    objectives = tuple(ObjectiveSnapshot("ctf_intel", GREEN, far, carrier_id=OTHER)
                       if o.kind == "ctf_intel" and o.team == GREEN else o for o in frame.objectives)
    d = ModePolicyMemory().decide(replace(frame, objectives=objectives), bot)
    assert d.role == "order_attack_escort" and d.position == far


def test_when_the_carrier_is_gone_every_attack_bot_attacks_the_intel_again():
    carrier = _player(OTHER, BLUE, (300.0, 300.0, 40.0), carried=3)
    for job in (JOB_ESCORT, JOB_PUSH):                            # a stale job changes nothing
        reset = _decide(ATTACK, params=(6.0, 40.0, job), enemy_intel_carrier=-1, players=(carrier,))
        assert reset.role == "order_attack" and reset.position == (402.0, 402.0, 40.0)


def test_attack_with_the_intel_in_its_own_hands_still_goes_home():
    bot = _player(BOT, BLUE, (100.0, 100.0, 40.0), carried=5)
    _, frame = _ctf((_order(ATTACK, params=(6.0, 40.0, JOB_ESCORT)),), enemy_intel_carrier=BOT)
    d = ModePolicyMemory().decide(replace(frame, players=(bot,)), bot)
    assert d.role == "order_capture" and d.position == BASE


def test_attack_bots_split_by_id_between_escort_and_attack():
    roles, _ = _split_roles(ATTACK, [11, 12, 13, 14], lambda n: attack_escort_count(n, 0.5))
    assert roles == {11: "order_attack_escort", 12: "order_attack_escort",
                     13: "order_attack", 14: "order_attack"}


def test_a_single_attack_bot_attacks_and_the_ratio_always_leaves_an_attacker():
    assert _split_roles(ATTACK, [11], lambda n: attack_escort_count(n, 0.5))[0] == {11: "order_attack"}
    assert _split_roles(ATTACK, [11], lambda n: attack_escort_count(n, 1.0))[0] == {11: "order_attack"}
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
    assert director._objectives_player_orders(None)[0].params == (9.0, 55.0, 0.0)


def test_director_without_config_falls_back_to_the_defaults():
    director = _director(_bot(1, "Rex", TEAM1))
    director.give_order("hold", SimpleNamespace(id=9, team=TEAM1))
    assert director._objectives_player_orders(None)[0].params == (6.0, 40.0, 0.0)


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
    director.give_order("escort", issuer, ("rex",))
    director.give_order("hold", issuer, ("ada",))
    issuer.alive = issuer.spawned = False
    assert {o.carrier_id: o.state for o in director._objectives_player_orders(None)} == {1: ESCORT, 2: HOLD}


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
        ("Rex", "hold", "holding position (in position)"),
        ("Ada", "protect", "dead, waiting to respawn"),
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
    assert sent[-1] == "Join a team to command bots"


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
    asyncio.run(handle_command(server, spectator, "status"))
    assert sent == ["Join a team to command bots"]
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
