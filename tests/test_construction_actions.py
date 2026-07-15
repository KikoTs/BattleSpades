"""Construction reservations and shared prefab action regressions."""

from __future__ import annotations

from types import SimpleNamespace

import shared.constants as C

from server import prefabs
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import BotAction, BotActionKind
from server.construction import ConstructionSafetyService
from server.game_constants import TEAM1, TEAM2
from server.prefab_actions import PrefabActionService


class _World:
    def __init__(self, solids=()):
        self.solids = set(solids)
        self.map_metadata = SimpleNamespace(spawn_zones={}, base_zones={})

    def get_solid(self, x, y, z):
        return (int(x), int(y), int(z)) in self.solids

    def set_block(self, x, y, z, solid=True, color=0):
        coordinate = int(x), int(y), int(z)
        if solid:
            self.solids.add(coordinate)
        else:
            self.solids.discard(coordinate)
        return True

    def get_height(self, x, y):
        heights = [z for sx, sy, z in self.solids if sx == int(x) and sy == int(y)]
        return min(heights) if heights else 239


def _server(world=None):
    server = SimpleNamespace(
        world_manager=world or _World(),
        mode=None,
        players={},
        teams={
            TEAM1: SimpleNamespace(color=(0, 0, 255), infinite_blocks=False),
            TEAM2: SimpleNamespace(color=(255, 0, 0), infinite_blocks=False),
        },
        loop_count=77,
        broadcasts=[],
    )

    def broadcast(data, **kwargs):
        server.broadcasts.append((bytes(data), kwargs))

    server.broadcast = broadcast
    server.construction = ConstructionSafetyService(server)
    return server


def test_team_path_reservation_blocks_only_friendly_builder():
    server = _server()
    service = server.construction
    cell = (20, 20, 20)

    assert service.reserve_path(1, TEAM1, (cell,)) is not None
    token, reason = service.reserve_construction(2, TEAM1, (cell,))
    assert token is None
    assert reason == "reserved construction or friendly path"

    enemy_token, reason = service.reserve_construction(3, TEAM2, (cell,))
    assert enemy_token is not None
    assert reason == ""


def test_reservations_expire_without_background_work():
    now = [10.0]
    server = _server()
    service = ConstructionSafetyService(server, clock=lambda: now[0])
    token, _reason = service.reserve_construction(
        1, TEAM1, ((30, 30, 30),), ttl=0.25
    )
    assert token is not None
    assert service.active_count == 1
    now[0] = 10.26
    assert service.active_count == 0


def test_ctf_capture_bounds_are_protected_from_construction():
    server = _server()
    server.mode = SimpleNamespace(
        base_bounds={TEAM1: (90, 100, 110, 120, 20, 40)}
    )
    token, reason = server.construction.reserve_construction(
        4, TEAM1, ((95, 115, 30),)
    )
    assert token is None
    assert reason == "spawn or objective zone"


def test_prefab_service_uses_colored_observer_and_plain_owner_paths(monkeypatch):
    world = _World(solids={(10, 10, 11)})
    server = _server(world)
    sent = []
    player = SimpleNamespace(
        id=5,
        name="Builder",
        team=TEAM1,
        alive=True,
        spawned=True,
        loadout=[int(C.PREFAB_TOOL)],
        tool=int(C.PREFAB_TOOL),
        tool_is_raw=True,
        class_id=int(C.CLASS_SOLDIER),
        prefabs=["prefab_test"],
        blocks=10,
        send=lambda data, **kwargs: sent.append(bytes(data)),
    )
    monkeypatch.setattr(prefabs, "prefab_allowed", lambda _player, _name: True)
    monkeypatch.setattr(
        prefabs,
        "get_registry",
        lambda: SimpleNamespace(get=lambda _name: object()),
    )
    monkeypatch.setattr(
        prefabs,
        "expand_prefab",
        lambda *_args, **_kwargs: [((10, 10, 10), (10, 20, 30))],
    )

    accepted = PrefabActionService(server).place(
        player,
        name="prefab_test",
        position=(10, 10, 10),
        color=(0, 0, 255),
        loop_count=66,
    )

    assert accepted is True
    assert player.blocks == 9
    assert world.get_solid(10, 10, 10)
    assert [payload[0] for payload, _kwargs in server.broadcasts] == [33]
    assert [payload[0] for payload in sent] == [32, 29]


