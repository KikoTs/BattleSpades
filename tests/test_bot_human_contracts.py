"""Cross-layer contracts for authored access, fairness and guard relief."""
from dataclasses import replace
from types import SimpleNamespace
import asyncio
import time
import math

import pytest
import shared.constants as C

from server.bot_ai.behavior_memory import BehaviorMemory
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.guard_handoffs import GuardHandoffs
from server.bot_ai.horde_strategy import HordeCoordinator
from server.bot_ai.messages import BotActionKind, ObjectiveSnapshot
from server.bot_ai.prefab_ascent import PrefabAscent
from server.bot_ai.prefab_policy import load_bot_prefab_geometry
from server.bot_ai.recovery_skills import StepKind, standable
from server.prefab_actions import PrefabActionService
from tests.test_bot_project_sites import make_world, player
from tests.test_simple_bot_tactics import _frame


def ascent_fixture():
    world = make_world(thick=True)
    world.prefab_geometry = {g.name:g for g in load_bot_prefab_geometry(
        ("prefab_zombiehand", "prefab_zombiebone"))}
    actor = player(class_id=int(C.CLASS_ZOMBIE), blocks=1000,
        loadout=(int(C.ZOMBIEHAND_TOOL), int(C.ZOMBIE_PREFAB_TOOL)),
        prefabs=("prefab_zombiehand", "prefab_zombiebone"))
    return world, actor, PrefabAscent(world)


def proposal(controller, actor, *, start=100., **kwargs):
    for tick in range(200):
        frame = replace(_frame(actor, created_at=start+tick*.125), frame_id=tick+1, **kwargs)
        order = controller.step(frame, actor, (24.5,20.5,2.75))
        if order is not None and order.action.kind is BotActionKind.PLACE_PREFAB:
            return frame, order
    pytest.fail(f"No connected authored ascent: {controller.metrics}")


def test_zombie_prefab_has_real_connected_jump_route_and_pays_native_authority(monkeypatch):
    world, actor, controller = ascent_fixture()
    frame, order = proposal(controller, actor)
    plan = controller.slots[(actor.player_id, actor.generation)].proposal
    assert plan.route.rise >= 6
    assert any(s.kind is StepKind.JUMP for s in plan.route.steps)
    assert all(not s.build_cells and not s.dig_cells for s in plan.route.steps)
    owner = SimpleNamespace(id=actor.player_id, team=actor.team, is_bot=True,
        class_id=actor.class_id, alive=True, spawned=True, eye=actor.eye,
        x=actor.position[0], y=actor.position[1], z=actor.position[2],
        prefabs=list(actor.prefabs), blocks=actor.blocks, tool=actor.tool,
        loadout=list(actor.loadout), tool_is_raw=True, block_color=0xAABBCC,
        send=lambda *_a, **_kw: None)
    owner.set_tool = lambda tool, raw=True: setattr(owner,"tool",tool)
    server = SimpleNamespace(world_manager=world._vxl, players={owner.id:owner}, loop_count=1,
        teams={owner.team:SimpleNamespace(color=(10,20,30), infinite_blocks=False)},
        broadcast=lambda *_a, **_kw: None)
    server.prefab_actions = PrefabActionService(server)
    monkeypatch.setattr("server.prefab_actions.play_sound",lambda *_a, **_kw:None)
    before=set(world._vxl.solids)
    assert BotActionGateway(server).execute(owner,order.action)
    assert set(world._vxl.solids)-before == set(plan.cells)-before
    assert owner.blocks == actor.blocks-plan.authored_cost
    confirmed=replace(actor, blocks=owner.blocks, last_action_request_id=order.action.request_id,
        last_action_accepted=True, last_task_accepted=True)
    ready=controller.step(replace(frame,created_at=frame.created_at+.125),confirmed,(24.5,20.5,2.75))
    assert ready is not None and ready.route is not None
    assert all(standable(world,s.destination) for s in ready.route.steps)


