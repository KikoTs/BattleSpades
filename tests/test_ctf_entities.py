import asyncio
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from modes.ctf import CTFMode  # noqa: E402
from server.entities.registry import EntityRegistry  # noqa: E402
from server.game_constants import TEAM1, TEAM2  # noqa: E402
from server.map_metadata import MapZone  # noqa: E402
from server.team import Team  # noqa: E402
from shared.bytes import ByteReader  # noqa: E402
from shared.packet import ChangePlayer, MinimapZone, PickPickup, SetScore  # noqa: E402


class _World:
    def team_base_anchor(self, team):
        return (64.0, 100.0, 50.0) if team == TEAM1 else (448.0, 400.0, 50.0)

    def dry_ground_anchor(self, x, y):
        return (float(x), float(y), 50.0)

    def dry_surface_anchor(self, x, y):
        return (float(x), float(y), 60.0)


class _Server:
    def __init__(self):
        self.config = SimpleNamespace(entities_wire_ready=True)
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

    def broadcast(self, data, **_kwargs):
        self.packets.append(data)

    def broadcast_create_entity(self, ent):
        self.created.append(ent)

    def broadcast_destroy_entity(self, entity_id):
        self.destroyed.append(entity_id)

    def broadcast_set_score(self, _team, reason=None):
        pass


class _Connection:
    def __init__(self, player=None):
        self.player = player
        self.sent = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(bytes(data))


def _packets(server, packet_type):
    return [
        packet_type(ByteReader(data[1:]))
        for data in server.packets
        if data and data[0] == packet_type.id
    ]


def test_ctf_places_team_tents_and_flags_at_entity_surface_height():
    server = _Server()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())

    entities = server.entity_registry.all()
    assert len(entities) == 4
    assert sorted(e.type for e in entities) == [
        int(C.BASE), int(C.BASE), int(C.INTEL_PICKUP), int(C.INTEL_PICKUP)
    ]
    assert {e.state for e in entities} == {TEAM1, TEAM2}
    assert all(e.z == 60.0 for e in entities)
    assert len(server.created) == 2
    assert all(e.type == int(C.INTEL_PICKUP) for e in server.created)
    assert all(not e.wire_visible for e in entities if e.type == int(C.BASE))
    assert all(e.type != int(C.BASE)
               for e in server.entity_registry.static_entities())
    zones = _packets(server, MinimapZone)
    assert len(zones) == 2
    assert {zone.key for zone in zones} == {TEAM1, TEAM2}
    assert all(zone.icon_id == 6 for zone in zones)
    assert {(zone.A2018, zone.A2019) for zone in zones} == {
        (59, 69), (443, 453)
    }


def test_ctf_flag_hides_on_pickup_and_reappears_when_carrier_dies():
    server = _Server()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())
    original_id = mode._intel_entities[TEAM2]
    player = SimpleNamespace(
        id=7, name="Blue", team=TEAM1, x=200.0, y=210.0, z=40.0,
        vx=0.0, vy=0.0, vz=0.0, pickup_id=None,
        pickup_burdensome=False, pickup_state=None, _world_object=None,
    )

    asyncio.run(mode._pickup_intel(player, TEAM2))
    assert mode._intel_entities[TEAM2] is None
    assert server.entity_registry.get(original_id) is None
    pickup = _packets(server, PickPickup)[-1]
    assert pickup.player_id == player.id
    assert pickup.pickup_id == int(C.INTEL_PICKUP)
    # Retail uses this wire bit to render the carrier's burdened/special state.
    assert pickup.burdensome == 1
    visibility = _packets(server, ChangePlayer)
    assert visibility[-1].player_id == player.id
    assert visibility[-1].type == C.SET_HIGH_MINIMAP_VISIBILITY
    assert visibility[-1].high_minimap_visibility == 1

    created_before_drop = len(server.created)
    asyncio.run(mode.on_player_death(player, killer=None, kill_type=0))
    replacement = mode._intel_entities[TEAM2]
    assert replacement is not None and replacement != original_id
    flag = server.entity_registry.get(replacement)
    assert flag.type == int(C.INTEL_PICKUP) and flag.state == TEAM2
    assert len(server.created) == created_before_drop + 1
    assert server.created[-1].entity_id == replacement
    assert (flag.x, flag.y, flag.z) == (200.0, 210.0, 60.0)
    visibility = _packets(server, ChangePlayer)
    assert visibility[-1].high_minimap_visibility == 0


