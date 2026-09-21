"""Shared human/bot placement, inventory, and lifecycle regressions."""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

import pytest

import shared.constants as C
from protocol.packet_handler import PacketHandler
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import BotAction, BotActionKind
from server.deployable_inventory import (
    STOCK_RULES,
    deployable_inventory_snapshot,
    deployable_stock,
)
from server.round_lifecycle import RoundLifecycle
from tests.test_equipment_handlers import _server_player


TOOLS = (C.MEDPACK_TOOL, C.DYNAMITE_TOOL, C.LANDMINE_TOOL,
         C.C4_TOOL, C.RADAR_STATION_TOOL)
POSITION = (101.0, 100.0, 62.0)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("server.deployable_actions.time.monotonic", lambda: now[0])
    return now


def _place(server, player, position=POSITION):
    service = server.deployable_actions
    methods = {
        int(C.MEDPACK_TOOL): service.place_medpack,
        int(C.DYNAMITE_TOOL): service.place_dynamite,
        int(C.LANDMINE_TOOL): service.place_landmine,
        int(C.RADAR_STATION_TOOL): service.place_radar,
    }
    if player.tool == int(C.C4_TOOL):
        return service.place_c4(player, position, face=4)
    return methods[player.tool](player, position)


@pytest.mark.parametrize("tool", TOOLS)
def test_rejected_geometry_or_authorization_spends_no_stock_or_cadence(tool, clock):
    server, player, _ = _server_player(tool, [tool])
    initial = STOCK_RULES[tool].initial
    for invalid in ((float("nan"), 100, 62), (400, 100, 62), (100, 100, 30)):
        assert not _place(server, player, invalid)
        assert player.deployable_stock[tool] == initial
        assert player._deployable_next_use == {}
    player.alive = False
    assert not _place(server, player)
    player.alive = True
    player.loadout = []
    assert not _place(server, player)
    assert player.deployable_stock[tool] == initial
    assert player._deployable_next_use == {}
    assert not server.entity_registry.all()


@pytest.mark.parametrize("tool", TOOLS)
def test_success_debits_once_and_duplicate_packet_cannot_place_twice(tool, clock):
    server, player, _ = _server_player(tool, [tool])
    rule = STOCK_RULES[tool]
    assert _place(server, player)
    assert player.deployable_stock[tool] == rule.initial - 1
    assert player._deployable_next_use[tool] == clock[0] + rule.interval
    assert not _place(server, player)
    assert player.deployable_stock[tool] == rule.initial - 1
    assert len(server.entity_registry.all()) == 1


@pytest.mark.parametrize("tool", TOOLS)
def test_registry_creation_failure_does_not_charge_or_start_cooldown(tool, clock, monkeypatch):
    server, player, _ = _server_player(tool, [tool])

    def fail(*args, **kwargs):
        raise RuntimeError("entity id space exhausted")

    monkeypatch.setattr(server.entity_registry, "place", fail)
    with pytest.raises(RuntimeError, match="entity id"):
        _place(server, player)
    assert player.deployable_stock[tool] == STOCK_RULES[tool].initial
    assert player._deployable_next_use == {}
    assert not server.entity_registry.all()


@pytest.mark.parametrize("tool", TOOLS)
def test_broadcast_failure_after_creation_does_not_grant_free_entity(tool, clock, monkeypatch):
    server, player, _ = _server_player(tool, [tool])

    def fail(*args, **kwargs):
        raise RuntimeError("transport unavailable")

    monkeypatch.setattr(server, "broadcast_create_entity", fail)
    with pytest.raises(RuntimeError, match="transport"):
        _place(server, player)
    assert len(server.entity_registry.all()) == 1
    assert player.deployable_stock[tool] == STOCK_RULES[tool].initial - 1
    assert player._deployable_next_use[tool] > clock[0]


@pytest.mark.parametrize("tool", TOOLS)
def test_spawn_notification_restock_pickup_and_tool_switch_lifecycle(tool, clock):
    server, player, _ = _server_player(tool, [tool])
    rule = STOCK_RULES[tool]
    player.restock_ammo()  # notification after spawn, not an extra pickup
    assert player.deployable_stock[tool] == rule.initial
    assert _place(server, player)
    deadline = player._deployable_next_use[tool]
    player.set_tool(C.BLOCK_TOOL, raw=True)
    player.set_tool(tool, raw=True)
    player._reset_equipment_state()
    assert player.deployable_stock[tool] == rule.initial - 1
    assert player._deployable_next_use[tool] == deadline
    player.restock_ammo(int(C.AMMO_CRATE))
    assert player.deployable_stock[tool] == min(rule.maximum, rule.initial - 1 + rule.restock)
    assert player._deployable_next_use[tool] == deadline
    assert not _place(server, player)  # a pickup does not bypass cadence
    player.restock_ammo(int(C.AMMO_CRATE))
    assert player.deployable_stock[tool] <= rule.maximum
    player.alive = False
    player.deployable_stock[tool] = 0
    player.restock_ammo(int(C.AMMO_CRATE))
    assert player.deployable_stock[tool] == 0
    player.spawn(100.5, 100.5, 59.75)
    assert player.deployable_stock[tool] == rule.initial
    assert player._deployable_next_use == {}


