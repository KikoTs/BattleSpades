"""Fixed-step gameplay scheduler.

The retail client predicts at 60 Hz.  This runtime keeps simulation at the same
fixed delta, drains input before physics, and invokes replication only after a
completed step.  Blocking I/O and unbounded work do not belong in this module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .main import BattleSpadesServer

logger = logging.getLogger(__name__)


class SimulationRuntime:
    """Run deterministic fixed-delta ticks for one server instance."""

    MAX_CATCH_UP_STEPS = 5

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server
        self._stat_ticks = 0
        self._stat_slow = 0
        self._stat_max_ms = 0.0
        self._stat_sum_ms = 0.0
        self._subsystem_stat: dict[str, list[float]] = {}

    async def run(self) -> None:
        """Run until ``server.running`` becomes false."""
        server = self.server
        accumulator = 0.0
        last_time = time.perf_counter()
        while server.running:
            current = time.perf_counter()
            accumulator += current - last_time
            last_time = current
            accumulator = min(
                accumulator,
                server.tick_interval * self.MAX_CATCH_UP_STEPS,
            )

            while accumulator >= server.tick_interval:
                accumulator -= server.tick_interval
                server.loop_count += 1
                await self.step()
                # Publish every crossed 30 Hz cadence boundary.  Sending only
                # once after a multi-step catch-up batch stretched anchors and
                # produced visible observer/local motion gaps after a hitch.
                server._broadcast_world_updates()
                # Give the sibling ENet service task a chance to flush/receive
                # between bounded catch-up steps instead of monopolising all
                # five simulation frames in one event-loop turn.
                await asyncio.sleep(0)
            await asyncio.sleep(0.001)

    async def _measure_async(
        self, name: str, operation: Callable[[], Awaitable[object]]
    ) -> object:
        start = time.perf_counter()
        try:
            return await operation()
        finally:
            self._record_subsystem(name, (time.perf_counter() - start) * 1000.0)

    def _measure_sync(self, name: str, operation: Callable[[], object]) -> object:
        start = time.perf_counter()
        try:
            return operation()
        finally:
            self._record_subsystem(name, (time.perf_counter() - start) * 1000.0)

    def _record_subsystem(self, name: str, elapsed_ms: float) -> None:
        """Record bounded lifetime metrics and the current health-log window."""

        self.server.metrics.record_subsystem(name, elapsed_ms)
        values = self._subsystem_stat.setdefault(name, [0.0, 0.0, 0.0])
        values[0] += 1.0
        values[1] += float(elapsed_ms)
        values[2] = max(values[2], float(elapsed_ms))

    async def step(self) -> None:
        """Advance exactly one authoritative gameplay tick."""
        server = self.server
        tick_start = time.perf_counter()

        await self._measure_async("packet_drain", server._drain_ingame_packets)
        if server.bots is not None:
            await self._measure_async(
                "bots", lambda: server.bots.update(server.tick_interval)
            )

        # Stock GameScene processes incoming Damage(37) before the frame's
        # scene/Character physics (gameScene.pyd update core 0x10149CF0;
        # process_packet_damage 0x1018C270). Detecting projectile impacts after
        # players made the authoritative body consume Snowball knockback one
        # input frame later than retail, leaving a repeatable ~0.3-block owner
        # correction. Advance only projectiles here; generic entities/turrets
        # keep their independent post-player scheduling below.
        self._measure_sync(
            "projectiles", lambda: server._update_grenades(server.tick_interval)
        )
        await self._measure_async("players", self._simulate_players)
        # Client-origin terrain packets are validated during packet drain but
        # commit here.  The owner has now replayed the action loop against the
        # same pre-edit collision map used by the retail movement history.
        self._measure_sync(
            "world_mutations", server.world_mutations.commit_ready
        )
        prefab_actions = getattr(server, "prefab_actions", None)
        if prefab_actions is not None:
            self._measure_sync("prefabs", prefab_actions.tick)
        self._measure_sync("terrain_repair", server.terrain_repair.tick)
        self._measure_sync("a2s", server.a2s_handler.update)
        await self._measure_async("mode", self._tick_mode)
        await self._measure_async(
            "plugins",
            lambda: server.plugin_manager.call_event(
                "on_tick",
                server.loop_count,
                budget_ms=float(
                    getattr(server.config, "plugin_event_budget_ms", 2.0)
                ),
            ),
        )
        await self._measure_async("respawns", server._process_respawns)
        def _tick_entities() -> None:
            skipped = server.entity_registry.tick(
                server._build_entity_ctx(),
                max_on_tick=int(
                    getattr(server.config, "entity_tick_batch_limit", 8192)
                ),
            )
            if skipped:
                server.metrics.skipped_entity_ticks += int(skipped)

        self._measure_sync("entities", _tick_entities)
        self._measure_sync(
            "turrets",
            lambda: server.rocket_turret_controller.update(
                server.tick_interval, time.monotonic()
            ),
        )
        self._measure_sync("fire", server.fire_controller.update)
        self._update_second_schedulers()

        tick_ms = (time.perf_counter() - tick_start) * 1000.0
        server.metrics.record_tick(tick_ms)
        self._record_health(tick_ms)

    async def _simulate_players(self) -> None:
        """Consume at most one observed input row per owner this server tick.

        Thread/tick context: gameplay thread, after packet drain and before
        terrain commits. Queued rows remain queued for later ticks. Replaying
        two or more rows here is unsafe until replication has explicit state-
        transition watermarks: live retail captures showed batches crossing a
        block mutation or jetpack activation and reconciling old client history
        against new server topology/physics, causing ADJUSTs and hard SNAPs.
        """
        server = self.server
        for player in tuple(server.players.values()):
            connection = getattr(player, "connection", None)
            if (
                not bool(getattr(player, "is_bot", False))
                and connection is not None
                and not getattr(connection, "in_game", False)
            ):
                # A network player is eligible only after its first ClientData
                # completed reveal_world_to. The same gate is lowered before
                # a map/mode rollover swaps the VXL, while ENet is still
                # delivering the disconnect event. Simulating either joining
                # or retiring bodies here crosses native-scene/world epochs
                # and caused a measurable rollover hitch.
                continue
            await player.simulate_tick(server.tick_interval)

    async def _tick_mode(self) -> None:
        server = self.server
        if server.mode is None:
            return
        await server.mode.on_tick(server.loop_count)
        budget = min(
            len(server._mode_events),
            int(getattr(server.config, "mode_event_drain_budget", 512)),
        )
        for _ in range(budget):
            name, args = server._mode_events.popleft()
            handler = getattr(server.mode, name, None)
            if handler is not None:
                await handler(*args)
            await server.plugin_manager.call_event(name, *args)

    def _update_second_schedulers(self) -> None:
        server = self.server
        if server.loop_count % server.tick_rate != 0:
            return
        if (
            server.mode is not None
            and server.mode.started
            and not server.mode.ended
            and getattr(server.mode, "time_limit", 0) > 0
        ):
            from server.scoreboard import send_round_timer

            countdown = getattr(
                server.mode,
                "countdown_seconds_remaining",
                None,
            )
            remaining = (
                float(countdown(time.time()))
                if callable(countdown)
                else server.mode.time_limit - server.mode.elapsed_time
            )
            send_round_timer(server, remaining)
        if server.vote_manager.active:
            server.vote_manager.tick(time.time())

    def _record_health(self, tick_ms: float) -> None:
        server = self.server
        self._stat_ticks += 1
        self._stat_sum_ms += tick_ms
        self._stat_max_ms = max(self._stat_max_ms, tick_ms)
        if tick_ms > 10.0:
            self._stat_slow += 1
        if self._stat_ticks < 600:
            return

        inputs = []
        for player in server.players.values():
            applied = getattr(player, "input_frames_applied", 0)
            dropped = getattr(player, "input_frames_dropped", 0)
            stale = getattr(player, "input_frames_stale", 0)
            overflow = getattr(player, "input_frames_overflow", 0)
            starved = getattr(player, "input_starved_ticks", 0)
            position_reports = getattr(player, "position_reports_received", 0)
            if applied or dropped or starved or position_reports:
                inputs.append(
                    f"{player.name}:appl={applied} stale={stale} "
                    f"overflow={overflow} starve={starved} pos={position_reports}"
                )
                player.input_frames_applied = 0
                player.input_frames_dropped = 0
                player.input_frames_stale = 0
                player.input_frames_overflow = 0
                player.input_starved_ticks = 0
                player.position_reports_received = 0
        subsystem_summary = " ".join(
            f"{name}={values[1] / values[0]:.2f}/{values[2]:.2f}ms"
            for name, values in sorted(self._subsystem_stat.items())
            if values[0]
        )
        logger.info(
            "tick stats: avg=%.2fms max=%.2fms slow(>10ms)=%d/%d%s%s",
            self._stat_sum_ms / self._stat_ticks,
            self._stat_max_ms,
            self._stat_slow,
            self._stat_ticks,
            ("  inputs[" + " | ".join(inputs) + "]") if inputs else "",
            ("  subsystems[" + subsystem_summary + "]") if subsystem_summary else "",
        )
        self._stat_ticks = self._stat_slow = 0
        self._stat_max_ms = self._stat_sum_ms = 0.0
        self._subsystem_stat.clear()