def test_bot_gateway_routes_prefab_to_shared_service():
    calls = []
    server = SimpleNamespace(
        loop_count=12,
        prefab_actions=SimpleNamespace(
            place=lambda player, **kwargs: calls.append((player, kwargs)) or True
        ),
    )

    class _Bot:
        id = 2
        team = TEAM1
        is_bot = True
        alive = True
        spawned = True
        loadout = [int(C.PREFAB_TOOL)]
        tool = -1
        block_color = 0x123456

        def set_tool(self, tool, raw=True):
            self.tool = int(tool)

    bot = _Bot()
    action = BotAction(
        BotActionKind.PLACE_PREFAB,
        tool_id=int(C.PREFAB_TOOL),
        position=(50.0, 60.0, 70.0),
        argument="prefab_fort_wall",
        yaw=1.6,
    )

    assert BotActionGateway(server).execute(bot, action) is True
    assert calls[0][1]["name"] == "prefab_fort_wall"
    assert calls[0][1]["yaw"] == 1
    assert calls[0][1]["snap_to_surface"] is True


def test_zombie_prefab_tool_routes_through_the_same_authoritative_service():
    calls = []
    server = SimpleNamespace(
        loop_count=12,
        prefab_actions=SimpleNamespace(
            place=lambda player, **kwargs: calls.append((player, kwargs)) or True
        ),
    )

    class _ZombieBot:
        id = 8
        team = TEAM2
        is_bot = True
        alive = True
        spawned = True
        loadout = [int(C.ZOMBIEHAND_TOOL), int(C.ZOMBIE_PREFAB_TOOL)]
        tool = int(C.ZOMBIEHAND_TOOL)
        block_color = 0x336633

        def set_tool(self, tool, raw=True):
            self.tool = int(tool)

    bot = _ZombieBot()
    action = BotAction(
        BotActionKind.PLACE_PREFAB,
        tool_id=int(C.ZOMBIE_PREFAB_TOOL),
        position=(50.0, 60.0, 70.0),
        argument="prefab_zombiehand",
        yaw=0.0,
    )

    assert BotActionGateway(server).execute(bot, action) is True
    assert bot.tool == int(C.ZOMBIE_PREFAB_TOOL)
    assert calls[0][1]["name"] == "prefab_zombiehand"


def test_prefab_service_authorizes_native_zombie_prefab_tool(monkeypatch):
    world = _World(solids={(10, 10, 11)})
    server = _server(world)
    player = SimpleNamespace(
        id=15,
        name="ZombieBuilder",
        team=TEAM2,
        alive=True,
        spawned=True,
        loadout=[int(C.ZOMBIEHAND_TOOL), int(C.ZOMBIE_PREFAB_TOOL)],
        tool=int(C.ZOMBIE_PREFAB_TOOL),
        tool_is_raw=True,
        class_id=int(C.CLASS_ZOMBIE),
        prefabs=["prefab_zombiehand"],
        blocks=1000,
        send=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(prefabs, "prefab_allowed", lambda _player, _name: True)
    monkeypatch.setattr(
        prefabs,
        "get_registry",
        lambda: SimpleNamespace(get=lambda _name: object()),
    )
    monkeypatch.setattr(
        prefabs,
        "expand_prefab",
        lambda *_args, **_kwargs: [((10, 10, 10), (10, 20, 30))],
    )

    assert PrefabActionService(server).place(
        player,
        name="prefab_zombiehand",
        position=(10, 10, 10),
    ) is True


def test_production_prefab_queue_drains_in_bounded_cell_batches(monkeypatch):
    world = _World(solids={(10, 10, 11)})
    server = _server(world)
    server.simulation_runtime = object()
    server.config = SimpleNamespace(prefab_cell_batch_limit=1, prefab_queue_limit=4)
    sent = []
    player = SimpleNamespace(
        id=6,
        name="QueuedBuilder",
        team=TEAM1,
        alive=True,
        spawned=True,
        x=100.0,
        y=100.0,
        z=20.0,
        loadout=[int(C.PREFAB_TOOL)],
        tool=int(C.PREFAB_TOOL),
        tool_is_raw=True,
        class_id=int(C.CLASS_SOLDIER),
        prefabs=["prefab_test"],
        blocks=10,
        send=lambda data, **kwargs: sent.append(bytes(data)),
    )
    server.players[player.id] = player
    monkeypatch.setattr(prefabs, "prefab_allowed", lambda _player, _name: True)
    monkeypatch.setattr(
        prefabs,
        "get_registry",
        lambda: SimpleNamespace(get=lambda _name: object()),
    )
    monkeypatch.setattr(
        prefabs,
        "expand_prefab",
        lambda *_args, **_kwargs: [
            ((10, 10, 10), (10, 20, 30)),
            ((11, 10, 10), (10, 20, 30)),
        ],
    )
    service = PrefabActionService(server)

    assert service.place(
        player, name="prefab_test", position=(10, 10, 10)
    )
    assert service.pending_count == 1
    assert player.blocks == 8
    assert not world.get_solid(10, 10, 10)

    assert service.tick() == 1
    assert service.pending_count == 1
    assert service.tick() == 1
    assert service.pending_count == 0
    assert [payload[0] for payload, _kwargs in server.broadcasts] == [33, 33]
    assert [payload[0] for payload in sent] == [32, 32, 29]
