"""Focused regressions for the production simple bot equipment layer."""

from __future__ import annotations

from dataclasses import replace
import math
import random
from types import SimpleNamespace

import shared.constants as C

from server.bot_ai.director import _choose_bot_loadout
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import (
    BotAction,
    BotActionKind,
    BotIntentPriority,
    BotProfile,
    MovementAffordance,
    MovementIntent,
    ObjectiveSnapshot,
    PerceptionFrame,
    PlayerSnapshot,
)
from server.bot_ai.simple_navigation import RoutePlan, RouteStep
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState, _Goal
from server.class_selection import normalize_class_selection
from server.game_constants import TEAM1, TEAM2
from server.projectiles import PROJECTILE_SPECS


class _TacticalWorld:
    def __init__(
        self,
        *,
        visible: bool = True,
        water_step: RouteStep | None = None,
        route_step: RouteStep | None = None,
        route_steps_by_water: dict[bool, RouteStep | None] | None = None,
        bridge_line: (
            tuple[tuple[int, int, int], tuple[int, int, int]] | None
        ) = None,
    ) -> None:
        self.visible = bool(visible)
        self._water_step = water_step
        self._route_step = route_step
        self._route_steps_by_water = route_steps_by_water
        self._bridge_line = bridge_line
        self.plan_calls: list[bool] = []
        self.water_step_calls: list[dict[str, object]] = []

    def has_line_of_sight(self, _origin, _target) -> bool:
        return self.visible

    def water_step(self, _position, **kwargs) -> RouteStep | None:
        self.water_step_calls.append(dict(kwargs))
        return self._water_step

    def surface(self, x, y, _z, **_kwargs):
        support_z = 239 if int(x) >= 10 else 238
        return SimpleNamespace(
            x=int(x),
            y=int(y),
            support_z=support_z,
            position=(float(x) + 0.5, float(y) + 0.5, support_z - 2.25),
        )

    def plan(self, *_args, **kwargs) -> RoutePlan:
        allow_water = bool(kwargs.get("allow_water", False))
        self.plan_calls.append(allow_water)
        route_step = (
            self._route_steps_by_water[allow_water]
            if self._route_steps_by_water is not None
            else self._route_step
        )
        steps = (route_step,) if route_step is not None else ()
        return RoutePlan(steps, bool(steps), 1)

    def water_bridge_line(self, *_args, **_kwargs):
        return self._bridge_line


def _profile() -> BotProfile:
    return BotProfile(
        name="Tactics",
        difficulty="normal",
        skill=0.7,
        aggression=0.6,
        caution=0.5,
        teamwork=0.7,
        creativity=0.65,
        reaction_time=0.0,
        tracking_delay=0.1,
        turn_speed=4.0,
        turn_acceleration=12.0,
        recoil_control=0.6,
        burst_discipline=0.65,
        preferred_range=24.0,
        aim_noise=0.04,
    )


def _player(
    player_id: int,
    team: int,
    position: tuple[float, float, float],
    *,
    class_id: int = int(C.CLASS_SOLDIER),
    is_bot: bool = False,
    loadout: tuple[int, ...] = (int(C.MINIGUN_TOOL), int(C.SPADE_TOOL)),
    weapon_tool: int = int(C.MINIGUN_TOOL),
    tool: int | None = None,
    health: int = 100,
    oriented_stock: tuple[tuple[int, int], ...] = (),
    grounded: bool = True,
    wade: bool = False,
) -> PlayerSnapshot:
    selected_tool = int(weapon_tool if tool is None else tool)
    return PlayerSnapshot(
        player_id=int(player_id),
        generation=1,
        team=int(team),
        class_id=int(class_id),
        alive=True,
        spawned=True,
        position=position,
        eye=(position[0], position[1], position[2] - 1.0),
        orientation=(1.0, 0.0, 0.0),
        velocity=(0.0, 0.0, 0.0),
        health=int(health),
        tool=selected_tool,
        blocks=100,
        ammo_clip=30,
        ammo_reserve=120,
        is_bot=bool(is_bot),
        weapon_tool=int(weapon_tool),
        loadout=tuple(int(value) for value in loadout),
        oriented_stock=oriented_stock,
        grounded=bool(grounded),
        wade=bool(wade),
    )


def _frame(
    observer: PlayerSnapshot,
    *players: PlayerSnapshot,
    created_at: float = 100.0,
    objectives: tuple[ObjectiveSnapshot, ...] = (),
) -> PerceptionFrame:
    return PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=float(created_at),
        mode_id="tdm",
        players=(observer, *players),
        profile=_profile(),
        objectives=objectives,
        mode_phase="active",
    )


