"""Long production-like soak of one BattleSpades server with a full bot lobby.

The soak runs the REAL ``BattleSpadesServer`` (ENet host, fixed-step 60 Hz
runtime, bot worker process, plugins, anticheat, conduct) in this process,
exactly like ``server/launcher.py`` (gc.freeze, faulthandler, bounded
logging), with only these soak-specific config overrides:

* its own UDP port (default 27020), revival/Steam off, own log/report files;
* a full bot lobby (``[bots]`` backfill, default 14 bots, process worker);
* short round clocks for every mode (``[modes.<code>].time_limit``);
* an explicit map rotation (default: every ``maps/*.vxl``), so the natural
  end-of-round map vote walks the whole catalog.

A driver coroutine switches the game mode through the production
``MatchTransitionService.request_mode_change`` (the ``/mode`` admin path)
after every ``--rounds-per-mode`` natural map rollovers, so map rollovers,
mode rollovers and in-mode sub-rounds (VIP/Zombie) all happen repeatedly.
After ``--minutes`` an optional sweep (``--sweep``) loads every map the
rotation did not reach, so every VXL is loaded and unloaded at least once.

Nothing here changes gameplay code. Instrumentation is read-only except for
thin timing wrappers around spawn selection, the rollover, and the
anticheat report tick (to count failures that module logs at DEBUG only).

Outputs (``--out``, default ``logs/soak/<timestamp>``):

* ``samples.jsonl``   - one row per ``--sample-seconds``: RSS of the server
  and bot worker, Python allocated blocks, asyncio tasks, threads, tick
  p50/p95/p99/max for the window, effective tick rate, event-loop lag,
  subsystem p99s, queue lengths, peer reliable backlog, worker status,
  spawn-selection cost, player/bot counts, anticheat counters.
* ``events.jsonl``    - transitions (map/mode, duration, result), segment
  summaries (tick percentiles per map+mode segment), log captures
  (WARNING+, every ``anticheat``/``conduct`` record, first tracebacks).
* ``server.log``      - the normal server log (rotated, 50 backups).
* ``fault.log``       - faulthandler output (native crashes).
* ``summary.json``    - written at exit by the harness; run
  ``scripts/soak/analyze_soak.py <out>`` for the Markdown report.

Examples::

    py -3.12 scripts/soak/soak_server.py --minutes 90 --sweep
    py -3.12 scripts/soak/soak_server.py --minutes 5 --round-seconds 60 \
        --modes tdm,ctf --bots 8 --out logs/soak/quick

Stop early with Ctrl+C (or create ``<out>/STOP``); the harness always shuts
the server down cleanly and writes ``summary.json``.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import copy
import faulthandler
import gc
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

from server.config import load_config  # noqa: E402
from server.logging_runtime import configure_logging  # noqa: E402

DEFAULT_MODES = ("tdm", "ctf", "oc", "dia", "tc", "mh", "dem", "zom", "vip")
ALL_MODE_CODES = (
    "tdm", "ctf", "cctf", "arena", "vip", "zom", "mh", "tc", "dia", "dem", "oc",
)


def _percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * pct))
    return float(ordered[max(0, min(index, len(ordered) - 1))])


def _tick_stats(values):
    return {
        "n": len(values),
        "avg": (sum(values) / len(values)) if values else 0.0,
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values) if values else 0.0,
        "over_10ms": sum(1 for v in values if v > 10.0),
        "over_16ms": sum(1 for v in values if v > 16.7),
    }


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self._stream = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, row: dict) -> None:
        text = json.dumps(row, default=str)
        with self._lock:
            self._stream.write(text + "\n")
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            self._stream.close()


class CaptureHandler(logging.Handler):
    """Count every record and keep the interesting ones (cheap, lock-light).

    Runs on the thread that logs (usually the gameplay thread) BEFORE the
    bounded queue, so it sees records even if the file sink drops them.
    """

    def __init__(self, events: JsonlWriter, clock) -> None:
        super().__init__(level=logging.DEBUG)
        self.events = events
        self.clock = clock
        self.by_level: Counter = Counter()
        self.by_logger_level: Counter = Counter()
        self.warning_keys: Counter = Counter()
        self.anticheat: Counter = Counter()
        self.conduct: Counter = Counter()
        self.tracebacks: dict[str, dict] = {}
        self.traceback_counts: Counter = Counter()

    @staticmethod
    def _key(record: logging.LogRecord) -> str:
        template = str(record.msg)
        return f"{record.name}:{template[:120]}"

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            level = record.levelname
            self.by_level[level] += 1
            self.by_logger_level[f"{record.name}|{level}"] += 1
            name = record.name
            is_ac = name == "anticheat" or name.startswith("server.anticheat")
            is_conduct = name.startswith("server.conduct") or name == "conduct"
            if record.levelno < logging.WARNING and not (is_ac or is_conduct):
                return
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001
                message = str(record.msg)
            key = self._key(record)
            if record.levelno >= logging.WARNING:
                self.warning_keys[f"{level}|{key}"] += 1
            if is_ac:
                self.anticheat[message.split(" player=")[0][:80]] += 1
            if is_conduct:
                self.conduct[message.split(" source=")[0].split(" player=")[0][:80]] += 1
            row = {
                "t": self.clock(),
                "wall": time.time(),
                "kind": "log",
                "level": level,
                "logger": name,
                "message": message[:2000],
            }
            if record.exc_info:
                text = "".join(traceback.format_exception(*record.exc_info))
                self.traceback_counts[key] += 1
                if key not in self.tracebacks:
                    self.tracebacks[key] = {"first_t": row["t"], "text": text[-6000:]}
                # Keep only the first 3 tracebacks per key in the event file.
                if self.traceback_counts[key] <= 3:
                    row["traceback"] = text[-6000:]
            # Always keep anticheat/conduct; cap repeated warnings per key.
            if is_ac or is_conduct or self.warning_keys[f"{level}|{key}"] <= 20:
                self.events.write(row)
        except Exception:  # noqa: BLE001 - a diagnostics handler must never raise
            pass


class SoakHarness:
    def __init__(self, args) -> None:
        self.args = args
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.samples = JsonlWriter(self.out / "samples.jsonl")
        self.events = JsonlWriter(self.out / "events.jsonl")
        self.t0 = time.monotonic()
        self.server = None
        self.proc = psutil.Process(os.getpid())
        self.stop_requested = False
        self.stop_reason = ""
        # Tick samples drained from RuntimeMetrics.
        self.window_ticks: list[float] = []
        self.segment_ticks: list[float] = []
        self.segment_key = None
        self.segment_started = 0.0
        self.segment_index = 0
        # Spawn timing.
        self.spawn_ms: dict[str, list[float]] = defaultdict(list)
        self.spawn_totals: Counter = Counter()
        self.spawn_max: dict[str, float] = defaultdict(float)
        self.spawn_failures: Counter = Counter()
        # Transitions.
        self.transitions: list[dict] = []
        self.natural_rollovers_since_mode = 0
        self.mode_index = 0
        self.last_transition_at = 0.0
        self.round_started_at = 0.0
        self.visited_maps: set[str] = set()
        self.visited_modes: set[str] = set()
        self.mode_requests: list[dict] = []
        self.anticheat_report_failures = 0
        self.loop_lag_max = 0.0
        self.loop_lag_samples: list[float] = []
        self.last_loop_count = 0
        self.last_sample_at = None
        self.last_cpu = None
        self.peak_rss = 0
        self.peak_worker_rss = 0
        self.worker_pids: set[int] = set()
        self.worker_restart_seen = 0
        self.kicks: list[dict] = []
        self.capture: CaptureHandler | None = None
        # Optional profilers (--profile-entities / --profile-stalls).
        self.entity_prof: dict[str, list[float]] = {}
        self.heartbeat = time.perf_counter()
        self.main_thread_id = threading.get_ident()
        self.stall_stacks: Counter = Counter()
        self.stall_examples: dict[str, dict] = {}

    # ------------------------------------------------------------------ util
    def now(self) -> float:
        return round(time.monotonic() - self.t0, 3)

    def event(self, kind: str, **fields) -> None:
        row = {"t": self.now(), "wall": time.time(), "kind": kind}
        row.update(fields)
        self.events.write(row)

    def current_map(self) -> str:
        return str(getattr(self.server.config, "default_map", "?"))

    def current_mode(self) -> str:
        code = getattr(self.server.config, "default_mode", None) or getattr(
            getattr(self.server, "mode", None), "mode_code", "?"
        )
        return str(code).lower()

    # --------------------------------------------------------------- config
    def build_config(self):
        args = self.args
        config = copy.deepcopy(load_config(Path(args.config)))
        config.port = int(args.port)
        config.name = f"{config.name} [SOAK]"
        config.max_players = max(int(config.max_players), args.bots + 4)
        modes = [m.strip().lower() for m in args.modes.split(",") if m.strip()]
        self.modes = modes
        config.default_mode = modes[0]
        maps = sorted(p.stem for p in (ROOT / config.maps_path).glob("*.vxl"))
        if args.maps:
            wanted = [m.strip() for m in args.maps.split(",") if m.strip()]
            maps = [m for m in wanted if m in maps]
        self.catalog = maps
        config.map_rotation = list(maps)
        config.default_map = args.start_map or maps[0]
        # Bots: full lobby, production isolation (process worker).
        config.bots.enabled = True
        config.bots.configured = True
        config.bots.population_mode = "backfill"
        config.bots.fill_target = int(args.bots)
        config.bots.max_bots = int(args.bots)
        config.bots.worker = args.worker
        # Short round clocks for every mode.
        settings = copy.deepcopy(dict(getattr(config, "mode_settings", {}) or {}))
        for code in ALL_MODE_CODES:
            overlay = dict(settings.get(code, {}) or {})
            overlay["time_limit"] = float(args.round_seconds)
            settings[code] = overlay
        config.mode_settings = settings
        # Isolation: no public registration, own logs/reports.
        try:
            config.revival.enabled = False
        except AttributeError:
            pass
        try:
            config.steam.enabled = False
        except AttributeError:
            pass
        config.log_file = str((self.out / "server.log").resolve())
        config.log_console = False
        config.log_backup_count = 50
        config.log_max_bytes = 32 * 1024 * 1024
        config.bans_path = str((self.out / "bans.json").resolve())
        try:
            config.anticheat.report_path = str((self.out / "anticheat.jsonl").resolve())
        except AttributeError:
            pass
        return config

    # -------------------------------------------------------- instrumentation
    def instrument(self) -> None:
        server = self.server
        harness = self

        # Spawn selection cost (module functions are imported per call).
        import server.spawn_selection as spawn_selection

        def timed(name, fn):
            def wrapper(*a, **k):
                start = time.perf_counter()
                try:
                    return fn(*a, **k)
                except Exception:
                    harness.spawn_failures[name] += 1
                    raise
                finally:
                    elapsed = (time.perf_counter() - start) * 1000.0
                    harness.spawn_ms[name].append(elapsed)
                    harness.spawn_totals[name] += 1
                    if elapsed > harness.spawn_max[name]:
                        harness.spawn_max[name] = elapsed
            wrapper.__wrapped__ = fn
            return wrapper

        for name in ("choose_team_spawn", "rescue_spawn", "emergency_spawn"):
            original = getattr(spawn_selection, name, None)
            if callable(original):
                setattr(spawn_selection, name, timed(name, original))
        # WorldManager is replaced on map change? Patch the class method.
        from server.world_manager import WorldManager

        original_gsp = WorldManager.get_spawn_point
        WorldManager.get_spawn_point = timed("world.get_spawn_point", original_gsp)

        # Anticheat report tick swallows exceptions at DEBUG: count them.
        import server.anticheat_report as report

        original_tick = report._tick

        def counted_tick(*a, **k):
            try:
                return original_tick(*a, **k)
            except Exception as exc:
                harness.anticheat_report_failures += 1
                if harness.anticheat_report_failures <= 3:
                    harness.event(
                        "anticheat_report_failure",
                        error=repr(exc),
                        traceback=traceback.format_exc()[-4000:],
                    )
                raise

        report._tick = counted_tick

        # Kicks/disconnects: wrap Player.disconnect for humans.
        from server.player import Player

        original_disconnect = getattr(Player, "disconnect", None)
        if callable(original_disconnect):
            def disconnect(player_self, *a, **k):
                try:
                    harness.kicks.append({
                        "t": harness.now(),
                        "player": getattr(player_self, "name", "?"),
                        "is_bot": bool(getattr(player_self, "is_bot", False)),
                        "args": [str(v) for v in a],
                    })
                    harness.event(
                        "player_disconnect_call",
                        player=getattr(player_self, "name", "?"),
                        is_bot=bool(getattr(player_self, "is_bot", False)),
                        args=[str(v) for v in a],
                        stack="".join(traceback.format_stack(limit=8)[:-1])[-2500:],
                    )
                except Exception:  # noqa: BLE001
                    pass
                return original_disconnect(player_self, *a, **k)
            Player.disconnect = disconnect

        # Transitions: time every rollover and restart.
        transition = server.match_transition
        original_rollover = transition._rollover
        original_restart = transition.restart_round

        async def rollover(*, map_name, mode_name, candidate_world):
            before_map, before_mode = harness.current_map(), harness.current_mode()
            start = time.perf_counter()
            harness.event("rollover_begin", from_map=before_map, from_mode=before_mode,
                          to_map=map_name, to_mode=mode_name)
            ok = False
            message = ""
            try:
                result = await original_rollover(
                    map_name=map_name, mode_name=mode_name,
                    candidate_world=candidate_world,
                )
                ok = bool(getattr(result, "ok", False))
                message = str(getattr(result, "message", ""))
                return result
            except Exception as exc:
                message = f"EXC {exc!r}"
                raise
            finally:
                try:
                    harness.record_rollover(start, before_map, before_mode,
                                            map_name, mode_name, ok, message)
                except Exception:  # noqa: BLE001 - never break the rollover
                    harness.event("harness_error", where="rollover",
                                  traceback=traceback.format_exc()[-3000:])

        async def restart_round():
            start = time.perf_counter()
            result = await original_restart()
            try:
                row = {
                    "t": harness.now(), "type": "restart", "from_map": harness.current_map(),
                    "from_mode": harness.current_mode(), "to_map": harness.current_map(),
                    "to_mode": harness.current_mode(),
                    "ok": bool(getattr(result, "ok", False)),
                    "message": str(getattr(result, "message", "")),
                    "duration_ms": round((time.perf_counter() - start) * 1000.0, 1),
                }
                harness.transitions.append(row)
                harness.event("transition", **{k: v for k, v in row.items() if k != "t"})
                harness.round_started_at = time.monotonic()
            except Exception:  # noqa: BLE001
                pass
            return result

        transition._rollover = rollover
        transition.restart_round = restart_round

        # VXL preflight (runs in a worker thread before the rollover).
        original_load = transition._load_world_candidate

        def load_candidate(map_name, mode_name):
            start = time.perf_counter()
            error = None
            try:
                return original_load(map_name, mode_name)
            except Exception as exc:
                error = repr(exc)
                raise
            finally:
                try:
                    harness.event("map_load", map=map_name, mode=mode_name,
                                  ms=round((time.perf_counter() - start) * 1000.0, 1),
                                  error=error)
                except Exception:  # noqa: BLE001
                    pass

        transition._load_world_candidate = load_candidate

    def record_rollover(self, start, before_map, before_mode, map_name, mode_name, ok, message):
        """Log one rollover row and advance the mode-rotation counter."""

        harness = self
        server = self.server
        duration = (time.perf_counter() - start) * 1000.0
        kind = "mode" if str(mode_name).lower() != before_mode else "map"
        row = {
            "t": harness.now(), "type": kind, "from_map": before_map,
            "from_mode": before_mode, "to_map": map_name,
            "to_mode": str(mode_name).lower(), "ok": ok,
            "message": message, "duration_ms": round(duration, 1),
            "bots_after": len(getattr(server.bots, "bots", []) or [])
            if server.bots is not None else 0,
            "rss_mb": round(harness.proc.memory_info().rss / 2**20, 1),
        }
        harness.transitions.append(row)
        harness.event("transition", **{k: v for k, v in row.items() if k != "t"})
        harness.last_transition_at = time.monotonic()
        harness.round_started_at = time.monotonic()
        if ok and kind == "map":
            harness.natural_rollovers_since_mode += 1
        if ok and kind == "mode":
            harness.natural_rollovers_since_mode = 0

    # --------------------------------------------------------------- sampling
    def drain_ticks(self) -> None:
        samples = self.server.metrics.tick_samples_ms
        if not samples:
            return
        values = list(samples)
        samples.clear()
        self.window_ticks.extend(values)
        self.segment_ticks.extend(values)

    def close_segment(self, reason: str) -> None:
        if self.segment_key is None:
            return
        stats = _tick_stats(self.segment_ticks)
        self.event(
            "segment",
            index=self.segment_index,
            map=self.segment_key[0],
            mode=self.segment_key[1],
            seconds=round(time.monotonic() - self.segment_started, 1),
            reason=reason,
            ticks=stats,
            rss_mb_end=round(self.proc.memory_info().rss / 2**20, 1),
        )
        self.segment_ticks = []
        self.segment_index += 1

    def check_segment(self) -> None:
        key = (self.current_map(), self.current_mode())
        if key != self.segment_key:
            self.drain_ticks()
            self.close_segment("changed")
            self.segment_key = key
            self.segment_started = time.monotonic()
            self.visited_maps.add(key[0])
            self.visited_modes.add(key[1])

    def worker_status(self) -> dict:
        bots = getattr(self.server, "bots", None)
        if bots is None:
            return {}
        try:
            status = bots.status()
        except Exception as exc:  # noqa: BLE001
            return {"error": repr(exc)}
        fields = {}
        for name in (
            "running", "process_id", "restarts", "stalled_restarts",
            "planned_recycles", "crash_restarts",
            "intent_silence_seconds", "queued_frames", "queued_intents",
            "pending_terrain_cells", "dropped_frames", "dropped_intents",
            "snapshot_required", "snapshot_rejections",
        ):
            fields[name] = getattr(status, name, None)
        return fields

    def sample(self) -> None:
        server = self.server
        self.drain_ticks()
        now = time.monotonic()
        rss = self.proc.memory_info().rss
        self.peak_rss = max(self.peak_rss, rss)
        cpu = self.proc.cpu_times()
        cpu_total = cpu.user + cpu.system
        worker = self.worker_status()
        worker_rss = None
        pid = worker.get("process_id")
        if pid:
            self.worker_pids.add(int(pid))
            try:
                worker_rss = psutil.Process(int(pid)).memory_info().rss
                self.peak_worker_rss = max(self.peak_worker_rss, worker_rss)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                worker_rss = None
        loop_count = int(getattr(server, "loop_count", 0))
        if self.last_sample_at is not None:
            dt = now - self.last_sample_at
            tick_hz = (loop_count - self.last_loop_count) / dt if dt > 0 else 0.0
            cpu_pct = (cpu_total - self.last_cpu) / dt * 100.0 if dt > 0 else 0.0
        else:
            tick_hz = cpu_pct = None
        self.last_sample_at = now
        self.last_loop_count = loop_count
        self.last_cpu = cpu_total

        metrics = server.metrics
        subsystems = {}
        for name, values in list(metrics.subsystem_samples_ms.items()):
            vals = list(values)
            values.clear()
            if vals:
                subsystems[name] = {
                    "avg": round(sum(vals) / len(vals), 3),
                    "p99": round(_percentile(vals, 0.99), 3),
                    "max": round(max(vals), 3),
                }
        counters = {
            name: getattr(metrics, name)
            for name in (
                "dropped_ingame_packets", "skipped_plugin_callbacks",
                "skipped_entity_ticks", "map_mutation_overflows",
                "dropped_mode_events", "committed_world_mutations",
                "rejected_world_mutations", "expired_world_mutations",
                "world_mutation_queue_peak", "terrain_repair_cells",
                "terrain_repair_sends", "terrain_repair_queue_peak",
                "dropped_terrain_repairs", "failed_terrain_repair_sends",
                "bot_perception_entity_overflow",
            )
            if hasattr(metrics, name)
        }
        counters["subsystem_failures"] = dict(getattr(metrics, "subsystem_failures", {}) or {})
        try:
            counters["logging_dropped"] = int(server.telemetry.dropped_log_records)
        except Exception:  # noqa: BLE001
            pass

        queues = {}
        for name, value in list(vars(server).items()):
            if isinstance(value, (list, dict, set)) and name.startswith("_"):
                continue
            try:
                from collections import deque
                if isinstance(value, deque):
                    queues[name] = len(value)
            except Exception:  # noqa: BLE001
                pass
        for holder_name in ("world_mutations", "terrain_repair", "prefab_actions"):
            holder = getattr(server, holder_name, None)
            if holder is None:
                continue
            for name, value in list(vars(holder).items()):
                from collections import deque
                if isinstance(value, deque):
                    queues[f"{holder_name}.{name}"] = len(value)

        peers = []
        for connection in list(getattr(server, "connections", {}).values()):
            peer = getattr(connection, "peer", None)
            if peer is None:
                continue
            try:
                peers.append({
                    "name": getattr(getattr(connection, "player", None), "name", None),
                    "in_game": bool(getattr(connection, "in_game", False)),
                    "reliable_in_transit": int(peer.reliableDataInTransit),
                    "rtt": int(peer.roundTripTime),
                    "loss": int(peer.packetLoss),
                    "throttle": int(peer.packetThrottle),
                })
            except Exception:  # noqa: BLE001
                pass

        players = list(getattr(server, "players", {}).values())
        bots = [p for p in players if getattr(p, "is_bot", False)]
        humans = [p for p in players if not getattr(p, "is_bot", False)]
        ac_bots: Counter = Counter()
        ac_humans: Counter = Counter()
        for player in players:
            counts = getattr(player, "anticheat_counts", None)
            if counts:
                (ac_bots if getattr(player, "is_bot", False) else ac_humans).update(counts)
        afk = {}
        for player in humans:
            state = getattr(player, "_conduct_afk", None)
            grief = getattr(player, "_conduct_grief", None)
            afk[str(getattr(player, "name", "?"))] = {
                "idle": round(float(getattr(state, "idle_seconds", 0.0)), 1) if state else None,
                "warned": bool(getattr(state, "warned", False)) if state else None,
                "grief_points": round(float(getattr(grief, "points", 0.0)), 2) if grief else None,
                "alive": bool(getattr(player, "alive", False)),
                "team": int(getattr(player, "team", -1)),
            }
        spawn = {}
        for name, values in self.spawn_ms.items():
            if values:
                spawn[name] = {
                    "n": len(values),
                    "p50": round(_percentile(values, 0.5), 3),
                    "p99": round(_percentile(values, 0.99), 3),
                    "max": round(max(values), 3),
                }
            self.spawn_ms[name] = []

        ticks = _tick_stats(self.window_ticks)
        self.window_ticks = []
        lag = list(self.loop_lag_samples)
        self.loop_lag_samples = []
        mode = getattr(server, "mode", None)
        row = {
            "t": self.now(),
            "wall": time.time(),
            "map": self.current_map(),
            "mode": self.current_mode(),
            "mode_ended": bool(getattr(mode, "ended", False)),
            "transition_busy": bool(getattr(server.match_transition, "in_progress", False)),
            "rss_mb": round(rss / 2**20, 1),
            "worker_rss_mb": round(worker_rss / 2**20, 1) if worker_rss else None,
            "py_blocks": sys.getallocatedblocks(),
            "gc_counts": gc.get_count(),
            "asyncio_tasks": len(asyncio.all_tasks()),
            "threads": threading.active_count(),
            "thread_names": sorted(t.name for t in threading.enumerate()),
            "handles": getattr(self.proc, "num_handles", lambda: None)(),
            "cpu_pct": round(cpu_pct, 1) if cpu_pct is not None else None,
            "tick_hz": round(tick_hz, 2) if tick_hz is not None else None,
            "ticks": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in ticks.items()},
            "loop_lag_ms": {
                "p99": round(_percentile(lag, 0.99), 2),
                "max": round(max(lag), 2) if lag else 0.0,
            },
            "subsystems": subsystems,
            "counters": counters,
            "queues": queues,
            "peers": peers,
            "players": len(players),
            "bots": len(bots),
            "bots_alive": sum(1 for p in bots if getattr(p, "alive", False)),
            "humans": len(humans),
            "worker": worker,
            "spawn": spawn,
            "anticheat_bots": dict(ac_bots),
            "anticheat_humans": dict(ac_humans),
            "anticheat_report_failures": self.anticheat_report_failures,
            "humans_conduct": afk,
            "log_levels": dict(self.capture.by_level) if self.capture else {},
            "entity_profile": self.drain_entity_profile(),
        }
        self.samples.write(row)
        restarts = int(worker.get("restarts") or 0) + int(worker.get("stalled_restarts") or 0)
        if restarts > self.worker_restart_seen:
            self.event("worker_restart", status=worker)
            self.worker_restart_seen = restarts

    # ---------------------------------------------------------------- driver
    def instrument_entities(self) -> None:
        """Time every EntityBehavior hook per class (``--profile-entities``)."""

        from server.entities.behaviors import EntityBehavior
        import server.entities as entities_pkg
        import importlib
        import pkgutil

        for info in pkgutil.iter_modules(entities_pkg.__path__):
            try:
                importlib.import_module(f"server.entities.{info.name}")
            except Exception:  # noqa: BLE001
                pass
        harness = self
        seen = set()
        stack = [EntityBehavior]
        while stack:
            cls = stack.pop()
            for sub in cls.__subclasses__():
                if sub not in seen:
                    seen.add(sub)
                    stack.append(sub)
        for cls in seen:
            for hook in ("on_tick", "on_touch"):
                original = cls.__dict__.get(hook)
                if original is None:
                    continue

                def make(original=original, key=f"{cls.__name__}.{hook}"):
                    def wrapper(self_b, ent, *a, **k):
                        start = time.perf_counter()
                        try:
                            return original(self_b, ent, *a, **k)
                        finally:
                            ms = (time.perf_counter() - start) * 1000.0
                            row = harness.entity_prof.setdefault(key, [0, 0.0, 0.0])
                            row[0] += 1
                            row[1] += ms
                            if ms > row[2]:
                                row[2] = ms
                            if ms > 5.0:
                                harness.event(
                                    "slow_entity_hook", hook=key, ms=round(ms, 2),
                                    entity_kind=str(getattr(ent, "kind", "")),
                                    entity_type=getattr(ent, "entity_type", None),
                                    pos=[round(float(getattr(ent, c, 0.0)), 1) for c in "xyz"],
                                    map=harness.current_map(), mode=harness.current_mode(),
                                )
                    return wrapper
                setattr(cls, hook, make())

    def drain_entity_profile(self) -> dict:
        out = {k: {"n": v[0], "total_ms": round(v[1], 2), "max_ms": round(v[2], 2)}
               for k, v in self.entity_prof.items() if v[0]}
        self.entity_prof = {}
        return out

    def stall_watchdog(self) -> None:
        """Thread: dump the main-thread stack whenever the loop stalls."""

        threshold = self.args.stall_ms / 1000.0
        captured_for = None
        while not self.stop_requested:
            time.sleep(0.02)
            stale = time.perf_counter() - self.heartbeat
            if stale < threshold:
                captured_for = None
                continue
            beat = self.heartbeat
            if captured_for == beat:
                continue
            captured_for = beat
            frame = sys._current_frames().get(self.main_thread_id)
            if frame is None:
                continue
            text = "".join(traceback.format_stack(frame, limit=25))
            key = " <- ".join(
                f"{f.name}@{Path(f.filename).name}:{f.lineno}"
                for f in traceback.extract_stack(frame, limit=6)[::-1]
            )
            self.stall_stacks[key] += 1
            if key not in self.stall_examples:
                self.stall_examples[key] = {"t": self.now(), "stack": text[-5000:]}
            self.event("loop_stall", stale_ms=round(stale * 1000.0, 1), where=key,
                       map=self.current_map(), stack=text[-3000:])

    async def loop_lag_probe(self) -> None:
        interval = 0.05
        while not self.stop_requested:
            start = time.perf_counter()
            await asyncio.sleep(interval)
            self.heartbeat = time.perf_counter()
            lag = max(0.0, (self.heartbeat - start - interval) * 1000.0)
            self.loop_lag_samples.append(lag)
            if lag > self.loop_lag_max:
                self.loop_lag_max = lag

    async def driver(self) -> None:
        args = self.args
        server = self.server
        while not server.running:
            await asyncio.sleep(0.1)
        self.event("server_running", map=self.current_map(), mode=self.current_mode(),
                   catalog=self.catalog, modes=self.modes)
        self.round_started_at = time.monotonic()
        self.last_transition_at = time.monotonic()
        next_sample = time.monotonic()
        deadline = self.t0 + args.minutes * 60.0
        stuck_limit = args.round_seconds * 2 + 180.0
        while not self.stop_requested:
            await asyncio.sleep(1.0)
            self.check_segment()
            now = time.monotonic()
            if now >= next_sample:
                next_sample = now + args.sample_seconds
                try:
                    self.sample()
                except Exception as exc:  # noqa: BLE001
                    self.event("sample_error", error=repr(exc),
                               traceback=traceback.format_exc()[-3000:])
            if (self.out / "STOP").exists():
                self.stop_reason = "STOP file"
                break
            if now >= deadline:
                self.stop_reason = "duration reached"
                break
            transition = server.match_transition
            mode = server.mode
            busy = bool(getattr(transition, "in_progress", False)) or bool(
                getattr(transition, "_preparing_map", False)
            ) or getattr(transition, "_request_task", None) is not None
            ended = bool(getattr(mode, "ended", False)) or bool(
                getattr(mode, "_end_sequence_running", False)
            )
            if now - self.last_transition_at > stuck_limit and not busy:
                self.event("stuck_round", map=self.current_map(), mode=self.current_mode(),
                           since=round(now - self.last_transition_at, 1), ended=ended)
                self.last_transition_at = now  # avoid spamming
                self.request_next_mode(reason="stuck")
                continue
            if (
                self.natural_rollovers_since_mode >= args.rounds_per_mode
                and not busy and not ended
                and now - self.round_started_at >= args.mode_change_delay
            ):
                self.request_next_mode(reason="rotation")
        if args.sweep and self.stop_reason != "STOP file":
            await self.sweep()

    def request_next_mode(self, reason: str) -> None:
        current = self.current_mode()
        for _ in range(len(self.modes)):
            self.mode_index = (self.mode_index + 1) % len(self.modes)
            if self.modes[self.mode_index] != current:
                break
        target = self.modes[self.mode_index]
        result = self.server.match_transition.request_mode_change(target)
        row = {"target": target, "from": current, "reason": reason,
               "ok": bool(result.ok), "message": result.message}
        self.mode_requests.append(row)
        self.event("mode_request", **row)
        if result.ok:
            # Prevent a second request before the rollover lands.
            self.natural_rollovers_since_mode = -10**6
        else:
            self.natural_rollovers_since_mode = self.args.rounds_per_mode

    async def wait_transition_idle(self, timeout: float) -> bool:
        transition = self.server.match_transition
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            busy = bool(getattr(transition, "in_progress", False)) or bool(
                getattr(transition, "_preparing_map", False)
            ) or getattr(transition, "_request_task", None) is not None
            mode = self.server.mode
            ended = bool(getattr(mode, "_end_sequence_running", False))
            if not busy and not ended:
                return True
            await asyncio.sleep(0.5)
        return False

    async def sweep(self) -> None:
        """Load every map the rotation missed (and each mode not yet seen)."""

        missing_maps = [m for m in self.catalog if m not in self.visited_maps]
        missing_modes = [m for m in self.modes if m not in self.visited_modes]
        self.event("sweep_begin", missing_maps=missing_maps, missing_modes=missing_modes)
        for target in missing_modes:
            if not await self.wait_transition_idle(120.0):
                self.event("sweep_timeout", target=target)
                break
            result = self.server.match_transition.request_mode_change(target)
            self.event("sweep_request", target=target, type="mode", ok=result.ok,
                       message=result.message)
            await asyncio.sleep(self.args.sweep_dwell)
            self.check_segment()
            self.sample()
        for target in missing_maps:
            if not await self.wait_transition_idle(120.0):
                self.event("sweep_timeout", target=target)
                break
            result = self.server.match_transition.request_map_change(target)
            self.event("sweep_request", target=target, type="map", ok=result.ok,
                       message=result.message)
            await asyncio.sleep(self.args.sweep_dwell)
            self.check_segment()
            self.sample()
        await self.wait_transition_idle(60.0)
        self.check_segment()
        self.event("sweep_end", unvisited=[m for m in self.catalog if m not in self.visited_maps])

    # ------------------------------------------------------------------ main
    async def run(self) -> None:
        from server.main import BattleSpadesServer
        from server.telemetry import TelemetryService

        config = self.build_config()
        logging_runtime = configure_logging(config, self.out)
        self.capture = CaptureHandler(self.events, self.now)
        logging.getLogger().addHandler(self.capture)
        logging.getLogger("BattleSpades").info(
            "SOAK harness: port=%d bots=%d round=%ss modes=%s maps=%d",
            config.port, self.args.bots, self.args.round_seconds,
            ",".join(self.modes), len(self.catalog),
        )
        gc.collect()
        gc.freeze()
        self.server = BattleSpadesServer(
            config, telemetry=TelemetryService(logging_runtime)
        )
        self.instrument()
        if self.args.profile_entities:
            self.instrument_entities()
        self.main_thread_id = threading.get_ident()
        if self.args.profile_stalls:
            threading.Thread(target=self.stall_watchdog, name="soak-stall-watchdog",
                             daemon=True).start()
        loop = asyncio.get_running_loop()

        def request_stop(*_):
            self.stop_requested = True
            self.stop_reason = self.stop_reason or "signal"

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_stop)
            except NotImplementedError:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(request_stop))

        server_task = asyncio.create_task(self.server.start(), name="soak-server")
        lag_task = asyncio.create_task(self.loop_lag_probe(), name="soak-lag")
        driver_task = asyncio.create_task(self.driver(), name="soak-driver")
        started = time.time()
        try:
            done, _pending = await asyncio.wait(
                {server_task, driver_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if server_task in done:
                exc = server_task.exception()
                self.stop_reason = f"server task ended: {exc!r}"
                self.event("server_task_ended", error=repr(exc))
                raise RuntimeError(self.stop_reason) from exc
            # Propagate driver failures after writing the shutdown summary;
            # silently gathering them used to turn a broken soak into exit 0.
            await driver_task
        finally:
            self.stop_requested = True
            try:
                self.drain_ticks()
                self.close_segment("shutdown")
                self.sample()
            except Exception:  # noqa: BLE001
                pass
            stop_started = time.perf_counter()
            try:
                await asyncio.wait_for(self.server.stop(), timeout=60.0)
            except Exception as exc:  # noqa: BLE001
                self.event("stop_error", error=repr(exc))
            stop_ms = (time.perf_counter() - stop_started) * 1000.0
            for task in (driver_task, lag_task, server_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(driver_task, lag_task, server_task, return_exceptions=True)
            summary = {
                "started": started,
                "elapsed_minutes": round((time.monotonic() - self.t0) / 60.0, 2),
                "stop_reason": self.stop_reason,
                "stop_ms": round(stop_ms, 1),
                "args": vars(self.args),
                "catalog": self.catalog,
                "visited_maps": sorted(self.visited_maps),
                "unvisited_maps": [m for m in self.catalog if m not in self.visited_maps],
                "visited_modes": sorted(self.visited_modes),
                "transitions": self.transitions,
                "mode_requests": self.mode_requests,
                "peak_rss_mb": round(self.peak_rss / 2**20, 1),
                "peak_worker_rss_mb": round(self.peak_worker_rss / 2**20, 1),
                "worker_pids": sorted(self.worker_pids),
                "spawn_totals": dict(self.spawn_totals),
                "spawn_max_ms": {k: round(v, 3) for k, v in self.spawn_max.items()},
                "spawn_failures": dict(self.spawn_failures),
                "loop_lag_max_ms": round(self.loop_lag_max, 2),
                "log_levels": dict(self.capture.by_level),
                "log_by_logger_level": dict(self.capture.by_logger_level),
                "warning_keys": dict(self.capture.warning_keys.most_common(200)),
                "anticheat_messages": dict(self.capture.anticheat),
                "conduct_messages": dict(self.capture.conduct),
                "traceback_counts": dict(self.capture.traceback_counts),
                "first_tracebacks": self.capture.tracebacks,
                "anticheat_report_failures": self.anticheat_report_failures,
                "disconnect_calls": self.kicks[-200:],
                "stall_stacks": dict(self.stall_stacks.most_common(30)),
                "stall_examples": self.stall_examples,
            }
            (self.out / "summary.json").write_text(
                json.dumps(summary, indent=2, default=str), encoding="utf-8"
            )
            logging.getLogger().removeHandler(self.capture)
            logging_runtime.stop()
            self.samples.close()
            self.events.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=str(ROOT / "config.toml"))
    parser.add_argument("--port", type=int, default=27020)
    parser.add_argument("--bots", type=int, default=14)
    parser.add_argument("--worker", choices=("process", "thread"), default="process")
    parser.add_argument("--round-seconds", type=float, default=180.0)
    parser.add_argument("--modes", default=",".join(DEFAULT_MODES))
    parser.add_argument("--maps", default="", help="comma list; default every maps/*.vxl")
    parser.add_argument("--start-map", default="")
    parser.add_argument("--rounds-per-mode", type=int, default=2,
                        help="natural map rollovers before the driver switches mode")
    parser.add_argument("--mode-change-delay", type=float, default=30.0,
                        help="seconds into a round before a mode switch is requested")
    parser.add_argument("--minutes", type=float, default=90.0)
    parser.add_argument("--sample-seconds", type=float, default=15.0)
    parser.add_argument("--sweep", action="store_true",
                        help="after the timed soak, load every unvisited map/mode")
    parser.add_argument("--sweep-dwell", type=float, default=25.0)
    parser.add_argument("--profile-entities", action="store_true",
                        help="time every EntityBehavior on_tick/on_touch per class")
    parser.add_argument("--profile-stalls", action="store_true",
                        help="watchdog thread dumps the main stack on event-loop stalls")
    parser.add_argument("--stall-ms", type=float, default=120.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    if not args.out:
        args.out = str(ROOT / "logs" / "soak" / time.strftime("%Y%m%d-%H%M%S"))
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)  # maps/prefabs/plugins paths are relative to the repo
    with (out / "fault.log").open("a", encoding="utf-8") as fault_stream:
        faulthandler.enable(fault_stream)
        try:
            asyncio.run(SoakHarness(args).run())
        except KeyboardInterrupt:
            pass
        finally:
            faulthandler.disable()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
