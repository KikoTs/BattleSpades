"""A deferred bot construct belongs to the life and inventory that paid for it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import shared.constants as C

from server import prefabs
from server.prefab_actions import PrefabActionService
from tests.test_construction_actions import _World, _server
from tests.test_equipment_handlers import _server_player


CELLS = ((10, 10, 10), (11, 10, 10), (12, 10, 10))


def queued(monkeypatch, *, human=False, actual_player=False):
    if actual_player:
        server, player, _ = _server_player(C.PREFAB_TOOL, [C.PREFAB_TOOL])
        server.world_manager = _World(solids={(10, 10, 11)})
    else:
        server = _server(_World(solids={(10, 10, 11)}))
        server.config = SimpleNamespace()
        player = SimpleNamespace(
            id=6, name="QueuedBot", team=2, alive=True, spawned=True,
            x=100., y=100., z=20., loadout=[int(C.PREFAB_TOOL)],
            tool=int(C.PREFAB_TOOL), tool_is_raw=True,
            class_id=int(C.CLASS_SOLDIER), deaths=0,
            bot_generation=1, replication_generation=1,
            send=lambda *_args, **_kwargs: None,
            _block_wallet_max=lambda: 100,
        )
    player.is_bot = not human
    player.prefabs = ["prefab_test"]
    player.blocks = 10
    server.players[player.id] = player
    if not actual_player:
        server.simulation_runtime = object()
    server.config.prefab_cell_batch_limit = 1
    server.config.prefab_queue_limit = 4
    monkeypatch.setattr(prefabs, "prefab_allowed", lambda owner, name: name in owner.prefabs)
    monkeypatch.setattr(prefabs, "get_registry", lambda: SimpleNamespace(get=lambda _name: object()))
    monkeypatch.setattr(prefabs, "expand_prefab", lambda *_a, **_kw: [(cell, (10, 20, 30)) for cell in CELLS])
    service = PrefabActionService(server)
    assert service.place(player, name="prefab_test", position=CELLS[0])
    assert player.blocks == 7 and service.pending_count == 1
    return server, player, service


@pytest.mark.parametrize("committed", (0, 1))
def test_death_cancels_remaining_cells_and_does_not_refund_dead_wallet(monkeypatch, committed):
    server, player, service = queued(monkeypatch)
    for _ in range(committed):
        assert service.tick() == 1
    player.alive = player.spawned = False
    player.deaths += 1
    assert service.tick() == 0
    assert service.pending_count == 0 and server.construction.active_count == 0
    assert set(CELLS) & server.world_manager.solids == set(CELLS[:committed])
    assert player.blocks == 7


@pytest.mark.parametrize("kill_type", (int(C.WEAPON_KILL), int(C.CLASS_CHANGE_KILL)))
def test_actual_player_death_and_immediate_respawn_cannot_commit_or_refund_old_job(monkeypatch, kill_type):
    server, player, service = queued(monkeypatch, actual_player=True)
    assert service.tick() == 1
    old_deaths, old_life = player.deaths, player.replication_generation
    player.die(kill_type=kill_type)
    player.spawn(100.5, 100.5, 59.75)
    player.set_tool(C.PREFAB_TOOL, raw=True)
    fresh_blocks = player.blocks
    assert player.replication_generation == old_life + 1
    assert player.deaths == old_deaths + (kill_type != int(C.CLASS_CHANGE_KILL))
    assert service.tick() == 0
    assert player.blocks == fresh_blocks
    assert set(CELLS) & server.world_manager.solids == {CELLS[0]}
    assert service.pending_count == 0 and server.construction.active_count == 0


@pytest.mark.parametrize("field,value", (
    ("tool", int(C.BLOCK_TOOL)), ("tool_is_raw", False),
    ("prefabs", ["prefab_other"]), ("loadout", [int(C.BLOCK_TOOL)]),
    ("class_id", int(C.CLASS_MINER)), ("team", 3), ("is_bot", False),
))
def test_same_life_selection_change_cancels_and_refunds_only_unbuilt_cells(monkeypatch, field, value):
    server, player, service = queued(monkeypatch)
    assert service.tick() == 1
    setattr(player, field, value)
    assert service.tick() == 0
    assert player.blocks == 9
    assert set(CELLS) & server.world_manager.solids == {CELLS[0]}
    assert service.cancel_owner(player.id) == 0
    assert player.blocks == 9 and server.construction.active_count == 0


@pytest.mark.parametrize("replacement", (False, True))
def test_bot_replacement_never_receives_a_previous_generation_refund(monkeypatch, replacement):
    server, player, service = queued(monkeypatch)
    if replacement:
        fresh = SimpleNamespace(**vars(player))
        fresh.blocks = 100
        server.players[player.id] = fresh
    else:
        player.bot_generation += 1
        player.blocks = 100
        fresh = player
    assert service.tick() == 0
    assert fresh.blocks == 100
    assert not set(CELLS) & server.world_manager.solids
    assert server.construction.active_count == 0


def test_cancellation_after_pickup_cannot_exceed_wallet_maximum(monkeypatch):
    _, player, service = queued(monkeypatch)
    player.blocks = 99  # another authoritative source filled the wallet meanwhile
    assert service.cancel_owner(player.id) == 1
    assert player.blocks == 100
    assert service.cancel_owner(player.id) == 0
    assert player.blocks == 100


@pytest.mark.parametrize("new_life", (False, True))
def test_round_cancel_all_refunds_only_the_original_life(monkeypatch, new_life):
    server, player, service = queued(monkeypatch)
    if new_life:
        player.replication_generation += 1
        player.blocks = 100
    service.cancel_all()
    assert player.blocks == (100 if new_life else 10)
    assert service.pending_count == 0 and server.construction.active_count == 0


def test_human_prefab_job_keeps_existing_tool_switch_semantics(monkeypatch):
    server, player, service = queued(monkeypatch, human=True)
    player.tool = int(C.BLOCK_TOOL)
    assert sum(service.tick() for _ in CELLS) == len(CELLS)
    assert set(CELLS).issubset(server.world_manager.solids)
    assert player.blocks == 7 and service.pending_count == 0
