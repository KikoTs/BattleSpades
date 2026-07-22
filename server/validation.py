"""Safe configuration helpers for isolated parity servers."""

from __future__ import annotations

from copy import deepcopy

from server.config import ServerConfig


PUBLIC_SERVER_PORT = 27015
DEFAULT_VALIDATION_PORT = 27016


def build_validation_config(
    source: ServerConfig,
    *,
    port: int = DEFAULT_VALIDATION_PORT,
    map_name: str = "ArcticBase",
    mode: str = "tdm",
) -> ServerConfig:
    """Return an isolated config without mutating the live configuration."""
    if int(port) == PUBLIC_SERVER_PORT:
        raise ValueError("validation server cannot use the public server port")

    config = deepcopy(source)
    config.port = int(port)
    config.default_map = str(map_name)
    config.default_mode = str(mode)
    config.name = f"{source.name} [VALIDATION]"
    config.bot_count = 0
    # An explicit [bots] table supersedes the legacy count. Validation must
    # disable both sources or a production backfill target silently injects
    # combat traffic into supposedly isolated movement captures.
    config.bots.enabled = False
    return config
