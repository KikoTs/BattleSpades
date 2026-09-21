"""Loopback native-client gate for exact construct selection and placement.

The real server exports an anchored flat map, accepts the client's class
transaction, validates its prefab and replicates the committed cells. The
fixture never edits operator configuration or registers a public server.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import load_config
from server.main import BattleSpadesServer


async def wait_until(predicate: Callable[[], bool], timeout: float = 20.0) -> None:
    """Wait for fixture readiness with a bounded deadline."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError("local prefab fixture timed out")
        await asyncio.sleep(0.01)


async def run(args: argparse.Namespace) -> None:
    """Run the native two-peer test and preserve server-side evidence."""
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    config = load_config(ROOT / "tools/protocol168-parity.toml")
    config.port = args.port
    server = BattleSpadesServer(config)
    server_task = asyncio.create_task(server.start())
    client: asyncio.subprocess.Process | None = None
    report: dict[str, Any] = {
        "host": "127.0.0.1", "port": args.port, "passed": False,
        "placements": [],
    }
    try:
        await wait_until(lambda: server.running or server_task.done())
        if server_task.done():
            await server_task
        server.world_manager.generate_flat_map()
        server.mode.time_limit = 0
        server.mode.get_spawn_point = lambda _player: (100.5, 100.5, 59.3)
        for z in range(63, 240):
            assert server.world_manager.set_block(104, 100, z, True, 0xAABBCC)

        place = server.prefab_actions.place

        def observe_place(player: Any, **kwargs: Any) -> bool:
            """Record the production decision without changing its inputs."""
            entry = {
                "name": kwargs.get("name"), "position": kwargs.get("position"),
                "class_id": player.class_id, "prefabs": player.prefabs,
                "tool": player.tool, "tool_is_raw": player.tool_is_raw,
                "alive": player.alive, "spawned": player.spawned,
                "blocks_before": player.blocks,
            }
            entry["accepted"] = place(player, **kwargs)
            entry["blocks_after"] = player.blocks
            report["placements"].append(entry)
            logging.info("Prefab fixture decision: %s", entry)
            return entry["accepted"]

        server.prefab_actions.place = observe_place
        client = await asyncio.create_subprocess_exec(
            str(args.client.resolve()), "127.0.0.1", str(args.port), "6", "--prefab",
            cwd=str(args.client.resolve().parent),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        stdout, stderr = await asyncio.wait_for(client.communicate(), timeout=85.0)
        (evidence / "client.stdout.log").write_bytes(stdout)
        (evidence / "client.stderr.log").write_bytes(stderr)
        print(stdout.decode(errors="replace"), end="")
        if client.returncode:
            raise RuntimeError(
                f"native prefab probe failed ({client.returncode}): "
                f"{stderr.decode(errors='replace')}"
            )
        assert len(report["placements"]) == 1
        placement = report["placements"][0]
        assert placement["accepted"] and placement["prefabs"] == ["prefab_caltrop"]
        assert placement["blocks_before"] - placement["blocks_after"] == 11
        report["passed"] = True
    finally:
        if client is not None and client.returncode is None:
            client.kill()
            stdout, stderr = await client.communicate()
            (evidence / "client.stdout.log").write_bytes(stdout)
            (evidence / "client.stderr.log").write_bytes(stderr)
        await server.stop()
        server_task.cancel()
        await asyncio.gather(server_task, return_exceptions=True)
        (evidence / "server-result.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
    print(f"PASS: local authoritative construct selection and placement; evidence={evidence}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, required=True)
    parser.add_argument("--port", type=int, default=32891)
    parser.add_argument("--evidence", type=Path, default=ROOT / "tmp/local-prefab-gate")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not args.client.is_file():
        parser.error("requires a built native probe and an unprivileged local UDP port")
    os.chdir(ROOT)
    args.evidence.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(
            args.evidence / "server.log", mode="w", encoding="utf-8")],
    )
    asyncio.run(run(args))
