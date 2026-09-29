"""Zombie survivor refuge election and its use by the bot policy."""

from __future__ import annotations

from dataclasses import replace

import shared.constants as C

from server.bot_ai.messages import ObjectiveSnapshot
from server.bot_ai.policies import objective_decision_for
from server.bot_ai.zombie_refuge import elect_refuge, refuge_breached
from tests.test_bot_architecture import _frame, _player_snapshot


def _terrain(plateau=((140, 140), 8), water=()):
    """Flat ground at z=60 with one raised plateau (top z=60-height)."""

    (px, py), height = plateau

    def surface_z(x, y):
        if (x, y) in water:
            return 239
        if abs(x - px) <= 4 and abs(y - py) <= 4:
            return 60 - height
        return 60

    return surface_z


def test_election_picks_the_plateau_near_the_survivors():
    survivors = [(120.0, 120.0, 57.75), (124.0, 118.0, 57.75)]
    zombies = [(60.0, 60.0, 57.75)]
    refuge = elect_refuge(_terrain(), survivors, zombies)
    assert refuge is not None
    x, y, z = refuge
    assert abs(x - 140.5) <= 4.0 and abs(y - 140.5) <= 4.0
    assert z == 52.0 - 2.25


def test_election_ignores_water_and_excluded_spots():
    water = {(x, y) for x in range(136, 145) for y in range(136, 145)}
    survivors = [(120.0, 120.0, 57.75)]
    refuge = elect_refuge(_terrain(water=water), survivors)
    assert refuge is None or abs(refuge[0] - 140.5) > 4.0

    plateau = elect_refuge(_terrain(), survivors)
    assert plateau is not None
    again = elect_refuge(_terrain(), survivors, exclude=[plateau])
    assert again is None or (abs(again[0] - plateau[0]) > 8.0 or abs(again[1] - plateau[1]) > 8.0)


def test_breach_needs_a_zombie_on_the_top_not_below_it():
    refuge = (140.5, 140.5, 49.75)
    assert refuge_breached(refuge, [(142.0, 141.0, 50.0)])
    assert not refuge_breached(refuge, [(142.0, 141.0, 58.0)])  # at the foot
    assert not refuge_breached(refuge, [(160.0, 141.0, 50.0)])


def test_survivors_route_to_the_refuge_and_fortify_there():
    observer = _player_snapshot(1, 2, (100.0, 100.0, 57.75), is_bot=True)
    anchor = ObjectiveSnapshot("team_anchor", 2, (100.0, 100.0, 57.75))
    enemy_anchor = ObjectiveSnapshot("team_anchor", 3, (400.0, 100.0, 57.75))
    refuge = ObjectiveSnapshot("zombie_refuge", 2, (140.5, 140.5, 49.75))
    zombie = replace(
        _player_snapshot(2, 3, (300.0, 100.0, 57.75)), class_id=int(C.CLASS_ZOMBIE)
    )
    for phase in ("countdown", "active"):
        frame = replace(
            _frame(1, observer, zombie),
            mode_id="zom",
            mode_phase=phase,
            objectives=(anchor, enemy_anchor, refuge),
        )
        decision = objective_decision_for(frame, observer)
        assert decision is not None
        assert decision.role.startswith("zombie_") and "refuge" in decision.role
        assert decision.directive == "fortify"
        assert abs(decision.position[0] - 140.5) < 4.0
        assert abs(decision.position[1] - 140.5) < 4.0
        assert decision.position[2] == 49.75
        assert decision.watch_position == zombie.position

    # Infected never use the survivors' refuge.
    hunter = replace(observer, class_id=int(C.CLASS_ZOMBIE), team=3)
    prey = _player_snapshot(2, 2, (150.0, 100.0, 57.75))
    frame = replace(
        _frame(2, hunter, prey),
        mode_id="zom",
        mode_phase="active",
        objectives=(anchor, enemy_anchor, refuge),
    )
    hunt = objective_decision_for(frame, hunter)
    assert hunt is not None and hunt.role == "zombie_hunt_survivor"


def test_last_survivor_runs_for_a_refuge_farther_from_the_horde():
    observer = _player_snapshot(1, 2, (100.0, 100.0, 57.75), is_bot=True)
    zombie = replace(
        _player_snapshot(2, 3, (90.0, 100.0, 57.75)), class_id=int(C.CLASS_ZOMBIE)
    )
    refuge = ObjectiveSnapshot("zombie_refuge", 2, (130.5, 100.5, 49.75))
    marker = ObjectiveSnapshot("last_survivor", 2, observer.position, carrier_id=1)
    frame = replace(
        _frame(3, observer, zombie),
        mode_id="zom",
        mode_phase="active",
        objectives=(refuge, marker),
    )
    decision = objective_decision_for(frame, observer)
    assert decision is not None
    assert decision.role == "zombie_last_survivor_escape"
    assert decision.position == refuge.position


def test_election_skips_refuges_outside_the_survivors_walkable_region():
    survivors = [(120.0, 120.0, 57.75)]

    def region_of(x, y):
        # The plateau is a separate region (reachable only by digging).
        return 2 if abs(x - 140) <= 4 and abs(y - 140) <= 4 else 1

    refuge = elect_refuge(_terrain(), survivors, region_of=region_of)
    assert refuge is None or abs(refuge[0] - 140.5) > 4.0 or abs(refuge[1] - 140.5) > 4.0
    # Same plateau, same region: elected as before.
    assert elect_refuge(_terrain(), survivors, region_of=lambda x, y: 1) is not None
