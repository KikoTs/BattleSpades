"""auto_join.py - autonomously drive the game client into a spawned player.

Talks to the in-game tracer console (game_console.GameConsole) and walks the
same path the UI takes (read from loadingMenu.py / selectTeam.py sources):

  1. ensure the client is connected (LoadingMenu.on_start -> manager.connect)
  2. pump game_scene.load_next_ugc_prefab() until prefabs are loaded
  3. scene.team_selected(team)
  4. scene.create_player(GameClass(...))
  5. manager.set_scene(GameScene)
  6. wait until scene.player exists

Usage:
    py scripts/auto_join.py                     # defaults: 127.0.0.1:27015, team1, class 0
    py scripts/auto_join.py --team 3 --class-id 1
    py scripts/auto_join.py --server 127.0.0.1:27015 --wait 120
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from game_console import GameConsole, ConsoleError  # noqa: E402

TEAM1 = 2
TEAM2 = 3


def wait_for(console: GameConsole, code: str, timeout: float, what: str,
             interval: float = 1.0) -> str:
    """Poll `code` in-game until it evaluates truthy; return its repr."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            last = console.run(code)
        except ConsoleError as exc:
            last = f"<error: {exc}>"
        if last not in ("False", "None", "0", "''", '""', ""):
            return last
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}; last={last}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="127.0.0.1:27015")
    ap.add_argument("--team", type=int, default=TEAM1, help="2=team1 3=team2")
    ap.add_argument("--class-id", type=int, default=0)
    ap.add_argument("--wait", type=float, default=120.0,
                    help="seconds to wait for the game console to come up")
    ap.add_argument("--console-port", type=int, default=32896,
                    help="tracer console port; launch a 2nd client with "
                         "PHYSICS_TRACER_CONSOLE_PORT=32897 (and "
                         "PHYSICS_TRACER_PORT=32898) to drive two observable "
                         "clients for replication tests")
    args = ap.parse_args()

    console = GameConsole(port=args.console_port)
    print("waiting for game console...")
    console.connect(wait_seconds=args.wait)
    print("console up; waiting for game manager (boot)...")
    wait_for(console, "manager is not None", timeout=args.wait,
             what="GameManager (game boot)")
    print("checking connection state")

    connected = console.run(
        "bool(manager.client) and not manager.client.disconnected")
    if connected != "True":
        print(f"not connected (client={connected}); starting connect to {args.server}")
        console.run(
            "from aoslib.scenes.ingame_menus.loadingMenu import LoadingMenu\n"
            f"manager.set_menu(LoadingMenu, identifier='{args.server}', from_server_menu=True)\n"
            "_ = 'connect started'"
        )

    print("waiting for map transfer to finish...")
    wait_for(console,
             "bool(manager.client) and manager.client.map_percentage >= 1.0",
             timeout=90.0, what="map transfer")

    # The transfer percentage covers the NETWORK stream only; the client
    # builds the world asynchronously afterwards. Spawning before the build
    # finishes drops the player into an empty world (falls into water, then
    # gets entombed when the terrain materialises around them — the
    # 'stuck, rolled back on jump' syndrome). Gate on world content
    # stabilising: sample a spread of columns until the surface-z signature
    # is non-empty and identical on two consecutive polls.
    print("waiting for map BUILD to finish (world content stable)...")
    probe_code = (
        "m = getattr(manager.game_scene, 'map', None)\n"
        "_ = '' if m is None else ''.join('1' if m.get_solid(x, y, z) else '0' "
        "for x in range(32, 512, 96) for y in range(32, 512, 96) "
        "for z in (140, 170, 200, 225, 235, 239))"
    )
    deadline = time.monotonic() + 120.0
    prev_sig = None
    while True:
        sig = console.run(probe_code)
        if "1" in sig and sig == prev_sig:
            print("map build stable")
            break
        prev_sig = sig
        if time.monotonic() > deadline:
            raise TimeoutError(f"map build never stabilised; last={sig[:120]}")
        time.sleep(2.0)

    print("pumping UGC prefab loading...")
    wait_for(console, "manager.game_scene.load_next_ugc_prefab()",
             timeout=60.0, what="prefab loading", interval=0.05)

    print(f"selecting team {args.team} and class {args.class_id}; creating player")
    spawn_code = (
        "scene_g = manager.game_scene\n"
        f"team = scene_g.teams[{args.team}]\n"
        "scene_g.team_selected(team)\n"
        "from aoslib.scenes.main.gameClass import GameClass\n"
        f"gc = GameClass(manager, {args.class_id}, manager.disabled_tools, "
        f"manager.movement_speed_multipliers[{args.class_id}], manager.config, "
        "manager.enable_fall_on_water_damage)\n"
        "scene_g.class_selected(gc)\n"
        "scene_g.create_player(gc)\n"
        "from aoslib.scenes.main.gameScene import GameScene\n"
        "manager.set_scene(GameScene)\n"
        "_ = 'spawn flow done, scene=' + type(manager.scene).__name__"
    )

    # The first create_player after a (re)connect is sometimes ignored by
    # the client state machine; retry until scene.player materialises.
    player = None
    for attempt in range(1, 6):
        print(console.run(spawn_code))
        try:
            player = wait_for(console,
                              "getattr(manager.scene, 'player', None) is not None",
                              timeout=8.0, what="scene.player")
            break
        except TimeoutError:
            print(f"spawn attempt {attempt} produced no player; retrying...")
    if player is None:
        raise TimeoutError("scene.player never appeared after 5 spawn attempts")
    print(f"player exists: {player}")
    print(console.run(
        "p = manager.scene.player\n"
        "_ = {'type': type(p).__name__, "
        "'attrs': [n for n in dir(p) if not n.startswith('_')][:80]}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
