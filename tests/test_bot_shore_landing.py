"""Captured London bank geometry must produce a sustained native dry exit."""

import asyncio
from dataclasses import replace
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import shared.constants as C

from server.bot_ai.director import BotDirector
from server.bot_ai.messages import VoxelChange, WorldDelta
from server.bot_ai.simple_navigation import SimpleVoxelWorld
from server.bot_ai.simple_worker import SimpleBotBrain
from server.config import ServerConfig
from server.main import BattleSpadesServer
from tests.test_simple_bot_tactics import _frame


@pytest.mark.parametrize("tool", [int(C.SPADE_TOOL), int(C.MACHETE_TOOL),
                                  int(C.PICKAXE_TOOL), int(C.SUPERSPADE_TOOL)])
def test_london_high_bank_excavation_preserves_dry_floor_and_native_landing(monkeypatch, tool):
    async def scenario():
        server = BattleSpadesServer(ServerConfig())
        assert server.world_manager.load_map("London")
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        bot = await director.add_bot(team=2, class_id=(3 if tool == int(C.SUPERSPADE_TOOL)
                                                       else int(C.CLASS_SOLDIER)))
        bot.loadout = [tool]
        bot.set_tool(tool, raw=True)
        bot.set_position(296.5, 228.5, 236.75)
        bot.wade, bot.grounded = True, False
        world = SimpleVoxelWorld()
        world.load(director._make_map_snapshot(current=True))
        mutation_cells = []
        def mutation(x, y, z, solid, color, version):
            mutation_cells.append((x, y, z, solid))
            world.apply(WorldDelta(world.map_epoch, version,
                                   (VoxelChange(x, y, z, solid, color),)))
        subscription = server.world_manager.subscribe_mutations(mutation)
        brain = SimpleBotBrain(world)
        # This fixture isolates survival; after reaching land the actor has
        # no strategic reason to leave its newly excavated ledge.
        monkeypatch.setattr(brain, "_select_goal", lambda *_args, **_kwargs: None)
        runtime = director._runtime[bot.id]
        clock = [time.monotonic() + 1.]
        base, dry_since, trace = clock[0], None, []
        with patch.object(time, "monotonic", side_effect=lambda: clock[0]):
            for tick in range(30 * 60):
                clock[0] = base + tick / 60.
                if tick % 8 == 0:
                    frame = replace(_frame(director._snapshot_player(bot), created_at=clock[0]),
                                    frame_id=tick + 1, map_epoch=world.map_epoch,
                                    topology_version=world.topology_version)
                    runtime.intent = brain.decide(frame)
                server.loop_count = tick
                if tick % 2 == 0:
                    director._apply_motor(runtime, clock[0], 2. / 60.)
                await bot.simulate_tick(1. / 60.)
                director.observe_player_physics(bot, clock[0])
                server.world_mutations.commit_ready()
                if tick % 60 == 0:
                    trace.append((tick / 60, tuple(round(v, 3) for v in bot.position),
                                  runtime.intent.debug_role, bot.wade, bot.grounded,
                                  runtime.intent.action.position,
                                  director._snapshot_player(bot).last_action_accepted))
                dry = (bot.alive and bot.grounded and not bot.wade and bot.z < 236.
                       and server.world_manager.spawn_position_is_safe(bot.position))
                dry_since = (tick if dry_since is None else dry_since) if dry else None
                if dry_since is not None and tick - dry_since >= 120:
                    break
        server.world_manager.unsubscribe_mutations(subscription)
        assert dry_since is not None and tick - dry_since >= 120, "\n".join(map(str, trace)) + repr(mutation_cells)
        assert bot.alive and bot.deaths == 0 and director.terrain_recoveries == 0, trace
        assert mutation_cells, trace
        # The new exit floor is preserved by the tool's actual gateway
        # footprint, not merely named as a dry waypoint by the planner.
        assert server.world_manager.get_solid(297, 228, 238), mutation_cells
        assert all(not server.world_manager.get_solid(297, 228, z) for z in (235, 236, 237))
    asyncio.run(scenario())


@pytest.mark.parametrize("removed", [False, True])
def test_only_actual_exit_cell_removal_renews_swim_progress_once(removed):
    from server.bot_ai.messages import MovementAffordance
    from server.bot_ai.simple_navigation import RouteStep
    from server.bot_ai.simple_worker import _BotState
    from tests.test_simple_bot_tactics import _TacticalWorld, _player

    observer = _player(1, 2, (10.5, 10.5, 236.75), is_bot=True, wade=True, grounded=False)
    step = RouteStep((10.5, 11.5, 236.75), MovementAffordance.SWIM)
    world = _TacticalWorld(water_step=step)
    world.solid = lambda *_: not removed
    brain = SimpleBotBrain(world)
    brain.reset_for_map(1)
    state = _BotState(1, 1, observer.life_id, water_committed=True, water_recovery=True,
                      water_escape_position=observer.position, water_escape_at=100.,
                      water_breach_target=(11, 10, 236))
    brain._states[(observer.player_id, observer.generation)] = state
    result = brain.decide(_frame(observer, created_at=105.))
    if removed:
        assert result.debug_role == "water_exit"
        assert state.water_escape_at == 105. and state.water_breach_target is None
        brain.decide(_frame(observer, created_at=105.5))
        assert state.water_escape_at == 105.
    else:
        assert result.debug_role == "water_exit:cycle_blocked"


def test_next_excavation_face_preserves_owned_melee_cooldown():
    from server.bot_ai.messages import BotActionKind, MovementAffordance
    from server.bot_ai.simple_navigation import BreachPlan, RouteStep
    from server.bot_ai.simple_worker import _BotState, _Goal
    from tests.test_simple_bot_tactics import _TacticalWorld, _player

    observer = _player(1, 2, (296.5, 228.5, 236.75), is_bot=True)
    world = _TacticalWorld()
    world.solid = lambda *_: True
    brain, state = SimpleBotBrain(world), _BotState(1, 1, observer.life_id)
    goal = _Goal(("shore",), (297.5, 228.5, 235.75), "water_bank", 1., False)
    plan = BreachPlan((296, 228, 239), (297, 228, 238), (297, 228, 236),
                      ((297, 228, 235), (297, 228, 236), (297, 228, 237)),
                      int(C.MACHETE_TOOL), False, .7, 6)
    def act(current, now):
        step = RouteStep(goal.position, MovementAffordance.BREACH, current)
        return brain._breach_intent(_frame(observer, created_at=now), observer,
                                    state, goal, step, now).action.kind
    assert act(plan, 100.) is BotActionKind.MELEE
    next_face = replace(plan, target_cell=(297, 228, 235))
    assert act(next_face, 100.125) is BotActionKind.NONE
    assert act(next_face, 100.75) is BotActionKind.MELEE
