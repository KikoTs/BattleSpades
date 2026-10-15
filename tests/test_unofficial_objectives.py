"""Rules exercised through real players, terrain and accepted combat packets."""
import asyncio
from types import SimpleNamespace

import pytest
import shared.constants as C
from server.game_constants import TEAM1, TEAM2
from tests.test_unofficial_modes import add_player, make_game, start


async def drain(server):
    while server._mode_events:
        name, args = server._mode_events.popleft()
        handler = getattr(server.mode, name, None)
        if handler is not None:
            await handler(*args)


@pytest.mark.parametrize("code", ("crumbling_run", "red_light_raid"))
def test_late_race_joiner_can_complete_every_checkpoint(code):
    async def scenario():
        server, mode, _, _, now = make_game(code)
        await start(mode)
        late = add_player(server, 2, TEAM1)
        server.respawn_player(late)
        await mode.on_player_join(late)
        assert late in mode.participants and mode.penalty[late] == 0.0
        for checkpoint in mode.course[1:]:
            late.position = checkpoint
            now[0] += .1
            await mode.tick_live(now[0], .1)
        assert late in mode.finished
        assert mode.finished[late] == pytest.approx(now[0] - mode.race_start)
    asyncio.run(scenario())


def test_hot_potato_late_join_has_three_lives_and_departing_carrier_is_cleared():
    async def scenario():
        server, mode, blue, green, now = make_game("hot_potato")
        await start(mode)
        late = add_player(server, 2, TEAM1)
        server.respawn_player(late)
        await mode.on_player_join(late)
        assert mode.lives[late] == 3
        mode.lives[blue] = 0
        mode.eliminated.add(blue)
        await mode.tick_live(now[0], .1)
        assert mode.phase == "live"  # Green and the late joiner still compete.
        mode.pass_to(late)
        await mode.on_player_leave(late)
        server.players.pop(late.id)
        assert mode.holder is None and late not in mode.lives
        assert late.pickup_id is None
        await mode.tick_live(now[0], .1)
        assert mode.last_result == (green,)
    asyncio.run(scenario())


def test_false_wall_stash_once_and_enemy_cache_delivery():
    async def scenario():
        _, mode, blue, green, now = make_game("false_wall")
        mode.preparation = 10
        await mode.on_mode_start()
        await mode.on_tick(1)
        blue.position = (blue.x + 3, blue.y, blue.z)
        await mode.stash(blue)
        token = next(c for c in mode.cargo if c.team == TEAM1)
        hidden = token.position
        blue.position = (blue.x + 3, blue.y, blue.z)
        await mode.stash(blue)
        assert token.position == hidden
        now[0] += 11
        await mode.on_tick(2)
        assert not mode.can_take(blue, token)
        await mode.take(green, token)
        green.position = mode.home(TEAM2)
        await mode.objectives_tick(now[0], .25)
        assert mode.last_result == (TEAM2,)
    asyncio.run(scenario())


def test_tunnel_rats_needs_two_deliveries():
    async def scenario():
        _, mode, blue, _, now = make_game("tunnel_rats")
        await start(mode)
        for count in (1, 2):
            await mode.take(blue, mode.cargo[0])
            blue.position = mode.home(TEAM1)
            await mode.objectives_tick(now[0], .25)
            assert mode.points[TEAM1] == count
        assert mode.last_result == (TEAM1,)
    asyncio.run(scenario())


def test_bridge_fall_returns_relay_then_delivery_wins():
    async def scenario():
        _, mode, blue, _, now = make_game("bridge_chicken")
        await start(mode)
        token = mode.cargo[0]
        await mode.take(blue, token)
        blue.position = (256, 256, 120)
        await mode.objectives_tick(now[0], .25)
        assert token.holder is None and token.position == token.home
        assert blue.alive and blue.deaths == 0
        await mode.take(blue, token)
        blue.position = mode.home(TEAM2)
        await mode.objectives_tick(now[0], .25)
        assert mode.last_result == (TEAM1,)
    asyncio.run(scenario())


