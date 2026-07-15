"""Scene-safe skeletons for the five remaining retail Match Lobby modes.

These classes deliberately implement only shared lifecycle, map resources,
clock handling, and validated configuration.  Registering the correct retail
mode IDs lets clients enter the intended scenes while future objective logic
can be added behind ordinary BaseMode hooks without another composition-root
rewrite.  They must not claim score until their objective services exist.
"""

from __future__ import annotations

import logging

from server import mode_data

from .base_mode import BaseMode


logger = logging.getLogger(__name__)


class _LobbyModeSkeleton(BaseMode):
    """Common bounded lifecycle for a recovered but incomplete objective mode."""

    mode_code = "nor"

    def __init__(self, server) -> None:
        super().__init__(server)
        data = mode_data.get(self.mode_code)
        self.score_limit = int(data.default_score_limit)
        self.time_limit = server.config.configured_time_limit(
            self.mode_code, data.default_time_limit
        )

    async def on_mode_start(self) -> None:
        await super().on_mode_start()
        for team in self.server.teams.values():
            team.reset()
        logger.warning(
            "%s started as a scene-safe objective skeleton; clock and shared "
            "resources are active, objective scoring is not implemented",
            self.name,
        )


class MultiHillMode(_LobbyModeSkeleton):
    """Multi-Hill shell with recovered base-count and rotation timing rules."""

    name = "Multi-Hill"
    description = "Capture the rotating active command posts."
    mode_code = "mh"

    def __init__(self, server) -> None:
        super().__init__(server)
        self.max_active_bases = int(server.config.mode_rule(
            "mh", "max_active_bases", "RULE_MULTIHILL_MAX_ACTIVE_BASES"
        ))
        self.base_active_time = float(server.config.mode_rule(
            "mh", "base_active_time", "RULE_BASE_ACTIVE_TIME"
        ))


class TerritoryControlMode(_LobbyModeSkeleton):
    """Gangster Territory Control shell with capture-rate configuration."""

    name = "Territory Control"
    description = "Capture and hold active territories."
    mode_code = "tc"

    def __init__(self, server) -> None:
        super().__init__(server)
        self.max_active_bases = int(server.config.mode_rule(
            "tc", "max_active_bases", "RULE_TC_MAX_ACTIVE_BASES"
        ))
        self.capture_rate = float(server.config.mode_rule(
            "tc", "capture_rate", "RULE_CAPTURE_RATE"
        ))


class DiamondMineMode(_LobbyModeSkeleton):
    """Diamond Mine shell retaining every recovered lobby rule."""

    name = "Diamond Mine"
    description = "Mine and deliver active diamonds for your team."
    mode_code = "dia"

    def __init__(self, server) -> None:
        super().__init__(server)
        self.max_active_bases = int(server.config.mode_rule(
            "dia", "max_active_bases", "RULE_DIAMOND_MAX_ACTIVE_BASES"
        ))
        self.score_limit = int(server.config.mode_rule(
            "dia", "score_limit", "RULE_DIA_SCORE_TARGET"
        ))
        self.max_active_diamonds = int(server.config.mode_rule(
            "dia", "max_active_diamonds", "RULE_MAX_ACTIVE_DIAMONDS"
        ))
        self.diamond_lifetime = float(server.config.mode_rule(
            "dia", "diamond_lifetime", "RULE_DIAMOND_LIFETIME"
        ))


class DemolitionMode(_LobbyModeSkeleton):
    """Demolition shell retaining the build-state duration switch."""

    name = "Demolition"
    description = "Build defenses, then destroy the opposing objective."
    mode_code = "dem"

    def __init__(self, server) -> None:
        super().__init__(server)
        value = server.config.mode_rule(
            "dem", "build_state_length", "RULE_BUILD_STATE_LENGTH"
        )
        self.build_state_length = 0.0 if value is False else float(value)


class OccupationMode(_LobbyModeSkeleton):
    """Occupation shell retaining bomb count, fuse, and score rules."""

    name = "Occupation"
    description = "Plant and defend bombs at occupation objectives."
    mode_code = "oc"

    def __init__(self, server) -> None:
        super().__init__(server)
        score = server.config.mode_rule(
            "oc", "score_limit", "RULE_OCC_SCORE_TARGET"
        )
        self.score_limit = 0 if score is False else int(score)
        self.max_active_bombs = int(server.config.mode_rule(
            "oc", "max_active_bombs", "RULE_MAX_ACTIVE_BOMBS"
        ))
        self.bomb_fuse_time = float(server.config.mode_rule(
            "oc", "bomb_fuse_time", "RULE_BOMB_FUSE_TIME"
        ))


__all__ = [
    "DemolitionMode",
    "DiamondMineMode",
    "MultiHillMode",
    "OccupationMode",
    "TerritoryControlMode",
]