def test_stale_weapon_never_requests_fire_with_an_unowned_tool() -> None:
    smg = int(C.SMG_TOOL)
    turret = int(C.ROCKET_TURRET_TOOL)
    observer = _player(
        1,
        TEAM1,
        (10.0, 10.0, 20.0),
        class_id=int(C.CLASS_ENGINEER),
        is_bot=True,
        loadout=(smg, turret, int(C.PICKAXE_TOOL)),
        weapon_tool=int(C.ASSAULT_RIFLE_TOOL),
        tool=turret,
    )
    enemy = _player(2, TEAM2, (30.0, 10.0, 20.0))

    intent = SimpleBotBrain(_TacticalWorld()).decide(_frame(observer, enemy))

    assert intent is not None
    assert intent.tool_id == smg
    assert intent.action.kind is BotActionKind.FIRE
    assert intent.action.tool_id == smg
    assert intent.action.tool_id in observer.loadout


def test_rocket_requires_current_direct_sight() -> None:
    rocket = int(C.RPG_TOOL)
    observer = _player(
        1,
        TEAM1,
        (10.0, 10.0, 20.0),
        is_bot=True,
        loadout=(int(C.MINIGUN_TOOL), rocket, int(C.SPADE_TOOL)),
        oriented_stock=((rocket, 2),),
    )
    enemy = _player(2, TEAM2, (40.0, 10.0, 20.0))

    visible = SimpleBotBrain(_TacticalWorld(visible=True)).decide(
        _frame(observer, enemy)
    )
    hidden = SimpleBotBrain(_TacticalWorld(visible=False)).decide(
        _frame(observer, enemy)
    )

    assert visible is not None
    assert visible.action.kind is BotActionKind.ORIENTED
    assert visible.action.tool_id == rocket
    assert visible.action.end_position == enemy.eye
    assert visible.look is not None and visible.look.visible is True
    assert hidden is not None
    assert hidden.action.kind is not BotActionKind.ORIENTED


def test_visible_enemy_cannot_override_shore_route_while_wading() -> None:
    shore_step = RouteStep(
        (8.5, 10.5, 234.75),
        MovementAffordance.JUMP,
    )
    observer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
        wade=True,
    )
    enemy = _player(2, TEAM2, (20.5, 10.5, 234.75))

    intent = SimpleBotBrain(
        _TacticalWorld(visible=True, water_step=shore_step)
    ).decide(_frame(observer, enemy))

    assert intent is not None
    assert intent.debug_role == "water_exit"
    assert intent.priority is BotIntentPriority.SURVIVAL
    assert intent.movement.direction[0] < -0.9
    assert intent.movement.jump is True
    assert intent.look is not None
    assert intent.look.visible is False
    assert intent.look.target_player_id == -1
    assert intent.action.kind is BotActionKind.NONE


def test_open_water_swim_moves_straight_without_holding_jump() -> None:
    swim_step = RouteStep(
        (14.5, 10.5, 236.75),
        MovementAffordance.SWIM,
    )
    observer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
        grounded=False,
        wade=True,
    )

    intent = SimpleBotBrain(
        _TacticalWorld(water_step=swim_step)
    ).decide(_frame(observer))

    assert intent is not None
    assert intent.debug_role == "water_exit"
    assert intent.movement.affordance is MovementAffordance.SWIM
    assert intent.movement.direction[0] > 0.9
    assert intent.movement.jump is False


def test_bridge_personality_builds_a_block_line_instead_of_swimming() -> None:
    water_step = RouteStep(
        (11.5, 10.5, 236.75),
        MovementAffordance.SWIM,
    )
    world = _TacticalWorld(
        route_steps_by_water={False: None, True: water_step},
        bridge_line=((11, 10, 20), (16, 10, 20)),
    )
    brain = SimpleBotBrain(world)
    observer = _player(
        5,
        TEAM1,
        (10.5, 10.5, 17.75),
        is_bot=True,
        loadout=(
            int(C.MINIGUN_TOOL),
            int(C.BLOCK_TOOL),
            int(C.SPADE_TOOL),
        ),
    )
    teammates = (
        replace(observer, player_id=1),
        replace(observer, player_id=3),
    )
    frame = _frame(observer, *teammates)
    state = _BotState(
        map_epoch=frame.map_epoch,
        mode_epoch=frame.mode_epoch,
        life_id=observer.life_id,
    )
    goal = _Goal(
        ("bridge",),
        (100.5, 10.5, 17.75),
        "bridge_crossing",
        1.0,
        True,
    )

    intent = brain._navigation_intent(
        frame,
        observer,
        state,
        goal,
        frame.created_at,
    )

    assert world.plan_calls == [False]
    assert intent.debug_role == "bridge_crossing:bridge_builder"
    assert intent.action.kind is BotActionKind.BUILD_LINE
    assert intent.action.position == (11.0, 10.0, 20.0)
    assert intent.action.end_position == (16.0, 10.0, 20.0)
    assert intent.movement.jump is False


