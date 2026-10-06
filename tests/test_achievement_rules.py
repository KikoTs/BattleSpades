"""Per-achievement rules at the engine level: each boundary, each scope.

One qualifying event is fed at a time, so a threshold test fails if the
rule counts too early, too late or twice. docs/ACHIEVEMENTS.md states the
reading of every description tested here.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.constants as C

from server import achievements
from server.achievements import BY_NAME
from server.map_metadata import AchievementRegion, load_map_metadata
from tests.achievement_helpers import (
    HEADSHOT, MELEE, TEAM1, TEAM2, WEAPON, Clock, announcements, kill, make_player,
    make_server, progress, unlocked,
)

ROOT = Path(__file__).resolve().parents[1]
FALL = int(C.FALL_KILL)


def _pair(mode_code="tdm", **server_options):
    server = make_server(mode_code, **server_options)
    return server, make_player(server, 1, TEAM1), make_player(server, 2, TEAM2)


def _zombie_round(server, zombies, *, last_survivor=None):
    """Zombie mode with an outbreak in progress (zombies = TEAM1)."""
    server.achievements.zombie_round_started(zombies, TEAM1, TEAM2)
    server.mode.last_survivor_id = getattr(last_survivor, "id", None)


# ---------------------------------------------------------------------------
# Lifetime kill counters: threshold - 1 does not unlock, the threshold does
# ---------------------------------------------------------------------------

def _melee(tool):
    def prepare(server, killer, victim):
        killer.tool = int(tool)
        return MELEE
    return prepare


def _headshot(tool):
    def prepare(server, killer, victim):
        killer.tool = int(tool)
        return HEADSHOT
    return prepare


def _zombie_victim(tool, kill_type):
    def prepare(server, killer, victim):
        # The killer is a survivor (TEAM2), the victim a zombie (TEAM1).
        killer.team, victim.team = TEAM2, TEAM1
        if server.achievements._zombie_teams is None:
            _zombie_round(server, [victim])
        killer.tool = int(tool)
        return kill_type
    return prepare


def _jetpack(tool, kill_type=WEAPON):
    def prepare(server, killer, victim):
        killer.jetpack_active = True
        killer.tool = int(tool)
        return kill_type
    return prepare


def _low_health(server, killer, victim):
    killer.health = 9
    return WEAPON


def _zombie_in_water(server, killer, victim):
    if server.achievements._zombie_teams is None:
        _zombie_round(server, [killer])
    killer.wade = True
    killer.tool = int(C.ZOMBIEHAND_TOOL)
    return MELEE


def _with_intel(server, killer, victim):
    server.mode.mode_code = "cctf"
    server.mode.intel_holder = {TEAM1: None, TEAM2: killer}
    return WEAPON


KILL_COUNTERS = [
    ("spade_kill", _melee(C.SPADE_TOOL)),
    ("spade_kill", _melee(C.CLASSIC_SPADE_TOOL)),
    ("pickaxe_kill", _melee(C.PICKAXE_TOOL)),
    ("knife_zombies", _zombie_victim(C.KNIFE_TOOL, MELEE)),
    ("pistol_zombie_kill", _zombie_victim(C.PISTOL_TOOL, HEADSHOT)),
    ("pistol_zombie_kill", _zombie_victim(C.SNUB_PISTOL_TOOL, HEADSHOT)),
    ("sniper_kill", _headshot(C.SNIPER_TOOL)),
    ("shotgun_headshots", _headshot(C.SHOTGUN_TOOL)),
    ("classic_rifle_headshots", _headshot(C.RIFLE_TOOL)),
    ("jetpack_kill", _jetpack(C.RIFLE_TOOL)),
    ("jetpack_smg_kill", _jetpack(C.SMG_TOOL)),
    ("jetpack_smg_kill", _jetpack(C.SMG_TOOL, HEADSHOT)),
    ("low_health_killing", _low_health),
    ("zombie_kills_in_water_ach", _zombie_in_water),
    ("classic_kills_with_intel", _with_intel),
]


@pytest.mark.parametrize(
    "api_name,prepare", KILL_COUNTERS,
    ids=[f"{name}-{index}" for index, (name, _p) in enumerate(KILL_COUNTERS)],
)
def test_kill_counter_unlocks_exactly_at_its_threshold(api_name, prepare):
    row = BY_NAME[api_name]
    server, killer, victim = _pair()
    for done in range(1, row.threshold + 1):
        kill_type = prepare(server, killer, victim)
        # Keep the kill streak below five: it has achievements of its own.
        killer.kill_streak = 0
        kill(server, killer, victim, kill_type)
        assert progress(server, killer, row.stat) == done
        assert (api_name in unlocked(server, killer)) is (done == row.threshold), done
    names = [parameters[1] for _id, parameters in announcements(server)]
    assert names.count(row.display_name) == 1
    # One more changes the counter, not the unlock or the announcements.
    killer.kill_streak = 0
    kill(server, killer, victim, prepare(server, killer, victim))
    assert progress(server, killer, row.stat) == row.threshold + 1
    assert [p[1] for _id, p in announcements(server)].count(row.display_name) == 1


NOT_COUNTED = [
    # (statistic, why, prepare)
    ("spade_kill_count", "super spade is its own tool", _melee(C.SUPERSPADE_TOOL)),
    ("spade_kill_count", "a spade shot that is not melee", lambda s, k, v: (
        setattr(k, "tool", int(C.SPADE_TOOL)), WEAPON)[1]),
    ("pickaxe_kill_count", "knife", _melee(C.KNIFE_TOOL)),
    ("sniper_kill_count", "body shot", lambda s, k, v: (
        setattr(k, "tool", int(C.SNIPER_TOOL)), WEAPON)[1]),
    ("sniper_kill_count", "the semi-auto is another rifle", _headshot(C.SNIPER2_TOOL)),
    ("shotgun_headshots_count", "shotgun 2", _headshot(C.SHOTGUN2_TOOL)),
    ("classic_rifle_headshot_count", "sniper", _headshot(C.SNIPER_TOOL)),
    ("jetpack_smg_kill_count", "minigun", _jetpack(C.MINIGUN_TOOL)),
    ("jetpack_smg_kill_count", "smg grenade", _jetpack(C.SMG_TOOL, int(C.GRENADE_KILL))),
]


@pytest.mark.parametrize(
    "stat,_why,prepare", NOT_COUNTED, ids=[f"{row[0]}:{row[1]}" for row in NOT_COUNTED],
)
def test_near_misses_do_not_count(stat, _why, prepare):
    server, killer, victim = _pair()
    kill(server, killer, victim, prepare(server, killer, victim))
    assert progress(server, killer, stat) == 0


def test_knife_and_pistol_need_a_zombie_victim():
    server, killer, victim = _pair()
    killer.tool = int(C.KNIFE_TOOL)
    kill(server, killer, victim, MELEE)
    killer.tool = int(C.PISTOL_TOOL)
    kill(server, killer, victim, HEADSHOT)
    assert progress(server, killer, "knife_zombie_count") == 0
    assert progress(server, killer, "pistol_zombie_kill_count") == 0
    # A pistol body shot on a zombie is not a shot in the face.
    survivor = make_player(server, 3, TEAM2, tool=int(C.PISTOL_TOOL))
    zombie = make_player(server, 4, TEAM1)
    _zombie_round(server, [zombie])
    kill(server, survivor, zombie, WEAPON)
    assert progress(server, survivor, "pistol_zombie_kill_count") == 0
    kill(server, survivor, zombie, HEADSHOT)
    assert progress(server, survivor, "pistol_zombie_kill_count") == 1


def test_sniper_headshots_two_tiers_on_one_counter():
    server, killer, victim = _pair()
    killer.tool = int(C.SNIPER_TOOL)
    for done in range(1, 51):
        killer.kill_streak = 0
        # A miss between kills keeps the "without missing" run out of this.
        achievements.shot_resolved(server, killer, killer.tool, False)
        kill(server, killer, victim, HEADSHOT)
        expected = {"sniper_kill"} if 25 <= done < 50 else (
            {"sniper_kill", "sniper_kill_hard"} if done == 50 else set()
        )
        assert unlocked(server, killer) == expected, done


@pytest.mark.parametrize("health,max_health,counts", [
    (10, 100, False),   # exactly 10% is not below it
    (9, 100, True),
    (1, 100, True),
    (0, 100, False),    # the dead do not get it
    (19, 200, True),    # a 200 HP VIP: below 10% of its own maximum
    (20, 200, False),
    (4, 50, True),
    (5, 50, False),
])
def test_low_health_is_below_ten_percent_of_the_killers_maximum(health, max_health, counts):
    server, killer, victim = _pair()
    killer.health, killer.max_health = health, max_health
    kill(server, killer, victim)
    assert progress(server, killer, "low_health_kills") == int(counts)


def test_low_health_kills_accumulate_over_rounds():
    server, killer, victim = _pair()
    killer.health = 5
    for _ in range(9):
        killer.kill_streak = 0
        kill(server, killer, victim)
    achievements.match_ended(server, TEAM1)
    achievements.match_started(server)
    assert "low_health_killing" not in unlocked(server, killer)
    killer.kill_streak = 0
    kill(server, killer, victim)
    assert "low_health_killing" in unlocked(server, killer)


def test_jetpack_kills_need_the_jetpack_running():
    server, killer, victim = _pair()
    killer.tool = int(C.SMG_TOOL)
    kill(server, killer, victim)
    assert progress(server, killer, "jetpack_kill_count") == 0
    killer.jetpack_active = True
    kill(server, killer, victim, int(C.GRENADE_KILL))
    # Any kill while flying is a jetpack kill; only the SMG's bullets are
    # a jetpack drive-by.
    assert progress(server, killer, "jetpack_kill_count") == 1
    assert progress(server, killer, "jetpack_smg_kill_count") == 0


def test_killing_a_jetpacking_enemy():
    server, killer, victim = _pair()
    kill(server, killer, victim, jetpacking=False)
    assert "jetpack_killed_using" not in unlocked(server, killer)
    kill(server, killer, victim, jetpacking=True)
    assert "jetpack_killed_using" in unlocked(server, killer)


# ---------------------------------------------------------------------------
# Kills that never count
# ---------------------------------------------------------------------------

def test_team_kills_suicides_transitions_and_spectators_never_count():
    server = make_server()
    killer = make_player(server, 1, TEAM1, tool=int(C.SPADE_TOOL))
    mate = make_player(server, 2, TEAM1)
    enemy = make_player(server, 3, TEAM2)
    spectator = make_player(server, 4, int(C.TEAM_SPECTATOR))
    kill(server, killer, mate, MELEE)
    kill(server, killer, killer, MELEE)
    achievements.died(server, enemy, None, MELEE, False)
    kill(server, killer, spectator, MELEE)
    for transition in (C.TEAM_CHANGE_KILL, C.FORCED_TEAM_CHANGE_KILL, C.CLASS_CHANGE_KILL):
        kill(server, killer, enemy, int(transition))
    assert progress(server, killer, "spade_kill_count") == 0
    assert server.achievements.tracker(killer).round.get("kills", 0) == 0
    kill(server, killer, enemy, MELEE)
    assert progress(server, killer, "spade_kill_count") == 1


@pytest.mark.parametrize("state", ["ended", "ugc_runtime", "ugc", "tut", "tutorial"])
def test_nothing_counts_after_the_round_or_outside_real_matches(state):
    server, killer, victim = _pair()
    killer.tool = int(C.SPADE_TOOL)
    if state == "ended":
        server.mode.ended = True
    elif state == "ugc_runtime":
        server.config.ugc_runtime = True
    else:
        server.config.default_mode = state
    for _ in range(10):
        kill(server, killer, victim, MELEE)
    achievements.tick(server, 1.0)
    assert progress(server, killer, "spade_kill_count") == 0
    assert unlocked(server, killer) == set()


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------

def test_kill_streaks_unlock_at_five_ten_and_fifteen_without_dying():
    server, killer, victim = _pair()
    expected = {5: "misc_five_in_a_row", 10: "misc_ten_in_a_row", 15: "misc_fifteen_in_a_row"}
    earned = set()
    for streak in range(1, 16):
        kill(server, killer, victim)
        earned |= {expected[streak]} if streak in expected else set()
        assert unlocked(server, killer) == earned, streak
    assert [p[1] for _id, p in announcements(server)] == ["Five Alive", "Streaker", "OMG Hax!"]


def test_a_death_restarts_the_streak():
    server, killer, victim = _pair()
    for _ in range(4):
        kill(server, killer, victim)
    killer.kill_streak = 0  # Player.die resets it
    for _ in range(4):
        kill(server, killer, victim)
    assert unlocked(server, killer) == set()
    kill(server, killer, victim)
    assert unlocked(server, killer) == {"misc_five_in_a_row"}


# ---------------------------------------------------------------------------
# Sniper accuracy and the semi-auto's rapid kills
# ---------------------------------------------------------------------------

def _sniper_kill(server, killer, victim, headshot=True):
    kill(server, killer, victim, HEADSHOT if headshot else WEAPON)
    achievements.shot_resolved(server, killer, killer.tool, True)


def test_three_and_six_sniper_kills_without_a_miss():
    server, killer, victim = _pair()
    killer.tool = int(C.SNIPER_TOOL)
    for done in range(1, 7):
        killer.kill_streak = 0
        _sniper_kill(server, killer, victim, headshot=done % 2 == 0)
        expected = set()
        if done >= 3:
            expected.add("sniper_accuracy")
        if done >= 6:
            expected.add("sniper_accuracy_hard")
        assert unlocked(server, killer) & {"sniper_accuracy", "sniper_accuracy_hard"} == expected


def test_a_missed_sniper_shot_restarts_the_run():
    server, killer, victim = _pair()
    killer.tool = int(C.SNIPER_TOOL)
    _sniper_kill(server, killer, victim)
    _sniper_kill(server, killer, victim)
    achievements.shot_resolved(server, killer, killer.tool, False)
    _sniper_kill(server, killer, victim)
    _sniper_kill(server, killer, victim)
    assert "sniper_accuracy" not in unlocked(server, killer)
    # A hit that does not kill is not a miss; another gun's miss is not either.
    achievements.shot_resolved(server, killer, killer.tool, True)
    achievements.shot_resolved(server, killer, int(C.PISTOL_TOOL), False)
    _sniper_kill(server, killer, victim)
    assert "sniper_accuracy" in unlocked(server, killer)


def test_sniper_accuracy_ignores_other_weapons_kills():
    server, killer, victim = _pair()
    killer.tool = int(C.SNIPER2_TOOL)
    for _ in range(3):
        kill(server, killer, victim, HEADSHOT)
    killer.tool = int(C.SNIPER_TOOL)
    kill(server, killer, victim, int(C.GRENADE_KILL))
    assert "sniper_accuracy" not in unlocked(server, killer)


@pytest.mark.parametrize("gap,unlocks", [(10.0, True), (10.01, False)])
def test_three_semi_auto_kills_within_twenty_seconds(gap, unlocks):
    clock = Clock()
    server, killer, victim = _pair(clock=clock)
    killer.tool = int(C.SNIPER2_TOOL)
    for index in range(3):
        killer.kill_streak = 0
        kill(server, killer, victim, HEADSHOT if index else WEAPON)
        clock.now += gap
    # Three kills spanning 2 * gap seconds.
    assert ("sniper2_rapid_kill" in unlocked(server, killer)) is unlocks
    assert float(C.SNIPER2_RAPID_KILL_ACHIEVE_TIME) == 20.0
    assert int(C.SNIPER2_RAPID_KILL_ACHIEVE_COUNT) == 3


def test_rapid_kills_are_the_semi_autos_only():
    clock = Clock()
    server, killer, victim = _pair(clock=clock)
    killer.tool = int(C.SNIPER_TOOL)
    for _ in range(3):
        achievements.shot_resolved(server, killer, killer.tool, False)
        kill(server, killer, victim, HEADSHOT)
    assert "sniper2_rapid_kill" not in unlocked(server, killer)
    # A sliding window: two old kills and one new do not make three.
    killer.tool = int(C.SNIPER2_TOOL)
    kill(server, killer, victim)
    kill(server, killer, victim)
    clock.now += 20.5
    kill(server, killer, victim)
    assert "sniper2_rapid_kill" not in unlocked(server, killer)
    clock.now += 1.0
    kill(server, killer, victim)
    kill(server, killer, victim)
    assert "sniper2_rapid_kill" in unlocked(server, killer)


# ---------------------------------------------------------------------------
# Explosions
# ---------------------------------------------------------------------------

def _blast(server, thrower, kill_type, victims, origin=(100.0, 100.0, 50.0)):
    engine = server.achievements
    scope = engine.blast_begin(thrower, int(kill_type), origin)
    for victim in victims:
        achievements.damaged(server, victim, thrower, 100, int(kill_type))
        kill(server, thrower, victim, int(kill_type))
    engine.blast_end(scope)
    return scope


def test_three_kills_with_one_explosion():
    server = make_server()
    thrower = make_player(server, 1, TEAM1)
    enemies = [make_player(server, index, TEAM2) for index in range(2, 6)]
    _blast(server, thrower, C.GRENADE_KILL, enemies[:2])
    # Two blasts of two do not add up to one of three.
    _blast(server, thrower, C.GRENADE_KILL, enemies[:2])
    assert "misc_triple_explosion" not in unlocked(server, thrower)
    _blast(server, thrower, C.ROCKET_KILL, enemies[:3])
    assert "misc_triple_explosion" in unlocked(server, thrower)
    assert server.achievements._blasts == []


def test_only_the_blasts_own_kills_count_towards_the_three():
    server = make_server()
    thrower = make_player(server, 1, TEAM1)
    other = make_player(server, 9, TEAM1)
    enemies = [make_player(server, index, TEAM2) for index in range(2, 5)]
    engine = server.achievements
    scope = engine.blast_begin(thrower, int(C.GRENADE_KILL), (0.0, 0.0, 0.0))
    kill(server, thrower, enemies[0], int(C.GRENADE_KILL))
    kill(server, thrower, enemies[1], int(C.GRENADE_KILL))
    # A teammate's rifle kill and the thrower's own fall kill in the same
    # tick are not this explosion's.
    kill(server, other, enemies[2], WEAPON)
    kill(server, thrower, enemies[2], FALL)
    engine.blast_end(scope)
    assert unlocked(server, thrower) == set()


def test_a_blast_scope_is_closed_even_when_the_blast_raises():
    server = make_server()
    thrower = make_player(server, 1, TEAM1)

    @achievements.blast_scope
    def apply_blast(self, gx, gy, gz, damage, block_damage, kill_type, thrower, **_kwargs):
        assert len(self.achievements._blasts) == 1
        assert self.achievements._blasts[0].origin == (1.0, 2.0, 3.0)
        raise RuntimeError("blast failed")

    with pytest.raises(RuntimeError):
        apply_blast(server, 1, 2, 3, 100.0, 5.0, int(C.GRENADE_KILL), thrower)
    assert server.achievements._blasts == []
    with pytest.raises(RuntimeError):
        apply_blast(server, gx=1, gy=2, gz=3, damage=1.0, block_damage=1.0,
                    kill_type=int(C.GRENADE_KILL), thrower=None)
    assert server.achievements._blasts == []

    # A server without an engine (most test fakes) passes straight through.
    @achievements.blast_scope
    def plain(self, *args, **kwargs):
        return args, kwargs

    assert plain(SimpleNamespace(), 1, 2, kill_type=3) == ((1, 2), {"kill_type": 3})
    assert plain(server, "bad") == (("bad",), {})
    assert server.achievements.faults == {}


def test_landmine_kills_count_only_for_a_reburied_mine():
    server = make_server()
    owner = make_player(server, 1, TEAM1)
    victim = make_player(server, 2, TEAM2)
    solid = set()
    server.world_manager = SimpleNamespace(get_solid=lambda x, y, z: (x, y, z) in solid)
    origin = (120.5, 80.5, 199.5)  # the mine's centre, in the cell above its support
    for done in range(1, 5):
        victim.kill_streak = owner.kill_streak = 0
        _blast(server, owner, C.LANDMINE_KILL, [victim], origin)
        assert progress(server, owner, "landmine_hidden_count") == 0
    solid.add((120, 80, 199))  # a block placed back over the mine
    for done in range(1, 6):
        owner.kill_streak = 0
        _blast(server, owner, C.LANDMINE_KILL, [victim], origin)
        assert progress(server, owner, "landmine_hidden_count") == done
        assert ("landmine_hidden" in unlocked(server, owner)) is (done == 5)
    # A launched mine is not a landmine placed on the ground.
    _blast(server, owner, C.MINE_KILL, [victim], origin)
    assert progress(server, owner, "landmine_hidden_count") == 5


@pytest.mark.parametrize("charge_z,counts", [
    (52.25, False),   # on the floor the victim stands on (its top face)
    (52.74, False),
    (52.75, True),    # the side of the floor block: half a block down
    (53.25, True),    # the underside of the floor
    (60.0, True),
    (40.0, False),    # above the victim
])
def test_dynamite_counts_from_below_the_victims_feet(charge_z, counts):
    server, thrower, victim = _pair()
    victim.x, victim.y, victim.z = 100.0, 100.0, 50.0  # feet at 52.25
    _blast(server, thrower, C.DYNAMITE_KILL, [victim], (100.5, 100.5, charge_z))
    assert progress(server, thrower, "dynamite_below_count") == int(counts)


def test_a_crouched_victims_feet_are_higher():
    server, thrower, victim = _pair()
    victim.z = 50.0
    victim.hitbox_crouched = True  # feet at 51.35
    _blast(server, thrower, C.DYNAMITE_KILL, [victim], (100.5, 100.5, 51.9))
    assert progress(server, thrower, "dynamite_below_count") == 1


def test_dynamite_below_ten_kills_and_the_fall_through_the_blown_floor():
    server, thrower, victim = _pair()
    engine = server.achievements
    for done in range(1, 10):
        thrower.kill_streak = 0
        _blast(server, thrower, C.DYNAMITE_KILL, [victim], (100.5, 100.5, 53.25))
        assert progress(server, thrower, "dynamite_below_count") == done
    assert "dynamite_below" not in unlocked(server, thrower)
    # The tenth survives the blast and dies falling through the hole.
    scope = engine.blast_begin(thrower, int(C.DYNAMITE_KILL), (100.5, 100.5, 53.25))
    achievements.damaged(server, victim, thrower, 60, int(C.DYNAMITE_KILL))
    engine.blast_end(scope)
    thrower.kill_streak = 0
    kill(server, thrower, victim, FALL)
    assert "dynamite_below" in unlocked(server, thrower)
    # A fall after a hit from above, or after a rifle hit, is not one.
    scope = engine.blast_begin(thrower, int(C.DYNAMITE_KILL), (100.5, 100.5, 40.0))
    achievements.damaged(server, victim, thrower, 60, int(C.DYNAMITE_KILL))
    engine.blast_end(scope)
    kill(server, thrower, victim, FALL)
    achievements.damaged(server, victim, thrower, 60, WEAPON)
    kill(server, thrower, victim, FALL)
    assert progress(server, thrower, "dynamite_below_count") == 10


@pytest.mark.parametrize("hit_kill,unlocks", [
    (int(C.ROCKET_KILL), True),
    (int(C.ROCKET2_KILL), True),
    (int(C.GRENADE_KILL), False),
    (WEAPON, False),
    (int(C.ROCKET_TURRET_KILL), False),
])
def test_rocket_knockback_fall(hit_kill, unlocks):
    server, killer, victim = _pair()
    achievements.damaged(server, victim, killer, 40, hit_kill)
    kill(server, killer, victim, FALL)
    assert ("rocket_fall" in unlocked(server, killer)) is unlocks


def test_rocket_fall_needs_the_rocket_to_be_the_last_hit_of_this_life():
    server, killer, victim = _pair()
    other = make_player(server, 3, TEAM1)
    achievements.damaged(server, victim, killer, 40, int(C.ROCKET_KILL))
    # The victim dies to something else; the hit belonged to that life.
    kill(server, other, victim, WEAPON)
    kill(server, killer, victim, FALL)
    assert "rocket_fall" not in unlocked(server, killer)
    # Another player's later hit takes the fall credit away from the rocket.
    achievements.damaged(server, victim, killer, 40, int(C.ROCKET_KILL))
    achievements.damaged(server, victim, other, 5, WEAPON)
    kill(server, other, victim, FALL)
    assert unlocked(server, killer) == set() == unlocked(server, other)
    # A direct rocket kill is not a fall.
    achievements.damaged(server, victim, killer, 100, int(C.ROCKET_KILL))
    kill(server, killer, victim, int(C.ROCKET_KILL))
    assert "rocket_fall" not in unlocked(server, killer)
    # Zero damage and self damage record nothing.
    achievements.damaged(server, victim, killer, 0, int(C.ROCKET_KILL))
    achievements.damaged(server, victim, victim, 10, int(C.ROCKET_KILL))
    assert server.achievements.tracker(victim).last_hit is None


# ---------------------------------------------------------------------------
# Rockets while airborne, turrets
# ---------------------------------------------------------------------------

def _rocket(server, thrower, target, *, name="rocket", kill_type=None, **extra):
    spec = SimpleNamespace(
        name=name,
        kill_type=int(C.ROCKET_KILL if kill_type is None else kill_type),
    )
    explosion = SimpleNamespace(
        spec=spec, contact_player_id=getattr(target, "id", None),
        turret=None, turret_target_id=None,
    )
    for key, value in extra.items():
        setattr(explosion, key, value)
    achievements.projectile_exploding(server, explosion, thrower)
    return explosion


def test_five_direct_rocket_hits_while_airborne():
    server, shooter, target = _pair()
    shooter.airborne = True
    for done in range(1, 6):
        _rocket(server, shooter, target, name="rocket" if done % 2 else "rocket2")
        assert progress(server, shooter, "airborne_rocket_count") == done
        assert ("airborne_rockets" in unlocked(server, shooter)) is (done == 5)


def test_rocket_hits_that_do_not_count():
    server, shooter, target = _pair()
    mate = make_player(server, 3, TEAM1)
    shooter.airborne = False
    _rocket(server, shooter, target)                      # on the ground
    shooter.airborne = True
    _rocket(server, shooter, None)                        # hit terrain, splash only
    _rocket(server, shooter, mate)                        # a teammate
    _rocket(server, shooter, shooter)                     # itself
    _rocket(server, shooter, target, name="grenade")      # not a rocket
    _rocket(server, shooter, target, name="rocket_turret_rocket")
    shooter.alive = False
    _rocket(server, shooter, target)                      # fired before dying
    assert progress(server, shooter, "airborne_rocket_count") == 0


def test_rocket_hits_on_bots_follow_the_switch():
    server = make_server(count_bot_kills=False)
    shooter = make_player(server, 1, TEAM1, airborne=True)
    bot = make_player(server, 2, TEAM2, bot=True)
    _rocket(server, shooter, bot)
    assert progress(server, shooter, "airborne_rocket_count") == 0


def _turret_shot(server, owner, turret, target, victims=(), blocks=0, hurt=()):
    """One turret rocket: aimed at ``target``, killing ``victims``."""
    engine = server.achievements
    _rocket(server, owner, None, name="rocket_turret_rocket",
            kill_type=C.ROCKET_TURRET_KILL, turret=turret,
            turret_target_id=getattr(target, "id", None))
    scope = engine.blast_begin(owner, int(C.ROCKET_TURRET_KILL), (0.0, 0.0, 0.0))
    for victim in hurt:
        achievements.damaged(server, victim, owner, 20, int(C.ROCKET_TURRET_KILL))
    for victim in victims:
        achievements.damaged(server, victim, owner, 50, int(C.ROCKET_TURRET_KILL))
        owner.kill_streak = 0
        kill(server, owner, victim, int(C.ROCKET_TURRET_KILL))
    if blocks:
        cells = [(index, 0, 0) for index in range(blocks)]
        achievements.blocks_destroyed(server, owner, cells, [], [])
    engine.blast_end(scope)


def test_ten_kills_with_one_turret():
    server, owner, victim = _pair()
    first, second = SimpleNamespace(), SimpleNamespace()
    for _ in range(9):
        _turret_shot(server, owner, first, victim, [victim])
    # A second turret's kill is not the first turret's tenth.
    _turret_shot(server, owner, second, victim, [victim])
    assert "turret_accuracy" not in unlocked(server, owner)
    assert (first.achievement_kills, second.achievement_kills) == (9, 1)
    _turret_shot(server, owner, first, victim, [victim])
    assert "turret_accuracy" in unlocked(server, owner)


def test_evading_a_turret_counts_the_blocks_its_misses_break():
    server, owner, runner = _pair()
    turret = SimpleNamespace()
    _turret_shot(server, owner, turret, runner, blocks=60)
    assert "turret_evasion" not in unlocked(server, runner)
    # A rocket that hurt the runner was not evaded; neither was a lethal one.
    _turret_shot(server, owner, turret, runner, blocks=60, hurt=[runner])
    runner.alive = False
    _turret_shot(server, owner, turret, runner, blocks=60)
    runner.alive = True
    assert server.achievements.tracker(runner).match["turret_evaded_blocks"] == 60
    _turret_shot(server, owner, turret, runner, blocks=39)
    assert "turret_evasion" not in unlocked(server, runner)
    _turret_shot(server, owner, turret, runner, blocks=1)
    assert "turret_evasion" in unlocked(server, runner)
    assert unlocked(server, owner) == set()


def test_turret_evasion_is_per_match_and_not_for_the_owners_team():
    server, owner, runner = _pair()
    mate = make_player(server, 3, TEAM1)
    turret = SimpleNamespace()
    _turret_shot(server, owner, turret, runner, blocks=99)
    achievements.match_started(server)
    _turret_shot(server, owner, turret, runner, blocks=99)
    _turret_shot(server, owner, turret, mate, blocks=500)
    assert unlocked(server, runner) == set() == unlocked(server, mate)
    # An ordinary rocket has no turret target at all.
    engine = server.achievements
    scope = engine.blast_begin(owner, int(C.ROCKET_KILL), (0.0, 0.0, 0.0))
    achievements.blocks_destroyed(server, owner, [(1, 1, 1)] * 5, [], [])
    engine.blast_end(scope)
    assert server.achievements.tracker(runner).match["turret_evaded_blocks"] == 99


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------

def _cells(count, x=0):
    return [(x, index, 10) for index in range(count)]


def test_destroy_666_blocks_as_a_zombie():
    server = make_server()
    zombie = make_player(server, 1, TEAM1)
    survivor = make_player(server, 2, TEAM2)
    achievements.blocks_destroyed(server, zombie, _cells(50), [], [])
    assert progress(server, zombie, "block_as_zombie_count") == 0  # no outbreak yet
    _zombie_round(server, [zombie])
    achievements.blocks_destroyed(server, survivor, _cells(50), [], [])
    assert progress(server, survivor, "block_as_zombie_count") == 0
    # Collapsed cells are destroyed blocks too.
    achievements.blocks_destroyed(server, zombie, _cells(5), _cells(660, x=1), [_cells(660, x=1)])
    assert progress(server, zombie, "block_as_zombie_count") == 665
    assert "blocks_as_zombie" not in unlocked(server, zombie)
    achievements.blocks_destroyed(server, zombie, _cells(1), [], [])
    assert "blocks_as_zombie" in unlocked(server, zombie)


@pytest.mark.parametrize("size,unlocks", [(49, False), (50, True)])
def test_minigun_brings_down_a_fifty_block_structure(size, unlocks):
    server, gunner, _victim = _pair()
    chunk = _cells(size, x=3)
    achievements.blocks_destroyed(
        server, gunner, [(2, 0, 10)], chunk, [chunk], int(C.MINIGUN_TOOL)
    )
    assert ("minigun_demolish" in unlocked(server, gunner)) is unlocks


def test_minigun_demolition_needs_the_minigun_and_one_structure():
    server, gunner, _victim = _pair()
    big = _cells(80, x=3)
    # Another gun, a removal outside a shot, and a grenade blast.
    achievements.blocks_destroyed(server, gunner, [(2, 0, 10)], big, [big], int(C.SMG_TOOL))
    achievements.blocks_destroyed(server, gunner, [(2, 0, 10)], big, [big], None)
    engine = server.achievements
    scope = engine.blast_begin(gunner, int(C.GRENADE_KILL), (0.0, 0.0, 0.0))
    achievements.blocks_destroyed(server, gunner, [(2, 0, 10)], big, [big], int(C.MINIGUN_TOOL))
    engine.blast_end(scope)
    # Two structures of 30 are not one of 50.
    parts = [_cells(30, x=4), _cells(30, x=5)]
    achievements.blocks_destroyed(
        server, gunner, [(2, 0, 10)], parts[0] + parts[1], parts, int(C.MINIGUN_TOOL)
    )
    assert "minigun_demolish" not in unlocked(server, gunner)


def _grenade_collapse(server, thrower, sizes, kill_type=C.GRENADE_KILL):
    engine = server.achievements
    chunks = [_cells(size, x=index + 3) for index, size in enumerate(sizes)]
    scope = engine.blast_begin(thrower, int(kill_type), (0.0, 0.0, 0.0))
    achievements.blocks_destroyed(
        server, thrower, [(2, 0, 10)], [cell for chunk in chunks for cell in chunk], chunks
    )
    engine.blast_end(scope)


def test_five_hundred_block_structures_by_grenade():
    server, thrower, _victim = _pair()
    _grenade_collapse(server, thrower, [99])
    assert progress(server, thrower, "grenade_demolish_count") == 0
    for done in range(1, 5):
        _grenade_collapse(server, thrower, [100], C.CLASSIC_GRENADE_KILL if done % 2 else C.GRENADE_KILL)
        assert progress(server, thrower, "grenade_demolish_count") == done
    assert "grenade_demolish" not in unlocked(server, thrower)
    # One grenade felling two structures counts both.
    _grenade_collapse(server, thrower, [150, 100, 20])
    assert progress(server, thrower, "grenade_demolish_count") == 6
    assert "grenade_demolish" in unlocked(server, thrower)


@pytest.mark.parametrize("kill_type", [
    C.ROCKET_KILL, C.DYNAMITE_KILL, C.GRENADE_LAUNCHER_KILL, C.STICKY_GRENADE_KILL,
    C.ANTIPERSONNEL_GRENADE_KILL, C.C4_KILL,
])
def test_other_explosives_are_not_hand_grenades(kill_type):
    server, thrower, _victim = _pair()
    _grenade_collapse(server, thrower, [300], kill_type)
    assert progress(server, thrower, "grenade_demolish_count") == 0


def test_block_hooks_ignore_bots_and_empty_events():
    server = make_server()
    bot = make_player(server, 1, TEAM1, bot=True)
    _zombie_round(server, [bot])
    achievements.blocks_destroyed(server, bot, _cells(700), [], [])
    achievements.blocks_destroyed(server, None, _cells(700), [], [])
    achievements.blocks_destroyed(server, bot, [], [], [])
    assert server.achievements._dirty == {}
    assert server.achievements.faults == {}


# ---------------------------------------------------------------------------
# Demolition (the engine side; the mode supplies its objective cells)
# ---------------------------------------------------------------------------

def _demolition(server):
    objective = {TEAM1: set(), TEAM2: {(x, 0, 10) for x in range(2000)}}

    def enemy_objective_damage(player, cells):
        enemy = TEAM2 if player.team == TEAM1 else TEAM1
        count = sum(1 for cell in cells if tuple(cell) in objective[enemy])
        return (enemy, count) if count else None

    server.mode.mode_code = "dem"
    server.mode.enemy_objective_damage = enemy_objective_damage
    return [(x, 0, 10) for x in range(2000)]


def test_demolition_damage_one_round_and_many_rounds():
    server, attacker, _victim = _pair("dem")
    base = _demolition(server)
    achievements.blocks_destroyed(server, attacker, base[:60], base[60:99], [])
    assert server.achievements.tracker(attacker).match["demolition_damage"] == 99
    assert "demolition_damage_one_round" not in unlocked(server, attacker)
    # Cells outside the enemy base do not count.
    achievements.blocks_destroyed(server, attacker, [(5, 5, 5)] * 40, [], [])
    assert "demolition_damage_one_round" not in unlocked(server, attacker)
    achievements.blocks_destroyed(server, attacker, base[99:100], [], [])
    assert "demolition_damage_one_round" in unlocked(server, attacker)
    assert progress(server, attacker, "demolition_damage_many_rounds_count") == 100

    # 99 in each of the next rounds never repeats the one-round feat but adds
    # up to the lifetime 500.
    for round_index in range(4):
        achievements.match_ended(server, None)
        achievements.match_started(server)
        achievements.blocks_destroyed(server, attacker, base[:99], [], [])
        assert "demolition_damage_many_rounds" not in unlocked(server, attacker)
    assert progress(server, attacker, "demolition_damage_many_rounds_count") == 496
    achievements.match_started(server)
    achievements.blocks_destroyed(server, attacker, base[:4], [], [])
    assert "demolition_damage_many_rounds" in unlocked(server, attacker)


def test_one_round_damage_does_not_carry_over():
    server, attacker, _victim = _pair("dem")
    base = _demolition(server)
    achievements.blocks_destroyed(server, attacker, base[:99], [], [])
    achievements.match_started(server)
    achievements.blocks_destroyed(server, attacker, base[:99], [], [])
    assert "demolition_damage_one_round" not in unlocked(server, attacker)


def test_drill_gun_base_damage_counts_bores_and_its_blast():
    server, driller, _victim = _pair("dem")
    base = _demolition(server)
    engine = server.achievements
    # The bore: not an explosion, named by block_cause.
    with achievements.block_cause(server, driller, int(C.DRILL_KILL)):
        achievements.blocks_destroyed(server, driller, base[:81], base[81:100], [])
    assert progress(server, driller, "drillgun_demolition_count") == 100
    # The drill's terminal explosion.
    scope = engine.blast_begin(driller, int(C.DRILL_KILL), (0.0, 0.0, 0.0))
    achievements.blocks_destroyed(server, driller, base[:27], [], [])
    engine.blast_end(scope)
    assert progress(server, driller, "drillgun_demolition_count") == 127
    # A spade and a rocket damage the base but are not the drill gun.
    achievements.blocks_destroyed(server, driller, base[:10], [], [], int(C.SPADE_TOOL))
    scope = engine.blast_begin(driller, int(C.ROCKET_KILL), (0.0, 0.0, 0.0))
    achievements.blocks_destroyed(server, driller, base[:10], [], [])
    engine.blast_end(scope)
    assert progress(server, driller, "drillgun_demolition_count") == 127
    assert progress(server, driller, "demolition_damage_many_rounds_count") == 147
    with achievements.block_cause(server, driller, int(C.DRILL_KILL)):
        achievements.blocks_destroyed(server, driller, base[:872], [], [])
    assert "drillgun_demolition" not in unlocked(server, driller)
    with achievements.block_cause(server, driller, int(C.DRILL_KILL)):
        achievements.blocks_destroyed(server, driller, base[:1], [], [])
    assert "drillgun_demolition" in unlocked(server, driller)
    assert engine._blasts == []


def test_drilling_outside_demolition_is_not_base_damage():
    server, driller, _victim = _pair("tdm")
    with achievements.block_cause(server, driller, int(C.DRILL_KILL)):
        achievements.blocks_destroyed(server, driller, _cells(500), [], [])
    assert progress(server, driller, "drillgun_demolition_count") == 0


def test_final_damage_goes_to_the_last_attacker_of_the_losing_base():
    server, first, defender = _pair("dem")
    last = make_player(server, 3, TEAM1)
    base = _demolition(server)
    achievements.blocks_destroyed(server, last, base[:3], [], [])
    achievements.blocks_destroyed(server, first, base[3:6], [], [])
    achievements.blocks_destroyed(server, last, base[6:7], [], [])
    server.mode.ended = True
    achievements.match_ended(server, TEAM1)
    assert "demolition_final_damage" in unlocked(server, last)
    assert "demolition_final_damage" not in unlocked(server, first)
    assert unlocked(server, defender) == set()


@pytest.mark.parametrize("winner", [TEAM2, None])
def test_final_damage_needs_the_win(winner):
    server, attacker, _defender = _pair("dem")
    base = _demolition(server)
    achievements.blocks_destroyed(server, attacker, base[:3], [], [])
    achievements.match_ended(server, winner)
    assert "demolition_final_damage" not in unlocked(server, attacker)


def test_final_damage_is_lost_by_leaving_or_switching_sides():
    server, attacker, _defender = _pair("dem")
    base = _demolition(server)
    achievements.blocks_destroyed(server, attacker, base[:3], [], [])
    del server.players[attacker.id]
    achievements.match_ended(server, TEAM1)
    server.players[attacker.id] = attacker
    assert "demolition_final_damage" not in unlocked(server, attacker)
    # Switched to the losing side before the end: it did not win.
    achievements.match_started(server)
    achievements.blocks_destroyed(server, attacker, base[:3], [], [])
    attacker.team = TEAM2
    achievements.match_ended(server, TEAM1)
    attacker.team = TEAM1
    assert "demolition_final_damage" not in unlocked(server, attacker)
    # A bot's last hit leaves nobody to credit.
    achievements.match_started(server)
    bot = make_player(server, 4, TEAM1, bot=True)
    achievements.blocks_destroyed(server, attacker, base[:3], [], [])
    achievements.blocks_destroyed(server, bot, base[3:4], [], [])
    achievements.match_ended(server, TEAM1)
    assert "demolition_final_damage" not in unlocked(server, attacker)
    assert announcements(server) == []


# ---------------------------------------------------------------------------
# Timed counters
# ---------------------------------------------------------------------------

def _run(server, player, blocks, step=1.0):
    """Walk ``blocks`` along x on the ground, one tick per ``step``."""
    travelled = 0.0
    achievements.tick(server, 1.0 / 60.0)
    while travelled < blocks - 1e-9:
        player.x += step
        travelled += step
        achievements.tick(server, 1.0 / 60.0)


def test_running_adds_whole_blocks_and_only_on_the_ground():
    server, runner, _victim = _pair()
    _run(server, runner, 10, step=0.25)
    assert progress(server, runner, "distance_run") == 10
    # Not while airborne, dead, spectating or teleporting.
    runner.airborne = True
    _run(server, runner, 5)
    runner.airborne = False
    runner.alive = False
    _run(server, runner, 5)
    runner.alive = True
    runner.team = int(C.TEAM_SPECTATOR)
    _run(server, runner, 5)
    runner.team = TEAM1
    achievements.tick(server, 1.0 / 60.0)
    runner.x += 300.0
    achievements.tick(server, 1.0 / 60.0)
    assert progress(server, runner, "distance_run") == 10


def test_half_marathon_and_marathon_are_21_and_42_kilometres():
    server, runner, _victim = _pair()
    engine = server.achievements
    engine.add(runner, "distance_run", 20999)
    _run(server, runner, 0.9, step=0.3)
    assert unlocked(server, runner) == set()
    _run(server, runner, 0.3, step=0.3)
    assert unlocked(server, runner) == {"misc_half_marathon"}
    engine.add(runner, "distance_run", 20999)
    assert unlocked(server, runner) == {"misc_half_marathon"}
    _run(server, runner, 1.0)
    assert unlocked(server, runner) == {"misc_half_marathon", "misc_marathon"}
    assert progress(server, runner, "distance_run") == 42000


def test_an_hour_in_the_air():
    server, flyer, _victim = _pair()
    engine = server.achievements
    flyer.airborne = True
    for _ in range(90):
        achievements.tick(server, 1.0 / 60.0)
    assert progress(server, flyer, "airborne_seconds_count") == 1
    flyer.airborne = False
    for _ in range(120):
        achievements.tick(server, 1.0 / 60.0)
    assert progress(server, flyer, "airborne_seconds_count") == 1
    engine.add(flyer, "airborne_seconds_count", 3598)
    flyer.airborne = True
    for _ in range(29):
        achievements.tick(server, 1.0 / 60.0)
    assert "airborne_for_hour" not in unlocked(server, flyer)
    achievements.tick(server, 1.0 / 60.0)
    assert "airborne_for_hour" in unlocked(server, flyer)


def test_ticks_ignore_bots_and_bad_deltas():
    server = make_server()
    bot = make_player(server, 1, TEAM1, bot=True, airborne=True)
    human = make_player(server, 2, TEAM2, airborne=True)
    for dt in (0.0, -1.0, 5.0):
        achievements.tick(server, dt)
    assert progress(server, human, "airborne_seconds_count") == 0
    for _ in range(60):
        achievements.tick(server, 1.0 / 60.0)
    assert progress(server, human, "airborne_seconds_count") == 1
    assert not hasattr(bot, "achievement_tracker") or bot.achievement_tracker.remainder == {}


# ---------------------------------------------------------------------------
# Crates
# ---------------------------------------------------------------------------

def _health_crate(server, player, heal_to=100):
    before = achievements.crate_baseline(player)
    player.health = heal_to
    achievements.crate_collected(server, player, int(C.MOST_HEALTH_CRATES_COLLECTED), before)


def test_repair_150_health_with_health_drops_in_one_game():
    server, player, _victim = _pair()
    player.health = 40
    _health_crate(server, player)          # +60
    player.health = 11
    _health_crate(server, player)          # +89 -> 149
    assert "health_drop_greedy" not in unlocked(server, player)
    _health_crate(server, player)          # already full: heals nothing
    assert "health_drop_greedy" not in unlocked(server, player)
    player.health = 99
    _health_crate(server, player)          # +1 -> 150
    assert "health_drop_greedy" in unlocked(server, player)


def test_health_from_drops_is_per_game():
    server, player, _victim = _pair()
    player.health = 1
    _health_crate(server, player)          # +99
    achievements.match_started(server)
    player.health = 1
    _health_crate(server, player)          # +99 in the next game
    assert "health_drop_greedy" not in unlocked(server, player)
    # A Zombie or VIP sub-round is still the same game.
    achievements.zombie_round_started(server, [], TEAM1, TEAM2)
    player.health = 49
    _health_crate(server, player)          # +51 -> 150
    assert "health_drop_greedy" in unlocked(server, player)


def _ammo_crate(server, player, gained):
    before = achievements.crate_baseline(player)
    player.ammo_reserve += gained
    achievements.crate_collected(server, player, int(C.MOST_AMMO_CRATES_COLLECTED), before)


def test_a_full_resupply_of_ammunition_from_drops_in_one_game():
    from server.game_constants import WEAPON_PROFILES

    server, player, _victim = _pair()
    player.weapon = int(C.RIFLE_TOOL)
    capacity = int(WEAPON_PROFILES[player.weapon].reserve_ammo)
    assert capacity > 2
    player.ammo_reserve = 0
    _ammo_crate(server, player, capacity - 1)
    assert "ammo_drop_greedy" not in unlocked(server, player)
    _ammo_crate(server, player, 0)         # a crate taken with a full reserve
    assert "ammo_drop_greedy" not in unlocked(server, player)
    _ammo_crate(server, player, 1)
    assert "ammo_drop_greedy" in unlocked(server, player)


def test_ammo_from_drops_is_per_weapon_and_per_game():
    from server.game_constants import WEAPON_PROFILES

    server, player, _victim = _pair()
    player.weapon = int(C.RIFLE_TOOL)
    rifle = int(WEAPON_PROFILES[player.weapon].reserve_ammo)
    player.ammo_reserve = 0
    _ammo_crate(server, player, rifle - 1)
    # Another gun starts its own total.
    player.weapon = int(C.SMG_TOOL)
    _ammo_crate(server, player, 1)
    assert "ammo_drop_greedy" not in unlocked(server, player)
    achievements.match_started(server)
    player.weapon = int(C.RIFLE_TOOL)
    _ammo_crate(server, player, 1)
    assert "ammo_drop_greedy" not in unlocked(server, player)
    # A block crate is neither.
    before = achievements.crate_baseline(player)
    player.health, player.ammo_reserve = 500, 5000
    achievements.crate_collected(server, player, int(C.MOST_BLOCK_CRATES_COLLECTED), before)
    assert unlocked(server, player) == set()


# ---------------------------------------------------------------------------
# Zombie rounds (engine side)
# ---------------------------------------------------------------------------

def test_three_survivors_in_one_round_as_a_zombie():
    server = make_server("zom")
    zombie = make_player(server, 1, TEAM1, tool=int(C.ZOMBIEHAND_TOOL))
    survivors = [make_player(server, index, TEAM2) for index in range(2, 6)]
    _zombie_round(server, [zombie])
    kill(server, zombie, survivors[0], MELEE)
    kill(server, zombie, survivors[1], MELEE)
    assert "zombie_kills_humans" not in unlocked(server, zombie)
    # The next round starts the count again.
    server.achievements.zombie_round_finished([])
    _zombie_round(server, [zombie])
    kill(server, zombie, survivors[0], MELEE)
    kill(server, zombie, survivors[1], MELEE)
    assert "zombie_kills_humans" not in unlocked(server, zombie)
    kill(server, zombie, survivors[2], MELEE)
    assert "zombie_kills_humans" in unlocked(server, zombie)
    # A survivor's zombie kills are not these.
    survivor = survivors[3]
    for _ in range(3):
        kill(server, survivor, zombie)
    assert "zombie_kills_humans" not in unlocked(server, survivor)


def test_kills_between_zombie_rounds_do_not_count():
    server = make_server("zom")
    zombie = make_player(server, 1, TEAM1)
    survivor = make_player(server, 2, TEAM2)
    _zombie_round(server, [zombie])
    server.achievements.zombie_round_finished([])
    for _ in range(3):
        kill(server, zombie, survivor, MELEE)
    assert unlocked(server, zombie) == set()
    assert server.achievements.tracker(zombie).round == {}


def test_zombie_fall_kill():
    server = make_server("zom")
    zombie = make_player(server, 1, TEAM1)
    survivor = make_player(server, 2, TEAM2)
    kill(server, zombie, survivor, FALL)  # before any outbreak: not a zombie
    assert "zombie_fall" not in unlocked(server, zombie)
    _zombie_round(server, [zombie])
    kill(server, survivor, zombie, FALL)
    assert "zombie_fall" not in unlocked(server, survivor)
    kill(server, zombie, survivor, MELEE)
    assert "zombie_fall" not in unlocked(server, zombie)
    kill(server, zombie, survivor, FALL)
    assert "zombie_fall" in unlocked(server, zombie)


def test_zombie_water_kills_need_the_zombie_in_the_water():
    server = make_server("zom")
    zombie = make_player(server, 1, TEAM1)
    survivor = make_player(server, 2, TEAM2, wade=True)
    _zombie_round(server, [zombie])
    kill(server, zombie, survivor, MELEE)
    assert progress(server, zombie, "zombie_kills_in_water") == 0
    # A survivor wading is not a zombie in the water.
    kill(server, survivor, zombie)
    assert progress(server, survivor, "zombie_kills_in_water") == 0


@pytest.mark.parametrize("kills,expected", [
    (4, set()),
    (5, {"lastman_kills_zombies_easy"}),
    (9, {"lastman_kills_zombies_easy"}),
    (10, {"lastman_kills_zombies_easy", "lastman_kills_zombies_hard"}),
])
def test_last_man_standing_zombie_kills_in_one_round(kills, expected):
    server = make_server("zom")
    zombie = make_player(server, 1, TEAM1)
    last = make_player(server, 2, TEAM2)
    _zombie_round(server, [zombie], last_survivor=last)
    for _ in range(kills):
        last.kill_streak = 0
        kill(server, last, zombie)
    assert unlocked(server, last) == expected


def test_last_man_kills_need_the_marker_and_reset_each_round():
    server = make_server("zom")
    zombie = make_player(server, 1, TEAM1)
    last = make_player(server, 2, TEAM2)
    other = make_player(server, 3, TEAM2)
    _zombie_round(server, [zombie], last_survivor=None)
    for _ in range(5):
        last.kill_streak = 0
        kill(server, last, zombie)
    assert unlocked(server, last) == set()
    server.mode.last_survivor_id = last.id
    for _ in range(4):
        last.kill_streak = other.kill_streak = 0
        kill(server, last, zombie)
        kill(server, other, zombie)
    server.achievements.zombie_round_finished([])
    _zombie_round(server, [zombie], last_survivor=last)
    for _ in range(4):
        last.kill_streak = 0
        kill(server, last, zombie)
    assert unlocked(server, last) == set() == unlocked(server, other)
    kill(server, last, zombie)
    assert unlocked(server, last) == {"lastman_kills_zombies_easy"}


def _seconds(server, seconds, dt=0.5):
    for _ in range(int(round(seconds / dt))):
        achievements.tick(server, dt)


def test_twenty_eight_seconds_as_last_man_in_one_round():
    server = make_server("zom")
    zombie = make_player(server, 1, TEAM1)
    last = make_player(server, 2, TEAM2)
    _zombie_round(server, [zombie], last_survivor=last)
    _seconds(server, 27.5)
    assert "zombie_lms_one_round" not in unlocked(server, last)
    _seconds(server, 0.5)
    assert "zombie_lms_one_round" in unlocked(server, last)
    assert progress(server, last, "zombie_seconds_as_lms_count") == 28
    assert unlocked(server, zombie) == set()


def test_last_man_seconds_restart_each_round_but_add_up_for_life():
    server = make_server("zom")
    zombie = make_player(server, 1, TEAM1)
    last = make_player(server, 2, TEAM2)
    for _ in range(2):
        _zombie_round(server, [zombie], last_survivor=last)
        _seconds(server, 27.0)
        server.achievements.zombie_round_finished([])
        server.mode.last_survivor_id = None
    assert "zombie_lms_one_round" not in unlocked(server, last)
    assert progress(server, last, "zombie_seconds_as_lms_count") == 54
    # Dead time and time before being the last man do not count.
    _zombie_round(server, [zombie], last_survivor=None)
    _seconds(server, 10.0)
    server.mode.last_survivor_id = last.id
    last.alive = False
    _seconds(server, 10.0)
    assert progress(server, last, "zombie_seconds_as_lms_count") == 54


def test_ten_minutes_as_last_man_over_many_rounds():
    server = make_server("zom")
    zombie = make_player(server, 1, TEAM1)
    last = make_player(server, 2, TEAM2)
    server.achievements.add(last, "zombie_seconds_as_lms_count", 598)
    _zombie_round(server, [zombie], last_survivor=last)
    _seconds(server, 1.5)
    assert "zombie_lms_multi_round" not in unlocked(server, last)
    _seconds(server, 0.5)
    assert "zombie_lms_multi_round" in unlocked(server, last)


def test_surviving_a_zombie_round_and_the_stone_cold_killer():
    server = make_server("zom")
    first = make_player(server, 1, TEAM1)
    late = make_player(server, 2, TEAM1)
    survivors = [make_player(server, index, TEAM2) for index in range(3, 7)]
    _zombie_round(server, [first])
    kill(server, first, survivors[0], MELEE)
    kill(server, first, survivors[1], MELEE)
    # A zombie infected later out-kills nobody but did not start as one.
    kill(server, late, survivors[2], MELEE)
    kill(server, late, survivors[2], MELEE)
    # Killed in the last instant: still on the survivor team, not alive.
    survivors[1].alive = False
    server.achievements.zombie_round_finished([survivors[1], survivors[3]])
    assert unlocked(server, survivors[3]) == {"zombie_survivor"}
    assert unlocked(server, survivors[1]) == set()
    assert unlocked(server, survivors[0]) == set()
    assert unlocked(server, first) == {"zombie_mvp"}  # tied for the highest
    assert unlocked(server, late) == set()


def test_stone_cold_killer_needs_the_highest_tally_of_the_round():
    server = make_server("zom")
    first = make_player(server, 1, TEAM1)
    survivor = make_player(server, 2, TEAM2)
    bot = make_player(server, 3, TEAM1, bot=True)
    _zombie_round(server, [first])
    server.achievements.zombie_round_finished([])
    assert unlocked(server, first) == set()  # no kills at all
    _zombie_round(server, [first])
    kill(server, first, survivor, MELEE)
    for _ in range(2):
        kill(server, bot, survivor, MELEE)   # a bot out-killed it
    server.achievements.zombie_round_finished([])
    assert unlocked(server, first) == set()
    # Last round's tallies are gone.
    _zombie_round(server, [first])
    kill(server, first, survivor, MELEE)
    server.achievements.zombie_round_finished([])
    assert unlocked(server, first) == {"zombie_mvp"}


def test_most_kills_with_the_bot_switch_off_needs_kills_of_humans():
    server = make_server("zom", count_bot_kills=False)
    first = make_player(server, 1, TEAM1)
    bot = make_player(server, 2, TEAM2, bot=True)
    human = make_player(server, 3, TEAM2)
    _zombie_round(server, [first])
    for _ in range(3):
        kill(server, first, bot, MELEE)
    server.achievements.zombie_round_finished([])
    assert unlocked(server, first) == set()
    _zombie_round(server, [first])
    kill(server, first, human, MELEE)
    server.achievements.zombie_round_finished([])
    assert unlocked(server, first) == {"zombie_mvp"}


def test_a_departed_patient_zero_is_not_credited():
    server = make_server("zom")
    first = make_player(server, 1, TEAM1)
    survivor = make_player(server, 2, TEAM2)
    _zombie_round(server, [first])
    kill(server, first, survivor, MELEE)
    del server.players[first.id]
    server.achievements.zombie_round_finished([])
    server.players[first.id] = first
    assert unlocked(server, first) == set()


# ---------------------------------------------------------------------------
# Map regions
# ---------------------------------------------------------------------------

class _World:
    def __init__(self, regions, solid=(), official=True):
        self.solid = set(solid)
        self.map_metadata = SimpleNamespace(
            official_map=official, achievement_regions=list(regions)
        )

    def get_solid(self, x, y, z):
        return (int(x), int(y), int(z)) in self.solid

    def destroy(self, server, player, cells, *, collapsed=()):
        self.solid.difference_update(cells)
        self.solid.difference_update(collapsed)
        achievements.blocks_destroyed(server, player, list(cells), list(collapsed), [])


def _box(x0, x1, y0, y1, z0, z1):
    return (float(x0), float(x1), float(y0), float(y1), float(z0), float(z1))


def _region_server(regions, solid=(), mode_code="tdm", **world_options):
    server = make_server(mode_code)
    server.world_manager = _World(regions, solid, **world_options)
    achievements.match_started(server)
    return server


DRAGON = AchievementRegion(
    "map_isleofdoom_destroy", int(C.ACH_BLOCK_DESTROY_REGION), _box(10, 12, 10, 12, 20, 22),
)


def test_destroying_every_block_of_a_structure_credits_who_took_part():
    statue = [(x, 11, z) for x in (10, 11, 12) for z in (20, 21, 22)]
    outside = [(30, 30, 30), (13, 11, 21)]
    server = _region_server([DRAGON], statue + outside)
    world = server.world_manager
    breaker = make_player(server, 1, TEAM1)
    helper = make_player(server, 2, TEAM2)
    bystander = make_player(server, 3, TEAM1)
    world.destroy(server, breaker, statue[:4])
    world.destroy(server, bystander, outside[:1])       # not part of the statue
    world.destroy(server, helper, statue[4:5])
    assert unlocked(server, breaker) == set()
    # The rest collapses when its base is cut.
    world.destroy(server, breaker, statue[5:6], collapsed=statue[6:])
    assert unlocked(server, breaker) == {"map_isleofdoom_destroy"}
    assert unlocked(server, helper) == {"map_isleofdoom_destroy"}
    assert unlocked(server, bystander) == set()
    assert outside[1] in world.solid


def test_a_structure_with_blocks_left_is_not_destroyed():
    statue = [(x, 11, 21) for x in (10, 11, 12)]
    server = _region_server([DRAGON], statue)
    breaker = make_player(server, 1, TEAM1)
    server.world_manager.destroy(server, breaker, statue[:2])
    assert unlocked(server, breaker) == set()
    # A block built back into a razed cell is not the statue any more.
    server.world_manager.solid.add(statue[0])
    server.world_manager.destroy(server, breaker, statue[2:])
    assert unlocked(server, breaker) == {"map_isleofdoom_destroy"}


def test_region_blocks_removed_outside_combat_still_complete_it():
    statue = [(x, 11, 21) for x in (10, 11, 12)]
    server = _region_server([DRAGON], statue)
    breaker = make_player(server, 1, TEAM1)
    server.world_manager.destroy(server, breaker, statue[:1])
    # Fire burns the rest without any player's block event.
    server.world_manager.solid.clear()
    achievements.tick(server, achievements.REGION_RESYNC_SECONDS)
    assert unlocked(server, breaker) == {"map_isleofdoom_destroy"}


def test_bots_and_dead_rounds_do_not_take_part_but_the_blocks_are_gone():
    statue = [(x, 11, 21) for x in (10, 11, 12)]
    server = _region_server([DRAGON], statue)
    bot = make_player(server, 1, TEAM1, bot=True)
    human = make_player(server, 2, TEAM2)
    server.world_manager.destroy(server, bot, statue[:1])
    server.mode.ended = True
    server.world_manager.destroy(server, human, statue[1:2])
    server.mode.ended = False
    server.world_manager.destroy(server, None, statue[2:])
    assert unlocked(server, human) == set()
    assert announcements(server) == []


def test_regions_are_only_honoured_on_stock_maps():
    statue = [(x, 11, 21) for x in (10, 11, 12)]
    server = _region_server([DRAGON], statue, official=False)
    breaker = make_player(server, 1, TEAM1)
    server.world_manager.destroy(server, breaker, statue)
    assert unlocked(server, breaker) == set()
    # An unknown id in a stock map's metadata is ignored as well.
    unknown = AchievementRegion("map_made_up", int(C.ACH_BLOCK_DESTROY_REGION), DRAGON.bounds)
    jump = AchievementRegion("map_isleofdoom_destroy", int(C.ACH_JUMP_REGION), DRAGON.bounds)
    server = _region_server([unknown, jump], statue)
    assert server.achievements._regions == []


TOWERS = [
    AchievementRegion("map_zombieisland_destroy", int(C.ACH_BLOCK_DESTROY_REGION),
                      _box(0, 1, 0, 1, 0, 3), team=TEAM1),
    AchievementRegion("map_zombieisland_destroy", int(C.ACH_BLOCK_DESTROY_REGION),
                      _box(10, 11, 0, 1, 0, 3), team=TEAM1),
]


def test_both_towers_must_fall_to_zombies_who_worked_on_each():
    west = [(0, 0, z) for z in range(4)]
    east = [(10, 0, z) for z in range(4)]
    server = _region_server(TOWERS, west + east, "zom")
    world = server.world_manager
    both = make_player(server, 1, TEAM1)
    west_only = make_player(server, 2, TEAM1)
    survivor = make_player(server, 3, TEAM2)
    _zombie_round(server, [both, west_only])
    world.destroy(server, both, west[:1])
    world.destroy(server, west_only, west[1:3])
    world.destroy(server, survivor, west[3:])       # the wrong team finishes it
    assert unlocked(server, both) == set()
    world.destroy(server, survivor, east[:1])
    world.destroy(server, both, east[1:])
    assert unlocked(server, both) == {"map_zombieisland_destroy"}
    assert unlocked(server, west_only) == set()
    assert unlocked(server, survivor) == set()


def test_tower_offense_needs_a_zombie_not_just_the_blue_team():
    west = [(0, 0, 0)]
    east = [(10, 0, 0)]
    server = _region_server(TOWERS, west + east, "tdm")
    blue = make_player(server, 1, TEAM1)
    server.world_manager.destroy(server, blue, west + east)
    assert unlocked(server, blue) == set()


def test_a_tower_already_gone_at_match_start_disables_the_pair():
    east = [(10, 0, z) for z in range(4)]
    server = _region_server(TOWERS, east, "zom")
    assert server.achievements._regions == []
    zombie = make_player(server, 1, TEAM1)
    _zombie_round(server, [zombie])
    server.world_manager.destroy(server, zombie, east)
    assert unlocked(server, zombie) == set()


ALTAR = AchievementRegion(
    "map_maya_kill", int(C.ACH_KILL_REGION), _box(426.5, 451.5, 255, 285, 182, 192), weapon=1,
)


def test_melee_kill_from_the_temple_altar():
    server = _region_server([ALTAR])
    killer = make_player(server, 1, TEAM1, tool=int(C.KNIFE_TOOL))
    victim = make_player(server, 2, TEAM2)
    killer.x, killer.y, killer.z = 439.0, 270.0, 187.75
    kill(server, killer, victim, WEAPON)            # shot from the altar
    killer.x = 452.0
    kill(server, killer, victim, MELEE)             # a step outside it
    assert unlocked(server, killer) == set()
    killer.x = 451.5                                # the edge is inside
    kill(server, killer, victim, MELEE)
    assert unlocked(server, killer) == {"map_maya_kill"}
    # The victim's position is irrelevant: it is where the killer stands.
    other = make_player(server, 3, TEAM2, tool=int(C.SPADE_TOOL))
    victim.x, victim.y, victim.z = 439.0, 270.0, 187.75
    killer.x = 100.0
    kill(server, other, killer, MELEE)
    assert "map_maya_kill" not in unlocked(server, other)


BASEMENT = AchievementRegion(
    "map_zombieisland_zombie_kill", int(C.ACH_KILL_REGION),
    _box(265, 295, 231, 261, 229, 241), team=TEAM2, kills=5,
)


def test_five_zombie_kills_from_the_basement_in_one_round():
    server = _region_server([BASEMENT], mode_code="zom")
    zombie = make_player(server, 1, TEAM1)
    survivor = make_player(server, 2, TEAM2)
    survivor.x, survivor.y, survivor.z = 280.0, 246.0, 234.75
    _zombie_round(server, [zombie])
    for _ in range(4):
        survivor.kill_streak = 0
        kill(server, survivor, zombie)
    # The next round starts again; so does stepping out for a kill.
    server.achievements.zombie_round_finished([])
    _zombie_round(server, [zombie])
    for _ in range(4):
        survivor.kill_streak = 0
        kill(server, survivor, zombie)
    survivor.z = 200.0
    kill(server, survivor, zombie)
    assert "map_zombieisland_zombie_kill" not in unlocked(server, survivor)
    survivor.z = 234.75
    survivor.kill_streak = 0
    kill(server, survivor, zombie)
    assert "map_zombieisland_zombie_kill" in unlocked(server, survivor)


def test_basement_kills_need_zombies_and_the_survivor_side():
    server = _region_server([BASEMENT], mode_code="tdm")
    green = make_player(server, 2, TEAM2)
    blue = make_player(server, 1, TEAM1)
    for player in (green, blue):
        player.x, player.y, player.z = 280.0, 246.0, 234.75
    for _ in range(5):
        green.kill_streak = blue.kill_streak = 0
        kill(server, green, blue)        # not zombies outside Zombie mode
    assert unlocked(server, green) == set()
    _zombie_round(server, [blue])
    for _ in range(5):
        blue.kill_streak = 0
        kill(server, blue, green, MELEE)  # the zombie is not "hiding"
    assert "map_zombieisland_zombie_kill" not in unlocked(server, blue)


@pytest.mark.parametrize("cause,credited", [
    (int(C.ROCKET_KILL), True), (int(C.ROCKET2_KILL), True),
    (int(C.GRENADE_KILL), False), (None, False),
])
def test_an_authored_monolith_region_would_need_the_rocket_launcher(cause, credited):
    """No retail volume exists for Lunar Base; the rule is ready for one."""
    monolith = AchievementRegion(
        "map_moon_destroy", int(C.ACH_BLOCK_DESTROY_REGION), _box(0, 0, 0, 0, 0, 1),
    )
    cells = [(0, 0, 0), (0, 0, 1)]
    server = _region_server([monolith], cells)
    player = make_player(server, 1, TEAM1)
    engine = server.achievements
    scope = engine.blast_begin(player, cause, None) if cause is not None else None
    server.world_manager.destroy(server, player, cells)
    if scope is not None:
        engine.blast_end(scope)
    assert ("map_moon_destroy" in unlocked(server, player)) is credited


def test_an_authored_pillar_region_would_need_demolition():
    pillars = AchievementRegion(
        "map_concrete_destroy", int(C.ACH_BLOCK_DESTROY_REGION), _box(0, 0, 0, 0, 0, 0),
    )
    for mode_code, credited in (("tdm", False), ("dem", True)):
        server = _region_server([pillars], [(0, 0, 0)], mode_code)
        player = make_player(server, 1, TEAM1)
        server.world_manager.destroy(server, player, [(0, 0, 0)])
        assert ("map_concrete_destroy" in unlocked(server, player)) is credited


# ---------------------------------------------------------------------------
# The recovered retail volumes
# ---------------------------------------------------------------------------

def _regions(map_name):
    return load_map_metadata(ROOT / "maps" / f"{map_name}.vxl", "tdm").achievement_regions


def test_recovered_retail_regions_are_parsed():
    assert _regions("DragonIsland") == [AchievementRegion(
        "map_isleofdoom_destroy", int(C.ACH_BLOCK_DESTROY_REGION),
        (145.0, 175.0, 301.0, 331.0, 178.0, 202.0),
    )]
    assert _regions("MayanJungle") == [AchievementRegion(
        "map_maya_kill", int(C.ACH_KILL_REGION),
        (426.5, 451.5, 255.0, 285.0, 182.0, 192.0), weapon=1,
    )]
    assert _regions("SpookyMansion") == [
        AchievementRegion(
            "map_zombieisland_zombie_kill", int(C.ACH_KILL_REGION),
            (265.0, 295.0, 231.0, 261.0, 229.0, 241.0), team=TEAM2, kills=5,
        ),
        AchievementRegion(
            "map_zombieisland_destroy", int(C.ACH_BLOCK_DESTROY_REGION),
            (275.5, 286.5, 282.5, 321.5, 177.0, 227.0), team=TEAM1,
        ),
        AchievementRegion(
            "map_zombieisland_destroy", int(C.ACH_BLOCK_DESTROY_REGION),
            (272.0, 290.0, 196.5, 231.5, 193.0, 227.0), team=TEAM1,
        ),
    ]
    # The other stock maps have no recovered volume.
    assert _regions("London") == [] == _regions("LunarBase")


@pytest.mark.parametrize("map_name", ["DragonIsland", "MayanJungle", "SpookyMansion"])
def test_the_maps_with_recovered_volumes_are_stock_maps(map_name):
    metadata = load_map_metadata(ROOT / "maps" / f"{map_name}.vxl", "zom")
    assert metadata.official_map is True
    assert metadata.achievement_regions


def test_malformed_region_rows_are_skipped(tmp_path):
    import json

    (tmp_path / "Odd.vxl").write_bytes(b"")
    (tmp_path / "Odd.json").write_text(json.dumps({
        "ac_ids": ["map_maya_kill", 7, "map_maya_kill", "map_maya_kill", "short"],
        "ac_types": [1, 1, "x", 1],
        "ac_centres": [[1, 2, 3], [1, 2, 3], [1, 2, 3], [1, 2]],
        "ac_w_h_d": [[2, 2, 2], [2, 2, 2], [2, 2, 2], [2, 2, 2]],
        "ac_teams": [9],
    }), encoding="utf-8")
    regions = load_map_metadata(tmp_path / "Odd.vxl", "tdm").achievement_regions
    assert regions == [AchievementRegion(
        "map_maya_kill", int(C.ACH_KILL_REGION), (0.0, 2.0, 1.0, 3.0, 2.0, 4.0),
    )]
    (tmp_path / "Odd.json").write_text(json.dumps({"ac_ids": "nope"}), encoding="utf-8")
    assert load_map_metadata(tmp_path / "Odd.vxl", "tdm").achievement_regions == []


def test_the_stone_dragon_volume_holds_the_statue_on_the_real_map():
    from server.world_manager import WorldManager

    world = WorldManager(SimpleNamespace(maps_path=str(ROOT / "maps"), game_mode="tdm"))
    assert world.load_map("DragonIsland")
    server = make_server()
    server.world_manager = world
    achievements.match_started(server)
    (entry,) = server.achievements._regions
    assert entry.region.api_name == "map_isleofdoom_destroy"
    assert len(entry.remaining) == 2032
    assert all(world.get_solid(*cell) for cell in entry.remaining)
