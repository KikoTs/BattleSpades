"""Deterministic objective-policy tests for the isolated bot worker."""

from __future__ import annotations

import time

from server.bot_ai.messages import ObjectiveSnapshot, PerceptionFrame, PlayerSnapshot
from server.bot_ai.policies import objective_decision_for, objective_goal_for


def _player(
    player_id: int,
    team: int,
    position=(0.0, 0.0, 10.0),
    *,
    health: int = 100,
    carried: int = -1,
    class_id: int = 0,
) -> PlayerSnapshot:
    return PlayerSnapshot(
        player_id=player_id,
        generation=1,
        team=team,
        class_id=class_id,
        alive=True,
        spawned=True,
        position=position,
        eye=position,
        orientation=(1.0, 0.0, 0.0),
        velocity=(0.0, 0.0, 0.0),
        health=health,
        tool=6,
        blocks=50,
        ammo_clip=10,
        ammo_reserve=30,
        is_bot=True,
        carried_entity_id=carried,
    )


def _frame(
    mode: str,
    observer: PlayerSnapshot,
    *players,
    objectives=(),
    phase: str = "active",
):
    return PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=0,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=time.monotonic(),
        mode_id=mode,
        players=(observer, *players),
        objectives=tuple(objectives),
        mode_phase=phase,
    )


def test_ctf_carrier_routes_to_own_base() -> None:
    carrier = _player(1, 2, carried=99)
    own_base = ObjectiveSnapshot("ctf_base", 2, (20.0, 30.0, 60.0))
    enemy_intel = ObjectiveSnapshot("ctf_intel", 3, (400.0, 400.0, 60.0))

    assert objective_goal_for(
        _frame("ctf", carrier, objectives=(own_base, enemy_intel)), carrier
    ) == own_base.position


def test_vip_guard_and_attacker_have_distinct_goals() -> None:
    guard = _player(2, 2)
    attacker = _player(3, 2)
    own_vip = ObjectiveSnapshot("vip", 2, (50.0, 50.0, 60.0), carrier_id=10)
    enemy_vip = ObjectiveSnapshot("vip", 3, (300.0, 300.0, 60.0), carrier_id=11)
    frame = _frame("vip", guard, attacker, objectives=(own_vip, enemy_vip))

    guard_decision = objective_decision_for(frame, guard)
    attacker_decision = objective_decision_for(frame, attacker)

    assert guard_decision.role == "vip_guard_formation"
    assert attacker_decision.role == "vip_flank_attack"
    assert guard_decision.position != attacker_decision.position


def test_arena_wounded_bot_regroups_without_enemy_knowledge() -> None:
    wounded = _player(1, 2, health=30)
    teammate = _player(4, 2, position=(12.0, 8.0, 10.0))

    assert objective_goal_for(_frame("arena", wounded, teammate), wounded) == teammate.position


def test_classic_ctf_does_not_track_a_hidden_enemy_carrier() -> None:
    observer = _player(2, 2)
    own_base = ObjectiveSnapshot("ctf_base", 2, (20.0, 30.0, 60.0))
    stolen = ObjectiveSnapshot(
        "ctf_intel", 2, (250.0, 250.0, 60.0), carrier_id=9, state=2
    )
    enemy_intel = ObjectiveSnapshot("ctf_intel", 3, (400.0, 400.0, 60.0))

    decision = objective_decision_for(
        _frame(
            "cctf",
            observer,
            objectives=(own_base, stolen, enemy_intel),
        ),
        observer,
    )

    assert decision.role == "classic_ctf_attack_intel"
    assert decision.position == enemy_intel.position

    hidden_drop = ObjectiveSnapshot(
        "ctf_intel", 3, (275.0, 180.0, 60.0), state=1
    )
    assert objective_decision_for(
        _frame(
            "cctf",
            observer,
            objectives=(own_base, stolen, hidden_drop),
        ),
        observer,
    ) is None


def test_zombie_policy_changes_from_preparation_to_last_man_hunt() -> None:
    survivor = _player(1, 2)
    infected = _player(4, 3)
    survivor_anchor = ObjectiveSnapshot("team_anchor", 2, (64.0, 64.0, 50.0))
    zombie_anchor = ObjectiveSnapshot("team_anchor", 3, (448.0, 448.0, 50.0))

    preparation = objective_decision_for(
        _frame(
            "zom",
            survivor,
            objectives=(survivor_anchor, zombie_anchor),
            phase="countdown",
        ),
        survivor,
    )
    marker = ObjectiveSnapshot(
        "last_survivor", 2, survivor.position, carrier_id=survivor.player_id
    )
    hunt = objective_decision_for(
        _frame(
            "zom",
            infected,
            survivor,
            objectives=(survivor_anchor, zombie_anchor, marker),
            phase="active",
        ),
        infected,
    )

    assert preparation.role == "zombie_prepare_fortify"
    assert hunt.role == "zombie_hunt_last_survivor"
    assert hunt.position == survivor.position


def test_tdm_squads_advance_toward_enemy_side_instead_of_random_patrol() -> None:
    observer = _player(7, 2, position=(40.0, 40.0, 60.0))
    own_anchor = ObjectiveSnapshot("team_anchor", 2, (32.0, 32.0, 60.0))
    enemy_anchor = ObjectiveSnapshot("team_anchor", 3, (470.0, 470.0, 60.0))

    decision = objective_decision_for(
        _frame(
            "tdm",
            observer,
            objectives=(own_anchor, enemy_anchor),
        ),
        observer,
    )

    assert decision is not None
    assert decision.role == "team_assault_enemy_side"
    assert decision.position[0] > 450.0
    assert decision.position[1] > 450.0


def test_infected_zombie_hunts_nearest_survivor_without_visual_range() -> None:
    infected = _player(
        4,
        3,
        position=(450.0, 450.0, 60.0),
        class_id=4,
    )
    far_survivor = _player(1, 2, position=(30.0, 30.0, 60.0))
    near_survivor = _player(2, 2, position=(300.0, 330.0, 60.0))

    decision = objective_decision_for(
        _frame(
            "zom",
            infected,
            far_survivor,
            near_survivor,
            phase="active",
        ),
        infected,
    )

    assert decision is not None
    assert decision.role == "zombie_hunt_survivor"
    assert decision.position == near_survivor.position
