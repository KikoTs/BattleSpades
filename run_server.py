#!/usr/bin/env python3
"""
BattleSpades - Ace of Spades Server
Protocol 1.0 Battle Builders

Entry point for the server.
"""
import asyncio
import sys
import signal
import logging
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from server.main import BattleSpadesServer
from server.config import load_config

# Configure logging — NON-BLOCKING. Handlers that write to stdout/files run
# synchronously on the calling thread by default, and the game loop logs
# from the asyncio thread: a slow console pipe or disk flush would stall
# the 60Hz simulation (visible as movement lag bursts). All records go into
# a queue; a dedicated background thread drains it to the real handlers.
import queue
from logging.handlers import QueueHandler, QueueListener

_log_format = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
)

log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_format)
_file_handler = logging.FileHandler(log_dir / "log.txt", mode="a")
_file_handler.setFormatter(_log_format)

_log_queue: "queue.SimpleQueue" = queue.SimpleQueue()
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_root.addHandler(QueueHandler(_log_queue))
_log_listener = QueueListener(
    _log_queue, _console_handler, _file_handler, respect_handler_level=True
)
# Start the listener on a DAEMON thread (mirrors QueueListener.start
# internals): if the main thread dies, a non-daemon thread would keep a
# zombie process alive that still owns the server port.
import threading
_log_listener._thread = _log_listener_thread = threading.Thread(
    target=_log_listener._monitor, daemon=True
)
_log_listener_thread.start()

logger = logging.getLogger("BattleSpades")

# Native crashes (segfaults in enet/Cython extensions) kill the process with
# no Python traceback; faulthandler writes the thread stacks to a file so
# they stop being silent.
import faulthandler
_faulthandler_file = open(log_dir / "faulthandler.log", "a")
faulthandler.enable(_faulthandler_file)


def handle_shutdown(server: BattleSpadesServer):
    """Signal handler for graceful shutdown."""
    logger.info("Shutdown signal received...")
    asyncio.create_task(server.stop())


async def main():
    """Main entry point."""
    logger.info("=" * 50)
    logger.info("BattleSpades Server - Protocol 1.0 Battle Builders")
    logger.info("=" * 50)

    # Load configuration
    config_path = Path(__file__).parent / "config.toml"
    config = load_config(config_path)
    
    # Apply log level from config
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(log_level)
    logger.info(f"Log level set to: {config.log_level.upper()}")

    # Create and start server
    server = BattleSpadesServer(config)

    # Setup signal handlers
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: handle_shutdown(server))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            signal.signal(sig, lambda s, f: handle_shutdown(server))

    try:
        await server.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        await server.stop()

    logger.info("Server stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
