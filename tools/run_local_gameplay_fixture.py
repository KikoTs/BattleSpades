"""Loopback-only server/native-client gate for turret terrain, scores and votes.

Uses a controlled flat initial map, the real ENet server and native protocol
client. Only fixture setup and the final lethal hit are scripted; projectile
flight, blast replication, scoring, ballot CAST and rollover use production
paths. Never registers with a public service or edits an operator config.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import shared.constants as C
from server.config import load_config
from server.game_constants import TEAM1, TEAM2
from server.game_rules import get_rules
from server.main import BattleSpadesServer
from server.player import Player


class FixtureConnection:
    """A server-owned target, never a voting peer or external connection."""
    in_game = True

    def __init__(self, server):
        self.server = server
        self.player = None

    def send(self, *_args, **_kwargs):
        pass


async def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError("local gameplay fixture timed out")
        await asyncio.sleep(0.01)


async def run(args):
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    config = load_config(ROOT / "tools/protocol168-parity.toml")
    config.port = args.port
    config.mode_event_drain_budget = 1
    config.end_screen_seconds = 0.0
    server = BattleSpadesServer(config)
    server_task = asyncio.create_task(server.start())
    client = None
    report = {"host": "127.0.0.1", "port": args.port, "passed": False}
    try:
        await wait_until(lambda: server.running or server_task.done())
        if server_task.done():
            await server_task
        # Known terrain is exported through real MapSync before either peer
        # acts. Preserve the real catalog/map identity for the later rollover.
        server.world_manager.generate_flat_map()
        server.mode.time_limit = 0
        server.mode.get_spawn_point = lambda _player: (100.5, 100.5, 59.3)
        # The debug plateau is intentionally one voxel thick at z=62. Anchor
        # it to the retail support plane so this is a rocket-crater test, not
        # a legitimate collapse of an entirely floating test world.
        for z in range(63, 240):
            assert server.world_manager.set_block(104, 100, z, True, 0xAABBCC)
        wall = [(110, y, z) for y in range(98, 103) for z in range(57, 62)]
        for cell in wall:
            assert server.world_manager.set_block(*cell, True, 0xAABBCC)
        report["initial_map"] = server.config.map_name
        report["wall_before"] = [list(cell) for cell in wall]
        client = await asyncio.create_subprocess_exec(
            str(args.client.resolve()), str(args.port),
            cwd=str(args.client.resolve().parent),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        # The retail wire name is capped at 15 characters by the server.
        client_name = "LocalGameplayGate"[:15]
        await wait_until(lambda: any(p.name == client_name and p.spawned
                                    for p in server.players.values()), 40.0)
        owner = next(p for p in server.players.values() if p.name == client_name)
        await wait_until(lambda: owner.connection.in_game)
        target_connection = FixtureConnection(server)
        target = Player(server.get_next_player_id(), "FixtureTarget", TEAM2,
                        C.RIFLE_TOOL, target_connection)
        target_connection.player = target
        target.is_bot = True
        target.class_id = int(C.CLASS_SOLDIER)
        target.loadout = [int(C.RIFLE_TOOL)]
        server.players[target.id] = target
        server.teams[TEAM2].add_player(target)
        target.spawn(113.5, 100.5, 59.3)
        target.health = 1000
        server._broadcast_create_player(target, target.position)
        owner.rocket_turret_stock = 2
        turret = server.rocket_turret_controller.place(owner, (104.0, 100.0, 62.0), yaw=90.0)
        assert turret is not None, "real turret placement was refused"
        # Explicitly fire this bounded collision fixture toward its wall;
        # auto-targeting/LOS remains covered by test_rocket_turret.py.
        server.rocket_turret_controller._fire(turret, target, time.monotonic())
        turret.ammo = 0
        await wait_until(lambda: any(not server.world_manager.get_solid(*p) for p in wall))
        removed = [p for p in wall if not server.world_manager.get_solid(*p)]
        assert not server.world_manager.get_solid(110, 100, 59)
        assert server.world_manager.get_solid(100, 100, 62), "blast collapsed the anchored floor"
        report["player_floor_retained"] = True
        report["turret_wall_removed"] = [list(p) for p in removed]
        report["turret_entity_id"] = turret.entity_id
        await asyncio.sleep(0.3)
        assert server.vote_manager.ensure_map_vote(time.time())
        report["vote_candidates"] = list(server.vote_manager.candidates)
        await wait_until(lambda: server.vote_manager.next_map is not None)
        voted_map = server.vote_manager.next_map
        report["voted_map"] = voted_map
        protection = float(get_rules(config).get("RULE_SPAWN_PROTECTION_TIME"))
        await wait_until(lambda: time.monotonic() - target.spawned_at >= protection,
                         max(20.0, protection + 1.0))
        # Accept a real lethal hit at the timeout boundary with work already
        # queued and a one-event budget. The native peer must see score100/1.
        server.queue_mode_event("on_fixture_backlog")
        server.queue_mode_event("on_fixture_backlog")
        target.health = 100
        assert target.damage(999, source=owner, kill_type=int(C.KILL.WEAPON_KILL))
        server.mode.time_limit = 1
        server.mode.start_time = time.time() - 2
        server.mode._timeout_music_played = True
        await wait_until(lambda: server.mode.ended)
        report["personal_score"] = owner.score
        report["team_score"] = server.teams[TEAM1].score
        report["winner"] = server.mode.winner
        assert owner.score == 100 and server.teams[TEAM1].score == 1
        assert server.mode.winner == TEAM1
        stdout, stderr = await asyncio.wait_for(client.communicate(), timeout=70.0)
        (evidence / "client.stdout.log").write_bytes(stdout)
        (evidence / "client.stderr.log").write_bytes(stderr)
        print(stdout.decode(errors="replace"), end="")
        if client.returncode:
            raise RuntimeError(f"native probe failed ({client.returncode}): {stderr.decode(errors='replace')}")
        report["final_map"] = server.config.map_name
        assert server.config.map_name == voted_map
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
        (evidence / "server-result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: local authoritative gameplay gate; evidence={evidence}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, required=True)
    parser.add_argument("--port", type=int, default=32890)
    parser.add_argument("--evidence", type=Path, default=ROOT / "tmp/local-gameplay-gate")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not args.client.is_file():
        parser.error("requires a built native probe and an unprivileged local UDP port")
    os.chdir(ROOT)
    args.evidence.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(
                            args.evidence / "server.log", mode="w", encoding="utf-8")])
    asyncio.run(run(args))
