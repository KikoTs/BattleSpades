"""Player actions and complete objectives on the maps actually shipped to play."""
import asyncio
import math
from types import SimpleNamespace

import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import MinimapZone, MinimapBillboard, Restock
from server.builders.initial_info import build_initial_info
from server.game_constants import TEAM1
from modes.unofficial.catalog import RETIRED, SPECS
from tests.test_unofficial_modes import make_game, start


def test_curated_playlist_has_familiar_names_and_hides_retired_prototypes():
    from server_gui.catalog import mode_choices
    choices = {choice.code: choice.title for choice in mode_choices()}
    assert len(SPECS) == 13 and not RETIRED.intersection(choices)
    for code, title in (("floor_tax", "Spleef"), ("night_shift", "Survival"),
                        ("borrowed_arsenal", "Gun Game"), ("kingmaker", "King of the Hill")):
        assert choices[code] == "[Unofficial] " + title


def test_empty_human_server_waits_instead_of_starting_elimination_rounds_with_bots():
    async def scenario():
        _, mode, a, b, _ = make_game("floor_tax")
        a.is_bot = b.is_bot = True
        await mode.on_mode_start()
        await mode.on_tick(1)
        assert mode.phase == "waiting"
        a.is_bot = False
        await mode.on_tick(2)
        assert mode.phase == "prepare" and a in mode.participants
    asyncio.run(scenario())


def test_survival_repair_is_exposed_buildable_and_awards_only_once():
    async def scenario():
        server, mode, player, _, now = make_game("night_shift", map_name="BlackPineOutpost")
        await start(mode)
        player.set_tool(int(C.BLOCK_TOOL), raw=True)
        wm = server.world_manager
        for i, cell in enumerate(mode.repair_cells):
            assert not wm.get_solid(*cell)
            assert wm.get_solid(cell[0], cell[1], cell[2] + 1)
            assert not wm.get_solid(cell[0], cell[1] - 1, cell[2])
            player.position = (cell[0] + .5, cell[1] - 2.5, 93.75)
            player._last_block_build_at = None
            packet = SimpleNamespace(x=cell[0], y=cell[1], z=cell[2], loop_count=100 + i * 60)
            assert server.combat.handle_block_build(player, packet)
            player.last_applied_input_loop = packet.loop_count
            server.world_mutations.commit_ready()
            assert wm.get_solid(*cell)
        player.connection.sent.clear()
        await mode.repair_tick()
        assert mode.repair_rewarded and mode.wave == 1
        count = sum(raw[0] == Restock.id for raw in player.connection.sent)
        await mode.repair_tick()
        assert count == 1 == sum(raw[0] == Restock.id for raw in player.connection.sent)
        zones = [MinimapZone(ByteReader(raw[1:])) for raw in server.packets if raw[0] == MinimapZone.id]
        assert any(z.icon_id == 8 for z in zones)  # B, the repair marker
    asyncio.run(scenario())


def test_survival_spawn_failure_resolves_round_and_reset_clears_pending_wave():
    async def scenario():
        server, mode, _, _, now = make_game("night_shift")
        await start(mode)
        async def failed(**kwargs):
            return None
        server.bots.add_bot = failed
        await mode.objectives_tick(now[0], .1)
        now[0] += 21
        await mode.objectives_tick(now[0], .1)
        assert mode.phase == "over" and mode.last_result == ()
        await mode.prepare_round()
        assert mode.spawn_remaining == 0 and not mode.wave_bots and mode.wave == 0
    asyncio.run(scenario())


