"""Real mode facts, worker decisions, native actions and lifecycle fences."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import pickle
from types import SimpleNamespace

import pytest
import shared.constants as C

from modes.unofficial.catalog import SPECS
from server.bot_ai.director import BotDirector
from server.bot_ai.messages import BotAction, BotActionKind, PerceptionFrame
from server.bot_ai.simple_navigation import SimpleVoxelWorld
from server.bot_ai.simple_worker import SimpleBotBrain
from server.bot_ai.unofficial import combat_view, decision_for
from server.game_constants import TEAM1, TEAM2
from tests.test_unofficial_modes import make_game, start
from tests.test_unofficial_objectives import drain


def director_for(server):
    supervisor = SimpleNamespace(start=lambda _: None, discard_timeline=lambda: None,
                                 request_restart=lambda: None, close=lambda: None)
    return BotDirector(server, supervisor=supervisor)


def frame_for(director, mode, player, now):
    return PerceptionFrame(1, 1, 1, 0, player.id, director._player_generation(player),
                           now, mode.mode_code, director._snapshot_players(),
                           mode_phase=mode.phase, fun=mode.bot_snapshot())


def brain_for(server):
    world = SimpleVoxelWorld()
    world._vxl = server.world_manager.map
    world.map_epoch = 1
    world.topology_version = 0
    return SimpleBotBrain(world)


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.code)
def test_every_mode_admits_legal_bot_and_has_specific_objective(spec):
    async def scenario():
        server, mode, _, _, now = make_game(spec.code)
        director = director_for(server)
        server.bots = director
        try:
            await mode.on_mode_start()
            if spec.code == "night_shift":
                await mode.on_tick(1)
            bot = await director.add_bot(team=TEAM1)
            assert bot is not None
            assert bot.tool in mode.loadout(bot)
            if mode.phase == "waiting":
                await mode.on_tick(1)
            await mode.on_tick(2)
            frame = frame_for(director, mode, bot, now[0])
            assert pickle.loads(pickle.dumps(frame)) == frame
            observer = next(p for p in frame.players if p.player_id == bot.id)
            decision = decision_for(frame, observer)
            assert decision.role.startswith("fun_")
            assert decision.role not in ("fun_wait_round", "fun_wait_start")
            intent = brain_for(server).decide(frame)
            assert intent is not None and intent.life_id == observer.life_id
            assert intent.tool_id < 0 or intent.tool_id in bot.loadout
            # Same live body stops completely when a round ends.
            await mode.finish(())
            stopped = brain_for(server).decide(frame_for(director, mode, bot, now[0] + 1))
            assert stopped.movement.direction == (0, 0, 0)
            assert stopped.action.kind is BotActionKind.NONE
        finally:
            await director.close()
    asyncio.run(scenario())


def test_ffa_same_uniform_is_enemy_and_disguises_are_not_omniscient():
    async def scenario():
        server, mode, blue, green, now = make_game("borrowed_arsenal")
        green.team = TEAM1
        await start(mode)
        director = director_for(server)
        frame = frame_for(director, mode, blue, now[0])
        observer = frame.players[0]
        view, actor = combat_view(frame, observer)
        assert len({p.team for p in view.players}) == 2
        assert actor.player_id == blue.id
        server, mode, blue, green, now = make_game("block_hunt")
        await start(mode)
        green.disguised = True
        director = director_for(server)
        frame = frame_for(director, mode, blue, now[0])
        view, actor = combat_view(frame, frame.players[0])
        assert not next(p for p in view.players if p.player_id == green.id).alive
        mode.bot_clue_until = now[0] + 4
        # A far-away clue is not authorization to target a hidden player.
        frame = frame_for(director, mode, blue, now[0])
        assert not combat_view(frame, frame.players[0])[0].players[1].alive
        green.position = (blue.x + 2, blue.y, blue.z)
        frame = frame_for(director, mode, blue, now[0])
        assert combat_view(frame, frame.players[0])[0].players[1].alive
    asyncio.run(scenario())


def test_bot_disguises_through_stock_service_and_holds_still():
    async def scenario():
        server, mode, _, green, now = make_game("block_hunt")
        green.is_bot = True
        await start(mode)
        director = director_for(server)
        frame = frame_for(director, mode, green, now[0])
        observer = next(p for p in frame.players if p.player_id == green.id)
        green.position = decision_for(frame, observer).position
        frame = frame_for(director, mode, green, now[0])
        intent = brain_for(server).decide(frame)
        assert intent.action.kind is BotActionKind.DISGUISE
        assert director.gateway.execute(green, intent.action)
        assert green.disguised and green.disguise_stock == int(C.DISGUISE_RESTOCK_AMOUNT)
        held = brain_for(server).decide(frame_for(director, mode, green, now[0] + 1))
        assert held.movement.direction == (0, 0, 0)
        assert held.action.kind is BotActionKind.NONE
    asyncio.run(scenario())


def test_bot_paint_scores_through_native_gateway_and_stays_bounded():
    async def scenario():
        server, mode, blue, _, now = make_game("paint_raid")
        blue.is_bot = True
        await start(mode)
        blue.position = mode.anchor("center")
        director = director_for(server)
        frame = frame_for(director, mode, blue, now[0])
        assert len(frame.fun.paint) <= 9
        intent = brain_for(server).decide(frame)
        assert intent.action.kind is BotActionKind.PAINT
        assert director.gateway.execute(blue, intent.action)
        await drain(server)
        assert mode.points[TEAM1] == 1
        assert not director.gateway.execute(blue, replace(intent.action, position=(1, 1, 96)))
        await mode.finish(())
        assert not director.gateway.execute(blue, intent.action)
    asyncio.run(scenario())


def test_red_light_prisoners_and_late_joiners_do_not_act():
    async def scenario():
        for code in ("red_light_raid", "lockdown", "borrowed_arsenal"):
            server, mode, blue, _, now = make_game(code)
            await start(mode)
            if code == "red_light_raid":
                mode.red = True
            elif code == "lockdown":
                mode.prisoners.add(blue)
            else:
                mode.participants.remove(blue)
            director = director_for(server)
            intent = brain_for(server).decide(frame_for(director, mode, blue, now[0]))
            assert intent.movement.direction == (0, 0, 0)
            assert intent.action.kind is BotActionKind.NONE
    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["false_wall", "last_exit", "tunnel_rats", "bridge_chicken",
                                 "smugglers_run", "reverse_siege", "spadeball"])
def test_cargo_changes_collection_into_the_right_delivery(code):
    async def scenario():
        server, mode, blue, _, now = make_game(code)
        await start(mode)
        token = next(c for c in mode.cargo if mode.can_take(blue, c))
        await mode.take(blue, token)
        if code == "last_exit":
            mode.exit_open = True
        director = director_for(server)
        frame = frame_for(director, mode, blue, now[0])
        decision = decision_for(frame, frame.players[0])
        expected = mode.anchor("exit") if code == "last_exit" else mode.home(
            TEAM2 if code in ("bridge_chicken", "smugglers_run", "reverse_siege", "spadeball") else TEAM1)
        assert decision.position == expected
        if token.hidden:
            blue.position = (250, 240, 93.75)
            facts = mode.bot_snapshot()
            assert next(c for c in facts.cargo if c.holder == blue.id).position == token.home
    asyncio.run(scenario())


def test_night_helper_can_repair_collect_and_evacuate():
    async def scenario():
        server, mode, blue, _, now = make_game("night_shift")
        blue.is_bot = True
        await start(mode)
        director = director_for(server)
        frame = frame_for(director, mode, blue, now[0])
        assert decision_for(frame, frame.players[0]).directive == "repair"
        for cell in mode.repair_cells:
            mode.set_cell(cell, True)
        mode.stage = 3
        frame = frame_for(director, mode, blue, now[0])
        assert decision_for(frame, frame.players[0]).role == "fun_hold_evacuation"
    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["crumbling_run", "red_light_raid"])
@pytest.mark.parametrize("production_map", [False, True])
def test_race_bot_finishes_using_native_physics_and_ordered_checkpoints(code, production_map):
    async def scenario():
        from modes.unofficial.catalog import CATALOG
        server, mode, blue, _, now = make_game(code, map_name=CATALOG[code].map_name if production_map else None)
        await start(mode)
        director = director_for(server)
        brain = brain_for(server)
        intent = None
        for tick in range(60 * (450 if production_map else 90)):
            now[0] += 1 / 60
            server.loop_count += 1
            if tick % 8 == 0:
                frame = replace(frame_for(director, mode, blue, now[0]), frame_id=tick + 1)
                intent = brain.decide(frame) or intent
            move = intent.movement
            moving = any(move.direction)
            if moving:
                blue.set_orientation_vector(*move.direction)
            blue.update_input(moving, False, False, False, move.jump, False, False, move.sprint)
            await blue.update(1 / 60)
            await mode.tick_live(now[0], 1 / 60)
            if blue in mode.finished:
                break
        assert blue in mode.finished, (blue.position, mode.checkpoint[blue], mode.penalty[blue], intent.debug_role)
        assert mode.checkpoint[blue] == len(mode.course) - 1
        assert blue.deaths == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("map_name", [None, "FoundryRun"])
def test_course_through_real_bot_motor_finishes(map_name):
    async def scenario():
        server, mode, _, _, now = make_game("crumbling_run", map_name=map_name)
        director = director_for(server)
        server.bots = director
        await mode.on_mode_start()
        bot = await director.add_bot(team=TEAM1)
        await mode.on_tick(1)
        await mode.on_tick(2)
        brain = brain_for(server)
        runtime = director._runtime[bot.id]
        try:
            for tick in range(60 * (450 if map_name else 90)):
                now[0] += 1 / 60
                server.loop_count += 1
                director._movement_probes_this_tick = 0
                if tick % 8 == 0:
                    frame = replace(frame_for(director, mode, bot, now[0]), frame_id=tick + 1)
                    runtime.intent = brain.decide(frame) or runtime.intent
                director._apply_motor(runtime, now[0], 1 / 60)
                await bot.update(1 / 60)
                await mode.tick_live(now[0], 1 / 60)
                if bot in mode.finished:
                    break
            assert bot in mode.finished, (bot.position, mode.checkpoint[bot], mode.penalty[bot], runtime.intent.debug_role)
        finally:
            await director.close()
    asyncio.run(scenario())


def test_bridge_bot_plans_the_actual_34_cell_island_gap():
    async def scenario():
        server, mode, blue, _, now = make_game("bridge_chicken")
        await start(mode)
        await mode.take(blue, next(c for c in mode.cargo if c.team == TEAM1))
        blue.position = (215.5, 256.5, 93.75)
        director = director_for(server)
        brain = brain_for(server)
        frame = frame_for(director, mode, blue, now[0])
        intent = brain.decide(frame)
        assert brain.skills.active(frame.players[0])
        assert "bridge" in intent.debug_role
        assert intent.action.kind in (BotActionKind.BUILD_LINE, BotActionKind.BUILD, BotActionKind.NONE)
    asyncio.run(scenario())


def test_night_helper_rebuilds_through_normal_gateway():
    async def scenario():
        server, mode, blue, _, now = make_game("night_shift")
        blue.is_bot = True
        await start(mode)
        cell = mode.repair_cells[0]
        blue.position = (cell[0] + .5, cell[1] + 2.5, 93.75)
        director = director_for(server)
        intent = brain_for(server).decide(frame_for(director, mode, blue, now[0]))
        assert intent.action.kind is BotActionKind.BUILD
        assert director.gateway.execute(blue, intent.action)
        server.world_mutations.commit_ready()
        assert server.world_manager.get_solid(*intent.action.position)
    asyncio.run(scenario())


def test_tunnel_bot_excavates_toward_a_cache_with_owned_pickaxe():
    async def scenario():
        server, mode, blue, _, now = make_game("tunnel_rats")
        await start(mode)
        blue.position = (218.5, 249.5, 99.75)
        director = director_for(server)
        intent = brain_for(server).decide(frame_for(director, mode, blue, now[0]))
        assert intent.action.kind is BotActionKind.MELEE
        assert intent.action.tool_id in blue.loadout
        assert intent.action.position is not None
        assert "route_breach" in intent.debug_role
        assert intent.debug_goal[0] > blue.x
    asyncio.run(scenario())
