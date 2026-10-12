"""Live-join, inventory and terrain regressions from the Block Hunt playtest."""
import asyncio
from types import SimpleNamespace

import pytest
import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import Restock
from server.combat_runtime import get_combat_system
from server.game_constants import TEAM1, TEAM2
from tests.test_unofficial_modes import add_player, make_game, start


@pytest.mark.parametrize("phase", ["prepare", "live"])
@pytest.mark.parametrize("team", [TEAM1, TEAM2])
def test_human_join_is_admitted_and_can_leave_waiting_area(phase, team):
    async def scenario():
        server, mode, _, _, now = make_game("block_hunt")
        await start(mode)
        mode.phase = phase
        mode.deadline = now[0] + 30
        player = add_player(server, 6, team)
        # Connection checks this before on_player_join or creating the body.
        assert mode.can_player_respawn(player)
        server.respawn_player(player)
        await mode.on_player_join(player)
        assert player in mode.participants
        if phase == "live" or team == mode.defenders:
            point = (player.x, player.y + 20, player.z)
            player.set_position(*point)
            await mode.on_tick(3)
            assert player.position == point
        assert player.alive and player.deaths == 0
        mode.phase = "live"
        mode.eliminated.add(player)
        assert not mode.can_player_respawn(player)
        assert not mode.can_join_round(player)
    asyncio.run(scenario())


def test_false_wall_human_can_join_a_live_round_but_bot_backfill_waits():
    async def scenario():
        server, mode, _, _, _ = make_game("false_wall")
        await start(mode)
        human = add_player(server, 6, TEAM1)
        bot = add_player(server, 7, TEAM2, bot=True)
        assert mode.can_player_respawn(human)
        assert not mode.can_player_respawn(bot)
        server.respawn_player(human)
        await mode.on_player_join(human)
        assert mode.active(human)
    asyncio.run(scenario())


def test_disguises_refill_each_use_without_healing_or_resetting_cadence(monkeypatch):
    async def scenario():
        server, mode, _, hider, now = make_game("block_hunt")
        await start(mode)
        monkeypatch.setattr("server.deployable_actions.time.monotonic", lambda: now[0])
        hider.health = 43
        hider.set_tool(C.DISGUISE_TOOL, raw=True)
        hider.connection.sent.clear()
        for _ in range(12):
            assert server.deployable_actions.set_disguise(hider, active=True)
            assert hider.disguised and hider.health == 43
            assert hider.disguise_stock == int(C.DISGUISE_RESTOCK_AMOUNT)
            assert server.deployable_actions.set_disguise(hider, active=False)
            assert not server.deployable_actions.set_disguise(hider, active=True)
            now[0] += float(C.DISGUISE_SHOOT_INTERVAL) + .01
        refills = [Restock(ByteReader(p[1:])) for p in hider.connection.sent if p[0] == Restock.id]
        assert len(refills) == 12
        assert all(p.type == int(C.AMMO_CRATE) and p.player_id == hider.id for p in refills)
        mode.eliminated.add(hider)
        assert not server.deployable_actions.set_disguise(hider, active=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["prepare", "live"])
def test_hider_shovel_digs_real_terrain_but_never_hurts_players(phase):
    async def scenario():
        server, mode, hunter, hider, _ = make_game("block_hunt")
        await start(mode)
        mode.phase = phase
        assert int(C.SPADE_TOOL) in hider.loadout
        hider.set_tool(C.SPADE_TOOL, raw=True)
        hider.set_orientation_vector(0, 0, 1)
        cell = (int(hider.x), int(hider.y), 96)
        assert server.world_manager.get_solid(*cell)
        combat = get_combat_system(server)
        for _ in range(3):
            combat._resolve_spade_dig(hider, hider.eye, hider.orientation, SimpleNamespace())
        assert not server.world_manager.get_solid(*cell)
        assert cell in mode._dirty
        hunter.end_spawn_protection()
        before = hunter.health
        server.config.game_rules.apply({"RULE_ONE_HIT_KILL": True})
        assert not hunter.damage(999, hider, int(C.MELEE_KILL))
        assert hunter.health == before and hunter.alive
        assert not mode.allow_action(hunter, "dig", cell)
        assert not mode.allow_action(hider, "build", cell)
        assert not mode.allow_action(hider, "paint", cell)
    asyncio.run(scenario())


def test_spooky_mansion_anchors_and_bot_destinations_have_clear_ground():
    from server.config import ServerConfig
    from server.main import BattleSpadesServer
    from modes.unofficial.combat import BlockHunt
    from server.round_lifecycle import resolve_player_spawn

    server = BattleSpadesServer(ServerConfig())
    assert server.world_manager.load_map("SpookyMansion")
    mode = server.mode = BlockHunt(server)
    mode._load_arena()
    assert len(mode.hunt_points) == 10
    for point in (*mode.anchors.values(), *mode.hunt_points):
        assert server.world_manager.spawn_position_is_safe(point)
    for pid in range(16):
        player = add_player(server, pid, TEAM1 if pid % 2 else TEAM2)
        assert server.world_manager.spawn_position_is_safe(resolve_player_spawn(server, player))
