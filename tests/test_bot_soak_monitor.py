"""Accelerated bot-soak status and invariant monitoring."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import shared.constants as C

from server.bot_ai.messages import (
    BotAction,
    BotActionKind,
    BotIntent,
    MovementAffordance,
    MovementIntent,
)
from server.bot_ai.soak_monitor import BotSoakMonitor

from .test_bot_architecture import _player_snapshot
from server.game_constants import TEAM1, TEAM2


def _intent(
    frame_id: int,
    *,
    action=BotAction(),
    role="",
    jump=False,
    direction=(0.0, 0.0, 0.0),
    affordance=MovementAffordance.WALK,
    sprint=False,
):
    return BotIntent(
        bot_id=1,
        bot_generation=1,
        frame_id=frame_id,
        map_epoch=1,
        mode_epoch=1,
        topology_version=0,
        created_at=float(frame_id),
        expires_at=float(frame_id) + 1.0,
        movement=MovementIntent(
            direction=direction,
            jump=jump,
            sprint=sprint,
            affordance=affordance,
        ),
        action=action,
        debug_role=role,
    )


def test_monitor_flags_construction_while_enemy_is_point_blank() -> None:
    monitor = BotSoakMonitor(loop_seconds=3.0)
    observer = replace(
        _player_snapshot(1, TEAM1, (0.0, 0.0, 0.0), is_bot=True),
        class_id=int(C.CLASS_SOLDIER),
    )
    enemy = _player_snapshot(2, TEAM2, (4.0, 0.0, 0.0))
    build = _intent(
        1,
        action=BotAction(
            BotActionKind.BUILD,
            tool_id=int(C.BLOCK_TOOL),
            position=(1.0, 0.0, 1.0),
        ),
        role="fortify_build",
    )

    monitor.observe(1.0, observer, build, (observer, enemy))

    assert monitor.summary()["priority_violations"] == 1


def test_monitor_detects_stationary_repeated_action_and_jump_loops_once() -> None:
    monitor = BotSoakMonitor(loop_seconds=2.0, jump_loop_seconds=1.0)
    observer = _player_snapshot(1, TEAM1, (10.0, 10.0, 10.0), is_bot=True)
    enemy = _player_snapshot(2, TEAM2, (30.0, 10.0, 10.0))
    action = BotAction(
        BotActionKind.PLACE_PREFAB,
        tool_id=int(C.PREFAB_TOOL),
        position=(12.0, 10.0, 10.0),
        argument="prefab_test",
    )

    for index in range(8):
        monitor.observe(
            index * 0.5,
            observer,
            _intent(index + 1, action=action, role="stuck_build", jump=True),
            (observer, enemy),
        )

    summary = monitor.summary()
    assert summary["action_loops"] == 1
    assert summary["jump_loops"] == 1
    assert summary["max_stationary_seconds"] >= 3.0


def test_monitor_accepts_stationary_hold_without_action_as_non_looping() -> None:
    monitor = BotSoakMonitor(loop_seconds=1.0)
    observer = _player_snapshot(1, TEAM1, (10.0, 10.0, 10.0), is_bot=True)

    for index in range(6):
        monitor.observe(
            index * 0.5,
            observer,
            _intent(index + 1, role="fortify_hold"),
            (observer,),
        )

    assert monitor.summary()["action_loops"] == 0


def test_monitor_does_not_infer_water_from_low_dry_surface_height() -> None:
    monitor = BotSoakMonitor()
    observer = replace(
        _player_snapshot(1, TEAM1, (10.0, 10.0, 235.75), is_bot=True),
        wade=False,
    )

    monitor.observe(1.0, observer, _intent(1), (observer,))

    assert monitor.summary()["water_samples"] == 0


def test_monitor_flags_zero_motion_resource_navigation_stall_once() -> None:
    monitor = BotSoakMonitor(loop_seconds=1.0)
    observer = _player_snapshot(1, TEAM1, (10.0, 10.0, 10.0), is_bot=True)

    for index in range(6):
        monitor.observe(
            index * 0.5,
            observer,
            _intent(index + 1, role="resource"),
            (observer,),
        )

    assert monitor.summary()["navigation_stalls"] == 1


def test_monitor_flags_commanded_motion_without_displacement() -> None:
    monitor = BotSoakMonitor(loop_seconds=1.0)
    observer = _player_snapshot(1, TEAM1, (10.0, 10.0, 10.0), is_bot=True)

    for index in range(6):
        monitor.observe(
            index * 0.5,
            observer,
            _intent(
                index + 1,
                role="team_assault_enemy_side",
                direction=(1.0, 0.0, 0.0),
            ),
            (observer,),
        )

    assert monitor.summary()["navigation_stalls"] == 1


def test_monitor_flags_small_travel_oscillation_without_route_progress() -> None:
    monitor = BotSoakMonitor(loop_seconds=1.0)
    base = _player_snapshot(1, TEAM1, (10.0, 10.0, 10.0), is_bot=True)

    for index in range(15):
        observer = replace(
            base,
            position=(10.0 + float(index % 2), 10.0, 10.0),
        )
        monitor.observe(
            index * 0.5,
            observer,
            _intent(
                index + 1,
                role="team_assault_enemy_side",
                direction=(1.0 if index % 2 == 0 else -1.0, 0.0, 0.0),
            ),
            (observer,),
        )

    assert monitor.summary()["navigation_stalls"] == 1


def test_monitor_does_not_charge_a_prior_hold_to_new_travel() -> None:
    monitor = BotSoakMonitor(loop_seconds=1.0)
    observer = _player_snapshot(1, TEAM1, (10.0, 10.0, 10.0), is_bot=True)

    for index in range(6):
        monitor.observe(
            float(index),
            observer,
            _intent(index + 1, role="fortify_hold"),
            (observer,),
        )
    monitor.observe(
        6.0,
        observer,
        _intent(7, role="team_assault_enemy_side", direction=(1.0, 0.0, 0.0)),
        (observer,),
    )

    assert monitor.summary()["navigation_stalls"] == 0

    monitor.observe(
        7.0,
        observer,
        _intent(8, role="team_assault_enemy_side", direction=(1.0, 0.0, 0.0)),
        (observer,),
    )
    assert monitor.summary()["navigation_stalls"] == 1


def test_jump_build_placement_is_not_counted_as_jump_spam() -> None:
    monitor = BotSoakMonitor(loop_seconds=1.0, jump_loop_seconds=1.0)
    observer = _player_snapshot(1, TEAM1, (10.0, 10.0, 10.0), is_bot=True)
    placement = BotAction(
        BotActionKind.BUILD,
        tool_id=int(C.BLOCK_TOOL),
        position=(10.0, 10.0, 12.0),
    )

    for index in range(5):
        monitor.observe(
            float(index) * 0.5,
            observer,
            _intent(
                index + 1,
                action=placement,
                role="hole_jump_build_place",
                jump=True,
                affordance=MovementAffordance.BUILD_STEP,
            ),
            (observer,),
        )

    assert monitor.summary()["jump_loops"] == 0


def test_accelerated_soak_settles_actor_after_support_collapse() -> None:
    from scripts.bot_city_soak import CitySoak

    soak = object.__new__(CitySoak)
    soak.world = SimpleNamespace(
        solid=lambda x, y, z: (int(x), int(y), int(z)) == (5, 5, 20)
    )
    actor = SimpleNamespace(
        alive=True,
        wade=False,
        position=(5.5, 5.5, 10.75),
        grounded=False,
        airborne_until=99.0,
    )
    soak.actors = [actor]

    soak._settle_falling_actors()

    assert actor.position == (5.5, 5.5, 17.75)
    assert actor.grounded is True
    assert actor.airborne_until == 0.0


def test_accelerated_soak_integrates_drop_with_live_vertical_span() -> None:
    from scripts.bot_city_soak import CitySoak

    spans: list[int] = []
    terrain = SimpleNamespace(
        direction_is_traversable=lambda *_args, **_kwargs: True,
        classify=lambda _x, _y, _z, **kwargs: (
            spans.append(int(kwargs["vertical_span"]))
            or SimpleNamespace(support_z=14)
        ),
    )
    soak = object.__new__(CitySoak)
    soak.world = SimpleNamespace(
        action_planner=SimpleNamespace(terrain=terrain)
    )
    actor = SimpleNamespace(
        airborne_until=0.0,
        grounded=True,
        wade=False,
        position=(5.5, 5.5, 7.75),
    )

    soak._apply_intent(
        actor,
        _intent(
            1,
            direction=(1.0, 0.0, 0.0),
            affordance=MovementAffordance.DROP,
            sprint=True,
        ),
        1.0,
    )

    assert actor.position[0] > 6.5
    assert spans and set(spans) == {4}


def test_accelerated_soak_applies_centered_melee_to_terrain() -> None:
    from scripts.bot_city_soak import CitySoak

    deltas = []
    soak = object.__new__(CitySoak)
    soak.topology_version = 0
    soak.world = SimpleNamespace(
        solid=lambda x, y, z: (int(x), int(y), int(z)) == (4, 5, 6),
        apply=deltas.append,
    )
    actor = SimpleNamespace(
        airborne_until=0.0,
        grounded=True,
        wade=False,
        position=(4.5, 5.5, 3.75),
    )

    soak._apply_intent(
        actor,
        _intent(
            2,
            action=BotAction(
                BotActionKind.MELEE,
                tool_id=int(C.SPADE_TOOL),
                position=(4.5, 5.5, 6.5),
            ),
            role="hole_break_ceiling",
        ),
        2.0,
    )

    assert actor.last_action_accepted is True
    assert soak.topology_version == 1
    assert deltas[0].changed_cells[0].coordinate == (4, 5, 6)