@pytest.mark.parametrize("is_bot", (False, True))
def test_real_respawn_lifecycle_does_not_double_initial_consumable_stock(is_bot, clock):
    server, player, _ = _server_player(C.DYNAMITE_TOOL, [C.DYNAMITE_TOOL])
    player.is_bot = is_bot
    player.deployable_stock[C.DYNAMITE_TOOL] = 0
    player._deployable_next_use[C.DYNAMITE_TOOL] = clock[0] + 20.0
    player.alive = False
    RoundLifecycle(server).respawn_player(player)
    assert player.deployable_stock[C.DYNAMITE_TOOL] == int(C.DYNAMITE_INITIAL_STOCK)
    assert player._deployable_next_use == {}


def test_medpack_packets_and_bot_gateway_share_one_bounded_wallet(clock):
    server, player, _ = _server_player(C.MEDPACK_TOOL, [C.MEDPACK_TOOL])
    packet = bytes([90]) + struct.pack("<IBHHHB", 10, player.id, 101, 100, 62, 4)
    asyncio.run(PacketHandler(server).handle(player, packet))
    assert deployable_stock(player, C.MEDPACK_TOOL) == 1
    player.is_bot = True
    action = BotAction(BotActionKind.DEPLOY, tool_id=C.MEDPACK_TOOL,
                       position=POSITION, face=4)
    gateway = BotActionGateway(server)
    assert not gateway.execute(player, action)
    clock[0] += float(C.MEDPACK_SHOOT_INTERVAL)
    assert gateway.execute(player, action)
    assert deployable_stock(player, C.MEDPACK_TOOL) == 0
    clock[0] += 60.0
    assert not gateway.execute(player, action)
    asyncio.run(PacketHandler(server).handle(player, packet))
    assert len(server.entity_registry.all()) == int(C.MEDPACK_INITIAL_STOCK)


@pytest.mark.parametrize("tool", (C.DYNAMITE_TOOL, C.LANDMINE_TOOL))
def test_oriented_projectiles_and_placed_explosives_share_stock_and_cadence(tool, clock):
    server, player, _ = _server_player(tool, [tool])
    use = lambda: server.oriented_actions.use(
        player, tool_id=tool, position=player.position,
        velocity=(1.0, 0.0, 0.0), fuse=3.0,
    )
    assert _place(server, player)
    assert not use()
    clock[0] += 1.0
    if player.deployable_stock[tool] == 0:
        assert not use()
        player.restock_ammo(int(C.AMMO_CRATE))
    before = player.deployable_stock[tool]
    assert use()
    assert player.deployable_stock[tool] == before - 1
    assert not _place(server, player)
    clock[0] += 1.0
    assert _place(server, player)
    assert player.deployable_stock[tool] == before - 2


def test_c4_live_cap_and_remote_detonation_never_refund_stock(clock):
    server, player, _ = _server_player(C.C4_TOOL, [C.C4_TOOL])
    server._apply_blast = lambda *args, **kwargs: None
    assert _place(server, player)
    clock[0] += 1.0
    assert _place(server, player)
    player.restock_ammo(int(C.AMMO_CRATE))
    assert player.deployable_stock[C.C4_TOOL] == int(C.C4_RESTOCK_AMOUNT)
    assert deployable_stock(player, C.C4_TOOL) == 0  # two still live
    deadline = player._deployable_next_use[C.C4_TOOL]
    clock[0] += 1.0
    assert not _place(server, player)
    assert player._deployable_next_use[C.C4_TOOL] == deadline
    assert server.deployable_actions.detonate_c4(player)
    assert deployable_stock(player, C.C4_TOOL) == 1
    assert _place(server, player)
    assert server.deployable_actions.detonate_c4(player)
    clock[0] += 60.0
    assert deployable_stock(player, C.C4_TOOL) == 0
    assert not _place(server, player)


def test_radar_expiry_and_support_loss_do_not_restock(clock):
    server, player, _ = _server_player(C.RADAR_STATION_TOOL, [C.RADAR_STATION_TOOL])
    assert _place(server, player)
    radar = server.entity_registry.get(player._radar_entity_id)
    context = server._build_entity_ctx()
    radar.behavior.on_tick(radar, 0.1, context)
    context.now += radar.behavior.lifetime
    radar.behavior.on_tick(radar, 0.1, context)
    assert player._radar_entity_id is None
    clock[0] += 60.0
    assert deployable_stock(player, C.RADAR_STATION_TOOL) == 0
    assert not _place(server, player)
    player.restock_ammo(int(C.AMMO_CRATE))
    assert _place(server, player)
    assert server.world_manager.destroy_blocks([(101, 100, 62)])
    assert player._radar_entity_id is None
    assert deployable_stock(player, C.RADAR_STATION_TOOL) == 0


