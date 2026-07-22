from server.config import ServerConfig
from server.world_manager import WorldManager


def manager_with_solids(solids):
    manager = WorldManager(ServerConfig())
    manager.map = object()
    manager.get_solid = lambda x, y, z: (x, y, z) in solids
    return manager


def reference_find_unsupported_chunks(manager, removed_positions):
    """Pre-optimization traversal retained here as a result-parity oracle."""
    neighbors = manager.COLLAPSE_NEIGHBORS
    chunks = []
    visited = set()
    for sx, sy, sz in removed_positions:
        for dx, dy, dz in neighbors:
            start = (sx + dx, sy + dy, sz + dz)
            if start in visited or not manager.get_solid(*start):
                continue
            comp = []
            stack = [start]
            comp_seen = {start}
            grounded = False
            exhausted = False
            work = 0
            while stack:
                cx, cy, cz = stack.pop()
                if cz > 238:
                    grounded = True
                    break
                comp.append((cx, cy, cz))
                for ddx, ddy, ddz in neighbors:
                    work += 1
                    if work > manager.COLLAPSE_WORK_BUDGET:
                        exhausted = True
                        stack.clear()
                        break
                    nxt = (cx + ddx, cy + ddy, cz + ddz)
                    if nxt not in comp_seen and manager.get_solid(*nxt):
                        comp_seen.add(nxt)
                        stack.append(nxt)
            visited |= comp_seen
            if not grounded and not exhausted and comp:
                chunks.append(comp)
    return chunks


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


def test_grounded_proof_reuse_preserves_results_and_bounds_solid_queries():
    size = 40
    solids = {
        (100 + x, 100 + y, 100)
        for x in range(size)
        for y in range(size)
    }
    # DFS reaches this high-coordinate support early from each boundary start.
    # Without proof reuse, each start repeatedly walks toward the base plane.
    solids.update(
        (99 + size, 99 + size, z)
        for z in range(100, 240)
    )
    # Include an independent floating component so parity covers both outcomes.
    solids.update({(300, 300, 100), (301, 300, 100), (302, 300, 100)})
    removed = [
        (99, 100 + y, 100)
        for y in range(0, size, 2)
    ]
    removed.extend(
        (100 + x, 99, 100)
        for x in range(0, size, 2)
    )
    removed.append((299, 300, 100))

    reference_manager = manager_with_solids(solids)
    expected = reference_find_unsupported_chunks(reference_manager, removed)

    manager = manager_with_solids(solids)
    solid_queries = 0

    def counted_get_solid(x, y, z):
        nonlocal solid_queries
        solid_queries += 1
        return (x, y, z) in solids

    manager.get_solid = counted_get_solid
    actual = manager.find_unsupported_chunks(removed)

    assert actual == expected
    assert actual == [[(300, 300, 100), (301, 300, 100), (302, 300, 100)]]
    assert solid_queries <= 6_000
