"""Dedicated command-line lifecycle for the reconstructed retail tutorial."""

from __future__ import annotations

import argparse
import hashlib
import multiprocessing
from pathlib import Path
import sys
from typing import Sequence

from server.launcher import _emit_check_report, _parse_port, _run_server, _select_config
from server.release_check import CheckItem, CheckReport, run_release_check
from server.runtime_paths import RuntimePaths, read_version


SOURCE_ENTRYPOINT = Path(__file__).resolve().parents[1] / "run_tutorial.py"
TRAINING_MAP_NAME = "Training.vxl"
TRAINING_MAP_SHA256 = (
    "aea9cc551f46d449324d24e6fbf0be0c11fc76286d1b00cff6cfe036e4e2114d"
)


def build_parser() -> argparse.ArgumentParser:
    """Create the tutorial-only argument parser without starting a listener."""

    parser = argparse.ArgumentParser(
        prog="BattleSpadesTutorial",
        description="Reconstructed Ace of Spades training-level server",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--version",
        action="store_true",
        help="print the packaged tutorial/server version and exit",
    )
    action.add_argument(
        "--check",
        action="store_true",
        help="validate the release plus the byte-exact Training.vxl asset",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="load a TOML file for this process without changing config.toml",
    )
    parser.add_argument(
        "--port",
        type=_parse_port,
        default=None,
        help="override config.toml's UDP port for this tutorial process",
    )
    parser.add_argument(
        "--control-stdin",
        action="store_true",
        help="stop cleanly on parent stdin 'shutdown' or EOF",
    )
    return parser


def inspect_training_map(paths: RuntimePaths) -> str:
    """Validate the genuine retail map and return a compact success detail."""

    path = paths.maps / TRAINING_MAP_NAME
    if not path.is_file():
        raise OSError(f"missing tutorial map: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != TRAINING_MAP_SHA256:
        raise ValueError(
            f"unexpected Training.vxl SHA-256 {digest}; expected "
            f"{TRAINING_MAP_SHA256}"
        )
    return f"{path.name} sha256={digest}"


def configure_tutorial_runtime(
    config,
    paths: RuntimePaths,
    *,
    port: int | None = None,
):
    """Lock one loaded config to the isolated tutorial contract.

    The transform is in-memory only.  It never writes ``config.toml`` and it
    deliberately disables public registration, plugins, bots, voting/rotation,
    competitive damage, and ordinary map entities.  Shared network/logging
    settings remain operator-controlled unless explicitly listed below.
    """

    if port is not None and not 1 <= int(port) <= 65535:
        raise ValueError("tutorial port must be between 1 and 65535")

    config.tutorial_runtime = True
    config.name = "BattleSpades Tutorial"
    config.default_mode = "tut"
    config.default_map = "Training"
    config.maps_path = str(paths.maps)
    config.port = int(config.port if port is None else port)
    config.max_players = 12
    config.max_connections = 12
    config.score_limit = 0
    config.match_length_minutes = None
    config.mode_settings = {"tut": {"score_limit": 0, "time_limit": 0.0}}
    config.map_rotation = []
    config.end_screen_seconds = 0.0
    config.respawn_time = 0.0
    config.friendly_fire = False
    config.fall_damage = False
    config.water_damage = False
    config.auto_balance = False
    config.same_team_collision = False
    config.map_sync_mode = "full"
    config.entities_wire_ready = False

    config.bot_count = 0
    config.bots.configured = True
    config.bots.enabled = False
    config.bots.fill_target = 0
    config.bots.max_bots = 0
    config.plugins_enabled = False

    config.steam.enabled = False
    config.steam.public = False
    config.steam.require_registration = False
    config.revival.enabled = False
    config.revival.require_identity = False

    config.game_rules.apply({
        "RULE_ENABLE_BLOCKS": True,
        "RULE_ENABLE_FLARE_BLOCKS": False,
        "RULE_ENABLE_PREFABS": False,
        "RULE_ENABLE_GRAVESTONES": False,
        "RULE_ENABLE_CORPSE_EXPLOSION": False,
        "RULE_ENABLE_DEATH_CAM": False,
        "RULE_ENABLE_MINI_MAP": False,
        "RULE_ENABLE_SPECTATORS": False,
        "RULE_ENABLE_FALL_ON_WATER_DAMAGE": False,
        "RULE_ENABLE_COLOUR_PICKER": False,
        "RULE_RESPAWN_TIMES": 0,
        "RULE_ENABLE_EQUIPMENT_SPADE": True,
        "RULE_ENABLE_WEAPON_PISTOL": True,
    })
    return config


def _tutorial_check(paths: RuntimePaths) -> CheckReport:
    """Append tutorial-specific evidence to the standard bounded health check."""

    report = run_release_check(paths)
    items = list(report.items)
    try:
        detail = inspect_training_map(paths)
    except (OSError, ValueError) as exc:
        items.append(CheckItem("tutorial map", False, str(exc)))
    else:
        items.append(CheckItem("tutorial map", True, detail))
    return CheckReport(tuple(items))


def run(
    argv: Sequence[str] | None = None,
    *,
    paths: RuntimePaths | None = None,
) -> int:
    """Dispatch tutorial start/version/check without exposing it to run_server."""

    multiprocessing.freeze_support()
    try:
        arguments = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    runtime_paths = paths or RuntimePaths.discover(source_entry=SOURCE_ENTRYPOINT)
    if arguments.version:
        print(f"BattleSpades Tutorial {read_version(runtime_paths.root)}")
        return 0
    try:
        runtime_paths = _select_config(runtime_paths, arguments.config)
    except (OSError, ValueError) as exc:
        print(f"Tutorial startup failed: {exc}", file=sys.stderr)
        return 1
    if arguments.check:
        return _emit_check_report(_tutorial_check(runtime_paths))

    try:
        inspect_training_map(runtime_paths)
    except (OSError, ValueError) as exc:
        print(f"Tutorial startup failed: {exc}", file=sys.stderr)
        return 1

    # Registration is deliberately process-local and occurs only after the
    # dedicated command has validated its authentic map.  Importing modes or
    # running run_server.py therefore never makes `tut` selectable.
    from modes import register_mode
    from modes.tutorial import TutorialMode

    register_mode("tut", TutorialMode)
    return _run_server(
        runtime_paths,
        config_transform=lambda config: configure_tutorial_runtime(
            config,
            runtime_paths,
            port=arguments.port,
        ),
        banner="BattleSpades Tutorial - reconstructed retail training level",
        control_stdin=arguments.control_stdin,
    )


__all__ = [
    "TRAINING_MAP_NAME",
    "TRAINING_MAP_SHA256",
    "build_parser",
    "configure_tutorial_runtime",
    "inspect_training_map",
    "run",
]