def test_service_medpack_gives_three_25_health_touches_without_inventory_refund(clock):
    server, player, _ = _server_player(C.MEDPACK_TOOL, [C.MEDPACK_TOOL])
    assert _place(server, player)
    pack = server.entity_registry.all()[0]
    context = server._build_entity_ctx()
    assert pack.behavior.heal_amount == int(C.MEDPACK_HEAL_AMOUNT) == 25
    assert pack.behavior.uses == int(C.MEDPACK_USES) == 3
    for remaining in (2, 1, 0):
        player.health = 50
        assert pack.behavior.on_touch(pack, player, context)
        assert player.health == 75
        assert pack.behavior.uses == remaining
        assert player.deployable_stock[C.MEDPACK_TOOL] == 1
    assert server.entity_registry.get(pack.entity_id) is None


def test_turret_controller_debits_once_and_service_gates_placement_cadence(clock):
    server, player, _ = _server_player(C.ROCKET_TURRET_TOOL, [C.ROCKET_TURRET_TOOL])
    place = lambda: server.deployable_actions.place_rocket_turret(player, POSITION, yaw=0.0)
    assert place()
    assert player.rocket_turret_stock == int(C.ROCKET_TURRET_INITIAL_STOCK) - 1
    assert not place()
    clock[0] += float(C.ROCKET_TURRET_SHOOT_INTERVAL)
    assert place()
    assert player.rocket_turret_stock == 0
    clock[0] += 60.0
    assert not place()
    assert len(server.rocket_turrets) == int(C.ROCKET_TURRET_INITIAL_STOCK)


def test_inventory_snapshot_is_read_only_and_includes_equipped_empty_items(clock):
    server, player, _ = _server_player(C.MEDPACK_TOOL, [C.MEDPACK_TOOL])
    player.deployable_stock[C.MEDPACK_TOOL] = 0
    before = dict(player.deployable_stock)
    assert deployable_inventory_snapshot(player) == ((int(C.MEDPACK_TOOL), 0),)
    assert deployable_stock(player, C.LANDMINE_TOOL) == 0
    assert player.deployable_stock == before
    assert player._deployable_next_use == {}
    unknown = SimpleNamespace(loadout=[int(C.MEDPACK_TOOL)])
    assert deployable_inventory_snapshot(unknown) == ((int(C.MEDPACK_TOOL), 0),)
    assert not hasattr(unknown, "deployable_stock")


def test_machine_gun_snapshot_preserves_existing_one_owned_entity_limit(clock):
    server, player, _ = _server_player(C.MG_TOOL, [C.MG_TOOL])
    place = lambda: server.deployable_actions.place_machine_gun(player, POSITION, yaw=0.0)
    assert deployable_inventory_snapshot(player) == ((int(C.MG_TOOL), 1),)
    assert place()
    assert deployable_inventory_snapshot(player) == ((int(C.MG_TOOL), 0),)
    clock[0] += 60.0
    player.restock_ammo(int(C.AMMO_CRATE))
    assert not place()
    entity = server.entity_registry.all()[0]
    server.entity_registry.remove(entity.entity_id)
    assert deployable_inventory_snapshot(player) == ((int(C.MG_TOOL), 1),)
    assert place()


def test_disguise_snapshot_observes_existing_wallet_without_double_debit(clock):
    server, player, _ = _server_player(C.DISGUISE_TOOL, [C.DISGUISE_TOOL])
    initial = int(C.DISGUISE_INITIAL_STOCK)
    assert deployable_inventory_snapshot(player) == ((int(C.DISGUISE_TOOL), initial),)
    assert server.deployable_actions.set_disguise(player, active=True)
    assert deployable_stock(player, C.DISGUISE_TOOL) == initial - 1
    assert int(C.DISGUISE_TOOL) not in player.deployable_stock
    assert server.deployable_actions.set_disguise(player, active=False)
    assert not server.deployable_actions.set_disguise(player, active=True)
    clock[0] += float(C.DISGUISE_SHOOT_INTERVAL)
    assert server.deployable_actions.set_disguise(player, active=True)
    assert deployable_stock(player, C.DISGUISE_TOOL) == initial - 2


def test_turret_transport_failure_keeps_controller_charge_and_placement_deadline(clock, monkeypatch):
    server, player, _ = _server_player(C.ROCKET_TURRET_TOOL, [C.ROCKET_TURRET_TOOL])

    def fail(*args, **kwargs):
        raise RuntimeError("transport unavailable")

    monkeypatch.setattr(server, "broadcast_create_entity", fail)
    with pytest.raises(RuntimeError, match="transport"):
        server.deployable_actions.place_rocket_turret(player, POSITION, yaw=0.0)
    assert len(server.entity_registry.all()) == len(server.rocket_turrets) == 1
    assert player.rocket_turret_stock == int(C.ROCKET_TURRET_INITIAL_STOCK) - 1
    assert player._deployable_next_use[C.ROCKET_TURRET_TOOL] == clock[0] + float(C.ROCKET_TURRET_SHOOT_INTERVAL)
    assert not server.deployable_actions.place_rocket_turret(player, POSITION, yaw=0.0)