@pytest.mark.parametrize("code", ("towerfall", "scrapyard_engineers"))
def test_core_loss_wins_and_scrap_spawn_budget(code):
    async def scenario():
        server, mode, blue, _, now = make_game(code)
        await start(mode)
        await drain(server)
        if code == "scrapyard_engineers":
            assert blue.blocks == 250
            blue.position = mode.anchor("center")
            await mode.tick_live(now[0], .25)
            assert blue.blocks == 375
            await mode.tick_live(now[0], .25)
            assert blue.blocks == 375
            from server.builders.initial_info import build_initial_info
            from shared.packet import InitialInfo, Restock
            from shared.bytes import ByteReader
            packet = InitialInfo(ByteReader(bytes(build_initial_info(server).generate())[1:]))
            assert packet.block_wallet_multiplier == blue._block_wallet_multiplier() == .125
            refills = [Restock(ByteReader(p[1:])) for p in blue.connection.sent if p[0] == Restock.id]
            assert any(p.type == int(C.BLOCK_CRATE) for p in refills)
        mode.set_cell(mode.cores[TEAM2], False)
        await mode.tick_live(now[0], .25)
        assert mode.last_result == (TEAM1,)
    asyncio.run(scenario())


def test_spleef_cuts_aimed_floor_at_ten_hertz_and_eliminates_without_kill():
    async def scenario():
        server, mode, blue, green, now = make_game("floor_tax")
        await start(mode)
        blue.set_tool(int(C.SPADE_TOOL), raw=True)
        blue.orientation = (0, 0, 1)
        blue.input.primary_fire = True
        cell = (int(blue.x), int(blue.y), 82)
        await mode.tick_live(now[0], .1)
        assert not server.world_manager.get_solid(*cell)
        blue.position = (blue.x + 3, blue.y, blue.z)
        now[0] += .11
        await mode.tick_live(now[0], .1)
        assert not server.world_manager.get_solid(int(blue.x), int(blue.y), 82)
        assert int(C.GRENADE_TOOL) in blue.loadout
        blue.position = (blue.x, blue.y, 120)
        await mode.tick_live(now[0], .1)
        assert mode.last_result == (green,)
        assert blue.alive and blue.deaths == 0
    asyncio.run(scenario())


def test_lockdown_rescues_prisoners_and_blocks_their_construction():
    async def scenario():
        server, mode, blue, green, now = make_game("lockdown")
        helper = add_player(server, 2, TEAM1)
        await start(mode)
        await mode.on_player_death(blue, green, int(C.WEAPON_KILL))
        assert not mode.allow_action(blue, "build")
        helper.position = mode.jail(TEAM1)
        green.position = mode.home(TEAM1)
        for _ in range(13):
            await mode.tick_live(now[0], .25)
        assert blue not in mode.prisoners
        assert blue.alive and mode.phase == "live"
        await mode.on_player_death(green, blue, int(C.WEAPON_KILL))
        await mode.tick_live(now[0], .25)
        assert mode.last_result == (TEAM1,)
    asyncio.run(scenario())


def test_paint_ownership_transfers_with_canonical_team_color():
    async def scenario():
        server, mode, blue, green, _ = make_game("paint_raid")
        await start(mode)
        cell = (250, 250, 96)
        assert server.combat._commit_paint(blue, (cell,), 0, loop_count=1) == 1
        await drain(server)
        assert mode.points[TEAM1] == 1
        assert server.combat._commit_paint(green, (cell,), 0, loop_count=2) == 1
        await drain(server)
        assert mode.points[TEAM1] == 0 and mode.points[TEAM2] == 1
        from server.colors import pack_rgb
        assert server.world_manager.get_color(*cell) & 0xFFFFFF == pack_rgb(server.teams[TEAM2].color)
        assert server.combat._commit_paint(blue, ((210, 250, 96),), 0, loop_count=3) == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("code", ("smugglers_run", "reverse_siege"))
def test_smuggling_hidden_carry_delivery_totals_and_role_swap(code):
    async def scenario():
        _, mode, blue, green, now = make_game(code)
        await start(mode)
        token = mode.cargo[0]
        assert not mode.can_take(green, token)
        await mode.take(blue, token)
        assert blue.pickup_id is None and token.holder is blue
        blue.position = mode.home(TEAM2)
        for _ in range(8):
            await mode.objectives_tick(now[0], .25)
        assert mode.points[TEAM1] == 1
        await mode.timeout()
        assert mode.match_points[TEAM1] == 1
        await mode._new_round()
        assert mode.attackers == TEAM2 and (blue.team, green.team) == (TEAM1, TEAM2)
    asyncio.run(scenario())


