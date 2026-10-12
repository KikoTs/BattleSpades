"""Microbenchmark added mode facts/policies, excluding physics, networking and A*.

Uses the test arenas and real mode/player fixtures. Run from the repository:
py -3.12 scripts/benchmark_fun_bots.py --bots 12 --samples 200
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modes.unofficial.catalog import SPECS
from server.bot_ai.unofficial import decision_for
from server.game_constants import TEAM1, TEAM2
from tests.test_unofficial_bots import brain_for, director_for, frame_for
from tests.test_unofficial_modes import add_player, make_game, start


async def benchmark(count: int, samples: int, worker_samples: int = 0) -> None:
    print("mode,facts_p95_ms,all_bot_policies_p95_ms,facts_max_ms,policy_max_ms,payload_bytes,worker_p95_ms,worker_max_ms")
    import pickle
    for spec in SPECS:
        server, mode, blue, green, now = make_game(spec.code)
        for pid in range(2, count + 2):
            add_player(server, pid, TEAM1 if spec.code == "night_shift" or pid % 2 == 0 else TEAM2, bot=True)
        await start(mode)
        if spec.code == "paint_raid":
            for p in mode.players():
                p.position = mode.anchor("center")
        director = director_for(server)
        frames = [frame_for(director, mode, p, now[0]) for p in mode.players() if p.is_bot]
        observers = [next(p for p in frame.players if p.player_id == frame.observer_id) for frame in frames]
        facts, policies = [], []
        for _ in range(samples):
            t = time.perf_counter()
            snapshot = mode.bot_snapshot()
            facts.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            for frame, observer in zip(frames, observers):
                decision_for(replace(frame, fun=snapshot), observer)
            policies.append((time.perf_counter() - t) * 1000)
        p95 = lambda values: statistics.quantiles(values, n=20)[18]
        worker = [0.0, 0.0]
        if worker_samples:
            from server.bot_ai.planning_budget import PlanningBudget
            from server.bot_ai.simple_worker import decide_current_frame
            brain = brain_for(server)
            brain.world.planning_budget = PlanningBudget()
            brain.world.planning_budget.scale_for(count)
            worker = []
            for iteration in range(worker_samples):
                for index, frame in enumerate(frames):
                    current = replace(frame, frame_id=iteration * count + index + 1,
                                      created_at=now[0] + iteration * .14)
                    t = time.perf_counter()
                    intent = decide_current_frame(brain.world, brain, current)
                    worker.append((time.perf_counter() - t) * 1000)
                    if intent is None:
                        raise RuntimeError(f"{spec.code}: missing worker decision")
        print(f"{spec.code},{p95(facts):.4f},{p95(policies):.4f},{max(facts):.4f},{max(policies):.4f},{len(pickle.dumps(snapshot))},{p95(worker):.4f},{max(worker):.4f}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bots", type=int, default=12, choices=range(1, 65))
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--worker-samples", type=int, default=0,
                        help="Also time worker decisions on static bodies; excludes motor, physics and networking")
    args = parser.parse_args()
    asyncio.run(benchmark(args.bots, max(20, args.samples), max(0, args.worker_samples)))
