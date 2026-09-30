"""Summarize capped-soak evidence and compare memory for the same map and mode.

The report measures growth; it does not equate staying below a cap with proving
the absence of leaks. Missing or too-short evidence remains explicitly absent.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
from statistics import median


MEMORY_FIELDS = (
    "rss_mb", "uss_mb", "pss_mb", "worker_rss_mb", "worker_pss_mb",
    "total_rss_mb", "total_pss_mb", "cg_current_mb", "cg_peak_mb",
    "cg_anon_mb", "cg_file_mb",
)
GROWTH_FIELDS = ("cg_anon_mb", "cg_current_mb", "total_pss_mb", "total_rss_mb")


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def memory_growth(rows, transitions, *, warmup_seconds=1800, settle_seconds=60):
    """Compare matched map/mode medians in the first and last post-warmup thirds."""
    timed = [(number(row.get("t_s")), row) for row in rows]
    timed = sorted(((t, row) for t, row in timed if t is not None), key=lambda item: item[0])
    end = timed[-1][0] if timed else 0
    span = max(0, end - warmup_seconds)
    early_end, late_start = warmup_seconds + span / 3, end - span / 3
    change_times = [number(change.get("t")) for change in transitions if change.get("ok")]
    # Also infer changes from CSV so running reports do not need final summary.json.
    previous = None
    for t, row in timed:
        current = row.get("map"), row.get("mode")
        if previous is not None and current != previous:
            change_times.append(t)
        previous = current
    change_times = sorted(t for t in change_times if t is not None)
    groups = defaultdict(lambda: {"early": [], "late": []})
    change_index, last_change = 0, -math.inf
    for t, row in timed:
        while change_index < len(change_times) and change_times[change_index] <= t:
            last_change = change_times[change_index]
            change_index += 1
        if (t < warmup_seconds or t - last_change < settle_seconds
                or number(row.get("tm_active")) == 1):
            continue
        window = "early" if t <= early_end else "late" if t >= late_start else None
        if window:
            groups[(row.get("map"), row.get("mode"))][window].append(row)
    matched = []
    for (map_name, mode), windows in sorted(groups.items()):
        if min(len(windows["early"]), len(windows["late"])) < 3:
            continue
        metrics = {}
        for field in GROWTH_FIELDS:
            values = {window: [value for row in records
                               if (value := number(row.get(field))) is not None]
                      for window, records in windows.items()}
            if min(len(values["early"]), len(values["late"])) >= 3:
                early, late = median(values["early"]), median(values["late"])
                metrics[field] = {"early_median": round(early, 3),
                                  "late_median": round(late, 3),
                                  "delta": round(late - early, 3)}
        if metrics:
            matched.append({"map": map_name, "mode": mode,
                            "early_samples": len(windows["early"]),
                            "late_samples": len(windows["late"]), "metrics": metrics})
    deltas = {}
    for field in GROWTH_FIELDS:
        values = [group["metrics"][field]["delta"] for group in matched
                  if field in group["metrics"]]
        deltas[field] = round(median(values), 3) if values else None
    return {
        "status": "measured" if matched else "insufficient comparable samples",
        "method": "Equal-weight matched map/mode medians; first and last post-warmup thirds. No automatic leak verdict.",
        "warmup_seconds": warmup_seconds, "settle_seconds": settle_seconds,
        "early_window_seconds": [warmup_seconds, round(early_end, 3)],
        "late_window_seconds": [round(late_start, 3), end],
        "matched_groups": matched, "median_matched_delta_mb": deltas,
    }


def completed_mode_cycles(modes, transitions):
    """Count returns to the initial mode after visiting all configured modes."""
    if not modes:
        return 0
    initial, seen, cycles = modes[0], {modes[0]}, 0
    for change in transitions:
        if not change.get("ok") or change.get("type") != "mode":
            continue
        target = change.get("to_mode")
        if target == initial and set(modes) <= seen:
            cycles += 1
            seen = {initial}
        else:
            seen.add(target)
    return cycles


def read_json(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_report(out: Path):
    state, summary = read_json(out / "supervisor.json"), read_json(out / "summary.json")
    rows = []
    if (out / "memory.csv").exists():
        with (out / "memory.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
    churn = {}
    malformed_churn_lines = 0
    if (out / "churn.jsonl").exists():
        for line in (out / "churn.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                churn = json.loads(line)
            except json.JSONDecodeError:
                malformed_churn_lines += 1
    peaks = {}
    for field in MEMORY_FIELDS:
        values = [value for row in rows if (value := number(row.get(field))) is not None]
        peaks[field] = max(values) if values else None
    transitions = summary.get("transitions", [])
    counts = Counter(change.get("type") for change in transitions if change.get("ok"))
    modes = [mode.strip() for mode in summary.get("args", {}).get("modes", "").split(",") if mode.strip()]
    worker = {}
    for field in ("worker_restarts", "worker_planned_recycles", "worker_crash_restarts"):
        values = [value for row in rows if (value := number(row.get(field))) is not None]
        worker[field] = int(max(values)) if values else None
    return {
        "elapsed_seconds": state.get("elapsed_seconds"),
        "cap_readback_bytes": state.get("cap_readback_bytes"),
        "swap_readback_bytes": state.get("swap_readback_bytes"),
        "child_stop_reason": summary.get("stop_reason"),
        "child_elapsed_minutes": summary.get("elapsed_minutes"),
        "memory_samples": len(rows), "peak_memory_mb": peaks,
        "supervisor_cgroup_peak_mb": state.get("cgroup_peak_mb"),
        "transitions": {"successful": dict(counts),
                        "failed": sum(not change.get("ok") for change in transitions),
                        "complete_mode_cycles": completed_mode_cycles(modes, transitions),
                        "visited_maps": summary.get("visited_maps", []),
                        "visited_modes": summary.get("visited_modes", [])},
        "worker": worker,
        "churn": {key: value for key, value in churn.items() if key not in ("states", "wall", "t")},
        "malformed_churn_lines": malformed_churn_lines,
        "memory_growth": memory_growth(rows, transitions),
        "logs": {key: summary.get(key, {}) for key in (
            "log_levels", "warning_keys", "traceback_counts", "first_tracebacks",
            "anticheat_messages", "conduct_messages", "spawn_failures")},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    print(json.dumps(build_report(args.out), indent=2))


if __name__ == "__main__":
    main()
