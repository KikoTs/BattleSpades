"""Classic CTF corpse lifecycle and packet-ordering regressions."""

from __future__ import annotations

import asyncio

import pytest
import shared.constants as C
from modes.classic_ctf import ClassicCTFMode
from server.combat_runtime import get_combat_system
from server.config import ServerConfig
from server.connection import Connection
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.player import Player
from server.roster import catch_up_roster
from shared.bytes import ByteReader
from shared.packet import CreatePlayer, ExplodeCorpse, KillAction, SetColor


class _PlayerConnection:
    def __init__(self, server: BattleSpadesServer) -> None:
        self.server = server
        self.player = None
        self.sent: list[bytes] = []

    def send(self, data, reliable=True, prefix=0x30) -> None:
        self.sent.append(bytes(data))


class _JoiningConnection(_PlayerConnection):
    def __init__(self, server: BattleSpadesServer) -> None:
        super().__init__(server)
        self.known_player_lives: dict[int, tuple[int, int]] = {}
        self.known_player_deaths: dict[int, tuple[int, int]] = {}


def _classic_server() -> BattleSpadesServer:
    config = ServerConfig(default_mode="cctf")
    server = BattleSpadesServer(config)
    server.mode = ClassicCTFMode(server)
    server.world_manager.generate_flat_map()
    return server


def _add_player(
    server: BattleSpadesServer,
    player_id: int,
    name: str,
    team: int,
    position: tuple[float, float, float],
) -> Player:
    connection = _PlayerConnection(server)
    player = Player(player_id, name, team, int(C.RIFLE_TOOL), connection)
    connection.player = player
    player.spawn(*position)
    server.players[player_id] = player
    server.teams[team].add_player(player)
    return player


def _packet(data: bytes, packet_type):
    assert data[0] == packet_type.id
    return packet_type(ByteReader(data[1:]))


def test_classic_death_creates_client_character_corpse_without_grave(
    monkeypatch,
) -> None:
    server = _classic_server()
    victim = _add_player(server, 1, "Deuce", TEAM1, (100.5, 100.5, 59.75))
    broadcasts: list[bytes] = []
    monkeypatch.setattr(
        server,
        "broadcast",
        lambda data, *args, **kwargs: broadcasts.append(bytes(data)),
    )

    victim.die(killer=None, kill_type=int(C.KILL.FALL_KILL))

    state = server.corpse_lifecycle.get(victim)
    assert state is not None
    assert state.generation == victim.replication_generation
    assert state.position == victim.position
    assert state.exploded is False
    assert victim._grave_entity_id is None
    assert all(
        int(entity.type) != int(C.GRAVE_ENTITY)
        for entity in server.entity_registry.all()
    )
    assert [data[0] for data in broadcasts] == [KillAction.id]


def test_shooting_classic_corpse_emits_packet_36_once_and_uses_constants(
    monkeypatch,
) -> None:
    server = _classic_server()
    attacker = _add_player(server, 1, "Shooter", TEAM1, (90.5, 100.5, 59.75))
    victim = _add_player(server, 2, "Corpse", TEAM2, (100.5, 100.5, 59.75))
    broadcasts: list[bytes] = []
    blasts: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        server,
        "broadcast",
        lambda data, *args, **kwargs: broadcasts.append(bytes(data)),
    )
    monkeypatch.setattr(
        server,
        "_apply_blast",
        lambda *args, **kwargs: blasts.append((args, kwargs)),
    )
    # Isolate player/corpse/entity ordering from terrain in this unit fixture.
    monkeypatch.setattr(server.world_manager, "raycast", lambda *args: None)
    victim.die(killer=attacker, kill_type=int(C.KILL.WEAPON_KILL))
    broadcasts.clear()

    combat = get_combat_system(server)
    traced = combat._trace_authoritative_hit(
        attacker,
        attacker.eye,
        (1.0, 0.0, 0.0),
        100.0,
    )
    assert traced is not None
    assert traced[0] == "corpse"
    assert traced[1] is server.corpse_lifecycle.get(victim)

    assert combat._resolve_hitscan(
        attacker,
        (1.0, 0.0, 0.0),
        origin=attacker.eye,
    )
    assert len(broadcasts) == 1
    wire = _packet(broadcasts[0], ExplodeCorpse)
    assert len(broadcasts[0]) == 3
    assert wire.player_id == victim.id
    assert wire.show_explosion_effect == 1

    assert len(blasts) == 1
    args, kwargs = blasts[0]
    assert args[:3] == victim.position
    assert args[3:7] == (
        float(C.CORPSE_EXPLOSION_DAMAGE),
        float(C.CORPSE_EXPLOSION_BLOCK_DAMAGE),
        int(C.KILL.CORPSE_KILL),
        attacker,
    )
    assert kwargs == {
        "crater_radius": 1,
        "force_destroy": False,
        "blast_radius": float(C.CORPSE_EXPLOSION_RADIUS),
        "knockback_min": float(C.CORPSE_EXPLOSION_KNOCKBACK_MIN),
        "knockback_max": float(C.CORPSE_EXPLOSION_KNOCKBACK_MAX),
    }

    # A corpse is a one-shot target, even if two ShootPackets arrive together.
    assert not server.corpse_lifecycle.explode(
        traced[1], attacker, show_explosion_effect=True
    )
    assert len(broadcasts) == 1
    assert len(blasts) == 1