def test_ctf_late_join_gets_base_zones_and_current_carrier_visibility():
    server = _Server()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())
    carrier = SimpleNamespace(
        id=9, name="Carrier", team=TEAM1, x=200.0, y=210.0, z=40.0,
        vx=0.0, vy=0.0, vz=0.0, pickup_id=None,
        pickup_burdensome=False, pickup_state=None, _world_object=None,
    )
    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    connection = _Connection(carrier)

    mode.reveal_to(connection)

    assert len([data for data in connection.sent if data[0] == MinimapZone.id]) == 2
    visible = [
        ChangePlayer(ByteReader(data[1:])) for data in connection.sent
        if data[0] == ChangePlayer.id
    ]
    assert len(visible) == 1
    assert visible[0].player_id == carrier.id
    assert visible[0].high_minimap_visibility == 1


def test_ctf_dropped_intel_auto_returns_after_retail_timeout(monkeypatch):
    server = _Server()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())
    player = SimpleNamespace(
        id=7, name="Blue", team=TEAM1, alive=True,
        x=200.0, y=210.0, z=40.0, vx=0.0, vy=0.0, vz=0.0,
        pickup_id=None, pickup_burdensome=False, pickup_state=None,
        _world_object=None,
    )
    server.players[player.id] = player
    times = iter((100.0, 100.0, 161.0))
    monkeypatch.setattr("modes.ctf.time.time", lambda: next(times))
    asyncio.run(mode._pickup_intel(player, TEAM2))
    asyncio.run(mode._drop_intel(player, TEAM2))
    dropped_id = mode._intel_entities[TEAM2]

    asyncio.run(mode.on_tick(1))

    assert mode.intel_positions[TEAM2] == mode.intel_home_positions[TEAM2]
    assert mode._intel_entities[TEAM2] != dropped_id
    assert mode.intel_drop_time[TEAM2] == 0.0


def test_ctf_capture_uses_visible_five_block_base_zone():
    server = _Server()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())
    bx, by, bz = mode.base_positions[TEAM1]
    player = SimpleNamespace(x=bx + 4.75, y=by, z=bz)

    assert mode._is_at_base(player, TEAM1)
    player.x = bx + 5.25
    assert not mode._is_at_base(player, TEAM1)


def test_ctf_minimap_and_capture_use_authored_base_zone_bounds():
    server = _Server()
    server.world_manager.map_metadata = SimpleNamespace(base_zones={
        TEAM1: [MapZone(
            "base", TEAM1, 100.0, 110.0, 220.0,
            (-5, 5, -6, 6, -8, 2), "ugc_base_blue",
        )],
        TEAM2: [],
    })
    server.world_manager.map = SimpleNamespace(source_z_shift=7)
    mode = CTFMode(server)

    asyncio.run(mode.on_mode_start())

    blue_zone = next(zone for zone in _packets(server, MinimapZone)
                     if zone.key == TEAM1)
    assert (
        blue_zone.A2018, blue_zone.A2019,
        blue_zone.A2020, blue_zone.A2021,
        blue_zone.A2022, blue_zone.A2023,
    ) == (95, 105, 104, 116, 219, 229)
    assert mode._is_at_base(
        SimpleNamespace(x=95.0, y=104.0, z=mode.base_positions[TEAM1][2]),
        TEAM1,
    )