def test_combat_pursuit_preserves_swim_without_promoting_it_to_jump() -> None:
    swim_step = RouteStep(
        (11.5, 10.5, 236.75),
        MovementAffordance.SWIM,
    )
    world = _TacticalWorld(
        route_steps_by_water={False: None, True: swim_step},
    )
    observer = _player(
        3,
        TEAM1,
        (9.5, 10.5, 235.75),
        is_bot=True,
    )
    teammate = replace(observer, player_id=1)
    enemy = _player(2, TEAM2, (100.5, 10.5, 236.75))

    intent = SimpleBotBrain(world).decide(
        _frame(observer, teammate, enemy)
    )

    assert intent is not None
    assert intent.debug_role == "combat_pursuit"
    assert intent.movement.affordance is MovementAffordance.SWIM
    assert intent.movement.jump is False
    assert world.plan_calls == [True]


def test_wading_strategic_breach_hands_off_to_bank_recovery() -> None:
    breach_step = RouteStep(
        (11.5, 10.5, 236.75),
        MovementAffordance.BREACH,
    )
    world = _TacticalWorld(route_step=breach_step)
    brain = SimpleBotBrain(world)
    observer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
        grounded=False,
        wade=True,
    )
    frame = _frame(observer)
    state = _BotState(
        map_epoch=frame.map_epoch,
        mode_epoch=frame.mode_epoch,
        life_id=observer.life_id,
    )
    goal = _Goal(
        ("waterfront",),
        (100.5, 10.5, 236.75),
        "waterfront_goal",
        1.0,
        True,
    )

    intent = brain._navigation_intent(
        frame,
        observer,
        state,
        goal,
        frame.created_at,
        water_context=True,
    )

    assert intent.debug_role == "waterfront_goal:water_breach_handoff"
    assert intent.action.kind is BotActionKind.NONE
    assert intent.movement.direction == (0.0, 0.0, 0.0)
    assert state.water_recovery is True
    assert not state.route


def test_ctf_carrier_ignores_distant_fight_but_defends_at_close_range() -> None:
    observer = replace(
        _player(
            1,
            TEAM1,
            (10.0, 10.0, 20.0),
            is_bot=True,
        ),
        carried_entity_id=int(C.INTEL_PICKUP),
    )
    distant_enemy = _player(2, TEAM2, (40.0, 10.0, 20.0))
    own_base = ObjectiveSnapshot(
        "ctf_base", TEAM1, (80.0, 10.0, 20.0)
    )
    enemy_intel = ObjectiveSnapshot(
        "ctf_intel", TEAM2, (440.0, 10.0, 20.0)
    )
    route_step = RouteStep(
        (11.5, 10.5, 17.75),
        MovementAffordance.WALK,
    )
    frame = replace(
        _frame(
            observer,
            distant_enemy,
            objectives=(own_base, enemy_intel),
        ),
        mode_id="ctf",
    )

    objective_intent = SimpleBotBrain(
        _TacticalWorld(route_step=route_step)
    ).decide(frame)

    assert objective_intent is not None
    assert objective_intent.debug_role.startswith("ctf_capture")
    assert objective_intent.look is not None
    assert objective_intent.look.visible is False

    close_enemy = replace(distant_enemy, position=(14.0, 10.0, 20.0))
    close_enemy = replace(close_enemy, eye=(14.0, 10.0, 19.0))
    close_frame = replace(
        frame,
        players=(observer, close_enemy),
    )
    defence_intent = SimpleBotBrain(
        _TacticalWorld(route_step=route_step)
    ).decide(close_frame)

    assert defence_intent is not None
    assert defence_intent.look is not None
    assert defence_intent.look.visible is True
    assert defence_intent.look.target_player_id == close_enemy.player_id


def test_diamond_mode_directive_performs_real_surface_mining() -> None:
    observer = _player(
        1,
        TEAM1,
        (10.0, 10.0, 20.0),
        is_bot=True,
    )
    dropoff = ObjectiveSnapshot(
        "dia_dropoff", int(C.TEAM_NEUTRAL), (80.0, 90.0, 20.0), state=1
    )
    frame = replace(
        _frame(observer, objectives=(dropoff,)),
        mode_id="dia",
    )
    world = _TacticalWorld()
    world.solid = lambda _x, _y, _z: True

    intent = SimpleBotBrain(world).decide(frame)

    assert intent is not None
    assert intent.debug_role == "diamond_mine_blocks"
    assert intent.action.kind is BotActionKind.MELEE
    assert intent.action.tool_id in observer.loadout
    assert intent.action.position is not None