def test_respawn_silently_removes_corpse_before_generation_changes(
    monkeypatch,
) -> None:
    server = _classic_server()
    victim = _add_player(server, 1, "Respawn", TEAM1, (100.5, 100.5, 59.75))
    observed: list[tuple[bytes, int]] = []

    def capture(data, *args, **kwargs) -> None:
        observed.append((bytes(data), victim.replication_generation))

    monkeypatch.setattr(server, "broadcast", capture)
    victim.die(killer=None, kill_type=int(C.KILL.FALL_KILL))
    old_generation = victim.replication_generation
    observed.clear()

    victim.spawn(101.5, 100.5, 59.75)

    assert victim.replication_generation == old_generation + 1
    assert server.corpse_lifecycle.get(victim) is None
    assert len(observed) == 1
    wire = _packet(observed[0][0], ExplodeCorpse)
    assert wire.player_id == victim.id
    assert wire.show_explosion_effect == 0
    assert observed[0][1] == old_generation


def test_failed_pre_spawn_cleanup_retains_corpse_and_generation(
    monkeypatch,
) -> None:
    server = _classic_server()
    victim = _add_player(server, 1, "SpawnRetry", TEAM1, (100.5, 100.5, 59.75))
    monkeypatch.setattr(
        server,
        "broadcast",
        lambda data, *args, **kwargs: None,
    )
    victim.die(killer=None, kill_type=int(C.KILL.FALL_KILL))
    old_generation = victim.replication_generation

    def reject_cleanup(data, *args, **kwargs) -> None:
        if data[0] == ExplodeCorpse.id:
            raise RuntimeError("synthetic cleanup rejection")

    monkeypatch.setattr(server, "broadcast", reject_cleanup)
    with pytest.raises(RuntimeError, match="cleanup rejection"):
        victim.spawn(101.5, 100.5, 59.75)

    assert victim.replication_generation == old_generation
    assert server.corpse_lifecycle.get(victim) is not None

    monkeypatch.setattr(
        server,
        "broadcast",
        lambda data, *args, **kwargs: None,
    )
    victim.spawn(101.5, 100.5, 59.75)
    assert victim.replication_generation == old_generation + 1
    assert server.corpse_lifecycle.get(victim) is None


