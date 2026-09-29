"""Team-grief accounting, AFK kicks, and the [conduct] config table."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import shared.constants as C
from server import conduct
from server.config import ConductConfig, ServerConfig, load_config
from server.entities.behaviors import ProximityMineBehavior
from server.game_constants import TEAM1, TEAM2, TEAM_SPECTATOR
from server.main import BattleSpadesServer
from server.player import Player
from shared.bytes import ByteReader
from shared.packet import KillAction


class _Connection:
    def __init__(self, server) -> None:
        self.server = server
        self.player = None
        self.in_game = True
        self.sent: list[bytes] = []
        self.disconnected: list[int] = []

    def send(self, data, reliable=True, prefix=0x30) -> None:
        self.sent.append(bytes(data))

    def disconnect(self, reason: int = 0) -> None:
        self.disconnected.append(int(reason))


def _server(**conduct_overrides) -> BattleSpadesServer:
    config = ServerConfig(default_mode="tdm")
    for name, value in conduct_overrides.items():
        setattr(config.conduct, name, value)
    server = BattleSpadesServer(config)
    server.mode = None
    server.world_manager.generate_flat_map()
    server.broadcasts = []
    original = server.broadcast

    def broadcast(data, *args, **kwargs):
        server.broadcasts.append(bytes(data))
        return original(data, *args, **kwargs)

    server.broadcast = broadcast
    return server


def _player(server, player_id, name, team, position=(100.5, 100.5, 60.0)):
    connection = _Connection(server)
    player = Player(player_id, name, team, int(C.RIFLE_TOOL), connection)
    connection.player = player
    player.spawn(*position)
    player.end_spawn_protection()
    server.players[player_id] = player
    server.teams[team].add_player(player)
    return player


def _kill_actions(server) -> list[KillAction]:
    return [
        KillAction(ByteReader(data[1:]))
        for data in server.broadcasts
        if data[0] == KillAction.id
    ]


def _place_mine(server, owner, x=100, y=100, z=60):
    behavior = ProximityMineBehavior(
        thrower_id=owner.id,
        team=owner.team,
        damage=300.0,
        block_damage=0.0,
        crater_radius=1,
        kill_type=int(C.KILL.LANDMINE_KILL) if hasattr(C.KILL, "LANDMINE_KILL") else 14,
        blast_radius=6.0,
    )
    return server.entity_registry.place(
        int(C.LANDMINE_ENTITY), float(x), float(y), float(z),
        kind="landmine", player_id=owner.id, behavior=behavior,
    )


# --- indirect grief: shooting a teammate's mine ---------------------------

def test_teammate_shooting_owners_mine_is_charged_to_the_shooter():
    server = _server()
    assert server.config.friendly_fire is False
    owner = _player(server, 0, "Owner", TEAM1, (100.5, 100.5, 58.0))
    griefer = _player(server, 1, "Griefer", TEAM1, (120.5, 100.5, 58.0))
    mine = _place_mine(server, owner)

    server.entity_registry.damage_entity(
        mine.entity_id, 1.0, griefer, server._build_entity_ctx()
    )

    assert not owner.alive
    kills = _kill_actions(server)
    assert kills and kills[-1].player_id == owner.id
    # Kill feed names the teammate who set it off, not an owner suicide.
    assert kills[-1].killer_id == griefer.id
    state = conduct.grief_state(griefer)
    assert state.team_kills == 1
    assert state.points > 3.0
    # The owner is not charged for their own death.
    assert conduct.grief_state(owner).points == 0.0
    # Friendly fire off: the (distant) griefer is not hurt by the blast.
    assert griefer.alive


def test_enemy_shooting_a_mine_is_not_grief_and_keeps_owner_credit():
    server = _server()
    owner = _player(server, 0, "Owner", TEAM1, (100.5, 100.5, 58.0))
    enemy = _player(server, 1, "Enemy", TEAM2, (120.5, 100.5, 58.0))
    mine = _place_mine(server, owner)

    server.entity_registry.damage_entity(
        mine.entity_id, 1.0, enemy, server._build_entity_ctx()
    )

    assert not owner.alive
    assert _kill_actions(server)[-1].killer_id == owner.id
    assert conduct.grief_state(enemy).points == 0.0
    assert conduct.grief_state(owner).points == 0.0


def test_owner_triggering_own_mine_is_a_plain_suicide():
    server = _server()
    owner = _player(server, 0, "Owner", TEAM1, (100.5, 100.5, 58.0))
    mine = _place_mine(server, owner)

    server.entity_registry.damage_entity(
        mine.entity_id, 1.0, owner, server._build_entity_ctx()
    )

    assert not owner.alive
    assert _kill_actions(server)[-1].killer_id == owner.id
    assert conduct.grief_state(owner).points == 0.0


def test_chained_mines_keep_the_first_instigator():
    server = _server()
    owner = _player(server, 0, "Owner", TEAM1, (109.5, 100.5, 58.0))
    griefer = _player(server, 1, "Griefer", TEAM1, (140.5, 100.5, 58.0))
    first = _place_mine(server, owner, x=100)  # out of the owner's reach
    second = _place_mine(server, owner, x=105)  # in reach of both
    # The stock landmine (100 dmg, r6, ExplosionDamageManager falloff) is not
    # a one-shot at 4.5 blocks; this test is about attribution, not damage.
    owner.health = 10

    server.entity_registry.damage_entity(
        first.entity_id, 1.0, griefer, server._build_entity_ctx()
    )

    assert server.entity_registry.get(second.entity_id) is None
    assert not owner.alive
    assert _kill_actions(server)[-1].killer_id == griefer.id
    assert conduct.grief_state(griefer).team_kills == 1


def test_instigator_scope_ends_after_the_blast():
    server = _server()
    marker = object()
    with conduct.blast_instigator(server, marker):
        assert conduct.current_blast_instigator(server) is marker
        with conduct.blast_instigator(server, object()):
            assert conduct.current_blast_instigator(server) is marker
    assert conduct.current_blast_instigator(server) is None


# --- direct team damage / kills ---------------------------------------------

def test_direct_team_kills_warn_then_kick_for_griefing():
    server = _server()
    server.config.friendly_fire = True
    griefer = _player(server, 0, "Griefer", TEAM1)
    now = 1000.0
    kicked_at = None
    for index in range(1, 6):
        victim = _player(server, index, f"Mate{index}", TEAM1, (100.5 + index, 100.5, 60.0))
        conduct.record_team_harm(server, victim, griefer, 100, True, now=now)
        now += 10.0
        conduct.tick(server, 1.0, now=now)
        if griefer.connection.disconnected:
            kicked_at = index
            break

    assert kicked_at == 3
    assert griefer.connection.disconnected == [int(C.DISCONNECT.ERROR_KICK_GRIEFING)]
    # A private warning preceded the kick.
    assert any(b"griefing" in data for data in griefer.connection.sent)


def test_single_accidental_burst_never_kicks():
    server = _server()
    griefer = _player(server, 0, "Nader", TEAM1)
    for index in range(1, 6):  # one grenade kills five teammates at once
        victim = _player(server, index, f"Mate{index}", TEAM1)
        conduct.record_team_harm(server, victim, griefer, 100, True, now=500.0)
    conduct.tick(server, 1.0, now=500.5)

    assert conduct.grief_state(griefer).points <= server.config.conduct.grief_incident_max_points
    assert griefer.connection.disconnected == []


def test_grief_points_decay_so_occasional_accidents_never_accumulate():
    server = _server()
    player = _player(server, 0, "Clumsy", TEAM1)
    victim = _player(server, 1, "Mate", TEAM1)
    now = 0.0
    for _ in range(20):  # one team kill every ten minutes
        conduct.record_team_harm(server, victim, player, 100, True, now=now)
        now += 600.0
        conduct.tick(server, 1.0, now=now)
    assert player.connection.disconnected == []


def test_real_player_damage_path_counts_team_damage_with_friendly_fire():
    server = _server()
    server.config.friendly_fire = True
    shooter = _player(server, 0, "Shooter", TEAM1)
    victim = _player(server, 1, "Mate", TEAM1, (103.5, 100.5, 60.0))

    victim.damage(40, source=shooter, kill_type=int(C.WEAPON_KILL))

    state = conduct.grief_state(shooter)
    assert state.team_damage == 40
    assert 0.3 < state.points < 0.5


def test_enemy_damage_bots_and_admins_are_not_charged():
    server = _server()
    shooter = _player(server, 0, "Shooter", TEAM1)
    enemy = _player(server, 1, "Enemy", TEAM2)
    assert conduct.record_team_harm(server, enemy, shooter, 100, True) == 0.0

    mate = _player(server, 2, "Mate", TEAM1)
    shooter.admin = True
    assert conduct.record_team_harm(server, mate, shooter, 100, True) == 0.0
    shooter.admin = False
    shooter.is_bot = True
    assert conduct.record_team_harm(server, mate, shooter, 100, True) == 0.0


def test_grief_kick_can_be_disabled():
    server = _server(grief_kick_enabled=False)
    griefer = _player(server, 0, "Griefer", TEAM1)
    victim = _player(server, 1, "Mate", TEAM1)
    for step in range(10):
        conduct.record_team_harm(server, victim, griefer, 100, True, now=step * 5.0)
    conduct.tick(server, 1.0, now=60.0)
    assert griefer.connection.disconnected == []


# --- AFK ----------------------------------------------------------------------

_IDLE_FLAGS = (False,) * 8
_IDLE_ACTIONS = (False,) * 9


def _idle_frame(player, orientation=(1.0, 0.0, 0.0)):
    return conduct.observe_input(player, _IDLE_FLAGS, orientation, _IDLE_ACTIONS)


def test_identical_client_data_is_not_activity():
    server = _server()
    player = _player(server, 0, "Idle", TEAM1)
    assert _idle_frame(player) is True  # first frame establishes the baseline
    conduct.afk_state(player).idle_seconds = 100.0
    for _ in range(50):
        assert _idle_frame(player) is False
    assert conduct.afk_state(player).idle_seconds == 100.0
    # Passive flags (can_pickup, on fire) flip without user input.
    passive = list(_IDLE_ACTIONS)
    passive[3] = True
    passive[5] = True
    assert conduct.observe_input(player, _IDLE_FLAGS, (1.0, 0.0, 0.0), passive) is False
    # A key press, a trigger pull, or looking around resets the clock.
    assert conduct.observe_input(player, (True,) + _IDLE_FLAGS[1:], (1, 0, 0), passive)
    assert conduct.afk_state(player).idle_seconds == 0.0
    assert _idle_frame(player, (0.9, 0.3, 0.0)) is True


def test_afk_warns_at_nine_minutes_and_kicks_at_ten():
    server = _server()
    player = _player(server, 0, "Sleeper", TEAM1)
    _idle_frame(player)
    for _ in range(539):
        conduct.tick(server, 1.0)
    assert player.connection.sent == [] or not any(
        b"AFK" in data for data in player.connection.sent
    )
    conduct.tick(server, 1.0)
    assert any(b"AFK" in data for data in player.connection.sent)
    for _ in range(59):
        conduct.tick(server, 1.0)
    assert player.connection.disconnected == []
    conduct.tick(server, 1.0)
    assert player.connection.disconnected == [int(C.DISCONNECT.ERROR_AFK_TIMEOUT)]


def test_real_input_frames_reset_the_afk_clock():
    server = _server()
    player = _player(server, 0, "Mover", TEAM1)
    player.record_input_frame(1, _IDLE_FLAGS, (1.0, 0.0, 0.0), action_flags=_IDLE_ACTIONS)
    conduct.afk_state(player).idle_seconds = 500.0
    player.record_input_frame(2, _IDLE_FLAGS, (1.0, 0.0, 0.0), action_flags=_IDLE_ACTIONS)
    assert conduct.afk_state(player).idle_seconds == 500.0
    player.record_input_frame(3, _IDLE_FLAGS, (0.0, 1.0, 0.0), action_flags=_IDLE_ACTIONS)
    assert conduct.afk_state(player).idle_seconds == 0.0


def test_afk_clock_pauses_while_dead_loading_and_between_rounds():
    server = _server()
    player = _player(server, 0, "Waiter", TEAM1)
    player.alive = False
    for _ in range(700):
        conduct.tick(server, 1.0)
    player.alive = True
    player.connection.in_game = False
    for _ in range(700):
        conduct.tick(server, 1.0)
    player.connection.in_game = True
    server.mode = SimpleNamespace(started=True, ended=True)
    for _ in range(700):
        conduct.tick(server, 1.0)
    assert conduct.afk_state(player).idle_seconds == 0.0
    assert player.connection.disconnected == []


def test_spectators_admins_and_bots_have_their_own_afk_rules():
    server = _server()
    spectator = _player(server, 0, "Watcher", TEAM1)
    spectator.team = TEAM_SPECTATOR
    admin = _player(server, 1, "Boss", TEAM1)
    admin.admin = True
    bot = _player(server, 2, "Bot", TEAM2)
    bot.is_bot = True
    for _ in range(1000):
        conduct.tick(server, 1.0)
    assert spectator.connection.disconnected == []
    assert admin.connection.disconnected == []
    assert bot.connection.disconnected == []
    for _ in range(800):
        conduct.tick(server, 1.0)
    assert spectator.connection.disconnected == [int(C.DISCONNECT.ERROR_AFK_TIMEOUT)]


def test_afk_kick_disabled_with_zero():
    server = _server(afk_kick_seconds=0)
    player = _player(server, 0, "Idle", TEAM1)
    for _ in range(5000):
        conduct.tick(server, 1.0)
    assert player.connection.disconnected == []


# --- config -------------------------------------------------------------------

def test_conduct_config_table_loads(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        "[conduct]\n"
        "grief_kick_points = 12.5\n"
        "grief_kick_enabled = false\n"
        "afk_kick_seconds = 300\n"
        'reserved_names = ["Kiril", " "]\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.conduct.grief_kick_points == 12.5
    assert config.conduct.grief_kick_enabled is False
    assert config.conduct.afk_kick_seconds == 300.0
    assert config.conduct.reserved_names == ["Kiril"]
    assert config.conduct.afk_warn_seconds == ConductConfig().afk_warn_seconds


def test_shipped_config_toml_conduct_defaults_match_dataclass():
    config = load_config(Path(__file__).resolve().parents[1] / "config.toml")
    defaults = ConductConfig()
    for name, value in vars(defaults).items():
        assert getattr(config.conduct, name) == value, name
