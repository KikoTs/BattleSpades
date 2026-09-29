"""Turn one soak output directory into a Markdown report (stdout or --md).

Reads ``samples.jsonl``, ``events.jsonl`` and ``summary.json`` written by
``scripts/soak/soak_server.py`` (``summary.json`` is optional, so a still
running soak can be inspected) and prints:

* memory: server RSS / worker RSS / Python blocks over time, least-squares
  growth per hour, and RSS at every rollover (growth per round);
* tick time: per-window p50/p99/max over time (first vs last quarter, creep),
  tick-rate floor, event-loop lag, and p99/max per map+mode segment;
* queues, peer reliable backlog, worker status/restarts, spawn cost;
* transitions (map/mode/restart) with durations, VXL load times, failures,
  stuck rounds, and maps never visited;
* log levels, WARNING+/ERROR keys with counts, first tracebacks;
* anticheat counters split bots/humans and ``[conduct]`` records.

Example::

    py -3.12 scripts/soak/analyze_soak.py logs/soak/main-20260926 --md report.md
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def slope_per_hour(points):
    """Least-squares slope of (t_seconds, value) scaled to units/hour."""

    points = [(t, v) for t, v in points if v is not None]
    if len(points) < 3:
        return None
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    den = sum((t - mean_t) ** 2 for t, _ in points)
    if den == 0:
        return None
    num = sum((t - mean_t) * (v - mean_v) for t, v in points)
    return num / den * 3600.0


def fmt(value, digits=2):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--md", default="")
    ap.add_argument("--rss-every", type=float, default=300.0,
                    help="seconds between rows of the RSS-over-time table")
    args = ap.parse_args(argv)
    out = Path(args.out)
    samples = load_jsonl(out / "samples.jsonl")
    events = load_jsonl(out / "events.jsonl")
    summary = {}
    if (out / "summary.json").exists():
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    lines: list[str] = []
    w = lines.append

    w(f"# Soak report: `{out.name}`\n")
    if samples:
        w(f"- samples: {len(samples)} over {samples[-1]['t'] / 60:.1f} min")
    if summary:
        w(f"- stop reason: {summary.get('stop_reason')}; elapsed "
          f"{summary.get('elapsed_minutes')} min; clean stop took "
          f"{summary.get('stop_ms')} ms")
        a = summary.get("args", {})
        w(f"- config: bots={a.get('bots')} worker={a.get('worker')} "
          f"round={a.get('round_seconds')}s modes={a.get('modes')} "
          f"rounds/mode={a.get('rounds_per_mode')}")
    w("")

    # ---------------------------------------------------------------- memory
    w("## Memory\n")
    w("| t (min) | map | mode | bots | RSS MB | worker RSS MB | py blocks | tasks | threads | handles |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    next_t = 0.0
    for row in samples:
        if row["t"] >= next_t or row is samples[-1]:
            next_t = row["t"] + args.rss_every
            w(f"| {row['t'] / 60:.1f} | {row['map']} | {row['mode']} | {row['bots']} | "
              f"{row['rss_mb']} | {fmt(row.get('worker_rss_mb'))} | {row['py_blocks']} | "
              f"{row['asyncio_tasks']} | {row['threads']} | {fmt(row.get('handles'))} |")
    warm = [r for r in samples if r["t"] >= 600]  # ignore first 10 min warm-up
    for label, key in (("server RSS MB", "rss_mb"), ("worker RSS MB", "worker_rss_mb"),
                       ("python allocated blocks", "py_blocks"),
                       ("asyncio tasks", "asyncio_tasks"), ("handles", "handles")):
        s_all = slope_per_hour([(r["t"], r.get(key)) for r in samples])
        s_warm = slope_per_hour([(r["t"], r.get(key)) for r in warm])
        values = [r.get(key) for r in samples if r.get(key) is not None]
        if values:
            w(f"- {label}: min {min(values)} / max {max(values)} / last {values[-1]}; "
              f"trend {fmt(s_all, 1)}/h (after 10 min warm-up: {fmt(s_warm, 1)}/h)")
    transitions = [e for e in events if e.get("kind") == "transition"]
    if transitions:
        rss = [(i, e.get("rss_mb")) for i, e in enumerate(transitions) if e.get("rss_mb")]
        if len(rss) >= 3:
            per_round = slope_per_hour([(i, v) for i, v in rss])
            w(f"- RSS at each rollover (MB): {', '.join(str(v) for _, v in rss)}")
            if per_round is not None:
                w(f"- least-squares growth per rollover: {per_round / 3600.0:.2f} MB")
    w("")

    # ----------------------------------------------------------------- ticks
    w("## Tick time\n")
    if samples:
        quarter = max(1, len(samples) // 4)
        for label, chunk in (("first quarter", samples[:quarter]),
                             ("last quarter", samples[-quarter:])):
            p50 = statistics.median(r["ticks"]["p50"] for r in chunk if r["ticks"]["n"])
            p99 = statistics.median(r["ticks"]["p99"] for r in chunk if r["ticks"]["n"])
            mx = max(r["ticks"]["max"] for r in chunk)
            w(f"- {label}: median window p50 {p50:.3f} ms, median window p99 {p99:.3f} ms, max {mx:.2f} ms")
        creep = slope_per_hour([(r["t"], r["ticks"]["p99"]) for r in samples if r["ticks"]["n"]])
        creep50 = slope_per_hour([(r["t"], r["ticks"]["p50"]) for r in samples if r["ticks"]["n"]])
        w(f"- trend: p50 {fmt(creep50, 3)} ms/h, p99 {fmt(creep, 3)} ms/h")
        hz = [r["tick_hz"] for r in samples if r.get("tick_hz")]
        if hz:
            w(f"- effective tick rate: min {min(hz):.2f} Hz, median {statistics.median(hz):.2f} Hz")
        slow10 = sum(r["ticks"]["over_10ms"] for r in samples)
        slow16 = sum(r["ticks"]["over_16ms"] for r in samples)
        total = sum(r["ticks"]["n"] for r in samples)
        w(f"- ticks > 10 ms: {slow10} / {total}; > 16.7 ms: {slow16}")
        lag = [r["loop_lag_ms"]["max"] for r in samples]
        w(f"- event-loop lag (50 ms probe overshoot): max {max(lag):.1f} ms, "
          f"median window max {statistics.median(lag):.1f} ms")
        worst = sorted(samples, key=lambda r: r["ticks"]["max"], reverse=True)[:8]
        w("- worst windows: " + "; ".join(
            f"t={r['t'] / 60:.1f}m {r['map']}/{r['mode']} max {r['ticks']['max']:.1f} ms"
            + (" (transition)" if r.get("transition_busy") or r.get("mode_ended") else "")
            for r in worst))
        # Subsystem worst p99.
        sub_worst: dict[str, float] = defaultdict(float)
        sub_max: dict[str, float] = defaultdict(float)
        for r in samples:
            for name, s in (r.get("subsystems") or {}).items():
                sub_worst[name] = max(sub_worst[name], s["p99"])
                sub_max[name] = max(sub_max[name], s["max"])
        top = sorted(sub_max.items(), key=lambda kv: kv[1], reverse=True)[:10]
        w("- subsystem worst window p99 / max (ms): " + ", ".join(
            f"{n} {sub_worst[n]:.2f}/{m:.1f}" for n, m in top))
    segments = [e for e in events if e.get("kind") == "segment"]
    if segments:
        w("\n| # | map | mode | seconds | ticks | p50 ms | p99 ms | max ms | >10ms | RSS end MB |")
        w("|---|---|---|---|---|---|---|---|---|---|")
        for s in segments:
            t = s["ticks"]
            w(f"| {s['index']} | {s['map']} | {s['mode']} | {s['seconds']} | {t['n']} | "
              f"{t['p50']:.3f} | {t['p99']:.3f} | {t['max']:.2f} | {t['over_10ms']} | {s.get('rss_mb_end')} |")
        by_mode = defaultdict(list)
        for s in segments:
            if s["ticks"]["n"]:
                by_mode[s["mode"]].append(s)
        w("\n| mode | segments | worst p99 ms | worst max ms |")
        w("|---|---|---|---|")
        for mode, rows in sorted(by_mode.items()):
            w(f"| {mode} | {len(rows)} | {max(r['ticks']['p99'] for r in rows):.3f} | "
              f"{max(r['ticks']['max'] for r in rows):.2f} |")
    w("")

    # ------------------------------------------------ queues / worker / spawn
    w("## Queues, peers, worker, spawns\n")
    qmax: dict[str, int] = defaultdict(int)
    for r in samples:
        for name, value in (r.get("queues") or {}).items():
            qmax[name] = max(qmax[name], int(value))
    if qmax:
        w("- queue length max: " + ", ".join(f"{k}={v}" for k, v in sorted(qmax.items())))
    peer_max = max((p["reliable_in_transit"] for r in samples for p in r.get("peers", [])), default=None)
    w(f"- peer reliableDataInTransit max: {fmt(peer_max)} bytes (only real clients have peers)")
    if samples:
        last = samples[-1]
        w(f"- final counters: {json.dumps(last.get('counters', {}))}")
        workers = [r.get("worker") or {} for r in samples]
        restarts = max((int(x.get("restarts") or 0) for x in workers), default=0)
        stalled = max((int(x.get("stalled_restarts") or 0) for x in workers), default=0)
        silence = max((float(x.get("intent_silence_seconds") or 0.0) for x in workers), default=0.0)
        dropped_f = max((int(x.get("dropped_frames") or 0) for x in workers), default=0)
        dropped_i = max((int(x.get("dropped_intents") or 0) for x in workers), default=0)
        rejections = max((int(x.get("snapshot_rejections") or 0) for x in workers), default=0)
        pids = sorted({x.get("process_id") for x in workers if x.get("process_id")})
        w(f"- bot worker: restarts={restarts} stalled_restarts={stalled} pids={pids} "
          f"max intent silence={silence:.2f}s dropped frames={dropped_f} intents={dropped_i} "
          f"snapshot rejections={rejections}")
        overflow_rate: dict[str, list[float]] = defaultdict(list)
        bots_p99: dict[str, float] = defaultdict(float)
        for prev, cur in zip(samples, samples[1:]):
            dt = cur["t"] - prev["t"]
            a = (prev.get("counters") or {}).get("bot_perception_entity_overflow")
            b = (cur.get("counters") or {}).get("bot_perception_entity_overflow")
            if dt > 0 and a is not None and b is not None and cur["map"] == prev["map"]:
                overflow_rate[cur["map"]].append((b - a) / dt)
            bots_sub = (cur.get("subsystems") or {}).get("bots")
            if bots_sub:
                bots_p99[cur["map"]] = max(bots_p99[cur["map"]], bots_sub["p99"])
        heavy = sorted(
            ((m, statistics.median(v)) for m, v in overflow_rate.items() if v),
            key=lambda kv: -kv[1],
        )
        if heavy:
            w("- bot perception entity overflow (entities dropped past the 192 cap, per second, "
              "median) and worst `bots` subsystem p99 per map: " + ", ".join(
                  f"{m} {r:.0f}/s ({bots_p99.get(m, 0.0):.2f} ms)" for m, r in heavy))
        bots = [r["bots"] for r in samples]
        w(f"- bot population: min {min(bots)} max {max(bots)}; alive fraction median "
          f"{statistics.median(r['bots_alive'] / r['bots'] for r in samples if r['bots']):.2f}")
        spawn_p99: dict[str, float] = defaultdict(float)
        spawn_max: dict[str, float] = defaultdict(float)
        spawn_n: Counter = Counter()
        for r in samples:
            for name, s in (r.get("spawn") or {}).items():
                spawn_p99[name] = max(spawn_p99[name], s["p99"])
                spawn_max[name] = max(spawn_max[name], s["max"])
                spawn_n[name] += s["n"]
        for name in sorted(spawn_n):
            w(f"- spawn `{name}`: calls {spawn_n[name]}, worst window p99 "
              f"{spawn_p99[name]:.3f} ms, max {spawn_max[name]:.2f} ms")
    if summary.get("spawn_failures"):
        w(f"- spawn failures: {summary['spawn_failures']}")
    for e in events:
        if e.get("kind") == "worker_restart":
            w(f"- worker restart at t={e['t'] / 60:.1f} min: {e.get('status')}")
    w("")

    # ------------------------------------------------------------ transitions
    w("## Map / mode transitions\n")
    counts = Counter((e.get("type"), bool(e.get("ok"))) for e in transitions)
    w("- counts: " + ", ".join(f"{k[0]} ok={k[1]}: {v}" for k, v in sorted(counts.items())))
    loads = [e for e in events if e.get("kind") == "map_load"]
    if loads:
        ms = [e["ms"] for e in loads]
        w(f"- VXL preflight loads: {len(loads)}, median {statistics.median(ms):.0f} ms, "
          f"max {max(ms):.0f} ms ({max(loads, key=lambda e: e['ms'])['map']}); "
          f"errors: {[e for e in loads if e.get('error')]}")
    if transitions:
        durations = [e["duration_ms"] for e in transitions if e.get("type") in ("map", "mode")]
        if durations:
            w(f"- rollover commit duration: median {statistics.median(durations):.1f} ms, "
              f"max {max(durations):.1f} ms")
        w("\n| t (min) | type | from | to | ok | ms | bots after | RSS MB | message |")
        w("|---|---|---|---|---|---|---|---|---|")
        for e in transitions:
            w(f"| {e['t'] / 60:.1f} | {e.get('type')} | {e.get('from_map')}/{e.get('from_mode')} | "
              f"{e.get('to_map')}/{e.get('to_mode')} | {e.get('ok')} | {e.get('duration_ms')} | "
              f"{e.get('bots_after', '-')} | {e.get('rss_mb', '-')} | {e.get('message')} |")
    for e in events:
        if e.get("kind") in ("stuck_round", "mode_request", "sweep_request", "sweep_timeout",
                             "sweep_begin", "sweep_end", "harness_error", "sample_error",
                             "server_task_ended", "stop_error"):
            detail = {k: v for k, v in e.items() if k not in ("wall", "kind", "t", "traceback")}
            w(f"- t={e['t'] / 60:.1f}m {e['kind']}: {detail}")
    if summary:
        w(f"- maps visited ({len(summary.get('visited_maps', []))}/{len(summary.get('catalog', []))}); "
          f"unvisited: {summary.get('unvisited_maps')}")
        w(f"- modes visited: {summary.get('visited_modes')}")
    w("")

    # -------------------------------------------------------------- profilers
    prof: dict[str, list[float]] = {}
    for r in samples:
        for key, v in (r.get("entity_profile") or {}).items():
            row = prof.setdefault(key, [0, 0.0, 0.0])
            row[0] += v["n"]
            row[1] += v["total_ms"]
            row[2] = max(row[2], v["max_ms"])
    stalls = [e for e in events if e.get("kind") == "loop_stall"]
    slow_hooks = [e for e in events if e.get("kind") == "slow_entity_hook"]
    if prof or stalls or slow_hooks:
        w("## Profilers\n")
    if prof:
        w("| entity hook | calls | total ms | max ms |")
        w("|---|---|---|---|")
        for key, (n, total, mx) in sorted(prof.items(), key=lambda kv: -kv[1][2])[:15]:
            w(f"| {key} | {n} | {total:.0f} | {mx:.2f} |")
    if slow_hooks:
        by = Counter((e["hook"], e.get("entity_kind"), e.get("map"), e.get("mode")) for e in slow_hooks)
        w(f"\n- slow entity hooks (>5 ms): {len(slow_hooks)}; by hook/kind/map/mode: "
          + "; ".join(f"{k} x{v}" for k, v in by.most_common(10)))
    if stalls:
        by = Counter(e["where"] for e in stalls)
        w(f"\n- event-loop stalls captured: {len(stalls)} (max {max(e['stale_ms'] for e in stalls):.0f} ms)")
        for where, count in by.most_common(8):
            example = next(e for e in stalls if e["where"] == where)
            w(f"  - x{count}: `{where}`")
            w("```\n" + example["stack"].strip()[-1500:] + "\n```")
    w("")

    # ------------------------------------------------------------------ logs
    w("## Log health\n")
    if summary:
        w(f"- records by level: {summary.get('log_levels')}")
        keys = summary.get("warning_keys", {})
        if keys:
            w("\n| count | level | logger:message template |")
            w("|---|---|---|")
            for key, count in sorted(keys.items(), key=lambda kv: -kv[1]):
                level, _, rest = key.partition("|")
                w(f"| {count} | {level} | `{rest.replace('|', '/')}` |")
        tbs = summary.get("first_tracebacks", {})
        for key, tb in tbs.items():
            w(f"\n**{key}** (x{summary.get('traceback_counts', {}).get(key)}; first at t={tb['first_t'] / 60:.1f} min)\n")
            w("```\n" + tb["text"].strip()[-3000:] + "\n```")
        if summary.get("anticheat_report_failures"):
            w(f"- anticheat report tick failures (hidden at DEBUG): {summary['anticheat_report_failures']}")
    else:
        levels = Counter(e.get("level") for e in events if e.get("kind") == "log")
        w(f"- captured log rows by level: {dict(levels)}")
    w("")

    # ------------------------------------------------------ anticheat/conduct
    w("## Anticheat / conduct false-positive check\n")
    if samples:
        ac_bots = samples[-1].get("anticheat_bots") or {}
        peak_bots: Counter = Counter()
        peak_humans: Counter = Counter()
        for r in samples:
            for k, v in (r.get("anticheat_bots") or {}).items():
                peak_bots[k] = max(peak_bots[k], v)
            for k, v in (r.get("anticheat_humans") or {}).items():
                peak_humans[k] = max(peak_humans[k], v)
        w(f"- anticheat counters on bots (peak per kind across samples, per-map lifetimes): {dict(peak_bots) or 'none'}")
        w(f"- anticheat counters on humans: {dict(peak_humans) or 'none'}")
        del ac_bots
    ac_rows = [e for e in events if e.get("kind") == "log" and e.get("logger") == "anticheat"]
    cd_rows = [e for e in events if e.get("kind") == "log" and "conduct" in str(e.get("logger"))]
    w(f"- `anticheat` log records: {len(ac_rows)}")
    for e in ac_rows[:15]:
        w(f"  - t={e['t'] / 60:.1f}m {e['level']} {e['message'][:300]}")
    w(f"- `conduct` log records: {len(cd_rows)}")
    kinds = Counter(e["message"].split(" source=")[0].split(" player=")[0] for e in cd_rows)
    if kinds:
        w(f"  - by kind: {dict(kinds)}")
    for e in cd_rows[:15]:
        w(f"  - t={e['t'] / 60:.1f}m {e['level']} {e['message'][:300]}")
    discs = [e for e in events if e.get("kind") == "player_disconnect_call"]
    human_discs = [e for e in discs if not e.get("is_bot")]
    w(f"- Player.disconnect calls: {len(discs)} (humans: {len(human_discs)})")
    for e in human_discs[:10]:
        w(f"  - t={e['t'] / 60:.1f}m {e.get('player')} args={e.get('args')}")
    humans = [(r["t"], r.get("humans_conduct")) for r in samples if r.get("humans_conduct")]
    if humans:
        w("- human conduct state over time (idle s / warned / alive):")
        for t, state in humans[:: max(1, len(humans) // 30)]:
            w(f"  - t={t / 60:.1f}m {state}")
    text = "\n".join(lines) + "\n"
    if args.md:
        Path(args.md).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
