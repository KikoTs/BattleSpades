from server.config import ServerConfig
from server.world_manager import WorldManager


def manager_with_solids(solids):
    manager = WorldManager(ServerConfig())
    manager.map = object()
    manager.get_solid = lambda x, y, z: (x, y, z) in solids
    return manager


def test_z238_falls_but_z239_grounds_component():
    floating = {(10, 10, 238)}
    manager = manager_with_solids(floating)
    assert manager.find_unsupported_chunks([(10, 10, 237)]) == [
        [(10, 10, 238)]
    ]

    grounded = {(10, 10, 238), (10, 10, 239)}
    manager = manager_with_solids(grounded)
    assert manager.find_unsupported_chunks([(10, 10, 237)]) == []


def test_edge_connections_ground_but_three_axis_corners_do_not():
    edge_path = {(10, 10, 237), (11, 10, 238), (11, 10, 239)}
    manager = manager_with_solids(edge_path)
    assert manager.find_unsupported_chunks([(10, 10, 236)]) == []

    corner_path = {(20, 20, 237), (21, 21, 238), (22, 22, 239)}
    manager = manager_with_solids(corner_path)
    chunks = manager.find_unsupported_chunks([(20, 20, 236)])
    assert chunks == [[(20, 20, 237)]]


def test_components_over_visual_particle_limit_still_collapse():
    solids = {
        (100 + x, 100 + y, 100)
        for x in range(84)
        for y in range(100)
    }
    manager = manager_with_solids(solids)

    chunks = manager.find_unsupported_chunks([(99, 100, 100)])

    assert len(chunks) == 1
    assert len(chunks[0]) == 8400


def test_work_budget_exhaustion_never_returns_partial_component():
    solids = {(100 + x, 100, 100) for x in range(10)}
    manager = manager_with_solids(solids)
    manager.COLLAPSE_WORK_BUDGET = 5

    assert manager.find_unsupported_chunks([(99, 100, 100)]) == []
