"""Diamond Mine regressions: carrier cash-in cue, discovery cooldown,
post-end drops, and restart entity-id hygiene."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants as C
import shared.constants_gamemode as CG

from modes.diamond_mine import DiamondMineMode
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL
from shared.packet import LocalisedMessage
from tests.test_recovered_objective_modes import _decode, _Player, _Server, _zone


def _mode(monkeypatch, now, settings=None):
    monkeypatch.setattr("modes.diamond_mine.time.time", lambda: now[0])
    server = _Server({"dia": settings or {
        "score_limit": 5, "max_active_bases": 1, "max_active_diamonds": 2,
    }})
    server.world_manager.map_metadata.diamond_base_zones.append(
        _zone(TEAM_NEUTRAL, 150)
    )
    server.world_manager.map_metadata.diamond_base_capacities.append(3)
    mode = DiamondMineMode(server)
    mode._rng = SimpleNamespace(random=lambda: 0.0, randrange=lambda _n: 0)
    return server, mode


def _ids(player):
    return [(m.string_id, list(m.parameters)) for m in _decode(player.sent, LocalisedMessage)]


def test_carrier_gets_yourself_cue_and_team_gets_named_cue(monkeypatch) -> None:
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    carrier = _Player(1, TEAM1, (120.5, 100.5, 50.5))
    mate = _Player(2, TEAM1, (0.0, 0.0, 50.0))
    enemy = _Player(3, TEAM2, (0.0, 20.0, 50.0))
    for player in (carrier, mate, enemy):
        player.connection = player
        server.players[player.id] = player
    asyncio.run(mode.on_mode_start())
    mode._spawn_diamond(carrier.position, now=now[0])
    asyncio.run(mode.on_tick(1))
    assert carrier.id in mode.carriers

    for player in (carrier, mate, enemy):
        player.sent.clear()
    carrier.set_position(mode.active_dropoffs[0].zone.center)
    asyncio.run(mode.on_tick(2))

    assert server.teams[TEAM1].score == 1
    assert _ids(carrier) == [("DIAMOND_CASHED_IN_YOURSELF", [])]
    assert _ids(mate) == [("DIAMOND_CASHED_IN_YOURTEAM", [carrier.name])]
    assert _ids(enemy) == [("DIAMOND_CASHED_IN_OPPOSITION", [carrier.name])]


def test_dropping_a_diamond_does_not_delay_next_discovery(monkeypatch) -> None:
    now = [200.0]
    server, mode = _mode(monkeypatch, now)
    miner = _Player(1, TEAM1, (120.5, 100.5, 50.5))
    server.players[miner.id] = miner
    asyncio.run(mode.on_mode_start())

    asyncio.run(mode.on_blocks_destroyed(miner, ((120, 100, 50),), True))
    assert len(mode.ground_diamonds) == 1
    assert mode._next_discovery_at == 200.0 + float(CG.DIA_TIME_BETWEEN_DIAMOND_SPAWN)

    now[0] = 200.1
    asyncio.run(mode.on_tick(1))
    assert miner.id in mode.carriers
    now[0] = 214.0
    asyncio.run(mode.on_player_death(miner, None, int(C.KILL.FALL_KILL)))
    assert len(mode.ground_diamonds) == 1
    # Still the discovery cooldown, not "drop time + 15s".
    assert mode._next_discovery_at == 200.0 + float(CG.DIA_TIME_BETWEEN_DIAMOND_SPAWN)


def test_drop_after_match_end_clears_tool_without_new_entity(monkeypatch) -> None:
    now = [300.0]
    server, mode = _mode(monkeypatch, now)
    player = _Player(1, TEAM1, (120.5, 100.5, 50.5))
    server.players[player.id] = player
    asyncio.run(mode.on_mode_start())
    mode._spawn_diamond(player.position, now=now[0])
    asyncio.run(mode.on_tick(1))
    assert player.pickup_id == int(C.DIAMOND_PICKUP)
    created = len(server.created)

    mode.ended = True
    asyncio.run(mode.on_player_death(player, None, int(C.KILL.FALL_KILL)))

    assert player.pickup_id is None
    assert not mode.carriers
    assert not mode.ground_diamonds
    assert len(server.created) == created


def test_restart_does_not_destroy_rebuilt_entities_with_recycled_ids(
    monkeypatch,
) -> None:
    now = [400.0]
    server, mode = _mode(monkeypatch, now)
    asyncio.run(mode.on_mode_start())
    diamond = mode._spawn_diamond((125.5, 100.5, 50.5), now=now[0])

    rebuilt = []

    class _Resources:
        def rebuild(self):
            rebuilt.append(server.entity_registry.place(
                int(C.AMMO_CRATE), 10.0, 10.0, 60.0, kind="crate"
            ))

    server.map_resources = _Resources()
    server.entity_registry.clear()  # reset_round_runtime
    server.destroyed.clear()
    asyncio.run(mode.on_mode_start())

    crate = rebuilt[-1]
    assert crate.entity_id == diamond.entity_id
    assert server.destroyed == []
    assert server.entity_registry.get(crate.entity_id) is crate
    assert not mode.ground_diamonds


def test_diamond_create_entity_carries_lifetime_as_fuse(monkeypatch) -> None:
    # Retail sends RULE_DIAMOND_LIFETIME as the packet-21 fuse; the native
    # client's 3D label counts it down (a fuse of 0 showed "0" forever).
    now = [500.0]
    server, mode = _mode(monkeypatch, now)
    asyncio.run(mode.on_mode_start())
    diamond = mode._spawn_diamond((125.5, 100.5, 50.5), now=now[0])

    entity = server.entity_registry.get(diamond.entity_id)
    assert server.created[-1] is entity
    assert mode.diamond_lifetime > 0.0
    assert entity.to_wire_entity().fuse == mode.diamond_lifetime

    # A late joiner's replay counts down from the remaining lifetime.
    now[0] += 12.5
    asyncio.run(mode.on_tick(1))
    assert entity.to_wire_entity().fuse == mode.diamond_lifetime - 12.5
