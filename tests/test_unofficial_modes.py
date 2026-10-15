"""Exercise real server/player/packet boundaries for every community mode."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import shared.constants as C
from shared.packet import KillAction, CreatePlayer, InitialInfo, StateData
from shared.bytes import ByteReader
from modes import get_mode_class, canonical_mode_code
from modes.unofficial.catalog import SPECS, CATALOG
from modes.unofficial.maps import build_arena
from server.config import ServerConfig, resolve_mode_code
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.map_metadata import MapMetadata
from server.player import Player
from server.builders.initial_info import build_initial_info
from server.builders.state_data import build_state_data


class Connection:
    def __init__(self, server):
        self.server = server
        self.in_game = True
        self.sent = []
        self.player = None

    def send(self, data, reliable=True, **kwargs):
        self.sent.append(bytes(data))


def add_player(server, pid, team, *, bot=False):
    conn = Connection(server)
    player = Player(pid, f"Player{pid}", team, int(C.SMG_TOOL), conn)
    player.is_bot = bot
    conn.player = player
    server.players[pid] = player
    server.connections[pid] = conn
    server.teams[team].add_player(player)
    return player


class Bots:
    def __init__(self, server):
        self.server = server

    async def add_bot(self, team):
        pid = max(self.server.players, default=0) + 1
        bot = add_player(self.server, pid, team, bot=True)
        self.server.respawn_player(bot)
        await self.server.mode.on_player_join(bot)
        return bot

    async def remove_bot(self, bot, **kwargs):
        self.server.players.pop(bot.id, None)
        self.server.mode.participants.discard(bot)
        return True


def make_game(code, *, map_name=None):
    config = ServerConfig()
    config.default_mode = code
    config.achievements.enabled = False
    config.mode_settings = {code: {"prepare_time": 0}}
    server = BattleSpadesServer(config)
    spec = CATALOG[code]
    # Rules tests use a compact deterministic fixture; retail map coverage is
    # separate so the entire mode matrix does not reload a full island.
    from modes.unofficial.arena_designs import DESIGNS
    fixture_map = map_name or DESIGNS[code].base
    world, payload = build_arena(fixture_map)
    wm = server.world_manager
    wm.map = world
    wm.map_metadata = MapMetadata(unofficial=payload["unofficial"])
    wm.map_name = fixture_map
    wm.map_raw_bytes = world.generate_vxl(False)
    wm._refresh_world()
    # Avoid collecting ENet peers: assert actual encoded output below.
    server.packets = []
    server.broadcast = lambda data, **kwargs: server.packets.append(bytes(data))
    mode = get_mode_class(code)(server)
    server.mode = mode
    now = [100.0]
    mode.clock = lambda: now[0]
    blue = add_player(server, 0, TEAM1)
    green = add_player(server, 1, TEAM2)
    if code == "night_shift":
        server.bots = Bots(server)
        green.team = TEAM1
    return server, mode, blue, green, now


async def start(mode):
    await mode.on_mode_start()
    await mode.on_tick(1)
    await mode.on_tick(2)


def test_restart_without_a_ready_roster_clears_previous_round_state():
    async def scenario():
        server, mode, blue, green, _ = make_game("block_hunt")
        await start(mode)
        mode.eliminated.add(blue)
        mode.points[blue] = 2
        await mode.finish((TEAM1,))
        server.players.pop(green.id)
        await mode.on_mode_start()
        await mode.on_tick(3)
        assert mode.phase == "waiting" and mode.round_number == 0
        assert not mode.participants and not mode.eliminated
        assert not mode.points and not mode.match_points and not mode.last_result
        assert mode.deadline == 0.0
        await mode.deactivate()
    asyncio.run(scenario())


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.code)
def test_every_mode_starts_ticks_and_resets_without_killing_winners(spec):
    async def scenario():
        server, mode, blue, green, now = make_game(spec.code)
        await start(mode)
        assert mode.phase == "live"
        assert blue.alive and green.alive
        for frame in range(3, 15):
            now[0] += 1 / 60
            await mode.on_tick(frame)
        assert mode.phase == "live"
        server.packets.clear()
        result = (blue,) if spec.solo else (TEAM1,)
        await mode.finish(result)
        await mode.finish(result)
        assert mode.match_points.get(result[0], 0) <= 1
        assert mode.modify_incoming_damage(blue, 100, green, 0) == 0
        now[0] += 7
        await mode.on_tick(15)
        for frame in range(16, 200):
            await mode.on_tick(frame)
            if mode.phase == "prepare":
                break
        assert mode.phase == "prepare"
        assert mode.round_number == 2
        assert blue.deaths == green.deaths == 0
        assert not any(p[0] == KillAction.id for p in server.packets)
        assert any(p[0] == CreatePlayer.id for p in server.packets)
        await mode.deactivate()
        assert mode._subscription is None
        assert not mode._entities and not mode.markers
    asyncio.run(scenario())


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.code)
def test_unofficial_wire_metadata_uses_supported_retail_mode_and_classes(spec):
    server, mode, blue, green, _ = make_game(spec.code)
    assert resolve_mode_code(spec.code) == spec.code
    assert canonical_mode_code(spec.code.replace("_", "-")) == spec.code
    initial = build_initial_info(server)
    state = build_state_data(server)
    assert state.mode_type == 6  # MODE_TDM
    assert state.team1_classes == list(mode.roster())
    # Both packets are serializable with the retail fixed-point layouts.
    assert InitialInfo(ByteReader(bytes(initial.generate())[1:])).mode_key == initial.mode_key
    assert StateData(ByteReader(bytes(state.generate())[1:])).mode_type == state.mode_type


def test_disguise_hunters_and_hiders_exchange_roles_without_exchanging_teams():
    async def scenario():
        _, mode, blue, green, _ = make_game("block_hunt")
        await start(mode)
        assert C.DISGUISE_TOOL in green.loadout and C.DISGUISE_TOOL not in blue.loadout
        assert mode.attackers == TEAM1
        await mode._new_round()
        assert mode.attackers == TEAM2
        assert C.DISGUISE_TOOL in blue.loadout and C.DISGUISE_TOOL not in green.loadout
        assert (blue.team, green.team, blue.deaths, green.deaths) == (TEAM1, TEAM2, 0, 0)
    asyncio.run(scenario())


def test_checkpoint_order_and_retry_penalty():
    async def scenario():
        _, mode, blue, _, now = make_game("crumbling_run")
        await start(mode)
        blue.position = mode.course[-1]
        await mode.tick_live(now[0], 0.1)
        assert blue not in mode.finished
        blue.position = mode.course[1]
        await mode.tick_live(now[0], 0.1)
        assert mode.checkpoint[blue] == 1
        await mode.retry(blue)
        assert blue.position == mode.course[1]
        assert mode.penalty[blue] == 2
        assert blue.deaths == 0
    asyncio.run(scenario())


def test_last_exit_banks_only_completed_extractions():
    async def scenario():
        _, mode, blue, green, now = make_game("last_exit")
        await start(mode)
        token = mode.cargo[0]
        await mode.take(blue, token)
        assert not mode.points
        now[0] += mode.duration / 2 + 1
        green.position = mode.home(TEAM2)
        blue.position = mode.anchor("exit")
        for _ in range(17):
            await mode.objectives_tick(now[0], 0.25)
        assert mode.points[TEAM1] == token.value
        assert blue in mode.extracted and blue.alive and blue.deaths == 0
        assert token not in mode.cargo
    asyncio.run(scenario())


def test_hot_potato_cannot_be_dropped_and_tag_keeps_fuse():
    async def scenario():
        _, mode, blue, green, now = make_game("hot_potato")
        await start(mode)
        mode.pass_to(blue)
        fuse = mode.fuse
        assert await mode.handle_drop_pickup(blue, blue.position, (0, 0, 0))
        assert mode.holder is blue
        now[0] += 2
        assert mode.modify_incoming_damage(green, 20, blue, int(C.MELEE_KILL)) == 0
        assert mode.holder is green and mode.fuse == fuse
    asyncio.run(scenario())


def test_one_bullet_is_spent_on_a_miss_and_earned_by_a_kill():
    async def scenario():
        _, mode, blue, green, _ = make_game("one_in_the_spade")
        await start(mode)
        mode.weapon_fired(blue, int(C.PISTOL_TOOL))
        assert mode.bullets[blue] == 0
        assert not mode.allow_action(blue, "shoot")
        await mode.on_weapon_fired(blue)
        assert C.PISTOL_TOOL not in blue.loadout
        await mode.on_player_death(green, blue, 0)
        assert mode.bullets[blue] == 1 and C.PISTOL_TOOL in blue.loadout
    asyncio.run(scenario())


def test_terrain_changes_restore_and_disconnected_ids_do_not_keep_scores():
    async def scenario():
        server, mode, blue, _, now = make_game("false_wall")
        await start(mode)
        cell = (240, 256, 96)
        original = server.world_manager.get_color(*cell)
        mode.set_cell(cell, False)
        mode.award(blue)
        await mode.finish((TEAM1,))
        now[0] += 7
        await mode.on_tick(20)
        mode.restore_slice()
        assert server.world_manager.get_solid(*cell)
        assert server.world_manager.get_color(*cell) == original
        await mode.on_player_leave(blue)
        replacement = add_player(server, blue.id, TEAM1)
        assert replacement not in mode.participants and replacement not in mode.points
    asyncio.run(scenario())
