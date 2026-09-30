"""Supervise a long memory soak: memory cap, restart-and-log, join/leave churn.

The supervisor owns three things the soak itself cannot:

* **the memory cap** - on Linux it creates a cgroup v2 group with
  ``memory.max`` (and no swap) and starts the server inside it, so the server
  and its bot worker share one hard limit the way a small host's service
  does.  An OOM kill is counted from ``memory.events``;
* **restarts** - a crash (this development PC has faulty hardware) must not
  lose a six-hour run.  The server is started again for the remaining time
  and appends to the same files.  Every restart is written to
  ``supervisor.json`` with its exit code and cause: reported, never hidden;
* **the churn clients** - ``churn_client.py`` runs outside the cap and is
  restarted the same way.

``supervisor.json`` is rewritten every few seconds (``heartbeat_wall``), so a
reader can tell a live run from a dead one.  Create ``<out>/STOP`` to end the
run early and cleanly.

    python scripts/soak/soak_supervisor.py --out <dir> --minutes 360 \\
        --cap-mb 512 --port 28500 --clients 6 -- --tracemalloc window
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STDIO_CAP = 32 * 1024 * 1024


def write_state(path: Path, state: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    os.replace(temp, path)


def read_events(cgroup: Path | None) -> dict:
    values = {}
    if cgroup is None:
        return values
    try:
        for line in (cgroup / "memory.events").read_text().splitlines():
            key, _, value = line.partition(" ")
            values[key] = int(value)
    except (OSError, ValueError):
        pass
    return values


def make_cgroup(name: str, cap_mb: float) -> Path | None:
    """Create a cgroup v2 group limited to ``cap_mb``; ``None`` if impossible."""

    if cap_mb <= 0 or not sys.platform.startswith("linux"):
        return None
    base = Path("/sys/fs/cgroup")
    if not (base / "cgroup.controllers").exists():
        return None
    try:
        control = base / "cgroup.subtree_control"
        if "memory" not in control.read_text().split():
            control.write_text("+memory")
        group = base / name
        # A new group prevents another run's processes or OOM counters from
        # being mistaken for this run's evidence.
        group.mkdir()
        (group / "memory.max").write_text(str(int(cap_mb * 2**20)))
        (group / "memory.swap.max").write_text("0")
        (group / "memory.oom.group").write_text("1")
        if int((group / "memory.max").read_text()) != int(cap_mb * 2**20):
            raise OSError("memory.max did not retain the requested cap")
        if (group / "memory.swap.max").read_text().strip() != "0":
            raise OSError("memory.swap.max did not retain the no-swap setting")
        try:
            (group / "memory.peak").write_text("0")  # reset (kernel 6.12+)
        except OSError:
            pass
        return group
    except OSError as error:
        print(f"cgroup unavailable: {error}", file=sys.stderr)
        return None


def final_verdict(state: dict) -> tuple[str, int]:
    """Keep duration completion separate from a successful stability test."""
    failures = list(state.get("failures", []))
    if state["cap_requested_mb"] > 0 and not state["cap_mb"]:
        failures.append("requested memory cap unavailable")
    if state["restarts"]:
        failures.append("server exited unexpectedly")
    if state["client_restarts"]:
        failures.append("churn client exited unexpectedly")
    if state["oom_kills"]:
        failures.append("memory cap caused an OOM kill")
    if state["clients"] > 0 and not state.get("client_started"):
        failures.append("requested churn clients never started")
    state["failures"] = list(dict.fromkeys(failures))
    if failures:
        return "failed", 1
    if state["status"] != "finished":
        return "incomplete", 2
    return "passed", 0


def inspect_evidence(out: Path, state: dict) -> None:
    """Require recorded memory samples and actual joins, not just live PIDs."""
    try:
        with (out / "memory.csv").open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        if len(rows) < 2:
            state["failures"].append("fewer than two memory samples")
        if state["cap_mb"] and any(not row.get("cg_current_mb") for row in rows):
            state["failures"].append("memory samples are missing cgroup accounting")
        if any(int(row.get("worker_crash_restarts") or 0) for row in rows):
            state["failures"].append("bot worker crashed or stalled")
        if any(int(row.get("transitions_failed") or 0) for row in rows):
            state["failures"].append("map or mode transition failed")
        state["memory_samples"] = len(rows)
    except (OSError, ValueError):
        state["failures"].append("memory samples are missing or malformed")
    try:
        with (out / "events.jsonl").open(encoding="utf-8") as stream:
            if any(json.loads(line).get("kind") in {"memory_sample_error", "sample_error", "stop_error"}
                   for line in stream):
                state["failures"].append("memory sampling failed")
    except (OSError, ValueError):
        # Memory and churn samples above remain required. Older short fixtures
        # and uncapped harness runs may not have the optional event stream.
        pass
    if state["clients"] <= 0:
        return
    try:
        rows = [json.loads(line) for line in (out / "churn.jsonl").read_text().splitlines()]
        state["churn_joins"] = max((row.get("joins", 0) for row in rows), default=0)
        if not state["churn_joins"]:
            state["failures"].append("churn clients never joined a game")
        if any(row.get("decode_errors") or row.get("play_errors") for row in rows):
            state["failures"].append("churn clients reported protocol or play errors")
    except (OSError, ValueError):
        state["failures"].append("churn evidence is missing or malformed")


def tail(path: Path, size: int = 3000) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            stream.seek(max(0, end - size))
            return stream.read().decode("utf-8", "replace")
    except OSError:
        return ""


def cap_file(path: Path) -> None:
    try:
        if path.stat().st_size > STDIO_CAP:
            os.truncate(path, 0)
    except OSError:
        pass


def kill_cgroup(cgroup: Path | None) -> None:
    """Reap workers left behind by a crash in this run's private cgroup."""
    if cgroup is not None:
        (cgroup / "cgroup.kill").write_text("1")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--minutes", type=float, default=360.0)
    parser.add_argument("--cap-mb", type=float, default=512.0)
    parser.add_argument("--port", type=int, default=28500)
    parser.add_argument("--clients", type=int, default=6)
    parser.add_argument("--client-args", default="",
                        help="extra arguments for churn_client.py (one string)")
    parser.add_argument("--max-restarts", type=int, default=40)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("soak_args", nargs=argparse.REMAINDER,
                        help="arguments after -- go to memory_soak.py")
    args = parser.parse_args(argv)
    if args.soak_args and args.soak_args[0] == "--":
        args.soak_args = args.soak_args[1:]
    if args.minutes <= 0 or args.cap_mb < 0 or args.clients < 0 or args.max_restarts < 0:
        parser.error("minutes must be positive; cap, clients and restarts must be nonnegative")
    # These describe the supervisor's run, and must not be overridden in the
    # child's trailing arguments (which argparse would otherwise accept).
    reserved = {"--out", "--port", "--minutes", "--run-index", "--elapsed-offset"}
    if any(value.split("=", 1)[0] in reserved for value in args.soak_args):
        parser.error("soak arguments cannot override supervisor output, port or duration")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "supervisor.json"
    if state_path.exists() or (out / "memory.csv").exists():
        raise SystemExit("Use a new output directory so runs cannot share evidence")
    stop_file = out / "STOP"
    client_stop = out / "STOP_CLIENTS"
    for stale in (stop_file, client_stop):
        if stale.exists():
            stale.unlink()
    cgroup = make_cgroup(f"bs-soak-{args.port}-{os.getpid()}", args.cap_mb)
    started_wall = time.time()
    started = time.monotonic()
    total = args.minutes * 60.0
    state = {
        "status": "running",
        "supervisor_pid": os.getpid(),
        "platform": sys.platform,
        "started_wall": started_wall,
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started_wall)),
        "minutes": args.minutes,
        "cap_mb": args.cap_mb if cgroup is not None else 0,
        "cap_requested_mb": args.cap_mb,
        "cap_readback_bytes": int((cgroup / "memory.max").read_text()) if cgroup else None,
        "swap_readback_bytes": int((cgroup / "memory.swap.max").read_text()) if cgroup else None,
        "cgroup": str(cgroup) if cgroup else None,
        "port": args.port,
        "clients": args.clients,
        "soak_args": args.soak_args,
        "out": str(out),
        "server_pid": None,
        "client_pid": None,
        "restarts": [],
        "client_restarts": 0,
        "client_started": False,
        "failures": [],
        "verdict": "pending",
        "oom_kills": 0,
        "heartbeat_wall": started_wall,
    }
    if args.cap_mb > 0 and cgroup is None:
        state["status"] = "failed"
        state["verdict"], result = final_verdict(state)
        write_state(state_path, state)
        return result
    stopping = {"flag": False}

    def request_stop(*_):
        stopping["flag"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):
            pass

    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONFAULTHANDLER"] = "1"
    server_stdio = out / "server-stdio.log"
    client_stdio = out / "client-stdio.log"

    def start_server(run_index: int):
        elapsed = time.monotonic() - started
        remaining = max(0.001, (total - elapsed) / 60.0)
        command = [
            args.python, "-X", "faulthandler", str(HERE / "memory_soak.py"),
            "--out", str(out), "--port", str(args.port),
            "--minutes", f"{remaining:.6f}",
            "--run-index", str(run_index),
            "--elapsed-offset", f"{elapsed:.1f}",
            *args.soak_args,
        ]
        summary = out / "summary.json"
        if summary.exists():
            summary.rename(out / f"summary-run-{run_index - 1}.json")
        stream = server_stdio.open("ab")
        stream.write(f"\n=== start {run_index} at {time.strftime('%H:%M:%S')}: "
                     f"{' '.join(command)}\n".encode())
        stream.flush()
        if cgroup is not None:
            shell = (
                f"echo $$ > {shlex.quote(str(cgroup / 'cgroup.procs'))} && exec "
                + " ".join(shlex.quote(part) for part in command)
            )
            command = ["/bin/bash", "-c", shell]
        process = subprocess.Popen(
            command, cwd=str(ROOT), stdout=stream, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=environment,
        )
        stream.close()
        return process

    def start_client():
        elapsed = time.monotonic() - started
        remaining = max(0.001, (total - elapsed) / 60.0)
        command = [
            args.python, str(HERE / "churn_client.py"),
            "--port", str(args.port), "--slots", str(args.clients),
            "--minutes", f"{remaining:.6f}",
            "--out", str(out / "churn.jsonl"),
            "--stop-file", str(client_stop),
            "--seed", str(int(time.time()) & 0xFFFFFF),
            *shlex.split(args.client_args),
        ]
        stream = client_stdio.open("ab")
        process = subprocess.Popen(
            command, cwd=str(ROOT), stdout=stream, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=environment,
        )
        stream.close()
        return process

    run_index = 0
    server = start_server(run_index)
    server_started = time.monotonic()
    state["server_pid"] = server.pid
    client = None
    client_due = time.monotonic() + 25.0
    write_state(state_path, state)
    result = 0
    try:
        while True:
            time.sleep(3.0)
            now = time.monotonic()
            elapsed = now - started
            state["heartbeat_wall"] = time.time()
            state["elapsed_minutes"] = round(elapsed / 60.0, 2)
            state["elapsed_seconds"] = round(elapsed, 3)
            events = read_events(cgroup)
            state["oom_kills"] = max(state["oom_kills"], int(events.get("oom_kill", 0)))
            state["cgroup_events"] = events
            cap_file(server_stdio)
            cap_file(client_stdio)
            if stopping["flag"] and not stop_file.exists():
                stop_file.write_text("supervisor signal\n")
            code = server.poll()
            if code is not None:
                requested = stop_file.exists()
                try:
                    summary = json.loads((out / "summary.json").read_text())
                except (OSError, ValueError):
                    summary = {}
                # A zero exit alone is not evidence of a completed soak. The
                # harness must report its own duration reaching the deadline.
                finished = elapsed >= total and summary.get("stop_reason") == "duration reached"
                if code == 0 and (requested or finished):
                    state["status"] = "stopped" if requested else "finished"
                    break
                restart = {
                    "index": run_index + 1,
                    "at_minutes": round(elapsed / 60.0, 2),
                    "wall_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "exit_code": code,
                    "signal": -code if code < 0 else None,
                    "ran_seconds": round(now - server_started, 1),
                    "oom_kills_total": int(events.get("oom_kill", 0)),
                    "cause": (
                        "oom-kill (memory cap reached)"
                        if int(events.get("oom_kill", 0)) > sum(
                            1 for row in state["restarts"] if row["cause"].startswith("oom")
                        )
                        else "crash or unexpected exit"
                    ),
                    "fault_log_tail": tail(out / "fault.log"),
                    "stdio_tail": tail(server_stdio, 1500),
                }
                state["restarts"].append(restart)
                if requested or elapsed >= total or len(state["restarts"]) > args.max_restarts:
                    state["status"] = "failed" if not requested else "stopped"
                    result = 1
                    break
                write_state(state_path, state)
                kill_cgroup(cgroup)
                time.sleep(10.0)
                run_index += 1
                server = start_server(run_index)
                server_started = time.monotonic()
                state["server_pid"] = server.pid
                client_due = time.monotonic() + 25.0
            if args.clients > 0 and not stop_file.exists():
                if client is None:
                    if now >= client_due:
                        client = start_client()
                        state["client_pid"] = client.pid
                        state["client_started"] = True
                elif client.poll() is not None:
                    if elapsed < total or client.returncode != 0:
                        state["client_restarts"] += 1
                        if state["client_restarts"] > args.max_restarts:
                            state["failures"].append("churn client restart limit exceeded")
                            state["status"] = "failed"
                            break
                        client = None
                        client_due = now + 10.0
            if elapsed > total + 120.0:
                state["failures"].append("server exceeded the shutdown deadline")
                state["status"] = "failed"
                break
            write_state(state_path, state)
    finally:
        client_stop.write_text("stop\n")
        # The server may finish in the same polling interval as a client
        # crash. Check it before normal cleanup, even if the loop broke first.
        if client is not None and client.poll() not in (None, 0):
            state["failures"].append(f"churn client exited with code {client.returncode}")
        for process in (client, server):
            if process is None or process.poll() is not None:
                continue
            try:
                process.terminate()
                process.wait(timeout=90.0)
            except Exception:  # noqa: BLE001
                try:
                    process.kill()
                except Exception:  # noqa: BLE001
                    pass
        events = read_events(cgroup)
        state["oom_kills"] = max(state["oom_kills"], int(events.get("oom_kill", 0)))
        state["cgroup_events"] = events
        if cgroup is not None:
            kill_cgroup(cgroup)
            peak = cgroup / "memory.peak"
            try:
                state["cgroup_peak_mb"] = round(int(peak.read_text()) / 2**20, 1)
            except (OSError, ValueError):
                pass
        if state["status"] == "running":
            state["status"] = "stopped"
        state["ended_wall"] = time.time()
        state["heartbeat_wall"] = time.time()
        state["elapsed_minutes"] = round((time.monotonic() - started) / 60.0, 2)
        state["elapsed_seconds"] = round(time.monotonic() - started, 3)
        inspect_evidence(out, state)
        state["verdict"], result = final_verdict(state)
        if result == 1:
            state["status"] = "failed"
        write_state(state_path, state)
        if cgroup is not None:
            try:
                cgroup.rmdir()
            except OSError:
                pass
    return result


if __name__ == "__main__":
    raise SystemExit(main())
