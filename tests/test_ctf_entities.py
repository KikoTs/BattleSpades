import asyncio
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from modes.ctf import CTFMode  # noqa: E402
from server.entities.registry import EntityRegistry  # noqa: E402
from server.game_constants import TEAM1, TEAM2  # noqa: E402
from server.team import Team  # noqa: E402


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

    def broadcast_set_score(self, _team):
        pass


def test_ctf_places_team_tents_and_flags_at_entity_surface_height():
    server = _Server()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())

    entities = server.entity_registry.all()
    assert len(entities) == 4
    assert sorted(e.type for e in entities) == [int(C.FLAG), int(C.FLAG), int(C.BASE), int(C.BASE)]
    assert {e.state for e in entities} == {TEAM1, TEAM2}
    assert all(e.z == 60.0 for e in entities)
    assert len(server.created) == 4


def test_ctf_flag_hides_on_pickup_and_reappears_on_drop():
    server = _Server()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())
    original_id = mode._intel_entities[TEAM2]
    player = SimpleNamespace(name="Blue", team=TEAM1, x=200.0, y=210.0, z=40.0)

    asyncio.run(mode._pickup_intel(player, TEAM2))
    assert mode._intel_entities[TEAM2] is None
    assert server.entity_registry.get(original_id) is None

    asyncio.run(mode._drop_intel(player, TEAM2))
    replacement = mode._intel_entities[TEAM2]
    assert replacement is not None and replacement != original_id
    flag = server.entity_registry.get(replacement)
    assert flag.type == int(C.FLAG) and flag.state == TEAM2


def test_ctf_round_restart_replaces_objectives_without_duplicates():
    server = _Server()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())
    old_ids = {e.entity_id for e in server.entity_registry.all()}
    mode.intel_holder[TEAM1] = object()

    asyncio.run(mode.on_mode_start())

    assert len(server.entity_registry.all()) == 4
    assert old_ids.issubset(set(server.destroyed))
    assert mode.intel_holder == {TEAM1: None, TEAM2: None}