def test_gun_game_advances_after_two_kills_and_preserves_progress_on_respawn():
    async def scenario():
        server, mode, blue, green, _ = make_game("borrowed_arsenal")
        await start(mode)
        for score in range(1, 11):
            await mode.on_player_death(green, blue, int(C.WEAPON_KILL))
            assert mode.points[blue] == score
            if score < 10:
                assert blue.tool == mode.guns[score // 2]
                server.respawn_player(blue)
                assert mode.gun[blue] == mode.guns[score // 2]
        assert mode.last_result == (blue,)
    asyncio.run(scenario())


def test_hill_scores_for_every_teammate_and_contesting_pauses_score():
    async def scenario():
        server, mode, blue, green, now = make_game("kingmaker")
        helper = add_player(server, 2, TEAM1)
        await start(mode)
        helper.position = mode.anchor("center")
        await mode.tick_live(now[0], .25)
        assert mode.points[TEAM1] == 1
        green.position = mode.anchor("center")
        now[0] += 1
        await mode.tick_live(now[0], .25)
        assert mode.points[TEAM1] == 1 and mode.owner is None
        green.position = mode.home(TEAM2)
        mode.points[TEAM1] = 89
        now[0] += 1
        await mode.tick_live(now[0], .25)
        assert mode.last_result == (TEAM1,)
    asyncio.run(scenario())


def test_red_light_movement_resets_checkpoint_and_crumbling_recovers():
    async def scenario():
        _, mode, blue, _, now = make_game("red_light_raid")
        await start(mode)
        now[0] += 7
        await mode.tick_live(now[0], .25)
        assert mode.red
        now[0] += 1
        blue.position = (blue.x + 1, blue.y, blue.z)
        await mode.tick_live(now[0], .25)
        assert blue.position == mode.course[0] and mode.penalty[blue] == 2

        server, mode, blue, _, now = make_game("crumbling_run")
        await start(mode)
        blue.position = (215.5, 260.5, 79.75)
        cell = (215, 260, 82)
        await mode.tick_live(now[0], .1)
        now[0] += 1
        blue.position = mode.course[0]
        await mode.tick_live(now[0], .1)
        assert not server.world_manager.get_solid(*cell)
        now[0] += 9
        await mode.tick_live(now[0], .1)
        assert server.world_manager.get_solid(*cell)
    asyncio.run(scenario())


def test_spadeball_goal_has_no_deaths_and_five_wins():
    async def scenario():
        _, mode, blue, green, now = make_game("spadeball")
        await start(mode)
        for goal in range(1, 6):
            now[0] += 4
            await mode.take(blue, mode.ball)
            blue.position = mode.home(TEAM2)
            await mode.objectives_tick(now[0], .25)
            assert mode.points[TEAM1] == goal
        assert mode.last_result == (TEAM1,)
        assert blue.deaths == green.deaths == 0
    asyncio.run(scenario())


def test_survival_clearing_all_six_waves_advances_without_repairing():
    async def scenario():
        server, mode, blue, green, now = make_game("night_shift", map_name="BlackPineOutpost")
        await start(mode)
        assert mode.wave == 1
        for wave in range(1, 7):
            assert mode.wave == wave
            for _ in range(40):
                for bot in tuple(mode.wave_bots):
                    bot.end_spawn_protection()
                    bot.damage(1000, source=blue, kill_type=int(C.WEAPON_KILL))
                now[0] += 1.1
                await mode.objectives_tick(now[0], .25)
                if mode.intermission_until or mode.stage == 3:
                    break
            assert not mode.wave_bots and mode.spawn_remaining == 0
            assert not mode.repair_rewarded
            if wave < 6:
                assert mode.intermission_until > now[0]
                now[0] = mode.intermission_until + .01
                await mode.objectives_tick(now[0], .25)
        assert mode.stage == 3
        blue.position = mode.anchor("exit")
        for _ in range(25):
            await mode.objectives_tick(now[0], .25)
        assert mode.last_result == (TEAM1,)
        await mode.deactivate()
        assert not mode.wave_bots
    asyncio.run(scenario())


def test_actual_same_colour_kill_counts_without_team_grief():
    async def scenario():
        server, mode, blue, green, _ = make_game("one_in_the_spade")
        green.team = TEAM1
        await start(mode)
        green.end_spawn_protection()
        assert green.damage(100, source=blue, kill_type=int(C.WEAPON_KILL))
        await drain(server)
        assert mode.points[blue] == blue.kills == 1
        from server.conduct import grief_points
        assert grief_points(blue) == 0
        from server.combat_scores import round_stats
        assert round_stats(blue).awards[C.MOST_KILLS] == 1
    asyncio.run(scenario())


def test_build_refusal_repairs_client_prediction_and_late_bot_join_waits():
    async def scenario():
        server, mode, blue, _, _ = make_game("paint_raid")
        await start(mode)
        blue.set_tool(int(C.BLOCK_TOOL), raw=True)
        cell = (int(blue.x) + 1, int(blue.y), 95)
        packet = SimpleNamespace(x=cell[0], y=cell[1], z=cell[2], loop_count=1)
        assert not server.combat.handle_block_build(blue, packet)
        assert cell in server.terrain_repair._pending
        late = add_player(server, 2, TEAM1, bot=True)
        assert not mode.can_player_respawn(late)
        assert not mode.allow_action(late, "paint", (250, 250, 96))
        await mode._new_round()
        assert late in mode.participants and late.alive
    asyncio.run(scenario())


def test_native_mover_can_land_first_course_gap_without_sprint():
    async def scenario():
        _, mode, player, _, _ = make_game("crumbling_run")
        await start(mode)
        player.set_position(209.0, 255.5, 79.75)
        player.set_orientation_vector(1, .5, 0)
        for _ in range(10):
            player.update_input(False, False, False, False, False, False, False, False)
            await player.update(1 / 60)
        for frame in range(58):
            player.update_input(True, False, False, False, frame < 2, False, False, False)
            await player.update(1 / 60)
        assert player.x >= 211 and player.y >= 256
        assert not player.airborne and player.z < 80
    asyncio.run(scenario())


def test_restoration_clears_partial_damage_and_uses_recipients_own_actor():
    async def scenario():
        server, mode, blue, green, _ = make_game("towerfall")
        await start(mode)
        cell = mode.cores[TEAM1]
        server.world_manager.apply_block_damage(*cell, 1)
        assert cell not in mode._dirty and cell in server.world_manager.block_damage
        # Remove player zero. Restoration must never name that missing actor.
        server.players.pop(blue.id)
        server.connections.pop(blue.id)
        green.connection.sent.clear()
        await mode.deactivate()
        assert cell not in server.world_manager.block_damage
        from shared.packet import BlockBuildColored
        from shared.bytes import ByteReader
        builds = [BlockBuildColored(ByteReader(data[1:])) for data in green.connection.sent if data[0] == BlockBuildColored.id]
        assert builds and all(packet.player_id == green.id for packet in builds)
    asyncio.run(scenario())


def test_protected_mode_veto_precedes_one_hit_kill_rule():
    async def scenario():
        server, mode, blue, green, _ = make_game("block_hunt")
        await start(mode)
        server.config.game_rules.apply({"RULE_ONE_HIT_KILL": True})
        await mode.finish((TEAM1,))
        green.end_spawn_protection()
        assert not green.damage(1, blue, int(C.WEAPON_KILL))
        assert green.alive and green.deaths == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("code", ("false_wall", "borrowed_arsenal"))
def test_last_round_announces_match_once_and_preserves_winner(code):
    async def scenario():
        _, mode, blue, _, now = make_game(code)
        await start(mode)
        mode.rounds = 1
        # Keep presentation scheduling out of this deterministic lifecycle test.
        ends = []
        async def ended(winner):
            ends.append(winner)
            mode.ended = True
        mode.on_mode_end = ended
        owner = blue if mode.spec.solo else TEAM1
        await mode.finish((owner,))
        now[0] += 7
        for tick in range(5):
            await mode.on_tick(tick)
        assert ends == [None if mode.spec.solo else TEAM1]
        assert blue.alive and blue.deaths == 0
    asyncio.run(scenario())


def test_real_bot_director_joins_night_wave_with_legal_kit_and_target():
    async def scenario():
        from server.bot_ai.director import BotDirector
        server, mode, _, _, now = make_game("night_shift")
        supervisor = SimpleNamespace(start=lambda _: None, discard_timeline=lambda: None,
                                     request_restart=lambda: None, close=lambda: None)
        director = BotDirector(server, supervisor=supervisor)
        server.bots = director
        try:
            await start(mode)
            await mode.objectives_tick(now[0], .25)
            bot = next(iter(mode.wave_bots))
            assert bot.team == TEAM2 and mode.active(bot)
            assert bot.tool in mode.loadout(bot) and bot.class_id in mode.roster()
            assert bot.position == mode.home(TEAM2)
            anchors = director._objectives_team_anchors(mode)
            assert any(a.team == TEAM1 and a.position == mode.home(TEAM1) for a in anchors)
            await director._maintain_population(now[0] + 10)
            assert bot in director.bots  # Ordinary backfill must not retire the wave.
            await mode.deactivate()
            assert not director.bots
        finally:
            await director.close()
    asyncio.run(scenario())
