"""Low-overhead runtime metrics used by capacity and soak gates."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    index = int(round((len(sorted_values) - 1) * percentile))
    return float(sorted_values[max(0, min(index, len(sorted_values) - 1))])


@dataclass
class RuntimeMetrics:
    tick_samples_ms: deque = field(default_factory=lambda: deque(maxlen=36_000))
    subsystem_samples_ms: dict[str, deque] = field(default_factory=dict)
    world_serializations: int = 0
    world_sends: int = 0
    world_bytes: int = 0
    dropped_ingame_packets: int = 0
    skipped_plugin_callbacks: int = 0
    skipped_entity_ticks: int = 0
    map_mutation_overflows: int = 0
    dropped_mode_events: int = 0
    committed_world_mutations: int = 0
    rejected_world_mutations: int = 0
    expired_world_mutations: int = 0
    world_mutation_queue_peak: int = 0
    terrain_repair_cells: int = 0
    terrain_repair_sends: int = 0
    terrain_repair_queue_peak: int = 0
    dropped_terrain_repairs: int = 0
    failed_terrain_repair_sends: int = 0

    def record_tick(self, elapsed_ms: float) -> None:
        self.tick_samples_ms.append(float(elapsed_ms))

    def record_subsystem(self, name: str, elapsed_ms: float) -> None:
        """Record bounded timing for one named part of the gameplay tick."""
        samples = self.subsystem_samples_ms.get(name)
        if samples is None:
            samples = deque(maxlen=36_000)
            self.subsystem_samples_ms[name] = samples
        samples.append(float(elapsed_ms))

    def record_world_packet(self, size: int, recipients: int) -> None:
        self.world_serializations += 1
        self.world_sends += int(recipients)
        self.world_bytes += int(size) * int(recipients)

    def snapshot(self) -> dict[str, float | int]:
        values = sorted(self.tick_samples_ms)
        snapshot = {
            "tick_samples": len(values),
            "tick_avg_ms": sum(values) / len(values) if values else 0.0,
            "tick_p50_ms": _percentile(values, 0.50),
            "tick_p95_ms": _percentile(values, 0.95),
            "tick_p99_ms": _percentile(values, 0.99),
            "tick_max_ms": values[-1] if values else 0.0,
            "world_serializations": self.world_serializations,
            "world_sends": self.world_sends,
            "world_bytes": self.world_bytes,
            "dropped_ingame_packets": self.dropped_ingame_packets,
            "skipped_plugin_callbacks": self.skipped_plugin_callbacks,
            "skipped_entity_ticks": self.skipped_entity_ticks,
            "map_mutation_overflows": self.map_mutation_overflows,
            "dropped_mode_events": self.dropped_mode_events,
            "committed_world_mutations": self.committed_world_mutations,
            "rejected_world_mutations": self.rejected_world_mutations,
            "expired_world_mutations": self.expired_world_mutations,
            "world_mutation_queue_peak": self.world_mutation_queue_peak,
            "terrain_repair_cells": self.terrain_repair_cells,
            "terrain_repair_sends": self.terrain_repair_sends,
            "terrain_repair_queue_peak": self.terrain_repair_queue_peak,
            "dropped_terrain_repairs": self.dropped_terrain_repairs,
            "failed_terrain_repair_sends": self.failed_terrain_repair_sends,
        }
        for name, samples in sorted(self.subsystem_samples_ms.items()):
            subsystem_values = sorted(samples)
            snapshot[f"subsystem_{name}_avg_ms"] = (
                sum(subsystem_values) / len(subsystem_values)
                if subsystem_values else 0.0
            )
            snapshot[f"subsystem_{name}_p99_ms"] = _percentile(
                subsystem_values, 0.99
            )
        return snapshot