def test_dry_route_is_preferred_before_an_available_swim() -> None:
    dry_step = RouteStep(
        (10.5, 11.5, 17.75),
        MovementAffordance.WALK,
    )
    water_step = RouteStep(
        (11.5, 10.5, 17.75),
        MovementAffordance.SWIM,
    )
    world = _TacticalWorld(
        route_steps_by_water={False: dry_step, True: water_step},
    )
    brain = SimpleBotBrain(world)
    observer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 17.75),
        is_bot=True,
    )
    frame = _frame(observer)
    state = _BotState(
        map_epoch=frame.map_epoch,
        mode_epoch=frame.mode_epoch,
        life_id=observer.life_id,
    )
    goal = _Goal(
        ("dry-first",),
        (40.5, 40.5, 17.75),
        "dry_first",
        1.0,
        True,
    )

    intent = brain._navigation_intent(
        frame,
        observer,
        state,
        goal,
        frame.created_at,
    )

    assert intent.movement.affordance is MovementAffordance.WALK
    assert intent.movement.direction[1] > 0.9
    assert world.plan_calls == [False]


def test_airborne_false_wade_frame_does_not_release_water_commitment() -> None:
    shore_step = RouteStep(
        (9.5, 10.5, 235.75),
        MovementAffordance.JUMP,
    )
    world = _TacticalWorld(water_step=shore_step)
    brain = SimpleBotBrain(world)
    observer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
        grounded=False,
        wade=True,
    )
    first_frame = _frame(observer, created_at=100.0)

    first = brain.decide(first_frame)
    airborne = replace(observer, wade=False, grounded=False)
    second = brain.decide(replace(
        first_frame,
        frame_id=2,
        created_at=100.2,
        players=(airborne,),
    ))

    state = brain._states[(observer.player_id, observer.generation)]
    assert first is not None and first.debug_role == "water_exit"
    assert second is not None and second.debug_role == "water_exit"
    assert state.water_committed is True


def test_false_wade_frame_on_water_starts_water_commitment() -> None:
    """A decision-phase wade alias cannot leave dry navigation in control."""

    shore_step = RouteStep(
        (9.5, 10.5, 235.75),
        MovementAffordance.JUMP,
    )
    world = _TacticalWorld(water_step=shore_step)
    brain = SimpleBotBrain(world)
    observer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 237.7),
        is_bot=True,
        grounded=True,
        wade=False,
    )

    intent = brain.decide(_frame(observer, created_at=100.0))

    assert intent is not None
    assert intent.debug_role == "water_exit"
    state = brain._states[(observer.player_id, observer.generation)]
    assert state.water_committed is True


def test_airborne_bank_lip_releases_water_commitment_on_live_dry_support() -> None:
    shore_step = RouteStep(
        (9.5, 10.5, 235.75),
        MovementAffordance.JUMP,
    )
    world = _TacticalWorld(water_step=shore_step)
    brain = SimpleBotBrain(world)
    swimmer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
        grounded=False,
        wade=True,
    )
    first_frame = _frame(swimmer, created_at=100.0)
    assert brain.decide(first_frame) is not None

    over_bank = replace(
        swimmer,
        position=(9.5, 10.5, 235.75),
        eye=(9.5, 10.5, 234.75),
        wade=False,
        grounded=False,
    )
    second = brain.decide(replace(
        first_frame,
        frame_id=2,
        created_at=100.2,
        players=(over_bank,),
    ))

    state = brain._states[(swimmer.player_id, swimmer.generation)]
    assert second is not None
    assert second.debug_role != "water_exit"
    assert state.water_committed is False


def test_raised_jump_above_built_water_step_releases_water_commitment() -> None:
    world = _TacticalWorld(
        water_step=RouteStep(
            (9.5, 10.5, 235.75),
            MovementAffordance.JUMP,
        )
    )
    brain = SimpleBotBrain(world)
    swimmer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
        grounded=False,
        wade=True,
    )
    first_frame = _frame(swimmer, created_at=100.0)
    assert brain.decide(first_frame) is not None

    raised = replace(
        swimmer,
        position=(9.5, 10.5, 234.6),
        eye=(9.5, 10.5, 233.6),
        wade=False,
        grounded=False,
    )
    second = brain.decide(replace(
        first_frame,
        frame_id=2,
        created_at=100.2,
        players=(raised,),
    ))

    state = brain._states[(swimmer.player_id, swimmer.generation)]
    assert second is not None
    assert state.water_committed is False


def test_reached_goal_in_water_uses_nearest_shore_not_goal_coordinate() -> None:
    shore_step = RouteStep(
        (9.5, 10.5, 235.75),
        MovementAffordance.JUMP,
    )
    world = _TacticalWorld(water_step=shore_step)
    brain = SimpleBotBrain(world)
    observer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
        grounded=False,
        wade=True,
    )
    frame = _frame(observer)
    brain.reset_for_map(frame.map_epoch)
    state = _BotState(
        map_epoch=frame.map_epoch,
        mode_epoch=frame.mode_epoch,
        life_id=observer.life_id,
        goal=_Goal(
            ("water-contact",),
            (11.0, 10.5, 236.75),
            "combat_pursuit",
            1.0,
            True,
        ),
    )
    brain._states[(observer.player_id, observer.generation)] = state

    intent = brain.decide(frame)

    assert intent is not None and intent.debug_role == "water_exit"
    assert state.water_goal_reached is True
    assert world.water_step_calls[-1]["preferred_goal"] is None