@pytest.mark.parametrize("change",("missing_floor","old_life","rejected","moved_source"))
def test_prefab_never_adopts_unconfirmed_or_stale_route(change):
    world, actor, controller=ascent_fixture()
    frame, order=proposal(controller,actor)
    plan=controller.slots[(actor.player_id,actor.generation)].proposal
    world._vxl.solids.update(plan.cells)
    changes=dict(last_action_request_id=order.action.request_id,
                 last_action_accepted=True,last_task_accepted=True)
    if change=="missing_floor":world._vxl.solids.discard(plan.route.steps[-1].destination)
    if change=="old_life":changes["life_id"]=actor.life_id+1
    if change=="rejected":changes["last_task_accepted"]=False
    if change=="moved_source":changes["position"]=(actor.position[0]+3,*actor.position[1:])
    moved=replace(actor,**changes)
    result=controller.step(replace(frame,created_at=frame.created_at+.125),moved,(24.5,20.5,2.75))
    if change=="moved_source" and result is not None and result.route is not None:
        # A body that walked on while the placement was confirmed gets a
        # route re-threaded from where it now stands, never the stale one
        # whose first step starts three blocks away.
        from server.bot_ai.recovery_skills import node_of
        assert result.route.steps[0].source==node_of(moved.position)
        assert all(standable(world,s.destination) for s in result.route.steps)
    else:
        assert result is None or result.route is None


@pytest.mark.parametrize("changes",({"blocks":0},{"loadout":()}, {"wade":True}, {"grounded":False}))
def test_prefab_ascent_requires_owned_materials_tools_and_native_state(changes):
    _,actor,controller=ascent_fixture()
    actor=replace(actor,**changes)
    for i in range(50):
        result=controller.step(_frame(actor,created_at=100+i*.125),actor,(24.5,20.5,2.75))
        assert result is None or result.action.kind is BotActionKind.NONE


def test_guard_relief_requires_actual_arrival_and_aborts_expired_replacement():
    vip=player(player_id=5,is_bot=False,position=(20.5,20.5,17.75))
    old=player(player_id=1,position=(22.5,20.5,17.75))
    new=player(player_id=2,position=(35.5,20.5,17.75))
    objective=ObjectiveSnapshot("vip",vip.team,vip.position,carrier_id=vip.player_id)
    controller=GuardHandoffs()
    frame=_frame(old,new,vip,created_at=100.)
    assert controller.guards(frame,old.team,objective)==frozenset({old.player_id})
    frame=replace(frame,created_at=160.)
    assert controller.guards(frame,old.team,objective)==frozenset({1,2})
    # Directly underneath the VIP is not relief.
    below=replace(new,position=(20.5,20.5,25.75))
    assert controller.guards(replace(frame,players=(old,below,vip),created_at=162.),old.team,objective)==frozenset({1,2})
    assert controller.guards(replace(frame,created_at=169.),old.team,objective)==frozenset({1})
    frame=replace(frame,created_at=190.)
    assert controller.guards(frame,old.team,objective)==frozenset({1,2})
    arrived=replace(new,position=(23.,20.5,17.75))
    assert controller.guards(replace(frame,players=(old,arrived,vip),created_at=191.),old.team,objective)==frozenset({2})


def test_baiting_targets_and_roles_cannot_reset_a_motionless_horde_watchdog():
    horde=HordeCoordinator()
    for second in range(21):
        level=horde._observe_progress(1,(20.,20.,100.),(20.,20.,20.-second%2),
            (second%3,"climb" if second%2 else "hunt"),float(second))
    assert level>=2


def test_construction_success_prefers_method_without_reusing_unchecked_coordinates():
    memory=BehaviorMemory()
    memory.record("bridge",(20.,20.,20.),100.,True)
    assert 0 < memory.preference("bridge",101.) <= .12
    assert memory.penalty("bridge",(20.,20.,20.),101.)>0
    memory.record("bridge",(50.,50.,20.),102.,False)
    assert memory.preference("bridge",103.)<0
    assert memory.preference("bridge",230.)==0


def test_nearer_moving_prey_does_not_credit_a_stationary_hunter():
    horde=HordeCoordinator()
    for second in range(16):
        level=horde._observe_progress(1,(20.,20.,100.),(20.,20.,50.+second*1.5),
                                       (2,"hunt"),float(second))
    assert level>=2


def test_dig_aim_selects_visible_surface_of_occluded_voxel():
    from server.bot_ai.simple_navigation import visible_cell_aim
    world=make_world(thick=True)
    world._vxl.solids.update((23,20,z) for z in range(8,20))
    eye=(21.5,20.5,17.25)
    cell=(23,20,19)
    assert not world.has_line_of_sight(eye,(23.5,20.5,19.5))
    point=visible_cell_aim(world,eye,cell)
    assert point is not None
    assert tuple(math.floor(v) for v in point)==cell
    assert math.dist(eye,point)<=3.1 and world.has_line_of_sight(eye,point)


