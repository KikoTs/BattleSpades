"""Deterministic objective-policy tests for the isolated bot worker."""

from __future__ import annotations

import time

from server.bot_ai.messages import ObjectiveSnapshot, PerceptionFrame, PlayerSnapshot
from server.bot_ai.policies import (
    ModeBotPosture,
    mode_decision_allows_combat,
    mode_strategy_for,
    objective_decision_for,
    objective_goal_for,
)
from server.mode_data import MODES


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


def test_multihill_bots_claim_enemy_hill_and_fortify_friendly_hill() -> None:
    observer = _player(3, 2)
    neutral = ObjectiveSnapshot("mh_hill", 1, (200.0, 210.0, 55.0))
    claim = objective_decision_for(
        _frame("mh", observer, objectives=(neutral,)), observer
    )
    friendly = ObjectiveSnapshot("mh_hill", 2, neutral.position)
    defend = objective_decision_for(
        _frame("mh", observer, objectives=(friendly,)), observer
    )

    assert claim.role == "multihill_claim"
    assert claim.position == neutral.position
    assert defend.role == "multihill_defend"
    assert defend.directive == "fortify"


def test_demolition_bots_build_then_split_defence_and_assault() -> None:
    builder = _player(4, 2)
    attacker = _player(5, 2)
    own = ObjectiveSnapshot("dem_base", 2, (70.0, 80.0, 58.0))
    enemy = ObjectiveSnapshot("dem_base", 3, (430.0, 420.0, 58.0))
    build = objective_decision_for(
        _frame(
            "dem", builder, objectives=(own, enemy), phase="building"
        ),
        builder,
    )
    defend = objective_decision_for(
        _frame("dem", builder, objectives=(own, enemy)), builder
    )
    assault = objective_decision_for(
        _frame("dem", attacker, objectives=(own, enemy)), attacker
    )

    assert build.role == "demolition_build_defences"
    assert build.directive == "fortify"
    assert defend.role == "demolition_defend_base"
    assert assault.role == "demolition_assault_base"
    assert assault.position == enemy.position


def test_territory_bots_capture_hostile_and_periodically_defend_owned() -> None:
    defender = _player(4, 2, position=(50.0, 50.0, 60.0))
    attacker = _player(5, 2, position=(50.0, 50.0, 60.0))
    owned = ObjectiveSnapshot("tc_territory", 2, (100.0, 100.0, 60.0))
    neutral = ObjectiveSnapshot("tc_territory", 1, (200.0, 100.0, 60.0))

    defend = objective_decision_for(
        _frame("tc", defender, objectives=(owned, neutral)), defender
    )
    capture = objective_decision_for(
        _frame("tc", attacker, objectives=(owned, neutral)), attacker
    )

    assert defend.role == "territory_defend"
    assert capture.role == "territory_capture"
    assert capture.position == neutral.position


def test_diamond_carrier_cashes_in_and_teammate_collects_loose_diamond() -> None:
    carrier = _player(1, 2, carried=15)
    collector = _player(2, 2)
    dropoff = ObjectiveSnapshot("dia_dropoff", 1, (80.0, 90.0, 60.0), state=1)
    diamond = ObjectiveSnapshot("dia_diamond", 1, (220.0, 230.0, 60.0))

    cash = objective_decision_for(
        _frame("dia", carrier, objectives=(dropoff, diamond)), carrier
    )
    collect = objective_decision_for(
        _frame("dia", collector, objectives=(dropoff, diamond)), collector
    )

    assert cash.role == "diamond_cash_in"
    assert cash.position == dropoff.position
    assert collect.role == "diamond_collect"
    assert collect.position == diamond.position

    mining = objective_decision_for(
        _frame("dia", collector, objectives=(dropoff,)), collector
    )
    assert mining.role == "diamond_mine_blocks"
    assert mining.directive == "mine"


def test_occupation_attackers_deliver_and_defenders_dispose_live_bomb() -> None:
    attacker = _player(1, 2, carried=14)
    defender = _player(2, 3, position=(300.0, 250.0, 60.0), carried=14)
    target = ObjectiveSnapshot("oc_target", 3, (430.0, 250.0, 60.0))

    deliver = objective_decision_for(
        _frame("oc", attacker, objectives=(target,)), attacker
    )
    dispose = objective_decision_for(
        _frame("oc", defender, objectives=(target,)), defender
    )

    assert deliver.role == "occupation_deliver_bomb"
    assert deliver.position == target.position
    assert dispose.role == "occupation_dispose_bomb"
    assert dispose.position[0] < defender.position[0]


def test_every_protocol_and_public_mode_has_an_explicit_bot_strategy() -> None:
    for code in (*MODES, "arena"):
        strategy = mode_strategy_for(code)
        assert strategy.code == code
        assert strategy.objective
        assert isinstance(strategy.default_posture, ModeBotPosture)

    assert mode_strategy_for("classic-ctf").code == "cctf"
    assert mode_strategy_for("diamond_mine").code == "dia"
    assert mode_strategy_for("territory-control").code == "tc"


def test_mode_roles_select_materially_different_playstyles() -> None:
    carrier = _player(1, 2, carried=15)
    collector = _player(2, 2)
    guard = _player(4, 2)
    dropoff = ObjectiveSnapshot(
        "dia_dropoff", 1, (80.0, 90.0, 60.0), state=1
    )
    diamond = ObjectiveSnapshot("dia_diamond", 1, (220.0, 230.0, 60.0))

    cash = objective_decision_for(
        _frame("dia", carrier, objectives=(dropoff, diamond)), carrier
    )
    collect = objective_decision_for(
        _frame("dia", collector, objectives=(dropoff, diamond)), collector
    )
    defend = objective_decision_for(
        _frame("dia", guard, objectives=(dropoff,)), guard
    )

    assert cash.posture is ModeBotPosture.EVASIVE
    assert cash.objective_priority == 1.0
    assert collect.posture is ModeBotPosture.BALANCED
    assert defend.posture is ModeBotPosture.DEFEND
    assert defend.directive == "fortify"


def test_objective_commitment_limits_combat_without_disabling_self_defence() -> None:
    carrier = _player(1, 2, carried=15)
    dropoff = ObjectiveSnapshot(
        "dia_dropoff", 1, (80.0, 90.0, 60.0), state=1
    )
    decision = objective_decision_for(
        _frame("dia", carrier, objectives=(dropoff,)), carrier
    )
    distant_enemy = _player(9, 3, position=(40.0, 0.0, 10.0))
    close_enemy = _player(10, 3, position=(4.0, 0.0, 10.0))

    assert not mode_decision_allows_combat(
        decision,
        carrier,
        distant_enemy,
        now=time.monotonic(),
    )
    assert mode_decision_allows_combat(
        decision,
        carrier,
        close_enemy,
        now=time.monotonic(),
    )


def test_defender_responds_to_threat_near_protected_objective() -> None:
    defender = _player(4, 2, position=(70.0, 100.0, 60.0))
    hill = ObjectiveSnapshot("mh_hill", 2, (100.0, 100.0, 60.0))
    decision = objective_decision_for(
        _frame("mh", defender, objectives=(hill,)), defender
    )
    objective_threat = _player(9, 3, position=(125.0, 100.0, 60.0))

    assert decision.posture is ModeBotPosture.DEFEND
    assert mode_decision_allows_combat(
        decision,
        defender,
        objective_threat,
        now=time.monotonic(),
    )