def test_failed_shore_edge_uses_water_flow_before_strategic_route() -> None:
    water_step = RouteStep(
        (10.5, 11.5, 236.75),
        MovementAffordance.WALK,
    )
    strategic_step = RouteStep(
        (9.5, 10.5, 235.75),
        MovementAffordance.JUMP,
    )
    observer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
        wade=True,
    )
    frame = _frame(observer)
    world = _TacticalWorld(
        water_step=water_step,
        route_step=strategic_step,
    )
    brain = SimpleBotBrain(world)
    state = _BotState(
        map_epoch=frame.map_epoch,
        mode_epoch=frame.mode_epoch,
        life_id=observer.life_id,
    )
    state.goal = _Goal(
        key=("strategic",),
        position=(1.5, 10.5, 235.75),
        role="strategic",
        arrival_radius=1.0,
        sprint=True,
    )
    state.blocked_edges[((10, 10, 239), (9, 10, 238))] = (
        frame.created_at + 20.0
    )
    brain._states[(observer.player_id, observer.generation)] = state

    intent = brain.decide(frame)

    assert intent is not None
    assert intent.debug_role == "water_exit"
    assert intent.movement.direction[1] > 0.9
    assert world.water_step_calls[-1]["preferred_goal"] is None


def test_near_water_goal_stays_in_shore_recovery_until_dry() -> None:
    water_step = RouteStep(
        (10.5, 11.5, 236.75),
        MovementAffordance.WALK,
    )
    strategic_step = RouteStep(
        (9.5, 10.5, 235.75),
        MovementAffordance.JUMP,
    )
    observer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
        wade=True,
    )
    frame = _frame(observer)
    world = _TacticalWorld(
        water_step=water_step,
        route_step=strategic_step,
    )
    brain = SimpleBotBrain(world)
    brain.reset_for_map(frame.map_epoch)
    state = _BotState(
        map_epoch=frame.map_epoch,
        mode_epoch=frame.mode_epoch,
        life_id=observer.life_id,
    )
    state.goal = _Goal(
        key=("near-water-goal",),
        position=(11.0, 10.5, 236.75),
        role="combat_pursuit",
        arrival_radius=1.0,
        sprint=True,
    )
    brain._states[(observer.player_id, observer.generation)] = state

    first = brain.decide(frame)
    state.goal = replace(state.goal, position=(30.5, 10.5, 236.75))
    second = brain.decide(replace(
        frame,
        frame_id=2,
        created_at=frame.created_at + 0.2,
    ))

    assert first is not None and first.debug_role == "water_exit"
    assert second is not None and second.debug_role == "water_exit"
    assert state.water_recovery is True


def test_wading_segment_completion_hands_off_to_shore_recovery() -> None:
    shore_step = RouteStep(
        (10.5, 11.5, 236.75),
        MovementAffordance.WALK,
    )
    world = _TacticalWorld(water_step=shore_step, route_step=None)
    brain = SimpleBotBrain(world)
    observer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
        grounded=False,
        wade=True,
    )
    frame = _frame(observer)
    brain.reset_for_map(frame.map_epoch)
    state = _BotState(
        map_epoch=frame.map_epoch,
        mode_epoch=frame.mode_epoch,
        life_id=observer.life_id,
        goal=_Goal(
            ("water-segment",),
            (100.5, 10.5, 236.75),
            "water_segment",
            1.0,
            True,
        ),
    )
    brain._states[(observer.player_id, observer.generation)] = state

    intent = brain.decide(frame)

    assert intent is not None
    assert intent.debug_role == "water_exit"
    assert state.water_recovery is True
    assert world.water_step_calls[-1]["preferred_goal"] is None


def test_fallback_water_exit_blacklists_its_own_stalled_edge() -> None:
    shore_step = RouteStep(
        (8.5, 10.5, 235.75),
        MovementAffordance.JUMP,
    )
    observer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
        wade=True,
    )
    brain = SimpleBotBrain(_TacticalWorld(water_step=shore_step))
    first_frame = _frame(observer, created_at=100.0)

    first = brain.decide(first_frame)
    second = brain.decide(replace(
        first_frame,
        frame_id=2,
        created_at=102.0,
    ))

    assert first is not None and first.debug_role == "water_exit"
    assert second is not None
    assert second.debug_role == "water_exit:edge_blocked"
    state = brain._states[(observer.player_id, observer.generation)]
    assert state.blocked_edges


