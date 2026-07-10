import shared.constants as C
from server.config import ServerConfig
from server.game_constants import TEAM1
from server.main import BattleSpadesServer
from server.player import Player


class _Connection:
    def __init__(self, server):
        self.server = server
        self.sent = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(data)


def test_death_places_grounded_team_coloured_exploding_grave():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    connection = _Connection(server)
    player = Player(0, "GraveTest", TEAM1, C.RIFLE_TOOL, connection)
    player.spawn(100.5, 100.5, 59.75)
    server.players[player.id] = player
    server.teams[TEAM1].add_player(player)

    player.die(killer=None, kill_type=int(C.KILL.FALL_KILL))

    grave = server.entity_registry.get(player._grave_entity_id)
    assert grave is not None
    assert grave.type == C.GRAVE_ENTITY
    assert grave.state == TEAM1
    assert grave.color == tuple(server.teams[TEAM1].color)
    expected = server.world_manager.dry_surface_anchor(100.5, 100.5, search=0)
    assert (grave.x, grave.y, grave.z) == expected
    assert grave.behavior.fuse == C.GRAVE_EXPLOSION_FUSE
    assert grave.behavior.damage == C.GRAVE_EXPLOSION_DAMAGE
    assert grave.behavior.blast_radius == C.GRAVE_EXPLOSION_RADIUS
