"""Memory soak: the production-like soak plus a memory CSV and leak finders.

This is ``soak_server.py`` (the real server, a bot lobby, short rounds, map and
mode rotation) with everything a memory verdict needs added on top:

* ``memory.csv``       - one row per ``--mem-seconds`` (default 30 s): RSS and
  USS of the server, RSS of the bot worker, the cgroup's own accounting
  (current, peak, anon, file, OOM kills), Python allocated blocks, tracked
  object count, players/connections/entities/tasks/timers/threads/fds, the
  join and leave totals and the number of map changes so far.
* ``types.jsonl``      - object counts by type (``--types-seconds``).
* ``containers.jsonl`` - the length of every list/dict/set/deque reachable
  from the server object, its services and its players.  A container that
  grows with map changes or joins is a leak; this names it directly.
* ``tracemalloc.jsonl``- top growers by source line.  ``--tracemalloc always``
  compares against a baseline taken after warm-up (best for diagnosis, but
  the tracer itself costs memory).  ``--tracemalloc window`` traces a short
  window now and then and reports what that window allocated and still
  holds, so a long pass/fail run is not distorted between windows.

Rows are appended, so a supervisor can restart a crashed run into the same
files; ``--run-index`` marks which start a row belongs to.

Examples::

    py -3.12 scripts/soak/memory_soak.py --minutes 50 --tracemalloc always \\
        --config configs/official-diamond-mine.toml --bots 6 --port 28500
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
import csv
import faulthandler
import gc
import json
import os
from pathlib import Path
import sys
import threading
import time
import tracemalloc

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for entry in (str(ROOT), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import psutil  # noqa: E402

import soak_server  # noqa: E402

MB = float(2**20)

CSV_FIELDS = (
    "wall_iso", "wall", "t_s", "run", "uptime_s", "map", "mode",
    "map_changes", "transitions_failed", "players", "bots", "humans",
    "connections", "connections_in_game", "entities", "asyncio_tasks",
    "timers", "threads", "fds", "rss_mb", "uss_mb", "pss_mb", "worker_rss_mb",
    "worker_pss_mb", "total_rss_mb", "total_pss_mb",
    "cg_current_mb", "cg_peak_mb", "cg_anon_mb", "cg_file_mb",
    "cg_oom_kill", "py_blocks", "gc_objects", "tm_active", "tm_current_mb",
    "tm_overhead_mb", "connects", "disconnects", "joins", "worker_pid",
    "worker_restarts", "worker_planned_recycles", "worker_crash_restarts",
    "tick_p99_ms", "tick_max_ms",
)

# Light rotation of the 512 MB hosts: the Diamond Mine and Territory Control
# rotations without the four maps removed there for their size
# (tmp/beta01-rollout/frankfurt_light_rotation.sh).
LIGHT_ROTATION = (
    "AncientEgypt", "ArcticBase", "Atlantis", "BlockNess", "BranCastle",
    "CastleWars", "CityOfChicago", "DoubleDragon", "DragonIsland",
    "GreatWall", "London", "LunarBase", "SpookyMansion", "TheColosseum",
    "TokyoNeon",
)

_CONTAINER_TYPES = (list, dict, set, frozenset, deque, bytearray)


def _cgroup_dir() -> Path | None:
    """Directory of this process's cgroup v2 group, if there is one."""

    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                path = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
                if (path / "memory.current").exists():
                    return path
    except OSError:
        pass
    return None


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _read_keyed(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            key, _, value = line.partition(" ")
            try:
                values[key] = int(value)
            except ValueError:
                pass
    except OSError:
        pass
    return values


def _mb(value) -> str:
    return "" if value is None else f"{value / MB:.2f}"


class CappedJsonl:
    """Append JSON rows until ``limit`` bytes, then stop (never fill a disk)."""

    def __init__(self, path: Path, limit: int) -> None:
        self.path = path
        self.limit = int(limit)
        self.stream = path.open("a", encoding="utf-8")
        self.written = path.stat().st_size
        self.lock = threading.Lock()

    def write(self, row: dict) -> None:
        text = json.dumps(row, default=str) + "\n"
        with self.lock:
            if self.written + len(text) > self.limit:
                return
            self.stream.write(text)
            self.stream.flush()
            self.written += len(text)

    def close(self) -> None:
        with self.lock:
            self.stream.close()


class MemorySoakHarness(soak_server.SoakHarness):
    def __init__(self, args) -> None:
        super().__init__(args)
        limit = int(args.file_cap_mb * MB)
        # The base writers have no cap; swap them for capped ones.
        self.samples.close()
        self.events.close()
        self.samples = CappedJsonl(self.out / "samples.jsonl", limit)
        self.events = CappedJsonl(self.out / "events.jsonl", limit)
        self.types_log = CappedJsonl(self.out / "types.jsonl", limit)
        self.containers_log = CappedJsonl(self.out / "containers.jsonl", limit)
        self.tm_log = CappedJsonl(self.out / "tracemalloc.jsonl", limit)
        self.csv_path = self.out / "memory.csv"
        new_file = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        self.csv_stream = self.csv_path.open("a", encoding="utf-8", newline="")
        self.csv = csv.DictWriter(self.csv_stream, fieldnames=CSV_FIELDS)
        if new_file:
            self.csv.writeheader()
            self.csv_stream.flush()
        self.cgroup = _cgroup_dir()
        self.connects = 0
        self.disconnects = 0
        self.joins = 0
        self.tm_baseline: dict | None = None
        self.tm_window_started: float | None = None
        self.tm_next_window = time.monotonic() + args.tm_first_seconds
        self.tm_next_report = time.monotonic() + args.tm_first_seconds
        self.last_tick_stats = {"p99": 0.0, "max": 0.0}
        self.harness_started = time.monotonic()

    # --------------------------------------------------------------- config
    def build_config(self):
        config = super().build_config()
        # Small, bounded server log: the soak must not be able to fill a disk.
        config.log_backup_count = 2
        config.log_max_bytes = 8 * 1024 * 1024
        return config

    def instrument(self) -> None:
        super().instrument()
        server = self.server
        harness = self
        connect = server._on_connect_sync
        disconnect = server._on_disconnect_sync

        def on_connect(peer, data=0):
            harness.connects += 1
            return connect(peer, data)

        def on_disconnect(peer):
            harness.disconnects += 1
            return disconnect(peer)

        server._on_connect_sync = on_connect
        server._on_disconnect_sync = on_disconnect

        from server.connection import Connection

        original_new_player = Connection._on_new_player

        async def on_new_player(connection_self, packet):
            harness.joins += 1
            return await original_new_player(connection_self, packet)

        Connection._on_new_player = on_new_player

    # ------------------------------------------------------------- sampling
    def sample(self) -> None:
        # Keep the last tick window for the CSV before the base resets it.
        try:
            self.drain_ticks()
            if self.window_ticks:
                stats = soak_server._tick_stats(self.window_ticks)
                self.last_tick_stats = {"p99": stats["p99"], "max": stats["max"]}
        except Exception:  # noqa: BLE001
            pass
        super().sample()

    def memory_row(self) -> dict:
        server = self.server
        now = time.monotonic()
        proc = self.proc
        info = proc.memory_info()
        try:
            full_info = proc.memory_full_info()
            uss = full_info.uss
            pss = getattr(full_info, "pss", None)
        except (psutil.Error, OSError):
            uss = None
            pss = None
        worker = self.worker_status()
        worker_rss = None
        worker_pss = None
        pid = worker.get("process_id")
        if pid:
            try:
                worker_proc = psutil.Process(int(pid))
                worker_rss = worker_proc.memory_info().rss
                worker_pss = getattr(worker_proc.memory_full_info(), "pss", None)
            except (psutil.Error, OSError):
                worker_rss = None
        self.peak_rss = max(self.peak_rss, info.rss)
        cg = self.cgroup
        cg_stat = _read_keyed(cg / "memory.stat") if cg else {}
        cg_events = _read_keyed(cg / "memory.events") if cg else {}
        players = list(getattr(server, "players", {}).values())
        bots = sum(1 for p in players if getattr(p, "is_bot", False))
        connections = list(getattr(server, "connections", {}).values())
        loop = asyncio.get_running_loop()
        try:
            fds = proc.num_fds()
        except AttributeError:
            fds = proc.num_handles()
        tracing = tracemalloc.is_tracing()
        traced = tracemalloc.get_traced_memory()[0] if tracing else None
        overhead = tracemalloc.get_tracemalloc_memory() if tracing else None
        ok = sum(1 for row in self.transitions if row.get("ok") and row.get("type") != "restart")
        failed = sum(1 for row in self.transitions if not row.get("ok"))
        return {
            "wall_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "wall": f"{time.time():.1f}",
            "t_s": f"{self.args.elapsed_offset + (now - self.harness_started):.1f}",
            "run": self.args.run_index,
            "uptime_s": f"{now - self.harness_started:.1f}",
            "map": self.current_map(),
            "mode": self.current_mode(),
            "map_changes": ok,
            "transitions_failed": failed,
            "players": len(players),
            "bots": bots,
            "humans": len(players) - bots,
            "connections": len(connections),
            "connections_in_game": sum(1 for c in connections if getattr(c, "in_game", False)),
            "entities": len(getattr(server, "entities", {}) or {}),
            "asyncio_tasks": len(asyncio.all_tasks()),
            "timers": len(getattr(loop, "_scheduled", ()) or ()),
            "threads": threading.active_count(),
            "fds": fds,
            "rss_mb": _mb(info.rss),
            "uss_mb": _mb(uss),
            "pss_mb": _mb(pss),
            "worker_rss_mb": _mb(worker_rss),
            "worker_pss_mb": _mb(worker_pss),
            "total_rss_mb": _mb(info.rss + (worker_rss or 0)),
            "total_pss_mb": _mb(
                pss + (worker_pss or 0)
                if pss is not None and (not pid or worker_pss is not None) else None
            ),
            "cg_current_mb": _mb(_read_int(cg / "memory.current")) if cg else "",
            "cg_peak_mb": _mb(_read_int(cg / "memory.peak")) if cg else "",
            "cg_anon_mb": _mb(cg_stat.get("anon")) if cg else "",
            "cg_file_mb": _mb(cg_stat.get("file")) if cg else "",
            "cg_oom_kill": cg_events.get("oom_kill", "") if cg else "",
            "py_blocks": sys.getallocatedblocks(),
            "gc_objects": len(gc.get_objects()),
            "tm_active": int(tracing),
            "tm_current_mb": _mb(traced),
            "tm_overhead_mb": _mb(overhead),
            "connects": self.connects,
            "disconnects": self.disconnects,
            "joins": self.joins,
            "worker_pid": pid or "",
            "worker_restarts": int(worker.get("restarts") or 0),
            "worker_planned_recycles": int(worker.get("planned_recycles") or 0),
            "worker_crash_restarts": int(worker.get("crash_restarts") or 0),
            "tick_p99_ms": f"{self.last_tick_stats['p99']:.3f}",
            "tick_max_ms": f"{self.last_tick_stats['max']:.3f}",
        }

    def type_counts(self) -> None:
        counts: Counter = Counter()
        for obj in gc.get_objects():
            kind = type(obj)
            counts[f"{kind.__module__}.{kind.__qualname__}"] += 1
        self.types_log.write({
            "t": round(time.monotonic() - self.harness_started + self.args.elapsed_offset, 1),
            "run": self.args.run_index,
            "map": self.current_map(),
            "mode": self.current_mode(),
            "total": sum(counts.values()),
            "types": dict(counts.most_common(80)),
        })

    @staticmethod
    def _container_lengths(prefix: str, obj, out: dict, depth: int, seen: set) -> None:
        try:
            attributes = vars(obj)
        except TypeError:
            return
        for name, value in list(attributes.items()):
            key = f"{prefix}.{name}"
            if isinstance(value, _CONTAINER_TYPES):
                try:
                    size = len(value)
                except Exception:  # noqa: BLE001
                    continue
                if size:
                    out[key] = size
            elif depth > 0 and value is not None and id(value) not in seen:
                module = getattr(type(value), "__module__", "") or ""
                if module.split(".")[0] in ("server", "modes", "plugins", "commands", "protocol"):
                    seen.add(id(value))
                    MemorySoakHarness._container_lengths(key, value, out, depth - 1, seen)

    def container_census(self) -> None:
        server = self.server
        sizes: dict[str, int] = {}
        seen = {id(server)}
        self._container_lengths("server", server, sizes, 3, seen)
        per_player_total: Counter = Counter()
        per_player_max: dict[str, int] = {}
        for player in list(getattr(server, "players", {}).values()):
            found: dict[str, int] = {}
            self._container_lengths("player", player, found, 1, {id(player), id(server)})
            for key, size in found.items():
                per_player_total[key] += size
                if size > per_player_max.get(key, 0):
                    per_player_max[key] = size
        per_connection: Counter = Counter()
        for connection in list(getattr(server, "connections", {}).values()):
            found = {}
            self._container_lengths("connection", connection, found, 0, set())
            for key, size in found.items():
                per_connection[key] += size
        self.containers_log.write({
            "t": round(time.monotonic() - self.harness_started + self.args.elapsed_offset, 1),
            "run": self.args.run_index,
            "map": self.current_map(),
            "mode": self.current_mode(),
            "players": len(getattr(server, "players", {}) or {}),
            "server": sizes,
            "players_sum": dict(per_player_total),
            "players_max": per_player_max,
            "connections_sum": dict(per_connection),
        })

    # ---------------------------------------------------------- tracemalloc
    @staticmethod
    def _aggregate(snapshot) -> dict:
        snapshot = snapshot.filter_traces((
            tracemalloc.Filter(False, tracemalloc.__file__),
            tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
            tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
            tracemalloc.Filter(False, __file__),
        ))
        totals: dict[str, list] = {}
        for stat in snapshot.statistics("lineno"):
            frame = stat.traceback[0]
            totals[f"{frame.filename}:{frame.lineno}"] = [stat.size, stat.count]
        return totals

    def tracemalloc_report(self, label: str) -> None:
        snapshot = tracemalloc.take_snapshot()
        current = self._aggregate(snapshot)
        del snapshot
        baseline = self.tm_baseline
        if baseline is None:
            self.tm_baseline = current
            baseline = {}
            label = f"{label}-baseline"
        growth = []
        for key, (size, count) in current.items():
            before = baseline.get(key, (0, 0))
            diff = size - before[0]
            if diff > 0:
                growth.append((diff, count - before[1], size, count, key))
        growth.sort(reverse=True)
        root = str(ROOT)
        self.tm_log.write({
            "t": round(time.monotonic() - self.harness_started + self.args.elapsed_offset, 1),
            "run": self.args.run_index,
            "label": label,
            "map": self.current_map(),
            "mode": self.current_mode(),
            "map_changes": sum(1 for row in self.transitions if row.get("ok")),
            "traced_mb": round(tracemalloc.get_traced_memory()[0] / MB, 2),
            "growth_total_mb": round(sum(g[0] for g in growth) / MB, 3),
            "top": [
                {
                    "where": key.replace(root, "").lstrip("\\/"),
                    "grew_kb": round(diff / 1024.0, 1),
                    "grew_count": count_diff,
                    "size_kb": round(size / 1024.0, 1),
                    "count": count,
                }
                for diff, count_diff, size, count, key in growth[: self.args.tm_top]
            ],
        })

    def tracemalloc_step(self) -> None:
        mode = self.args.tracemalloc
        if mode == "off":
            return
        now = time.monotonic()
        if mode == "always":
            if now >= self.tm_next_report:
                self.tm_next_report = now + self.args.tm_seconds
                self.tracemalloc_report("always")
            return
        # Windowed: trace for a while, report what the window still holds.
        if self.tm_window_started is None:
            if now >= self.tm_next_window:
                tracemalloc.start(self.args.tm_frames)
                self.tm_baseline = {}
                self.tm_window_started = now
            return
        if now - self.tm_window_started >= self.args.tm_window_seconds:
            gc.collect()
            self.tracemalloc_report("window")
            tracemalloc.stop()
            self.tm_baseline = None
            self.tm_window_started = None
            self.tm_next_window = now + self.args.tm_seconds

    # ---------------------------------------------------------------- tasks
    async def memory_sampler(self) -> None:
        args = self.args
        while self.server is None or not self.server.running:
            await asyncio.sleep(0.2)
        next_row = time.monotonic()
        next_types = time.monotonic() + 5.0
        while not self.stop_requested:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            try:
                if now >= next_row:
                    next_row = now + args.mem_seconds
                    self.csv.writerow(self.memory_row())
                    self.csv_stream.flush()
                if now >= next_types:
                    next_types = now + args.types_seconds
                    self.type_counts()
                    self.container_census()
                self.tracemalloc_step()
            except Exception as exc:  # noqa: BLE001 - diagnostics never stop the soak
                self.event("memory_sample_error", error=repr(exc))

    async def driver(self) -> None:
        sampler = asyncio.create_task(self.memory_sampler(), name="soak-memory")
        try:
            await super().driver()
        finally:
            try:
                self.csv.writerow(self.memory_row())
                self.csv_stream.flush()
            except Exception:  # noqa: BLE001
                pass
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)

    async def run(self) -> None:
        try:
            await super().run()
        finally:
            for stream in (self.types_log, self.containers_log, self.tm_log):
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                self.csv_stream.close()
            except Exception:  # noqa: BLE001
                pass


