"""Same-map restarts must not retain invisible deployable/controller state."""
from types import SimpleNamespace

from server.main import BattleSpadesServer
from server.game_constants import TEAM1, TEAM2


class _Registry:
    def __init__(self):
        self.entities = [SimpleNamespace(entity_id=10), SimpleNamespace(entity_id=11)]
        self.cleared = False

    def all(self):
        return list(self.entities)

    def clear(self):
        self.cleared = True
        self.entities.clear()


def _server(entities_wire_ready=True):
    server = BattleSpadesServer.__new__(BattleSpadesServer)
    server.config = SimpleNamespace(entities_wire_ready=entities_wire_ready)
    player = SimpleNamespace(
        id=1,
        team=TEAM1,
        mounted_entity_id=10,
        _c4_entity_ids=[11],
        _radar_entity_id=12,
        on_fire=True,
        kill_streak=4,
    )
    server.players = {1: player}
    server._radar_station_counts = {TEAM1: 1, TEAM2: 0}
    server.entity_registry = _Registry()
    server.entities = {3: object()}
    server.rocket_turrets = {4: object()}
    server.projectile_engine = SimpleNamespace(projectiles=[object()])
    server.fire_controller = SimpleNamespace(
        clear=lambda: setattr(player, "on_fire", False)
    )
    server.destroyed = []
    server.hidden = []
    server.broadcast_destroy_entity = server.destroyed.append
    server._send_radar_visibility = (
        lambda target, visible: server.hidden.append((target.id, visible))
    )
    return server, player


def test_round_reset_clears_every_transient_owner_and_controller_index():
    server, player = _server()

    server.reset_round_runtime()

    # Same-map restarts deliberately keep GameScene alive, so its entity
    # dictionary must be cleared before the registry ids are reused.
    assert server.destroyed == [10, 11]
    assert server.hidden == [(1, False)]
    assert server._radar_station_counts == {TEAM1: 0, TEAM2: 0}
    assert server.entity_registry.cleared is True
    assert server.entities == {}
    assert server.rocket_turrets == {}
    assert server.projectile_engine.projectiles == []
    assert player.on_fire is False
    assert player.kill_streak == 0
    assert player.mounted_entity_id is None
    assert player._c4_entity_ids == []
    assert player._radar_entity_id is None


def test_round_reset_does_not_destroy_entities_never_put_on_wire():
    server, _player = _server(entities_wire_ready=False)

    server.reset_round_runtime()

    assert server.destroyed == []
    assert server.entity_registry.cleared is True


def test_round_reset_skips_server_only_objective_markers():
    server, _player = _server(entities_wire_ready=True)
    server.entity_registry.entities[0].wire_visible = False

    server.reset_round_runtime()

    assert server.destroyed == [11]
    assert server.entity_registry.cleared is True


def test_round_respawn_applies_pending_class_and_loadout_before_create_player():
    """End-round respawn must use the same selection boundary as death respawn.

    A stale Medic server row paired with a locally selected Miner makes the
    movement authority use the wrong class and maps the equipment slot to the
    health pack instead of dynamite.
    """
    server = BattleSpadesServer.__new__(BattleSpadesServer)
    server.world_manager = SimpleNamespace(
        get_spawn_point=lambda _team: (128.5, 256.5, 223.75)
    )
    server.mode = None
    created = []
    server._broadcast_create_player = (
        lambda target, spawn: created.append(
            (target.class_id, list(target.loadout), spawn)
        )
    )

    player = SimpleNamespace(
        team=TEAM1,
        class_id=17,                 # Medic from the finished round
        loadout=[5, 51],             # block + medpack
        pending_class_id=3,          # Miner selected for the next life
        pending_loadout=[5, 3, 9, 63, 21],
        death_time=123.0,
        spawn=lambda *_position: None,
        restock_ammo=lambda: None,
    )

    server.respawn_player(player)

    assert player.class_id == 3
    assert player.loadout == [5, 3, 9, 63, 21]
    assert player.pending_class_id is None
    assert player.pending_loadout is None
    assert created == [(3, [5, 3, 9, 63, 21], (128.5, 256.5, 223.75))]