def test_alternating_water_steps_cannot_reset_whole_swim_progress() -> None:
    left_step = RouteStep(
        (9.5, 10.5, 235.75),
        MovementAffordance.JUMP,
    )
    upper_step = RouteStep(
        (10.5, 9.5, 236.75),
        MovementAffordance.SWIM,
    )
    world = _TacticalWorld(water_step=left_step)
    brain = SimpleBotBrain(world)
    observer = _player(
        1,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
        grounded=False,
        wade=True,
    )
    frame = _frame(observer, created_at=100.0)

    intent = brain.decide(frame)
    for index in range(1, 6):
        world._water_step = upper_step if index % 2 else left_step
        intent = brain.decide(replace(
            frame,
            frame_id=index + 1,
            created_at=100.0 + float(index),
        ))

    assert intent is not None
    assert intent.debug_role == "water_exit:cycle_blocked"
    state = brain._states[(observer.player_id, observer.generation)]
    assert state.water_recovery is True
    assert state.blocked_edges


def test_stagnant_crowd_follower_blacklists_its_edge_and_replans() -> None:
    route_step = RouteStep(
        (11.5, 10.5, 17.75),
        MovementAffordance.WALK,
    )
    world = _TacticalWorld(route_step=route_step)
    brain = SimpleBotBrain(world)
    observer = _player(
        3,
        TEAM1,
        (10.5, 10.5, 17.75),
        is_bot=True,
    )
    teammates = tuple(
        _player(
            player_id,
            TEAM1,
            (10.5 + offset, 10.5, 17.75),
            is_bot=True,
        )
        for player_id, offset in ((1, -0.5), (5, 0.5), (7, 1.0))
    )
    frame = _frame(observer, *teammates, created_at=100.0)
    goal = _Goal(
        ("far-crowd-goal",),
        (100.5, 10.5, 17.75),
        "far_crowd_goal",
        1.0,
        True,
    )
    state = _BotState(
        map_epoch=frame.map_epoch,
        mode_epoch=frame.mode_epoch,
        life_id=observer.life_id,
    )

    moving = brain._navigation_intent(
        frame,
        observer,
        state,
        goal,
        frame.created_at,
    )
    detour_frame = replace(
        frame,
        frame_id=2,
        created_at=103.1,
    )
    detour = brain._navigation_intent(
        detour_frame,
        observer,
        state,
        goal,
        detour_frame.created_at,
    )

    assert moving.debug_role == "far_crowd_goal"
    assert detour.debug_role == "far_crowd_goal:crowd_detour"
    assert detour.movement.direction == (0.0, 0.0, 0.0)
    assert state.blocked_edges
    assert min(state.blocked_edges.values()) == detour_frame.created_at + 20.0
    assert not state.route
    assert state.crowd_detour_goal is not None
    assert math.hypot(
        state.crowd_detour_goal[0] - observer.position[0],
        state.crowd_detour_goal[1] - observer.position[1],
    ) >= 8.0


def test_physical_navigation_stall_survives_goal_role_switches() -> None:
    route_step = RouteStep(
        (9.5, 10.5, 235.75),
        MovementAffordance.JUMP,
    )
    world = _TacticalWorld(route_step=route_step)
    brain = SimpleBotBrain(world)
    observer = _player(
        3,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
    )
    frame = _frame(observer, created_at=100.0)
    state = _BotState(
        map_epoch=frame.map_epoch,
        mode_epoch=frame.mode_epoch,
        life_id=observer.life_id,
    )
    pursuit = _Goal(
        ("pursuit", 1),
        (1.5, 10.5, 235.75),
        "combat_pursuit",
        1.0,
        True,
    )
    last_seen = _Goal(
        ("last-seen", 1),
        (1.5, 11.5, 235.75),
        "chase_last_seen",
        1.0,
        True,
    )

    first = brain._navigation_intent(
        frame,
        observer,
        state,
        pursuit,
        frame.created_at,
    )
    switched_frame = replace(frame, frame_id=2, created_at=101.0)
    switched = brain._navigation_intent(
        switched_frame,
        observer,
        state,
        last_seen,
        switched_frame.created_at,
    )
    stalled_frame = replace(frame, frame_id=3, created_at=102.6)
    stalled = brain._navigation_intent(
        stalled_frame,
        observer,
        state,
        pursuit,
        stalled_frame.created_at,
    )

    assert first.debug_role == "combat_pursuit"
    assert switched.debug_role == "chase_last_seen"
    assert stalled.debug_role == "combat_pursuit:physical_edge_blocked"
    assert state.blocked_edges


