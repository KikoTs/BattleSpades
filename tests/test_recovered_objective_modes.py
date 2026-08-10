"""Native behavior regressions for TC, Diamond Mine, and Occupation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants as C
import shared.constants_gamemode as CG

from modes.diamond_mine import DiamondMineMode
from modes.occupation import OccupationMode
from modes.territory_control import TerritoryControlMode
from server.entities.registry import EntityRegistry
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL
from server.map_metadata import MapMetadata, MapZone
from server.team import Team
from shared.bytes import ByteReader
from shared.packet import (
    CreateEntity,
    LocalisedMessage,
    MinimapZone,
    PickPickup,
    TerritoryBaseState,
)


class _World:
    map_name = "RecoveredObjectives"
    map = SimpleNamespace(source_z_shift=0)

    def __init__(self) -> None:
        self.map_metadata = MapMetadata()

    def team_base_anchor(self, team: int):
        return (64.0, 256.0, 50.0) if team == TEAM1 else (448.0, 256.0, 50.0)

    def dry_ground_anchor(self, x, y, search=24):
        return float(x), float(y), 50.0

    def dry_surface_anchor(self, x, y, search=24):
        return float(x), float(y), 60.0


class _Server:
    def __init__(self, settings=None) -> None:
        self.config = SimpleNamespace(
            mode_settings=settings or {},
            configured_time_limit=lambda _mode, fallback: fallback,
        )
        self.world_manager = _World()
        self.entity_registry = EntityRegistry()
        self.teams = {
            TEAM1: Team(TEAM1, "Blue", (0, 0, 255)),
            TEAM2: Team(TEAM2, "Green", (0, 255, 0)),
        }
        self.players = {}
        self.packets = []
        self.created = []
        self.destroyed = []
        self.score_updates = []
        self.blasts = []
        self.loop_count = 1

    def broadcast(self, data, **_kwargs):
        self.packets.append(bytes(data))

    def broadcast_create_entity(self, entity):
        self.created.append(entity)

    def broadcast_destroy_entity(self, entity_id):
        self.destroyed.append(int(entity_id))

    def broadcast_set_score(self, team, reason=None):
        self.score_updates.append((team.id, team.score, reason))

    def _apply_blast(self, *args, **kwargs):
        self.blasts.append((args, kwargs))


class _Player:
    def __init__(self, player_id: int, team: int, position) -> None:
        self.id = int(player_id)
        self.name = f"Player {player_id}"
        self.team = int(team)
        self.alive = True
        self.spawned = True
        self.score = 0
        self.pickup_id = None
        self.pickup_burdensome = False
        self.pickup_state = None
        self._world_object = None
        self.vx = self.vy = self.vz = 0.0
        self.is_bot = False
        self.sent = []
        self.set_position(position)

    def set_position(self, position) -> None:
        self.x, self.y, self.z = (float(value) for value in position)
        self.position = (self.x, self.y, self.z)

    def send(self, data, **_kwargs):
        self.sent.append(bytes(data))


def _decode(rows, packet_type):
    return [
        packet_type(ByteReader(data[1:]))
        for data in rows
        if data and data[0] == packet_type.id
    ]


def _zone(team, x):
    return MapZone(
        "base",
        team,
        float(x),
        100.0,
        50.0,
        (-5.0, 5.0, -5.0, 5.0, -8.0, 8.0),
        "test_zone",
    )


def test_territory_control_initialises_native_hud_and_captures_neutral_base(
    monkeypatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr("modes.territory_control.time.time", lambda: now[0])
    server = _Server({"tc": {"max_active_bases": 3, "capture_rate": 1.0}})
    server.world_manager.map_metadata.neutral_base_zones.extend([
        _zone(TEAM_NEUTRAL, 100),
        _zone(TEAM_NEUTRAL, 200),
        _zone(TEAM_NEUTRAL, 300),
    ])
    mode = TerritoryControlMode(server)

    asyncio.run(mode.on_mode_start())

    states = _decode(server.packets, TerritoryBaseState)
    assert len(states) == 6
    assert [item.action for item in states] == [
        C.TC_INITIAL_INFO, C.TC_BASE_ACTIVATE,
        C.TC_INITIAL_INFO, C.TC_BASE_ACTIVATE,
        C.TC_INITIAL_INFO, C.TC_BASE_ACTIVATE,
    ]
    assert [territory.owner for territory in mode.territories] == [
        TEAM1, TEAM_NEUTRAL, TEAM2
    ]
    zones = _decode(server.packets, MinimapZone)
    assert [zone.icon_id for zone in zones] == [
        CG.ZONE_ICON_TERRITORY_A,
        CG.ZONE_ICON_TERRITORY_B,
        CG.ZONE_ICON_TERRITORY_C,
    ]

    middle = mode.territories[1]
    player = _Player(7, TEAM1, middle.zone.center)
    server.players[player.id] = player
    asyncio.run(mode._capture_tick(10.0))

    assert middle.owner == TEAM1
    assert middle.progress == 0.0
    assert server.teams[TEAM1].score == 2
    assert player.score == int(CG.TC_SCORE_CLAIM)


def test_diamond_is_uncovered_carried_as_native_tool_and_cashed_in(monkeypatch) -> None:
    now = [200.0]
    monkeypatch.setattr("modes.diamond_mine.time.time", lambda: now[0])
    server = _Server({
        "dia": {
            "score_limit": 5,
            "max_active_bases": 1,
            "max_active_diamonds": 2,
            "diamond_lifetime": 60,
        }
    })
    server.world_manager.map_metadata.diamond_base_zones.append(
        _zone(TEAM_NEUTRAL, 150)
    )
    server.world_manager.map_metadata.diamond_base_capacities.append(1)
    player = _Player(8, TEAM1, (120.5, 100.5, 50.5))
    server.players[player.id] = player
    mode = DiamondMineMode(server)
    mode._rng = SimpleNamespace(random=lambda: 0.0, randrange=lambda _n: 0)
    asyncio.run(mode.on_mode_start())

    asyncio.run(mode.on_blocks_destroyed(player, ((120, 100, 50),), True))
    assert len(mode.ground_diamonds) == 1
    assert player.score == int(CG.DIA_INDIVIDUAL_SCORE_FOR_MINED_DIAMOND)

    now[0] = 200.1
    asyncio.run(mode.on_tick(1))
    pickup = _decode(server.packets, PickPickup)[-1]
    assert pickup.pickup_id == int(C.DIAMOND_PICKUP)
    assert pickup.burdensome == 1
    assert player.pickup_id == int(C.DIAMOND_PICKUP)
    assert player.id in mode.carriers

    player.set_position(mode.active_dropoffs[0].zone.center)
    now[0] = 200.2
    asyncio.run(mode.on_tick(2))
    assert player.pickup_id is None
    assert player.id not in mode.carriers
    assert server.teams[TEAM1].score == 1
    assert player.score == (
        int(CG.DIA_INDIVIDUAL_SCORE_FOR_MINED_DIAMOND)
        + int(CG.DIA_INDIVIDUAL_SCORE_FOR_CASHED_IN_DIAMOND)
    )


def test_occupation_blue_drop_in_green_base_uses_fuse_and_scores_three(
    monkeypatch,
) -> None:
    now = [300.0]
    monkeypatch.setattr("modes.occupation.time.time", lambda: now[0])
    server = _Server({
        "oc": {"score_limit": 30, "max_active_bombs": 1, "bomb_fuse_time": 10}
    })
    metadata = server.world_manager.map_metadata
    metadata.occupation_base_zone = _zone(TEAM2, 400)
    metadata.occupation_bomb_points.append((100.0, 100.0, 60.0))
    player = _Player(9, TEAM1, (100.0, 100.0, 60.0))
    server.players[player.id] = player
    mode = OccupationMode(server)
    asyncio.run(mode.on_mode_start())

    asyncio.run(mode.on_tick(1))
    assert player.pickup_id == int(C.BOMB_PICKUP)
    player.set_position(mode.target_zone.center)
    asyncio.run(mode.handle_drop_pickup(player, player.position, (0.0, 0.0, 0.0)))
    bomb = next(iter(mode.bombs.values()))
    assert bomb.armed
    assert bomb.explode_at == 310.0

    now[0] = 310.1
    asyncio.run(mode.on_tick(2))
    assert server.teams[TEAM1].score == int(
        CG.OC_TEAM_SCORE_FOR_BOMB_EXPLOSION_IN_BASE
    )
    assert player.score == (
        int(CG.OC_SCORE_FOR_BOMB_EXPLOSION_IN_BASE)
        + 2 * int(CG.OC_SCORE_OCCUPY_SCORE)
    )
    assert len(server.blasts) == 1
    assert not mode.bombs
    assert len(mode._pending_spawns) == 1


def test_occupation_green_can_intercept_live_bomb_and_dispose_outside_base(
    monkeypatch,
) -> None:
    now = [400.0]
    monkeypatch.setattr("modes.occupation.time.time", lambda: now[0])
    server = _Server({
        "oc": {"score_limit": 30, "max_active_bombs": 1, "bomb_fuse_time": 10}
    })
    metadata = server.world_manager.map_metadata
    metadata.occupation_base_zone = _zone(TEAM2, 400)
    metadata.occupation_bomb_points.append((100.0, 100.0, 60.0))
    blue = _Player(10, TEAM1, (100.0, 100.0, 60.0))
    green = _Player(11, TEAM2, (200.0, 100.0, 60.0))
    server.players = {blue.id: blue, green.id: green}
    mode = OccupationMode(server)
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_tick(1))

    blue.set_position((200.0, 100.0, 60.0))
    asyncio.run(mode.handle_drop_pickup(blue, blue.position, (0.0, 0.0, 0.0)))
    blue.set_position((160.0, 100.0, 60.0))
    now[0] = 403.0
    green.set_position((200.0, 100.0, 60.0))
    asyncio.run(mode.on_tick(2))
    assert green.pickup_id == int(C.BOMB_PICKUP)

    green.set_position((250.0, 100.0, 60.0))
    now[0] = 410.1
    asyncio.run(mode.on_tick(3))
    assert server.teams[TEAM1].score == 0
    assert server.teams[TEAM2].score == 0
    assert green.score == int(CG.OC_SCORE_FOR_DISPOSAL)
    assert len(server.blasts) == 1


def test_recovered_objective_modes_reveal_native_hud_to_late_joiners() -> None:
    tc_server = _Server({"tc": {"max_active_bases": 3}})
    tc_server.world_manager.map_metadata.neutral_base_zones.extend([
        _zone(TEAM_NEUTRAL, 100),
        _zone(TEAM_NEUTRAL, 200),
        _zone(TEAM_NEUTRAL, 300),
    ])
    tc_mode = TerritoryControlMode(tc_server)
    asyncio.run(tc_mode.on_mode_start())
    tc_joiner = _Player(20, TEAM1, (0.0, 0.0, 0.0))
    tc_mode.reveal_to(tc_joiner)
    assert len(_decode(tc_joiner.sent, MinimapZone)) == 3
    assert [state.action for state in _decode(
        tc_joiner.sent, TerritoryBaseState
    )] == [
        C.TC_INITIAL_INFO, C.TC_BASE_ACTIVATE,
        C.TC_INITIAL_INFO, C.TC_BASE_ACTIVATE,
        C.TC_INITIAL_INFO, C.TC_BASE_ACTIVATE,
    ]

    dia_server = _Server({"dia": {"max_active_bases": 1}})
    dia_server.world_manager.map_metadata.diamond_base_zones.append(
        _zone(TEAM_NEUTRAL, 150)
    )
    dia_mode = DiamondMineMode(dia_server)
    asyncio.run(dia_mode.on_mode_start())
    diamond = dia_mode._spawn_diamond((125.5, 100.5, 50.5), now=1.0)
    dia_joiner = _Player(21, TEAM1, (0.0, 0.0, 0.0))
    dia_mode.reveal_to(dia_joiner)
    dia_zones = _decode(dia_joiner.sent, MinimapZone)
    assert len(dia_zones) == 1
    assert dia_zones[0].icon_id == int(CG.ZONE_ICON_DIAMONDMINE)
    dia_entities = _decode(dia_joiner.sent, CreateEntity)
    assert [(packet.entity.entity_id, packet.entity.type) for packet in dia_entities] == [
        (diamond.entity_id, int(C.DIAMOND_PICKUP))
    ]
    assert [packet.string_id for packet in _decode(
        dia_joiner.sent, LocalisedMessage
    )] == ["DIAMOND_START"]

    # The ordinary world reveal may already have sent this static entity.
    # Mode replay must honor that per-GameScene knowledge and not duplicate it.
    dia_mode.reveal_to(dia_joiner)
    assert len(_decode(dia_joiner.sent, CreateEntity)) == 1

    oc_server = _Server()
    oc_server.world_manager.map_metadata.occupation_base_zone = _zone(TEAM2, 400)
    oc_server.world_manager.map_metadata.occupation_bomb_points.append(
        (100.0, 100.0, 60.0)
    )
    oc_mode = OccupationMode(oc_server)
    asyncio.run(oc_mode.on_mode_start())
    oc_joiner = _Player(22, TEAM1, (0.0, 0.0, 0.0))
    oc_mode.reveal_to(oc_joiner)
    oc_zones = _decode(oc_joiner.sent, MinimapZone)
    assert len(oc_zones) == 1
    assert oc_zones[0].icon_id == int(CG.ZONE_ICON_OCCUPATION)
    bomb = next(iter(oc_mode.bombs.values()))
    oc_entities = _decode(oc_joiner.sent, CreateEntity)
    assert [(packet.entity.entity_id, packet.entity.type) for packet in oc_entities] == [
        (bomb.entity_id, int(C.BOMB_PICKUP))
    ]
    assert [packet.string_id for packet in _decode(
        oc_joiner.sent, LocalisedMessage
    )] == ["OCCUPATION_START_ATTACK"]


def test_round_restart_clears_carried_native_diamond(monkeypatch) -> None:
    now = [500.0]
    monkeypatch.setattr("modes.diamond_mine.time.time", lambda: now[0])
    server = _Server({"dia": {"max_active_bases": 1}})
    server.world_manager.map_metadata.diamond_base_zones.append(
        _zone(TEAM_NEUTRAL, 300)
    )
    player = _Player(30, TEAM1, (100.0, 100.0, 60.0))
    server.players[player.id] = player
    mode = DiamondMineMode(server)
    asyncio.run(mode.on_mode_start())
    mode._spawn_diamond(player.position, now=now[0])
    asyncio.run(mode.on_tick(1))
    assert player.pickup_id == int(C.DIAMOND_PICKUP)

    asyncio.run(mode.on_mode_start())

    assert player.pickup_id is None
    assert not mode.carriers
    assert not mode.ground_diamonds


def test_round_restart_clears_carried_bomb_and_respawns_cleanly(
    monkeypatch,
) -> None:
    now = [600.0]
    monkeypatch.setattr("modes.occupation.time.time", lambda: now[0])
    server = _Server({"oc": {"max_active_bombs": 1}})
    server.world_manager.map_metadata.occupation_base_zone = _zone(TEAM2, 400)
    server.world_manager.map_metadata.occupation_bomb_points.append(
        (100.0, 100.0, 60.0)
    )
    player = _Player(31, TEAM1, (100.0, 100.0, 60.0))
    server.players[player.id] = player
    mode = OccupationMode(server)
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_tick(1))
    assert player.pickup_id == int(C.BOMB_PICKUP)

    asyncio.run(mode.on_mode_start())

    assert player.pickup_id is None
    assert not mode.carriers
    assert len(mode.bombs) == 1
    assert next(iter(mode.bombs.values())).entity_id is not None