def test_public_vip_siege_does_not_use_hidden_roster_positions():
    from server.bot_ai.objective_siege import ObjectiveSiege
    from server.bot_ai.policies import ModeBotDecision
    world,actor,_=ascent_fixture()
    enemy=player(player_id=20,team=3,is_bot=False,position=(24.5,20.5,2.75))
    frame=replace(_frame(actor,enemy),mode_id="vip",mode_phase="active")
    siege=ObjectiveSiege(world)
    decision=ModeBotDecision(enemy.position,"vip_flank_attack")
    assert siege.order(frame,actor,decision) is None
    assert not siege.jobs and not siege.metrics


def test_pending_demolition_analysis_cannot_gain_prefab_grounding_connections():
    world,actor,controller=ascent_fixture()
    order=ObjectiveSnapshot("zombie_order",actor.team,(24.5,20.5,2.75),
        carrier_id=actor.player_id,state=3,planning_pending=True)
    frame=replace(_frame(actor),objectives=(order,))
    assert controller.step(frame,actor,order.position) is None
    assert not controller.leases and not controller.metrics


def test_native_zombie_builds_confirms_and_climbs_authored_prefab(monkeypatch):
    from tests.test_bot_architecture import _facing_fixture
    from server.class_selection import normalize_class_selection
    from server.bot_ai.messages import PerceptionFrame
    from server.bot_ai.simple_navigation import SimpleVoxelWorld
    from server.bot_ai.simple_worker import SimpleBotBrain

    server, director, bot, runtime = _facing_fixture()
    bot.apply_class_selection(normalize_class_selection(int(C.CLASS_ZOMBIE),
        prefabs=("prefab_zombiehand", "prefab_zombiebone")))
    bot.blocks = 1000
    bot.set_position(100.5,100.5,59.75)
    world = SimpleVoxelWorld()
    world._vxl = server.world_manager
    world.prefab_geometry = {g.name:g for g in load_bot_prefab_geometry(bot.prefabs)}
    brain = SimpleBotBrain(world)
    clock = [time.monotonic()]
    base = clock[0]
    monkeypatch.setattr(time,"monotonic",lambda:clock[0])
    director._map_epoch = director._mode_epoch = 1

    async def simulate():
        adopted = False
        for tick in range(720):
            clock[0] = base+tick/60
            server.loop_count = tick
            if tick%8 == 0:
                actor = director._snapshot_player(bot)
                frame = PerceptionFrame(tick+1,1,1,0,bot.id,runtime.generation,
                    clock[0],"zom",(actor,),profile=runtime.profile)
                if not adopted:
                    order = brain.prefab_ascent.step(frame,actor,(104.5,100.5,40.75))
                    if order and order.action.kind is BotActionKind.PLACE_PREFAB:
                        accepted = director.gateway.execute(bot,order.action)
                        assert accepted
                        director._record_action_result(runtime,order.action,accepted,clock[0])
                    if order and order.route:
                        command=brain.skills.adopt(actor,runtime.profile,order.route,
                            clock[0],purpose="prefab_ascent")
                        adopted=True
                        runtime.intent=brain._skill_intent(frame,actor,command,None)
                else:
                    command=brain.skills.step(actor,runtime.profile,clock[0])
                    if command and command.status=="done":
                        assert bot.grounded and bot.position[2] <= 54.25
                        assert bot.blocks == 865
                        return tick/60
                    if command:
                        runtime.intent=brain._skill_intent(frame,actor,command,None)
            if runtime.intent and tick%2 == 0:
                director._apply_motor(runtime,clock[0],1/30)
            await server.simulation_runtime._simulate_players()
            server.world_mutations.commit_ready()
            server.prefab_actions.tick()
            await asyncio.sleep(0)
        pytest.fail(f"Native ascent did not complete: {brain.skills.events}")

    assert asyncio.run(simulate()) <= 12