def test_navigation_cycle_inside_three_blocks_forces_replan() -> None:
    route_step = RouteStep(
        (9.5, 10.5, 235.75),
        MovementAffordance.JUMP,
    )
    world = _TacticalWorld(route_step=route_step)
    brain = SimpleBotBrain(world)
    observer = _player(
        3,
        TEAM1,
        (10.5, 10.5, 236.75),
        is_bot=True,
    )
    frame = _frame(observer, created_at=100.0)
    state = _BotState(
        map_epoch=frame.map_epoch,
        mode_epoch=frame.mode_epoch,
        life_id=observer.life_id,
    )
    goal = _Goal(
        ("cycle",),
        (1.5, 10.5, 235.75),
        "cycle_goal",
        1.0,
        True,
    )

    intent = brain._navigation_intent(
        frame,
        observer,
        state,
        goal,
        frame.created_at,
    )
    for index in range(1, 6):
        position = (
            11.1 if index % 2 else 10.5,
            10.5,
            236.75,
        )
        moving = replace(observer, position=position)
        created_at = 100.0 + float(index)
        intent = brain._navigation_intent(
            replace(
                frame,
                frame_id=index + 1,
                created_at=created_at,
                players=(moving,),
            ),
            moving,
            state,
            goal,
            created_at,
        )

    assert intent.debug_role == "cycle_goal:physical_edge_blocked"
    assert state.blocked_edges


def test_blocked_combat_strafe_cycles_through_corridor_directions() -> None:
    smg = int(C.SMG_TOOL)
    observer = _player(
        1,
        TEAM1,
        (10.0, 10.0, 20.0),
        is_bot=True,
        loadout=(smg, int(C.SPADE_TOOL)),
        weapon_tool=smg,
    )
    enemy = _player(2, TEAM2, (20.0, 10.0, 20.0))
    brain = SimpleBotBrain(_TacticalWorld(visible=True))

    intents = []
    for index in range(16):
        intents.append(
            brain.decide(
                _frame(
                    observer,
                    enemy,
                    created_at=100.0 + index * 0.2,
                )
            )
        )

    assert intents[0] is not None
    assert intents[0].movement.direction[1] < -0.9
    assert intents[5] is not None
    assert intents[5].movement.direction[1] > 0.9
    assert intents[10] is not None
    assert intents[10].movement.direction[0] > 0.9
    assert intents[15] is not None
    assert intents[15].movement.direction[0] < -0.9


def test_blocked_combat_movement_falls_back_to_voxel_route() -> None:
    smg = int(C.SMG_TOOL)
    observer = _player(
        1,
        TEAM1,
        (10.0, 10.0, 20.0),
        is_bot=True,
        loadout=(smg, int(C.SPADE_TOOL)),
        weapon_tool=smg,
    )
    enemy = _player(2, TEAM2, (20.0, 10.0, 20.0))
    route_step = RouteStep((10.5, 11.5, 17.75), MovementAffordance.WALK)
    brain = SimpleBotBrain(
        _TacticalWorld(visible=True, route_step=route_step)
    )

    intent = None
    for index in range(21):
        intent = brain.decide(
            _frame(observer, enemy, created_at=100.0 + index * 0.2)
        )

    assert intent is not None
    assert intent.debug_role == "combat_pursuit"
    assert intent.movement.direction[1] > 0.9
    assert intent.debug_path[-1] == route_step.waypoint


def test_crowd_adjustment_never_invents_idle_movement() -> None:
    observer = _player(3, TEAM1, (10.0, 10.0, 20.0), is_bot=True)
    teammate = replace(observer, player_id=1)
    frame = _frame(observer, teammate)

    movement = SimpleBotBrain._crowd_adjusted_movement(
        frame,
        MovementIntent(),
        action=BotAction(),
    )

    assert movement.direction == (0.0, 0.0, 0.0)


def test_swim_crowd_adjustment_preserves_forward_progress() -> None:
    observer = _player(3, TEAM1, (10.0, 10.0, 20.0), is_bot=True)
    teammate = replace(observer, player_id=1)
    frame = _frame(observer, teammate)

    movement = SimpleBotBrain._crowd_adjusted_movement(
        frame,
        MovementIntent(
            direction=(1.0, 0.0, 0.0),
            affordance=MovementAffordance.SWIM,
        ),
        action=BotAction(),
    )

    assert movement.direction[0] >= 0.35
    assert abs(
        movement.direction[0] ** 2 + movement.direction[1] ** 2 - 1.0
    ) < 1e-6


def test_rejected_oriented_attempt_falls_back_to_firearm_next_decision() -> None:
    rocket = int(C.RPG_TOOL)
    observer = _player(
        1,
        TEAM1,
        (10.0, 10.0, 20.0),
        is_bot=True,
        loadout=(int(C.MINIGUN_TOOL), rocket, int(C.SPADE_TOOL)),
        oriented_stock=((rocket, 2),),
    )
    enemy = _player(2, TEAM2, (40.0, 10.0, 20.0))
    brain = SimpleBotBrain(_TacticalWorld(visible=True))
    first_frame = _frame(observer, enemy)

    first = brain.decide(first_frame)
    rejected = replace(
        observer,
        last_action_kind=BotActionKind.ORIENTED.value,
        last_action_accepted=False,
        last_action_frame=first_frame.frame_id,
        last_action_at=100.05,
    )
    second = brain.decide(
        replace(
            first_frame,
            frame_id=2,
            created_at=100.2,
            players=(rejected, enemy),
        )
    )

    assert first is not None and first.action.kind is BotActionKind.ORIENTED
    assert second is not None and second.action.kind is BotActionKind.FIRE
    assert second.tool_id == int(C.MINIGUN_TOOL)


