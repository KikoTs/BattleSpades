"""Bot perception must not pay for decorative entities or re-sort every frame."""

from __future__ import annotations

from types import SimpleNamespace

import shared.constants as C
from server.bot_ai.director import BotDirector, _MAX_PERCEPTION_ENTITIES
from server.entities.registry import EntityRegistry
from server.game_constants import TEAM1, TEAM_NEUTRAL


def _players():
    return {
        0: SimpleNamespace(
            team=TEAM1, alive=True, spawned=True,
            position=(0.0, 0.0, 0.0), pickup_id=None,
        ),
    }


def _metrics():
    return SimpleNamespace(
        bot_perception_entity_overflow=0,
        bot_perception_entity_reranks=0,
    )


def test_map_flares_never_enter_bot_perception() -> None:
    """20thCenturyTown's 524 static lights used to overflow the 192 cap."""

    registry = EntityRegistry()
    for index in range(524):
        registry.place(
            int(C.FLARE_BLOCK), float(index % 400), 10.0, 30.0,
            state=TEAM_NEUTRAL, color=(255, 200, 100), kind="map_flare",
            # Static map ownership uses player 0, which is also a bot id:
            # a leaked flare made bot 0 think it already owned a deployable.
            player_id=0, behavior=None,
        )
    crates = [
        registry.place(int(C.AMMO_CRATE), 5.0 + index, 5.0, 30.0, kind="pickup")
        for index in range(3)
    ]
    server = SimpleNamespace(
        entity_registry=registry,
        projectile_engine=SimpleNamespace(projectiles=[]),
        players=_players(),
        metrics=_metrics(),
    )

    snapshots = BotDirector._snapshot_entities(SimpleNamespace(server=server))

    assert {item.entity_id for item in snapshots} == {
        crate.entity_id for crate in crates
    }
    assert all(item.entity_type != int(C.FLARE_BLOCK) for item in snapshots)
    assert server.metrics.bot_perception_entity_overflow == 0
    assert server.metrics.bot_perception_entity_reranks == 0


def test_overflow_ranking_is_cached_but_hazards_are_always_fresh() -> None:
    registry = EntityRegistry()
    for index in range(_MAX_PERCEPTION_ENTITIES + 60):
        registry.place(
            int(C.HEALTH_CRATE), 10.0 + index, 0.0, 0.0, kind="crate",
        )
    server = SimpleNamespace(
        entity_registry=registry,
        projectile_engine=SimpleNamespace(projectiles=[]),
        players=_players(),
        metrics=_metrics(),
    )
    director = SimpleNamespace(server=server)

    first = BotDirector._snapshot_entities(director)
    second = BotDirector._snapshot_entities(director)

    assert len(first) == len(second) == _MAX_PERCEPTION_ENTITIES
    assert [item.entity_id for item in first] == [
        item.entity_id for item in second
    ]
    # One full distance ranking serves both refreshes.
    assert server.metrics.bot_perception_entity_reranks == 1

    # A newly armed charge far away is selected immediately without waiting
    # for the ranking cache to expire.
    hazard = registry.place(
        int(C.DYNAMITE_ENTITY), 9000.0, 0.0, 0.0, state=TEAM1,
        kind="deployable", player_id=3,
        behavior=SimpleNamespace(blast_radius=5.0),
    )
    third = BotDirector._snapshot_entities(director)
    assert third[0].entity_id == hazard.entity_id
    assert len(third) == _MAX_PERCEPTION_ENTITIES
    assert server.metrics.bot_perception_entity_reranks == 1

    # A new ordinary entity forces a re-rank so a near pickup is not hidden
    # behind a stale order for up to the cache lifetime.
    near = registry.place(int(C.AMMO_CRATE), 1.0, 0.0, 0.0, kind="pickup")
    fourth = BotDirector._snapshot_entities(director)
    assert near.entity_id in {item.entity_id for item in fourth}
    assert server.metrics.bot_perception_entity_reranks == 2


def _refuge_director(surface_z):
    director = object.__new__(BotDirector)
    world = SimpleNamespace(
        map_name="Flat", map_file_crc=1, get_height=surface_z,
    )
    director.server = SimpleNamespace(world_manager=world, players={})
    director._zombie_refuge = {}
    director._zombie_refuge_history = []
    director._zombie_refuge_epoch = None
    director._zombie_refuge_job = None
    # A resolved "no botnav cache" lookup: heights-only elections.
    director._refuge_regions = (("Flat", 1), None)
    return director


def test_cold_zombie_refuge_election_is_spread_across_refreshes(monkeypatch) -> None:
    """The first Zombie refresh used to scan ~13k columns in one 53 ms tick."""

    import server.bot_ai.director as director_module
    from modes.zombie import ZombiePhase
    from server.bot_ai.zombie_refuge import elect_refuge

    def surface_z(x, y):
        if abs(x - 140) <= 4 and abs(y - 140) <= 4:
            return 52
        return 60

    calls = []

    def counted(x, y):
        calls.append((x, y))
        return surface_z(x, y)

    # Zero budget: every refresh advances exactly one candidate.
    monkeypatch.setattr(director_module, "_REFUGE_ELECTION_BUDGET_SECONDS", 0.0)
    director = _refuge_director(counted)
    survivors = [SimpleNamespace(position=(120.0, 120.0, 57.75))]
    zombies = [SimpleNamespace(position=(60.0, 60.0, 57.75), alive=True, spawned=True)]
    mode = SimpleNamespace(
        phase=ZombiePhase.ACTIVE,
        _living_survivors=lambda: survivors,
        _zombies=lambda: zombies,
    )

    assert director._zombie_refuge_objective(mode) is None
    assert len(calls) <= 26  # at most one candidate's height samples
    refreshes = 1
    refuge = None
    while refuge is None and refreshes < 5000:
        refuge = director._zombie_refuge_objective(mode)
        refreshes += 1

    expected = elect_refuge(
        surface_z, [(120.0, 120.0, 57.75)], [(60.0, 60.0, 57.75)],
    )
    assert refuge is not None and refuge.position == expected
    assert refreshes > 100
    assert director._zombie_refuge_job is None
    # Later refreshes reuse the elected refuge without another scan.
    calls.clear()
    assert director._zombie_refuge_objective(mode).position == expected
    assert calls == []
