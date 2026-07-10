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
    return parser.parse_args(argv)


async def run_validation_server(args) -> None:
    source = load_config(args.config)
    config = build_validation_config(
        source,
        port=args.port,
        map_name=args.map_name,
        mode=args.mode,
    )
    server = BattleSpadesServer(config)
    loop = asyncio.get_running_loop()

    def request_stop(*_unused) -> None:
        if server.running:
            loop.call_soon_threadsafe(asyncio.create_task, server.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            signal.signal(sig, request_stop)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
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


def main(argv=None) -> int:
    args = parse_args(argv)
    asyncio.run(run_validation_server(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