def test_gateway_direct_rocket_ray_fails_closed_on_intervening_wall() -> None:
    spec = PROJECTILE_SPECS[int(C.RPG_TOOL)]
    player = SimpleNamespace(
        id=1,
        team=TEAM1,
        eye=(10.0, 10.0, 19.0),
    )
    blocked_world = SimpleNamespace(
        raycast=lambda _x, _y, _z, _dx, _dy, _dz, length: (
            None if length <= float(spec.blast_radius) + 3.0 else (25, 10, 19)
        )
    )
    gateway = BotActionGateway(
        SimpleNamespace(
            world_manager=blocked_world,
            players={1: player},
        )
    )

    assert not gateway._oriented_launch_safe(
        player,
        (1.0, 0.0, 0.0),
        spec,
        (40.0, 10.0, 19.0),
    )
    gateway.server.world_manager.raycast = lambda *_args: None
    assert gateway._oriented_launch_safe(
        player,
        (1.0, 0.0, 0.0),
        spec,
        (40.0, 10.0, 19.0),
    )


def test_rocket_falls_back_to_firearm_when_teammate_is_in_blast_area() -> None:
    rocket = int(C.RPG_TOOL)
    observer = _player(
        1,
        TEAM1,
        (10.0, 10.0, 20.0),
        is_bot=True,
        loadout=(int(C.MINIGUN_TOOL), rocket, int(C.SPADE_TOOL)),
        oriented_stock=((rocket, 2),),
    )
    enemy = _player(2, TEAM2, (40.0, 10.0, 20.0))
    teammate = _player(3, TEAM1, (39.0, 10.0, 20.0))

    intent = SimpleBotBrain(_TacticalWorld()).decide(
        _frame(observer, enemy, teammate)
    )

    assert intent is not None
    assert intent.action.kind is BotActionKind.FIRE
    assert intent.action.tool_id == int(C.MINIGUN_TOOL)


def test_medic_moves_to_and_places_pack_for_most_injured_teammate() -> None:
    medpack = int(C.MEDPACK_TOOL)
    observer = _player(
        1,
        TEAM1,
        (10.0, 10.0, 20.0),
        class_id=int(C.CLASS_MEDIC),
        is_bot=True,
        loadout=(int(C.LIGHT_MACHINE_GUN_TOOL), medpack, int(C.PICKAXE_TOOL)),
        weapon_tool=int(C.LIGHT_MACHINE_GUN_TOOL),
    )
    wounded = _player(3, TEAM1, (13.0, 10.0, 20.0), health=35)

    intent = SimpleBotBrain(_TacticalWorld()).decide(
        _frame(observer, wounded)
    )

    assert intent is not None
    assert intent.debug_role == "medic_place_medpack"
    assert intent.action.kind is BotActionKind.DEPLOY
    assert intent.action.tool_id == medpack
    assert intent.action.position == wounded.position
    assert intent.action.face == 4


def test_turret_is_deployed_facing_a_nearby_strategic_route() -> None:
    turret = int(C.ROCKET_TURRET_TOOL)
    observer = _player(
        1,
        TEAM1,
        (10.0, 10.0, 20.0),
        class_id=int(C.CLASS_ENGINEER),
        is_bot=True,
        loadout=(int(C.SMG_TOOL), turret, int(C.PICKAXE_TOOL)),
        weapon_tool=int(C.SMG_TOOL),
    )
    objective = ObjectiveSnapshot(
        "team_anchor",
        TEAM2,
        (10.0, 10.0, 20.0),
    )

    intent = SimpleBotBrain(_TacticalWorld()).decide(
        _frame(observer, objectives=(objective,))
    )

    assert intent is not None
    assert intent.action.kind is BotActionKind.DEPLOY
    assert intent.action.tool_id == turret
    assert intent.action.position == observer.position
    assert intent.debug_role.startswith("deploy_")


def test_bot_loadout_draws_valid_variants_instead_of_only_defaults() -> None:
    class_id = int(C.CLASS_SOLDIER)
    selections = {
        _choose_bot_loadout(class_id, random.Random(seed))
        for seed in range(48)
    }

    assert len(selections) >= 10
    for requested in selections:
        normalized = normalize_class_selection(class_id, requested)
        assert set(requested).issubset(normalized.loadout)
        assert int(C.MINIGUN_TOOL) in requested or int(C.ASSAULT_RIFLE_TOOL) in requested
        assert int(C.RPG_TOOL) in requested or int(C.RPG2_TOOL) in requested