def test_crumbling_course_four_cells_open_to_air_and_next_checkpoint_is_personal():
    async def scenario():
        server, mode, player, other, now = make_game("crumbling_run", map_name="FoundryRun")
        await start(mode)
        assert sum(math.dist(a, b) for a, b in zip(mode.course, mode.course[1:])) > 800
        assert len(mode._checkpoint_zones) == 2
        before = mode._checkpoint_zones[other.id].bounds
        player.position = mode.course[1]
        await mode.tick_live(now[0], .05)
        assert mode._checkpoint_zones[player.id].bounds != before
        assert mode._checkpoint_zones[other.id].bounds == before
        player.position = (178.5, 160.5, 79.75)
        player.airborne = False
        now[0] += .1
        await mode.tick_live(now[0], .05)
        cells = {(178 + dx, 160 + dy, 82) for dx in (0, 1) for dy in (0, 1)}
        assert cells.issubset(mode.crumble)
        player.position = mode.course[1]
        now[0] += .31
        await mode.tick_live(now[0], .05)
        for x, y, z in cells:
            assert not any(server.world_manager.get_solid(x, y, h) for h in range(z, z + 12))
        now[0] += 8.1
        await mode.tick_live(now[0], .05)
        assert all(server.world_manager.get_solid(*cell) for cell in cells)
    asyncio.run(scenario())


def test_hot_potato_updates_small_marker_and_passing_does_not_reset_fuse():
    async def scenario():
        server, mode, a, b, now = make_game("hot_potato")
        await start(mode)
        fuse = mode.fuse
        mode.pass_to(b)
        assert mode.fuse == fuse
        b.position = (b.x + 5, b.y, b.z)
        now[0] += .11
        await mode.tick_live(now[0], .1)
        markers = [MinimapBillboard(ByteReader(raw[1:])) for raw in b.connection.sent if raw[0] == MinimapBillboard.id]
        assert markers and markers[-1].icon_name == "map_bomb_16"
        assert abs(markers[-1].x - b.x) < .02
        assert not mode._zones  # No flickering clear/re-add volumes around a moving player.
    asyncio.run(scenario())


def test_fixed_objectives_replay_to_late_joiners_and_clear_when_replaced():
    async def scenario():
        server, mode, player, _, _ = make_game("towerfall")
        await start(mode)
        player.connection.sent.clear()
        mode.reveal_to(player.connection)
        assert sum(raw[0] == 43 for raw in player.connection.sent) == 2
        previous = len(server.packets)
        mode.marker(55, mode.anchor("center"))
        mode.marker(55, mode.home(TEAM1))
        assert [raw[0] for raw in server.packets[previous:]] == [43, 44, 43]
    asyncio.run(scenario())


def test_momentum_is_explicitly_negotiated_and_never_changes_retail_owners():
    async def scenario():
        server, mode, retail, native, _ = make_game("crumbling_run", map_name="FoundryRun")
        await start(mode)
        native.connection.race_momentum_capable = True
        for player in (retail, native):
            info = build_initial_info(server)
            mode.configure_initial_info_for(player.connection, info)
            assert (("BATTLESPADES_RACE_MOMENTUM", "1") in info.custom_game_rules) == (player is native)
            player.position = (170.5, 158.5 if player is retail else 163.5, 79.75)
            player.orientation = (1, 0, 0)
            player.update_input(True, False, False, False, False, False, False, True)
        for frame in range(120):
            await retail.update(1 / 60)
            await native.update(1 / 60)
            if frame == 2:
                assert native.vx < retail.vx
        assert retail.vx < native.vx < retail.vx * 1.36
    asyncio.run(scenario())


def test_spleef_explosion_pushes_without_health_damage():
    async def scenario():
        server, mode, a, b, _ = make_game("floor_tax", map_name="Glassworks")
        await start(mode)
        a.position, b.position = (250.5, 250.5, 79.75), (253.5, 250.5, 79.75)
        a.end_spawn_protection()
        b.end_spawn_protection()
        hp = b.health
        server._apply_blast(252, 250.5, 79.75, 100, 0, int(C.GRENADE_KILL), a,
                            blast_radius=8, knockback_min=.1, knockback_max=.3)
        assert b.health == hp and b.alive
        assert any(abs(v) > .001 for v in b.velocity)
    asyncio.run(scenario())