def parse_args(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=str(ROOT / "configs" / "official-diamond-mine.toml"))
    parser.add_argument("--port", type=int, default=28500)
    parser.add_argument("--bots", type=int, default=6)
    parser.add_argument("--worker", choices=("process", "thread"), default="process")
    parser.add_argument("--round-seconds", type=float, default=150.0)
    parser.add_argument("--modes", default=",".join(soak_server.DEFAULT_MODES))
    parser.add_argument("--maps", default=",".join(LIGHT_ROTATION),
                        help="comma list; 'all' for every maps/*.vxl")
    parser.add_argument("--start-map", default="")
    parser.add_argument("--rounds-per-mode", type=int, default=2)
    parser.add_argument("--mode-change-delay", type=float, default=30.0)
    parser.add_argument("--minutes", type=float, default=50.0)
    parser.add_argument("--sample-seconds", type=float, default=30.0)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--sweep-dwell", type=float, default=25.0)
    parser.add_argument("--profile-entities", action="store_true")
    parser.add_argument("--profile-stalls", action="store_true")
    parser.add_argument("--stall-ms", type=float, default=120.0)
    parser.add_argument("--out", default="")
    parser.add_argument("--mem-seconds", type=float, default=30.0)
    parser.add_argument("--types-seconds", type=float, default=60.0)
    parser.add_argument("--tracemalloc", choices=("off", "always", "window"), default="off")
    parser.add_argument("--tm-frames", type=int, default=1)
    parser.add_argument("--tm-top", type=int, default=30)
    parser.add_argument("--tm-first-seconds", type=float, default=300.0,
                        help="warm-up before the baseline (always) or the first window")
    parser.add_argument("--tm-seconds", type=float, default=300.0,
                        help="seconds between reports (always) or between windows")
    parser.add_argument("--tm-window-seconds", type=float, default=300.0)
    parser.add_argument("--file-cap-mb", type=float, default=64.0,
                        help="size cap of every JSONL output file")
    parser.add_argument("--run-index", type=int, default=0,
                        help="which start of a supervised run this is")
    parser.add_argument("--elapsed-offset", type=float, default=0.0,
                        help="seconds already soaked before this start")
    args = parser.parse_args(argv)
    if args.maps.strip().lower() == "all":
        args.maps = ""
    if not args.out:
        args.out = str(ROOT / "logs" / "soak" / time.strftime("memory-%Y%m%d-%H%M%S"))
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    if args.tracemalloc == "always":
        tracemalloc.start(args.tm_frames)
    with (out / "fault.log").open("a", encoding="utf-8") as fault_stream:
        faulthandler.enable(fault_stream)
        try:
            asyncio.run(MemorySoakHarness(args).run())
        except KeyboardInterrupt:
            pass
        finally:
            faulthandler.disable()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