def test_failed_explosion_broadcast_leaves_corpse_retryable(monkeypatch) -> None:
    server = _classic_server()
    victim = _add_player(server, 1, "BlastRetry", TEAM1, (100.5, 100.5, 59.75))
    monkeypatch.setattr(
        server,
        "broadcast",
        lambda data, *args, **kwargs: None,
    )
    monkeypatch.setattr(server, "_apply_blast", lambda *args, **kwargs: None)
    victim.die(killer=None, kill_type=int(C.KILL.FALL_KILL))
    state = server.corpse_lifecycle.get(victim)
    assert state is not None

    def reject_explosion(data, *args, **kwargs) -> None:
        raise RuntimeError("synthetic explosion rejection")

    monkeypatch.setattr(server, "broadcast", reject_explosion)
    with pytest.raises(RuntimeError, match="explosion rejection"):
        server.corpse_lifecycle.explode(
            state, None, show_explosion_effect=True
        )
    assert state.exploded is False

    packets: list[bytes] = []
    monkeypatch.setattr(
        server,
        "broadcast",
        lambda data, *args, **kwargs: packets.append(bytes(data)),
    )
    assert server.corpse_lifecycle.explode(
        state, None, show_explosion_effect=True
    )
    assert state.exploded is True
    assert [data[0] for data in packets] == [ExplodeCorpse.id]


def test_joining_client_gets_one_death_then_only_silent_race_cleanup(
    monkeypatch,
) -> None:
    server = _classic_server()
    victim = _add_player(server, 1, "LateCorpse", TEAM1, (100.5, 100.5, 59.75))
    broadcasts: list[bytes] = []
    monkeypatch.setattr(
        server,
        "broadcast",
        lambda data, *args, **kwargs: broadcasts.append(bytes(data)),
    )
    monkeypatch.setattr(server, "_apply_blast", lambda *args, **kwargs: None)
    victim.die(killer=None, kill_type=int(C.KILL.FALL_KILL))

    joiner = _JoiningConnection(server)
    asyncio.run(Connection.send_existing_players(joiner))
    assert [data[0] for data in joiner.sent] == [
        CreatePlayer.id,
        SetColor.id,
        KillAction.id,
    ]

    # Opening gameplay without a state change must not replay KillAction.
    joiner.sent.clear()
    catch_up_roster(server, joiner)
    assert joiner.sent == []

    # The joiner was gated and missed the live explosion broadcast. Its first
    # reveal repairs only corpse visibility, without replaying the death/effect.
    state = server.corpse_lifecycle.get(victim)
    assert state is not None
    assert server.corpse_lifecycle.explode(
        state, None, show_explosion_effect=True
    )
    joiner.sent.clear()
    catch_up_roster(server, joiner)

    assert len(joiner.sent) == 1
    wire = _packet(joiner.sent[0], ExplodeCorpse)
    assert wire.player_id == victim.id
    assert wire.show_explosion_effect == 0

    joiner.sent.clear()
    catch_up_roster(server, joiner)
    assert joiner.sent == []

    # A client beginning its roster after the explosion never creates a stale
    # dead Character at all.
    later_joiner = _JoiningConnection(server)
    asyncio.run(Connection.send_existing_players(later_joiner))
    assert later_joiner.sent == []


def test_failed_silent_cleanup_retries_without_duplicate_killaction(
    monkeypatch,
) -> None:
    server = _classic_server()
    victim = _add_player(server, 1, "RetryCorpse", TEAM1, (100.5, 100.5, 59.75))
    monkeypatch.setattr(
        server,
        "broadcast",
        lambda data, *args, **kwargs: None,
    )
    monkeypatch.setattr(server, "_apply_blast", lambda *args, **kwargs: None)
    victim.die(killer=None, kill_type=int(C.KILL.FALL_KILL))

    joiner = _JoiningConnection(server)
    asyncio.run(Connection.send_existing_players(joiner))
    state = server.corpse_lifecycle.get(victim)
    assert state is not None
    assert server.corpse_lifecycle.explode(
        state, None, show_explosion_effect=True
    )
    joiner.sent.clear()

    original_send = joiner.send
    failed = False

    def fail_first_cleanup(data, reliable=True, prefix=0x30) -> None:
        nonlocal failed
        if data[0] == ExplodeCorpse.id and not failed:
            failed = True
            raise RuntimeError("synthetic ENet queue rejection")
        original_send(data, reliable=reliable, prefix=prefix)

    joiner.send = fail_first_cleanup
    with pytest.raises(RuntimeError, match="synthetic ENet"):
        catch_up_roster(server, joiner)

    joiner.send = original_send
    catch_up_roster(server, joiner)
    assert [data[0] for data in joiner.sent] == [ExplodeCorpse.id]
