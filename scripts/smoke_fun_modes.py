"""Join isolated community servers with native-v4 and stock-ticket peers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, default=ROOT.parent / "BattleSpadesClient/out/build/native-dev/src/RelWithDebInfo/aos_protocol168_live_session_smoke.exe")
    parser.add_argument("--port", type=int, default=27916)
    parser.add_argument("--seconds", type=int, default=12)
    parser.add_argument("--modes", nargs="+", default=["night_shift", "floor_tax", "crumbling_run", "hot_potato"])
    args = parser.parse_args()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", args.port))
    directory = ROOT / "build/fun-smoke"
    directory.mkdir(parents=True, exist_ok=True)
    hidden = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for mode in args.modes:
        import tomlkit
        document = tomlkit.parse((ROOT / f"configs/unofficial/{mode}.toml").read_text())
        document["server"]["lan_discovery"] = False
        fixture = directory / f"{mode}.toml"
        fixture.write_text(tomlkit.dumps(document), encoding="utf-8")
        status = directory / f"{mode}-status.json"
        status.unlink(missing_ok=True)
        with (directory / f"{mode}.log").open("w", encoding="utf-8") as log:
            server = subprocess.Popen(
                [sys.executable, "run_server.py", "--config", str(fixture),
                 "--port", str(args.port), "--control-stdin", "--status-file", str(status)],
                cwd=ROOT, stdin=subprocess.PIPE, stdout=log, stderr=log, text=True, creationflags=hidden)
            try:
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    if server.poll() is not None:
                        raise RuntimeError(f"{mode}: server exited; see {log.name}")
                    if status.exists() and json.loads(status.read_text()).get("state") == "running":
                        break
                    time.sleep(.1)
                else:
                    raise TimeoutError(f"{mode}: server startup timed out")
                result = subprocess.run([str(args.client), "127.0.0.1", str(args.port), str(args.seconds), "--community"],
                                        cwd=args.client.parent, timeout=95, capture_output=True, text=True,
                                        creationflags=hidden)
                print(result.stdout.strip(), result.stderr.strip(), flush=True)
                result.check_returncode()
            finally:
                if server.poll() is None:
                    try:
                        server.communicate("shutdown\n", timeout=20)
                    except subprocess.TimeoutExpired:
                        server.terminate()
                        server.wait(timeout=10)


if __name__ == "__main__":
    main()
