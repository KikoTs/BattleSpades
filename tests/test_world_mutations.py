"""Characterization tests for post-physics terrain mutation scheduling."""

from types import SimpleNamespace

from server.metrics import RuntimeMetrics
from server.world_mutations import PendingWorldMutation, WorldMutationService


def _server(*, queue_limit: int = 4):
    owner = SimpleNamespace(last_applied_input_loop=10)
    return SimpleNamespace(
        loop_count=100,
        players={7: owner},
        config=SimpleNamespace(
            world_mutation_queue_limit=queue_limit,
            world_mutation_batch_limit=2,
            world_mutation_cell_budget=8,
            world_mutation_timeout_ticks=30,
        ),
        metrics=RuntimeMetrics(),
    )


def _mutation(events: list[str], *, action_loop: int = 12, cells: int = 1):
    return PendingWorldMutation(
        owner_id=7,
        action_loop=action_loop,
        enqueued_tick=100,
        kind="test",
        cell_count=cells,
        apply=lambda: events.append("apply"),
        cancel=lambda: events.append("cancel"),
    )


def test_mutation_commits_only_after_owner_reaches_action_loop() -> None:
    events: list[str] = []
    server = _server()
    service = WorldMutationService(server)
    assert service.enqueue(_mutation(events)) is True

    assert service.commit_ready() == 0
    assert events == []
    server.players[7].last_applied_input_loop = 12
    assert service.commit_ready() == 1
    assert events == ["apply"]
    assert server.metrics.committed_world_mutations == 1


def test_full_queue_rejects_and_cancels_resource_reservation() -> None:
    events: list[str] = []
    server = _server(queue_limit=1)
    service = WorldMutationService(server)
    assert service.enqueue(_mutation(events)) is True
    assert service.enqueue(_mutation(events)) is False

    assert events == ["cancel"]
    assert service.pending_count == 1
    assert server.metrics.rejected_world_mutations == 1


def test_future_loop_expires_instead_of_retaining_unbounded_work() -> None:
    events: list[str] = []
    server = _server()
    service = WorldMutationService(server)
    assert service.enqueue(_mutation(events, action_loop=10_000)) is True

    server.loop_count = 130
    assert service.commit_ready() == 0
    assert events == ["cancel"]
    assert service.pending_count == 0
    assert server.metrics.expired_world_mutations == 1


def test_server_owned_bot_commits_after_physics_without_client_loop() -> None:
    """Bots have no ClientData watermark; the post-physics boundary is enough."""
    events: list[str] = []
    server = _server()
    server.players[7].is_bot = True
    server.players[7].last_applied_input_loop = None
    service = WorldMutationService(server)
    assert service.enqueue(_mutation(events, action_loop=10_000)) is True

    assert service.commit_ready() == 1
    assert events == ["apply"]
    assert service.pending_count == 0
    assert server.metrics.expired_world_mutations == 0


def test_cell_and_mutation_budgets_defer_excess_work() -> None:
    events: list[str] = []
    server = _server()
    server.players[7].last_applied_input_loop = 20
    service = WorldMutationService(server)
    for _ in range(3):
        assert service.enqueue(_mutation(events, cells=4)) is True

    assert service.commit_ready() == 2
    assert events == ["apply", "apply"]
    assert service.pending_count == 1
    assert service.commit_ready() == 1
    assert events == ["apply", "apply", "apply"]


def test_disconnect_cancels_owner_mutations_before_player_id_reuse() -> None:
    """A replacement player must never release the prior owner's mutation."""
    events: list[str] = []
    server = _server()
    service = WorldMutationService(server)
    assert service.enqueue(_mutation(events, action_loop=12)) is True

    service.cancel_owner(7)
    server.players[7] = SimpleNamespace(last_applied_input_loop=12)

    assert service.commit_ready() == 0
    assert service.pending_count == 0
    assert events == ["cancel"]
