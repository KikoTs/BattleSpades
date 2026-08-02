"""Focused regressions for the production simple bot equipment layer."""

from __future__ import annotations

from dataclasses import replace
import random
from types import SimpleNamespace

import shared.constants as C

from server.bot_ai.director import _choose_bot_loadout
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import (
    BotActionKind,
    BotIntentPriority,
    BotProfile,
    MovementAffordance,
    ObjectiveSnapshot,
    PerceptionFrame,
    PlayerSnapshot,
)
from server.bot_ai.simple_navigation import RouteStep
from server.bot_ai.simple_worker import SimpleBotBrain
from server.class_selection import normalize_class_selection
from server.game_constants import TEAM1, TEAM2
from server.projectiles import PROJECTILE_SPECS


class _TacticalWorld:
    def __init__(
        self,
        *,
        visible: bool = True,
        water_step: RouteStep | None = None,
    ) -> None:
        self.visible = bool(visible)
        self._water_step = water_step

    def has_line_of_sight(self, _origin, _target) -> bool:
        return self.visible

    def water_step(self, _position) -> RouteStep | None:
        return self._water_step


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
        grounded=True,
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
