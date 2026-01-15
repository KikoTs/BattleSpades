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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Add file handler
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
file_handler = logging.FileHandler(log_dir / "log.txt", mode='a')
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(file_handler)

logger = logging.getLogger("BattleSpades")


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
