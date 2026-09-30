"""Read a supervised memory soak without confusing progress with a six-hour pass."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time


def check(state: dict, minimum_minutes: float = 360.0, now: float | None = None) -> dict:
    """Return an honest pass/fail/incomplete verdict from durable run evidence."""
    now = time.time() if now is None else now
    result = {
        "status": state.get("status", "unknown"),
        "verdict": "incomplete",
        "elapsed_minutes": state.get("elapsed_minutes", 0),
        "elapsed_seconds": state.get("elapsed_seconds"),
        "required_minutes": minimum_minutes,
        "cap_mb": state.get("cap_mb", 0),
        "cap_readback_bytes": state.get("cap_readback_bytes"),
        "swap_readback_bytes": state.get("swap_readback_bytes"),
        "cgroup_peak_mb": state.get("cgroup_peak_mb"),
        "memory_samples": state.get("memory_samples"),
        "churn_joins": state.get("churn_joins"),
        "restarts": len(state.get("restarts", [])),
        "oom_kills": state.get("oom_kills", 0),
        "failures": list(state.get("failures", [])),
    }
    if state.get("status") == "running":
        result["heartbeat_age_seconds"] = round(now - state.get("heartbeat_wall", 0), 1)
        if result["heartbeat_age_seconds"] > 180:
            result["status"] = "stale"
        return result
    if state.get("verdict") == "failed" or result["failures"] or result["restarts"] or result["oom_kills"]:
        result["verdict"] = "failed"
    elif (state.get("status") == "finished" and state.get("verdict") == "passed"
          and state.get("minutes", 0) >= minimum_minutes
          and state.get("elapsed_minutes", 0) >= minimum_minutes
          and state.get("cap_mb") == 512):
        result["verdict"] = "passed"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--minimum-minutes", type=float, default=360.0)
    args = parser.parse_args()
    state = json.loads((args.out / "supervisor.json").read_text(encoding="utf-8"))
    result = check(state, args.minimum_minutes)
    from analyze_memory_soak import build_report
    result["evidence"] = build_report(args.out)
    print(json.dumps(result, indent=2))
    return {"passed": 0, "failed": 1, "incomplete": 2}[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
