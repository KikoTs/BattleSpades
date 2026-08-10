"""Long-running production lifecycle soak across every VXL and game mode.

The harness keeps one real ``BattleSpadesServer`` and one retained bot roster
alive while repeatedly exercising the production map rollover and same-map
round restart paths. Each boundary is deliberately seeded with unsupported
positions and stale controller data, then checked before AI ticks resume.

Examples::

    py -3.12 scripts/bot_transition_soak.py
    py -3.12 scripts/bot_transition_soak.py --map London --mode zom --cycles 8
    py -3.12 scripts/bot_transition_soak.py --json validation-reports/bot-transition.json
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from modes import get_mode_class
from scripts.bot_map_matrix import shipped_maps
from server.bot_ai.director import BotDirector
from server.config import ServerConfig
from server.main import BattleSpadesServer


CANONICAL_MODES = (
    "ctf",
    "cctf",
    "tdm",
    "arena",
    "vip",
    "zom",
    "mh",
    "tc",
    "dia",
    "dem",
    "oc",
)


@dataclass(slots=True)
class TransitionSoakResult:
    """Serializable lifecycle acceptance result."""

    maps: tuple[str, ...]
    modes: tuple[str, ...]
    cycles: int
    games_per_session: int
    bot_count: int
    worker: str
    session_count: int = 0
    game_boundaries: int = 0
    map_epoch: int = 0
    mode_epoch: int = 0
    clean_slate_resets: int = 0
    expected_clean_slate_resets: int = 0
    checked_spawns: int = 0
    elapsed_seconds: float = 0.0
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _poison_completed_game_state(director: BotDirector) -> None:
    """Seed the exact stale state that a lifecycle boundary must retire."""

    for bot in director.bots:
        bot.set_position(bot.x, bot.y, max(-96.0, float(bot.z) - 48.0))
        runtime = director._runtime[int(bot.id)]
        runtime.last_intent_frame = 2**30
        runtime.feedback_action_kind = "stale-game"
        runtime.feedback_action_accepted = False
        runtime.next_wall_probe_at = float("inf")
        runtime.wall_probe_clear = False
        runtime.burst_remaining = 99
        director._pending_gateway_actions[int(bot.id)] = (
            2**30,
            object(),
            float("inf"),
        )


def _validate_fresh_game(
    server: BattleSpadesServer,
    director: BotDirector,
    result: TransitionSoakResult,
    *,
    label: str,
) -> None:
    """Check spawn, world ownership, and controller postconditions."""

    world = server.world_manager
    if len(director.bots) != result.bot_count:
        result.failures.append(
            f"{label}: roster changed {len(director.bots)} != {result.bot_count}"
        )
    if director._pending_gateway_actions:
        result.failures.append(f"{label}: pending gateway actions survived")

    for bot in director.bots:
        result.checked_spawns += 1
        runtime = director._runtime.get(int(bot.id))
        if runtime is None:
            result.failures.append(f"{label}: bot {bot.id} has no runtime")
            continue
        if not world.spawn_position_is_safe(bot.position):
            result.failures.append(
                f"{label}: bot {bot.id} unsafe at {tuple(bot.position)!r}"
            )
        if bot._world_parent is not world.world:
            result.failures.append(
                f"{label}: bot {bot.id} owns a retired native world"
            )
        if not bot.alive or not bot.spawned:
            result.failures.append(f"{label}: bot {bot.id} was not respawned")
        if runtime.intent is not None or runtime.pending_action is not None:
            result.failures.append(f"{label}: bot {bot.id} retained an action")
        if runtime.last_intent_frame != -1:
            result.failures.append(
                f"{label}: bot {bot.id} retained frame {runtime.last_intent_frame}"
            )
        if runtime.feedback_action_kind:
            result.failures.append(
                f"{label}: bot {bot.id} retained action feedback"
            )
        if runtime.burst_remaining != 0 or not runtime.wall_probe_clear:
            result.failures.append(f"{label}: bot {bot.id} retained motor state")


async def _tick_live_bots(
    server: BattleSpadesServer,
    director: BotDirector,
    ticks: int,
) -> None:
    """Let the production planner, gateway, and native physics consume a game."""

    for _ in range(max(0, int(ticks))):
        server.loop_count += 1
        await director.update(1.0 / 60.0)
        for bot in tuple(director.bots):
            if bot.alive and bot.spawned:
                await bot.update(1.0 / 60.0)
        await server.mode.on_tick(server.loop_count)
        await asyncio.sleep(0)


async def run_transition_soak(
    *,
    maps: tuple[str, ...] | None = None,
    modes: tuple[str, ...] = CANONICAL_MODES,
    cycles: int = 1,
    games_per_session: int = 4,
    bots: int = 6,
    settle_ticks: int = 12,
    worker: str = "thread",
) -> TransitionSoakResult:
    """Exercise all requested map/mode pairs on one retained server runtime."""

    selected_maps = tuple(maps or shipped_maps())
    selected_modes = tuple(str(mode).lower() for mode in modes)
    selected_worker = str(worker).strip().lower()
    result = TransitionSoakResult(
        maps=selected_maps,
        modes=selected_modes,
        cycles=max(1, int(cycles)),
        games_per_session=max(1, int(games_per_session)),
        bot_count=max(1, int(bots)),
        worker=selected_worker,
    )
    if not selected_maps or not selected_modes:
        result.failures.append("at least one map and mode are required")
        return result
    for mode_name in selected_modes:
        if get_mode_class(mode_name) is None:
            result.failures.append(f"unknown mode: {mode_name}")
    if selected_worker not in {"thread", "process"}:
        result.failures.append(f"unknown worker: {selected_worker}")
    if result.failures:
        return result

    started_at = time.perf_counter()
    config = ServerConfig(
        default_map=selected_maps[0],
        default_mode=selected_modes[0],
        maps_path=str(ROOT / "maps"),
        prefabs_path=str(ROOT / "prefabs"),
    )
    config.bots.enabled = True
    config.bots.configured = True
    config.bots.population_mode = "fixed"
    config.bots.fill_target = result.bot_count
    config.bots.max_bots = result.bot_count
    config.bots.worker = selected_worker
    config.bots.clean_slate_games = 3

    server = BattleSpadesServer(config)
    if not server.world_manager.load_map(config.default_map):
        result.failures.append(f"failed to load initial map {config.default_map}")
        return result
    mode_class = get_mode_class(config.default_mode)
    if mode_class is None:
        result.failures.append(f"failed to resolve initial mode {config.default_mode}")
        return result
    server.mode = mode_class(server)
    await server.mode.on_mode_start()

    director = BotDirector(server)
    server.bots = director
    await director.start(initial_count=result.bot_count)
    first_session = True
    try:
        for cycle in range(result.cycles):
            for mode_name in selected_modes:
                for map_name in selected_maps:
                    label = f"cycle={cycle} mode={mode_name} map={map_name}"
                    if first_session:
                        first_session = False
                    else:
                        _poison_completed_game_state(director)
                        previous_map_epoch = director._map_epoch
                        candidate = await asyncio.to_thread(
                            server.match_transition._load_world_candidate,
                            map_name,
                            mode_name,
                        )
                        transition = await server.match_transition._rollover(
                            map_name=map_name,
                            mode_name=mode_name,
                            candidate_world=candidate,
                        )
                        result.game_boundaries += 1
                        if not transition.ok:
                            result.failures.append(
                                f"{label}: rollover failed: {transition.message}"
                            )
                            continue
                        if director._map_epoch != previous_map_epoch + 1:
                            result.failures.append(
                                f"{label}: map epoch did not advance exactly once"
                            )
                        _validate_fresh_game(
                            server,
                            director,
                            result,
                            label=f"{label} rollover",
                        )

                    result.session_count += 1
                    await _tick_live_bots(server, director, settle_ticks)
                    for game_index in range(1, result.games_per_session):
                        _poison_completed_game_state(director)
                        previous_mode_epoch = director._mode_epoch
                        restart = await server.match_transition.restart_round()
                        result.game_boundaries += 1
                        restart_label = f"{label} game={game_index + 1}"
                        if not restart.ok:
                            result.failures.append(
                                f"{restart_label}: restart failed: {restart.message}"
                            )
                            continue
                        if director._mode_epoch != previous_mode_epoch + 1:
                            result.failures.append(
                                f"{restart_label}: mode epoch did not advance"
                            )
                        _validate_fresh_game(
                            server,
                            director,
                            result,
                            label=restart_label,
                        )
                        await _tick_live_bots(server, director, settle_ticks)
    finally:
        await director.close()
        server.bots = None

    result.map_epoch = director._map_epoch
    result.mode_epoch = director._mode_epoch
    result.clean_slate_resets = director._clean_slate_resets
    result.expected_clean_slate_resets = result.game_boundaries // 3
    if result.clean_slate_resets != result.expected_clean_slate_resets:
        result.failures.append(
            "clean-slate cadence mismatch: "
            f"{result.clean_slate_resets} != {result.expected_clean_slate_resets}"
        )
    result.elapsed_seconds = round(time.perf_counter() - started_at, 3)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", action="append", dest="maps")
    parser.add_argument("--mode", action="append", dest="modes")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--games-per-session", type=int, default=4)
    parser.add_argument("--bots", type=int, default=6)
    parser.add_argument("--settle-ticks", type=int, default=12)
    parser.add_argument("--worker", choices=("thread", "process"), default="thread")
    parser.add_argument("--json", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = asyncio.run(
        run_transition_soak(
            maps=tuple(args.maps) if args.maps else None,
            modes=tuple(args.modes) if args.modes else CANONICAL_MODES,
            cycles=args.cycles,
            games_per_session=args.games_per_session,
            bots=args.bots,
            settle_ticks=args.settle_ticks,
            worker=args.worker,
        )
    )
    document = asdict(result)
    document["passed"] = result.passed
    rendered = json.dumps(document, indent=2, sort_keys=True)
    print(rendered)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