def test_ctf_round_restart_replaces_objectives_without_duplicates():
    server = _Server()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())
    old_wire_ids = {
        e.entity_id for e in server.entity_registry.all() if e.wire_visible
    }
    mode.intel_holder[TEAM1] = object()

    asyncio.run(mode.on_mode_start())

    assert len(server.entity_registry.all()) == 4
    assert old_wire_ids.issubset(set(server.destroyed))
    assert mode.intel_holder == {TEAM1: None, TEAM2: None}


def test_ctf_objective_rebuild_preserves_shared_map_resources():
    server = _Server()
    resource = server.entity_registry.place(
        int(C.AMMO_CRATE), 10.0, 20.0, 30.0, kind="map_ammo",
    )
    mode = CTFMode(server)
    # Simulate an allocator-reset collision with an id remembered by the old
    # round. The live kind, not this stale id, must decide what gets removed.
    mode._intel_entities[TEAM1] = resource.entity_id

    asyncio.run(mode.on_mode_start())

    assert server.entity_registry.get(resource.entity_id) is resource
    assert resource.entity_id not in server.destroyed
    assert len(server.entity_registry.all()) == 5


def test_ctf_capture_awards_retail_personal_and_team_scores():
    server = _Server()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())
    player = SimpleNamespace(
        id=7, name="Blue", team=TEAM1, alive=True, spawned=True,
        x=200.0, y=210.0, z=40.0, vx=0.0, vy=0.0, vz=0.0,
        pickup_id=None, pickup_burdensome=False, pickup_state=None,
        _world_object=None, captures=0, score=0,
    )

    asyncio.run(mode._pickup_intel(player, TEAM2))
    asyncio.run(mode._capture_intel(player, TEAM2))

    assert player.captures == 1
    assert player.score == 10
    assert server.teams[TEAM1].score == 1
    personal = _packets(server, SetScore)[-1]
    assert personal.type == int(C.SCORE.PLAYER)
    assert personal.specifier == player.id
    assert personal.reason == int(C.SCORE_REASON.CTF_CAPTURE_SCORE_REASON)
    assert personal.value == 10


def test_ctf_player_touch_return_awards_retail_claim_point():
    server = _Server()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())
    player = SimpleNamespace(id=8, score=4)

    asyncio.run(mode._return_intel(TEAM1, returned_by=player))

    assert player.score == 5
    personal = _packets(server, SetScore)[-1]
    assert personal.reason == int(C.SCORE_REASON.CTF_CLAIM_SCORE_REASON)
    assert personal.value == 5


def test_ctf_and_classic_award_one_personal_point_for_enemy_kill():
    server = _Server()
    mode = CTFMode(server)
    killer = SimpleNamespace(id=3, team=TEAM1, score=6)
    victim = SimpleNamespace(id=4, team=TEAM2)

    asyncio.run(mode.on_player_kill(killer, victim, kill_type=0))

    assert killer.score == 7
    personal = _packets(server, SetScore)[-1]
    assert personal.type == int(C.SCORE.PLAYER)
    assert personal.specifier == killer.id
    assert personal.reason == int(C.SCORE_REASON.KILL_SCORE_REASON)
    assert personal.value == 7


def test_ctf_does_not_score_teamkill_suicide_or_post_round_kill():
    server = _Server()
    mode = CTFMode(server)
    killer = SimpleNamespace(id=3, team=TEAM1, score=6)
    teammate = SimpleNamespace(id=4, team=TEAM1)

    asyncio.run(mode.on_player_kill(killer, teammate, kill_type=0))
    asyncio.run(mode.on_player_kill(killer, killer, kill_type=0))
    mode.ended = True
    enemy = SimpleNamespace(id=5, team=TEAM2)
    asyncio.run(mode.on_player_kill(killer, enemy, kill_type=0))

    assert killer.score == 6
    assert not _packets(server, SetScore)
