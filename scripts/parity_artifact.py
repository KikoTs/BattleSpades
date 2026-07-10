"""Correlated JSON artifacts for server/client parity scenarios."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ParityArtifact:
    def __init__(self, scenario: str):
        self.scenario = str(scenario)
        self.created_at = _utc_now()
        self.samples: list[dict] = []

    def record(self, marker: str, **snapshots) -> dict:
        """Append one marker with snapshots from every available authority."""
        sample = {
            "marker": str(marker),
            "monotonic_ns": time.monotonic_ns(),
            "captured_at": _utc_now().isoformat(),
        }
        sample.update(snapshots)
        self.samples.append(sample)
        return sample

    def write(self, destination: Path) -> Path:
        """Write a deterministic UTF-8 JSON document and return its path."""
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        stamp = self.created_at.strftime("%Y%m%dT%H%M%S.%fZ")
        path = destination / f"{self.scenario}-{stamp}.json"
        payload = {
            "scenario": self.scenario,
            "created_at": self.created_at.isoformat(),
            "samples": self.samples,
        }
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path
