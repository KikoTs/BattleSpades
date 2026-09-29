"""Competitive prefabs replicate the retail way: one BuildPrefabAction(30).

Retail ``PrefabManager.build_prefab`` (``add_to_user_blocks=True``) makes
every client expand the KV6 itself: ``add_user_block(...,
DEFAULT_PREFAB_HEALTH, replace_solids=True)`` per model voxel, a smoke ring
per top-layer voxel, and one wallet debit per model voxel for the owner
(measured on the live stock client 2026-09-26: superminibunker, 68 voxels,
wallet 500 -> 432, every cell health 9.0, 32 smoke rings; a second build over
the same solids debited 68 again).  The server therefore commits every model
cell in one post-physics tick with prefab health, charges the full model, and
sends one packet 30 to every in-game client including the owner, followed by
PrefabComplete(29) to the owner.  A shared per-tick cell budget defers whole
prefabs, never a partial one; the UGC editor lane keeps bounded batches.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import shared.constants as C
from server import prefabs
from server.config import ServerConfig
from server.game_constants import TEAM1
from server.main import BattleSpadesServer
from server.prefab_actions import (
    PrefabActionService,
    encode_block_manager_state,
)
from shared.bytes import ByteReader
from shared.packet import BlockBuildColored, BuildPrefabAction, PrefabComplete
from tests.test_construction_actions import _World, _server
from tests.test_join_mutation_catchup import CanonicalRecordingConnection


SUPERDOME_CELLS = 675  # largest stock competitive prefab
PREFAB_HEALTH = float(C.DEFAULT_PREFAB_HEALTH)
MODEL_RGB = (40, 50, 60)


def _row(count, *, x0=10, y=10, z=10):
    """A footprint of ``count`` cells resting on a solid row below it."""

    cells = []
    x, row = x0, 0
    for _ in range(count):
        cells.append((x, y + row, z))
        x += 1
        if x >= x0 + 200:
            x, row = x0, row + 1
    return cells


def _builder(player_id, *, blocks=1000, sent=None, is_bot=False, x=100.0):
    sent = [] if sent is None else sent
    return SimpleNamespace(
        id=player_id,
        name=f"Builder{player_id}",
        team=TEAM1,
        alive=True,
        spawned=True,
        x=x,
        y=100.0,
        z=20.0,
        eye=(x, 100.0, 18.0),
        loadout=[int(C.PREFAB_TOOL)],
        tool=int(C.PREFAB_TOOL),
        tool_is_raw=True,
        class_id=int(C.CLASS_SOLDIER),
        prefabs=["prefab_test"],
        blocks=blocks,
        is_bot=is_bot,
        deaths=0,
        bot_generation=1,
        replication_generation=1,
        _block_wallet_max=lambda: 100000,
        send=lambda data, **kwargs: sent.append(bytes(data)),
    )


def _service(monkeypatch, server, footprints):
    """Wire a deferred service whose model expands to ``footprints[name]``.

    The fake expansion applies the real 50/50 blend so colour assertions
    exercise the same arithmetic the client performs.
    """

    server.simulation_runtime = object()
    monkeypatch.setattr(prefabs, "prefab_allowed", lambda _p, _n: True)
    monkeypatch.setattr(
        prefabs, "get_registry",
        lambda: SimpleNamespace(get=lambda name: name),
    )

    def expand(model, *_args, base_color=None, **_kwargs):
        color = (
            MODEL_RGB if base_color is None
            else prefabs.blend_color(base_color, MODEL_RGB, 0.5)
        )
        return [(cell, color) for cell in footprints[model]]

    monkeypatch.setattr(prefabs, "expand_prefab", expand)
    service = PrefabActionService(server)
    # Reach/LOS are covered by the anticheat suites; these fixtures only
    # exercise commit and replication.
    monkeypatch.setattr(service, "_within_build_reach", lambda *_a: True)
    monkeypatch.setattr(service, "_footprint_visible", lambda *_a: True)
    return service


def _ids(payloads):
    return [payload[0] for payload in payloads]


def _native(server):
    """Terrain-construction packets broadcast by the service."""

    return [
        (payload, kwargs) for payload, kwargs in server.broadcasts
        if payload[0] in (BuildPrefabAction.id, 32, BlockBuildColored.id)
    ]


def _decode_build(payload) -> BuildPrefabAction:
    packet = BuildPrefabAction()
    packet.read(ByteReader(payload[1:]))
    return packet


def test_superdome_sized_prefab_commits_and_replicates_in_one_tick(monkeypatch):
    cells = _row(SUPERDOME_CELLS)
    world = _World(solids={(x, y, z + 1) for x, y, z in cells})
    server = _server(world)
    server.config = SimpleNamespace(prefab_cell_batch_limit=16, prefab_queue_limit=4)
    sent = []
    player = _builder(6, sent=sent)
    server.players[player.id] = player
    service = _service(monkeypatch, server, {"dome": cells})

    assert service.place(player, name="dome", position=cells[0])
    assert player.blocks == 1000 - SUPERDOME_CELLS
    assert not any(world.get_solid(*cell) for cell in cells)

    assert service.tick() == SUPERDOME_CELLS
    assert service.pending_count == 0
    assert all(world.get_solid(*cell) for cell in cells)
    native = _native(server)
    # ONE packet for the whole structure, to everyone including the owner.
    assert _ids(payload for payload, _kw in native) == [BuildPrefabAction.id]
    assert native[0][1].get("exclude") is None
    assert native[0][1].get("record_mutation") is False
    assert _ids(sent) == [PrefabComplete.id]
    server.broadcasts.clear()
    sent.clear()
    assert service.tick() == 0
    assert server.broadcasts == [] and sent == []


def test_build_prefab_action_carries_the_exact_retail_fields(monkeypatch):
    cells = _row(3)
    world = _World(solids={(x, y, z + 1) for x, y, z in cells})
    server = _server(world)
    server.config = SimpleNamespace()
    player = _builder(6)
    server.players[player.id] = player
    service = _service(monkeypatch, server, {"wall": cells})

    assert service.place(
        player, name="wall", position=cells[0], yaw=5, pitch=2, roll=3,
        color=(200, 100, 7),
    )
    service.tick()
    (payload, _kwargs), = _native(server)
    packet = _decode_build(payload)
    assert packet.player_id == player.id
    assert packet.prefab_name == "wall"
    assert tuple(packet.position) == cells[0]
    assert (packet.prefab_yaw, packet.prefab_pitch, packet.prefab_roll) == (1, 2, 3)
    assert tuple(packet.color) == (200, 100, 7)
    assert packet.add_to_user_blocks
    assert (packet.from_block_index, packet.to_block_index) == (0, 0)


def test_server_cells_store_the_client_blend_and_prefab_health(monkeypatch):
    cells = _row(3)
    world = _World(solids={(x, y, z + 1) for x, y, z in cells})
    server = _server(world)
    server.config = SimpleNamespace()
    player = _builder(6)
    server.players[player.id] = player
    service = _service(monkeypatch, server, {"wall": cells})

    # No packet colour: the team colour is the blend base, and the same base
    # is echoed so every client blends identically.
    assert service.place(player, name="wall", position=cells[0], color=None)
    service.tick()
    team_rgb = server.teams[TEAM1].color
    expected = prefabs.blend_color(team_rgb, MODEL_RGB, 0.5)
    assert all(world.colors[cell] == expected for cell in cells)
    assert all(world.block_health[cell] == PREFAB_HEALTH for cell in cells)
    (payload, _kwargs), = _native(server)
    assert tuple(_decode_build(payload).color) == tuple(team_rgb)


def test_blend_matches_the_client_int_truncation():
    # shared.common.blend_color(a, b, f) == int(a*f + b*(1-f)) per channel.
    assert prefabs.blend_color((1, 3, 255), (2, 4, 0), 0.5) == (1, 3, 127)
    # Live sample (stock client, 2026-09-26): base (44,117,179) blended with
    # superminibunker voxels rendered as (108..111, 148..151, 176..179): the
    # client re-randomises only the low two bits of each channel.
    blended = prefabs.blend_color((44, 117, 179), (176, 179, 179), 0.5)
    assert blended == (110, 148, 179)
    for sample in ((109, 150, 176), (111, 151, 177), (108, 148, 179)):
        assert tuple(c & ~3 for c in sample) == tuple(c & ~3 for c in blended)


def test_cell_budget_defers_whole_prefabs_never_splitting_one(monkeypatch):
    first, second = _row(600, y=10), _row(600, y=40)
    world = _World(solids={(x, y, z + 1) for x, y, z in first + second})
    server = _server(world)
    server.config = SimpleNamespace(prefab_competitive_cell_budget=1000)
    a, b = _builder(6, x=100.0), _builder(7, x=120.0)
    server.players.update({a.id: a, b.id: b})
    service = _service(monkeypatch, server, {"first": first, "second": second})

    assert service.place(a, name="first", position=first[0])
    assert service.place(b, name="second", position=second[0])

    assert service.tick() == 600
    assert all(world.get_solid(*cell) for cell in first)
    assert not any(world.get_solid(*cell) for cell in second)
    assert service.pending_count == 1
    assert len(_native(server)) == 1

    assert service.tick() == 600
    assert all(world.get_solid(*cell) for cell in second)
    assert service.pending_count == 0
    assert len(_native(server)) == 2


def test_two_prefabs_within_budget_share_one_tick(monkeypatch):
    first, second = _row(40, y=10), _row(40, y=40)
    world = _World(solids={(x, y, z + 1) for x, y, z in first + second})
    server = _server(world)
    server.config = SimpleNamespace()
    a, b = _builder(6, x=100.0), _builder(7, x=120.0)
    server.players.update({a.id: a, b.id: b})
    service = _service(monkeypatch, server, {"first": first, "second": second})

    assert service.place(a, name="first", position=first[0])
    assert service.place(b, name="second", position=second[0])
    assert service.tick() == 80
    assert service.pending_count == 0
    owners = [_decode_build(payload).player_id for payload, _kw in _native(server)]
    assert owners == [6, 7]


def test_prefab_larger_than_budget_still_commits_alone(monkeypatch):
    cells = _row(300)
    world = _World(solids={(x, y, z + 1) for x, y, z in cells})
    server = _server(world)
    server.config = SimpleNamespace(prefab_competitive_cell_budget=100)
    player = _builder(6)
    server.players[player.id] = player
    service = _service(monkeypatch, server, {"big": cells})

    assert service.place(player, name="big", position=cells[0])
    assert service.tick() == 300
    assert all(world.get_solid(*cell) for cell in cells)


def test_body_entering_footprint_before_commit_refuses_whole_prefab(monkeypatch):
    cells = _row(5)
    world = _World(solids={(x, y, z + 1) for x, y, z in cells})
    server = _server(world)
    server.config = SimpleNamespace()
    sent = []
    player = _builder(6, blocks=50, sent=sent)
    server.players[player.id] = player
    service = _service(monkeypatch, server, {"wall": cells})

    assert service.place(player, name="wall", position=cells[0])
    assert player.blocks == 45
    # Another player steps into the footprint after admission.
    x, y, z = cells[2]
    intruder = SimpleNamespace(
        id=9, alive=True, spawned=True, x=x + 0.5, y=y + 0.5, z=float(z) - 1.0,
    )
    server.players[intruder.id] = intruder

    assert service.tick() == 0
    assert not any(world.get_solid(*cell) for cell in cells)
    assert player.blocks == 50
    assert _native(server) == []
    assert _ids(sent) == [PrefabComplete.id]
    assert service.pending_count == 0 and server.construction.active_count == 0


def test_replaced_solids_stay_charged_one_to_one_with_the_model(monkeypatch):
    cells = _row(4)
    world = _World(solids={(x, y, z + 1) for x, y, z in cells})
    # One footprint cell is already solid at admission, another becomes
    # solid while queued: retail replace_solids overwrites both and the
    # client debits every model voxel, so the server does too.
    world.solids.add(cells[0])
    server = _server(world)
    server.config = SimpleNamespace()
    player = _builder(6, blocks=10)
    server.players[player.id] = player
    service = _service(monkeypatch, server, {"wall": cells})

    assert service.place(player, name="wall", position=cells[1])
    assert player.blocks == 6
    world.solids.add(cells[1])

    assert service.tick() == 4
    assert player.blocks == 6
    assert all(world.get_solid(*cell) for cell in cells)
    assert all(world.block_health[cell] == PREFAB_HEALTH for cell in cells)


def test_wallet_must_cover_the_whole_model(monkeypatch):
    cells = _row(4)
    world = _World(solids={(x, y, z + 1) for x, y, z in cells} | {cells[0]})
    server = _server(world)
    server.config = SimpleNamespace()
    player = _builder(6, blocks=3)  # only three NEW cells, four model voxels
    server.players[player.id] = player
    service = _service(monkeypatch, server, {"wall": cells})

    assert not service.place(player, name="wall", position=cells[1])
    assert player.blocks == 3 and service.pending_count == 0


def test_partially_out_of_world_prefab_is_refused_whole(monkeypatch):
    cells = [(510, 10, 10), (511, 10, 10), (512, 10, 10)]
    world = _World(solids={(x, y, z + 1) for x, y, z in cells})
    server = _server(world)
    server.config = SimpleNamespace()
    player = _builder(6)
    server.players[player.id] = player
    service = _service(monkeypatch, server, {"edge": cells})

    assert not service.place(player, name="edge", position=cells[0])
    assert player.blocks == 1000 and service.pending_count == 0


def test_infinite_block_team_is_not_charged(monkeypatch):
    cells = _row(4)
    world = _World(solids={(x, y, z + 1) for x, y, z in cells})
    server = _server(world)
    server.teams[TEAM1].infinite_blocks = True
    server.config = SimpleNamespace()
    player = _builder(6, blocks=0)
    server.players[player.id] = player
    service = _service(monkeypatch, server, {"wall": cells})

    assert service.place(player, name="wall", position=cells[0])
    assert service.tick() == 4
    assert player.blocks == 0
    assert len(_native(server)) == 1


def test_ugc_editor_lane_keeps_bounded_cell_batches(monkeypatch):
    cells = _row(5)
    world = _World(solids={(x, y, z + 1) for x, y, z in cells})
    server = _server(world)
    server.config = SimpleNamespace(prefab_cell_batch_limit=2)
    player = _builder(6)
    server.players[player.id] = player
    service = _service(monkeypatch, server, {"wall": cells})

    assert service.place(player, name="wall", position=cells[0])
    service._pending[0].editor_native = True
    assert service.tick() == 2
    assert service.pending_count == 1
    assert service.tick() == 2
    assert service.tick() == 1
    assert service.pending_count == 0
    # The editor lane replicates through its own native echo, not per cell,
    # and keeps default block health.
    assert _native(server) == []
    assert world.block_health == {}


def test_bot_prefab_uses_the_same_single_tick_commit(monkeypatch):
    cells = _row(120)
    world = _World(solids={(x, y, z + 1) for x, y, z in cells})
    server = _server(world)
    server.config = SimpleNamespace(prefab_cell_batch_limit=16)
    bot = _builder(6, is_bot=True)
    server.players[bot.id] = bot
    service = _service(monkeypatch, server, {"bunker": cells})

    assert service.place(bot, name="bunker", position=cells[0])
    assert service.tick() == 120
    assert all(world.get_solid(*cell) for cell in cells)
    assert service.pending_count == 0
    assert bot.blocks == 1000 - 120
    (payload, _kwargs), = _native(server)
    assert _decode_build(payload).player_id == bot.id


def test_block_manager_state_encoding_matches_the_stock_client():
    # Bytes produced by the stock client's own shared.packet.BlockManagerState
    # (user_blocks={(511,300,238): 4.75, (1,2,3): 9.0}, no damaged/occupied).
    client_bytes = bytes.fromhex(
        "260000000002000000ff012c01ee00130100020003002400000000"
    )
    assert encode_block_manager_state(
        [(511, 300, 238, 4.75), (1, 2, 3, 9.0)]
    ) == client_bytes
    # Remaining health rounds UP to the 0.25 wire step (never breaks early).
    assert encode_block_manager_state([(1, 2, 3, 8.9)])[-5] == 36


def _decode_state(payload):
    """Decode (damaged_rows, user_rows) of one BlockManagerState(38)."""

    import struct

    assert payload[0] == 38
    (damaged,) = struct.unpack_from("<i", payload, 1)
    offset = 5
    damaged_rows = []
    for _ in range(damaged):
        x, y, z, quarters, b, g, r = struct.unpack_from("<hhhBBBB", payload, offset)
        damaged_rows.append((x, y, z, quarters / 4.0, (r, g, b)))
        offset += 10
    (users,) = struct.unpack_from("<i", payload, offset)
    offset += 4
    rows = []
    for _ in range(users):
        x, y, z, quarters = struct.unpack_from("<hhhB", payload, offset)
        rows.append((x, y, z, quarters / 4.0))
        offset += 7
    assert struct.unpack_from("<i", payload, offset) == (0,)
    assert offset + 4 == len(payload)
    return damaged_rows, rows


def _decode_user_rows(payload):
    return _decode_state(payload)[1]


def test_late_joiner_gets_cells_then_prefab_health(monkeypatch):
    """Mid-load joiners get the cells as packet 33, then health via 38."""

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    ground = int(server.world_manager.get_height(70, 70))
    cells = [(70 + i, 70, ground - 1) for i in range(12)]
    joiner = CanonicalRecordingConnection(player_id=9)
    server.connections = {9: joiner}
    server.mark_map_snapshot_complete(joiner)

    player = _builder(6)
    server.players = {player.id: player}
    service = _service(monkeypatch, server, {"wall": cells})
    assert service.place(player, name="wall", position=cells[0])
    assert service.tick() == len(cells)
    # The joiner was still building its scene: no live packets reached it.
    assert joiner.sent == []

    server.replay_map_mutations(joiner)
    replayed = []
    for payload in joiner.sent:
        if payload[0] in (7, 38):
            # Colour pin / per-cell health row that follows each packet 33.
            continue
        assert payload[0] == BlockBuildColored.id
        packet = BlockBuildColored()
        packet.read(ByteReader(payload[1:]))
        replayed.append((packet.x, packet.y, packet.z))
    assert sorted(replayed) == sorted(cells)

    # Packet 33 lands at client health 3.0; the reveal then merges the
    # authoritative prefab health into the joiner's BlockManager.
    joiner.sent.clear()
    assert service.reveal_to(joiner) == len(cells)
    rows = [row for payload in joiner.sent for row in _decode_user_rows(payload)]
    assert sorted(rows) == sorted((*cell, PREFAB_HEALTH) for cell in cells)


def test_reveal_sends_remaining_health_in_bounded_batches(monkeypatch):
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    ground = int(server.world_manager.get_height(90, 90))
    cells = [(90 + i, 90, ground - 1) for i in range(5)]
    player = _builder(6)
    server.players = {player.id: player}
    service = _service(monkeypatch, server, {"wall": cells})
    assert service.place(player, name="wall", position=cells[0])
    service.tick()
    world = server.world_manager
    world.apply_block_damage(*cells[0], 2.5)
    world.destroy_blocks([cells[1]])
    server.config.prefab_health_state_batch = 2

    joiner = CanonicalRecordingConnection(player_id=9)
    # Four live prefab cells (user rows, initial health) plus the damaged
    # cell's DamagedBlock row (remaining health + original colour).
    assert service.reveal_to(joiner) == 5
    decoded = [_decode_state(p) for p in joiner.sent]
    assert all(len(d) + len(u) <= 2 for d, u in decoded)
    rows = {row[:3]: row[3] for _d, u in decoded for row in u}
    damaged = {row[:3]: row[3:] for d, _u in decoded for row in d}
    assert cells[1] not in rows and cells[1] not in damaged
    assert rows == {cell: PREFAB_HEALTH for cell in (cells[0], *cells[2:])}
    rgb = world.get_color(*cells[0]) & 0xFFFFFF
    assert damaged == {
        cells[0]: (
            PREFAB_HEALTH - 2.5,
            ((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF),
        )
    }


def test_reveal_without_prefab_cells_sends_nothing():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    joiner = CanonicalRecordingConnection(player_id=9)
    assert PrefabActionService(server).reveal_to(joiner) == 0
    assert joiner.sent == []


def test_competitive_budget_config_is_bounded():
    config = ServerConfig()
    assert config.prefab_competitive_cell_budget == 2048
