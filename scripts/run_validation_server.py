"""Run an isolated BattleSpades parity server without editing config.toml."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.config import load_config  # noqa: E402
from server.main import BattleSpadesServer  # noqa: E402
from server.logging_runtime import configure_logging  # noqa: E402
from server.validation import (  # noqa: E402
    DEFAULT_VALIDATION_PORT,
    build_validation_config,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Start an isolated BattleSpades parity server.",
    )
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--port", type=int, default=DEFAULT_VALIDATION_PORT)
    parser.add_argument("--map", dest="map_name", default="ArcticBase")
    parser.add_argument("--mode", default="tdm")
    parser.add_argument(
        "--infection-delay",
        type=float,
        help="Zombie-mode outbreak delay override for bounded retail tests",
    )
    parser.add_argument(
        "--fixed-spawn",
        nargs=2,
        type=int,
        metavar=("X", "Y"),
        help="use one deterministic dry spawn column for parity A/B runs",
    )
    parser.add_argument(
        "--fixed-spawn-step",
        type=int,
        default=0,
        help="advance each validation spawn by this many X columns",
    )
    parser.add_argument("--worldupdate-loop-offset", type=int)
    parser.add_argument("--worldupdate-self-row-interval", type=int)
    parser.add_argument("--worldupdate-airborne-self-row-interval", type=int)
    parser.add_argument("--jetpack-owner-handoff-input-frames", type=int)
    parser.add_argument("--jetpack-owner-release-handoff-input-frames", type=int)
    parser.add_argument("--movement-authority", choices=("server", "client"))
    parser.add_argument("--movement-input-latch", type=int, choices=(0, 1))
    parser.add_argument(
        "--worldupdate-include-self",
        choices=("true", "false"),
        help="A/B local reconciliation rows without editing production config",
    )
    parser.add_argument("--debug-selfrow", action="store_true")
    parser.add_argument(
        "--debug-parity",
        action="store_true",
        help="enable bounded client/server movement parity capture",
    )
    parser.add_argument(
        "--packet-trace",
        action="store_true",
        help="validation only: log decoded packet bytes at DEBUG level",
    )
    return parser.parse_args(argv)


async def run_validation_server(args) -> None:
    source = load_config(args.config)
    config = build_validation_config(
        source,
        port=args.port,
        map_name=args.map_name,
        mode=args.mode,
    )
    if args.worldupdate_loop_offset is not None:
        config.worldupdate_loop_offset = args.worldupdate_loop_offset
    if args.worldupdate_self_row_interval is not None:
        config.worldupdate_self_row_interval = max(
            1,
            int(args.worldupdate_self_row_interval),
        )
    if args.worldupdate_airborne_self_row_interval is not None:
        config.worldupdate_airborne_self_row_interval = max(
            1,
            int(args.worldupdate_airborne_self_row_interval),
        )
    if args.jetpack_owner_handoff_input_frames is not None:
        config.jetpack_owner_handoff_input_frames = max(
            0,
            min(120, int(args.jetpack_owner_handoff_input_frames)),
        )
    if args.jetpack_owner_release_handoff_input_frames is not None:
        config.jetpack_owner_release_handoff_input_frames = max(
            0,
            min(1200, int(args.jetpack_owner_release_handoff_input_frames)),
        )
    if args.movement_authority is not None:
        config.movement_authority = args.movement_authority
    if args.movement_input_latch is not None:
        config.movement_input_latch_frames = args.movement_input_latch
    if args.worldupdate_include_self is not None:
        config.worldupdate_include_self = args.worldupdate_include_self == "true"
    if args.debug_selfrow:
        config.debug_selfrow = True
    if args.debug_parity:
        config.debug_parity = True
        config.movement_debug_capture = True
    if args.packet_trace:
        # Packet hex formatting is intentionally opt-in and must never leak
        # into the production configuration or ordinary simulation hot path.
        config.packet_trace = True
        config.log_level = "DEBUG"
        config.log_suppress_packets = []
    if args.infection_delay is not None:
        # Copy both mapping levels: validation overrides must not mutate the
        # production config object returned by load_config or leak into a
        # later in-process scenario.
        settings = dict(getattr(config, "mode_settings", {}) or {})
        zombie_settings = dict(settings.get("zom", {}) or {})
        zombie_settings["infection_delay"] = max(
            0.0, float(args.infection_delay)
        )
        settings["zom"] = zombie_settings
        config.mode_settings = settings
    server = BattleSpadesServer(config)
    if args.fixed_spawn is not None:
        spawn_x, spawn_y = (int(value) for value in args.fixed_spawn)
        spawn_index = 0

        def fixed_spawn_point(_team: int) -> tuple[float, float, float]:
            """Resolve the requested column after the validation map loads."""
            nonlocal spawn_index
            requested_x = spawn_x + spawn_index * int(args.fixed_spawn_step)
            spawn_index += 1
            return server.world_manager.dry_ground_anchor(requested_x, spawn_y)

        server.world_manager.get_spawn_point = fixed_spawn_point
    loop = asyncio.get_running_loop()

    def request_stop(*_unused) -> None:
        if server.running:
            loop.call_soon_threadsafe(asyncio.create_task, server.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            signal.signal(sig, request_stop)

    logging_runtime = configure_logging(config, ROOT / "logs")
    logging.getLogger(__name__).info(
        "validation server: port=%d map=%s mode=%s",
        config.port,
        config.map_name,
        config.game_mode,
    )

    try:
        await server.start()
    finally:
        await server.stop()
        logging.getLogger(__name__).info(
            "validation logging dropped_records=%d",
            logging_runtime.dropped_records,
        )
        logging_runtime.stop()


def main(argv=None) -> int:
    args = parse_args(argv)
    asyncio.run(run_validation_server(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