def test_native_level_gap_jump_is_not_mistaken_for_an_already_completed_rise(monkeypatch):
    from tests.test_bot_architecture import _facing_fixture
    from server.bot_ai.simple_navigation import SimpleVoxelWorld
    from server.bot_ai.simple_worker import SimpleBotBrain
    from server.bot_ai.messages import PerceptionFrame
    from server.bot_ai.recovery_skills import AscentPlan, ClimbStep
    server, director, bot, runtime = _facing_fixture()
    floor = server.world_manager.get_height(100, 100)
    for y in range(98, 103):
        for z in range(floor, 239):
            server.world_manager.set_block(101, y, z, False, 0)
    bot.set_position(100.5, 100.5, floor-2.25)
    world = SimpleVoxelWorld()
    world._vxl = server.world_manager
    brain = SimpleBotBrain(world)
    clock = [time.monotonic()]
    base = clock[0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    plan = AscentPlan((ClimbStep(StepKind.JUMP, (100, 100, floor), (102, 100, floor)),), 1., 0, 0)

    async def simulate():
        for tick in range(240):
            clock[0] = base+tick/60
            server.loop_count = tick
            if tick % 8 == 0:
                actor = director._snapshot_player(bot)
                frame = PerceptionFrame(tick+1, 1, 1, 0, bot.id, runtime.generation,
                    clock[0], "zom", (actor,), profile=runtime.profile)
                command = (brain.skills.adopt(actor, runtime.profile, plan, clock[0], purpose="gap")
                           if tick == 0 else brain.skills.step(actor, runtime.profile, clock[0]))
                assert command is not None
                if command.status == "done":
                    assert bot.grounded and bot.x >= 102 and abs(bot.z-(floor-2.25)) < .2
                    return
                assert command.status != "failed", brain.skills.events
                runtime.intent = brain._skill_intent(frame, actor, command, None)
            if tick % 2 == 0:
                director._apply_motor(runtime, clock[0], 1/30)
            await server.simulation_runtime._simulate_players()
            await asyncio.sleep(0)
        pytest.fail(f"Level gap was not crossed: {bot.position} {brain.skills.events}")

    asyncio.run(simulate())


def test_native_pillar_recenters_before_jumping_beside_an_overhang(monkeypatch):
    from tests.test_bot_architecture import _facing_fixture
    from server.bot_ai.messages import PerceptionFrame
    from server.bot_ai.recovery_skills import ClimbAbilities, plan_pillar_up
    from server.bot_ai.simple_navigation import SimpleVoxelWorld
    from server.bot_ai.simple_worker import SimpleBotBrain

    server, director, bot, runtime = _facing_fixture(int(C.CLASS_SPECIALIST))
    terrain = server.world_manager
    floor = terrain.get_height(100, 100)
    for z in range(floor-8, floor):
        terrain.set_block(100, 100, z, True, 0x775533)
    for z in range(floor-12, floor):
        terrain.set_block(102, 100, z, True, 0x775533)
    terrain.set_block(101, 100, floor-12, True, 0x775533)
    bot.set_position(100.703, 100.529, floor-8-2.25)
    world = SimpleVoxelWorld()
    world._vxl = terrain
    brain = SimpleBotBrain(world)
    actor = director._snapshot_player(bot)
    plan = plan_pillar_up(world, (100, 100, floor-8), 6,
                         ClimbAbilities.from_observer(actor))
    assert plan is not None
    clock = [time.monotonic()]
    base = clock[0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    async def simulate():
        for tick in range(12*60):
            clock[0] = base+tick/60
            server.loop_count = tick
            if tick % 8 == 0:
                actor = director._snapshot_player(bot)
                frame = PerceptionFrame(tick+1, director._map_epoch, director._mode_epoch,
                    terrain.topology_version, bot.id, runtime.generation,
                    clock[0], "tdm", (actor,), profile=runtime.profile)
                command = (brain.skills.adopt(actor, runtime.profile, plan, clock[0], purpose="pillar")
                           if tick == 0 else brain.skills.step(actor, runtime.profile, clock[0]))
                assert command is not None, (bot.position, brain.skills.events)
                if command.status == "done":
                    assert bot.grounded and abs(bot.z-(floor-14-2.25)) < .2
                    assert abs(bot.x-100.5) < .15
                    return
                runtime.intent = brain._skill_intent(frame, actor, command, None)
            if tick % 2 == 0:
                director._apply_motor(runtime, clock[0], 1/30)
            await server.simulation_runtime._simulate_players()
            server.world_mutations.commit_ready()
            await asyncio.sleep(0)
        pytest.fail(f"Pillar did not clear the adjacent overhang: {bot.position} {brain.skills.events}")

    asyncio.run(simulate())
