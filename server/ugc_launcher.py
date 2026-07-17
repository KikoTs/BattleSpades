"""Dedicated command-line lifecycle for the reconstructed Map Creator."""

from __future__ import annotations

import argparse
import multiprocessing
from pathlib import Path
import sys
from typing import Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 release fallback
    tomllib = None

import toml

from server.launcher import _emit_check_report, _run_server
from server.release_check import CheckItem, CheckReport, run_release_check
from server.runtime_paths import RuntimePaths, read_version
from server.ugc_project import (
    TERRAINS,
    TARGET_MODES,
    UGCAssetLayout,
    UGCProject,
    create_project_files,
    discover_ugc_assets,
    project_slug,
    read_baseplate_presentation,
    terrain_spec,
)


SOURCE_ENTRYPOINT = Path(__file__).resolve().parents[1] / "run_map_creator.py"


def build_parser() -> argparse.ArgumentParser:
    """Create the side-effect-free Map Creator command parser."""

    parser = argparse.ArgumentParser(
        prog="BattleSpadesMapCreator",
        description="Retail-compatible Ace of Spades hosted UGC Map Creator",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--version", action="store_true", help="print version and exit")
    action.add_argument(
        "--check",
        action="store_true",
        help="validate the server plus all retail UGC baseplates and prefabs",
    )
    parser.add_argument(
        "--project",
        default=None,
        help=(
            "project name or path to a .ugc sidecar "
            "(default: [map_creator].project or MyUGCMap)"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="directory for new project triplets (default: ugc-projects)",
    )
    parser.add_argument(
        "--terrain",
        choices=[terrain.key for terrain in TERRAINS],
        default=None,
        help="terrain for a new project (default: grassland)",
    )
    parser.add_argument(
        "--target-mode",
        choices=list(TARGET_MODES),
        default=None,
        help="validation/game mode authored by editor objects (default: tdm)",
    )
    parser.add_argument("--title", default=None, help="new-project display title")
    parser.add_argument("--description", default=None, help="new-project description")
    parser.add_argument("--author", default=None, help="new-project author")
    parser.add_argument(
        "--retail-root",
        default=None,
        help="Ace of Spades directory containing ugc/maps and ugc/kv6",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing project from the selected baseplate",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="override config.toml's UDP port for this editor process",
    )
    return parser


def apply_map_creator_config(arguments, paths: RuntimePaths):
    """Overlay non-destructive ``[map_creator]`` defaults beneath CLI flags.

    The normal server deliberately ignores this launcher-only table. Relative
    paths are anchored to the portable application root, so a source checkout
    and an extracted release save and reopen the same project layout regardless
    of the terminal's current directory. ``--overwrite`` remains CLI-only to
    prevent a persistent config typo from replacing an existing map.
    """

    values = {}
    if paths.config.is_file():
        try:
            if tomllib is not None:
                with paths.config.open("rb") as stream:
                    document = tomllib.load(stream)
            else:  # pragma: no cover - exercised by Python 3.10 releases
                document = toml.load(paths.config)
        except Exception as exc:
            raise ValueError(
                f"cannot read Map Creator defaults from {paths.config}: {exc}"
            ) from exc
        values = document.get("map_creator", {})
        if not isinstance(values, dict):
            raise ValueError("config.toml [map_creator] must be a TOML table")

    def configured(name: str, fallback=None):
        current = getattr(arguments, name)
        if current not in (None, ""):
            return current
        value = values.get(name, fallback)
        return fallback if value in (None, "") else value

    arguments.project = str(configured("project", "MyUGCMap"))
    output_dir = configured("output_dir", None)
    arguments.output_dir = None if output_dir is None else str(output_dir)
    terrain = configured("terrain", None)
    if terrain is not None:
        terrain = str(terrain).strip().casefold()
        if terrain not in {item.key for item in TERRAINS}:
            raise ValueError(
                "map_creator.terrain must be one of "
                + ", ".join(item.key for item in TERRAINS)
            )
    arguments.terrain = terrain
    target_mode = configured("target_mode", None)
    if target_mode is not None:
        target_mode = str(target_mode).strip().casefold()
        if target_mode not in TARGET_MODES:
            raise ValueError(
                "map_creator.target_mode must be one of "
                + ", ".join(TARGET_MODES)
            )
    arguments.target_mode = target_mode
    for field in ("title", "description", "author"):
        value = configured(field, None)
        setattr(arguments, field, None if value is None else str(value))
    retail_root = configured("retail_root", None)
    arguments.retail_root = (
        None
        if retail_root is None
        else str(paths.resolve_configured_path(str(retail_root)))
    )
    return arguments


def resolve_project_paths(
    paths: RuntimePaths,
    project_value: str,
    output_dir: str | Path | None,
) -> tuple[str, Path, Path, Path]:
    """Resolve one portable VXL/TXT/UGC project triplet."""

    supplied = Path(project_value).expanduser()
    is_path = supplied.suffix.casefold() == ".ugc" or supplied.parent != Path(".")
    if is_path:
        sidecar = supplied if supplied.is_absolute() else paths.root / supplied
        sidecar = sidecar.resolve()
        slug = project_slug(sidecar.stem)
        sidecar = sidecar.with_name(slug + ".ugc")
        directory = sidecar.parent
    else:
        slug = project_slug(project_value)
        directory = (
            paths.root / "ugc-projects"
            if output_dir is None
            else paths.resolve_configured_path(output_dir)
        )
        sidecar = directory / f"{slug}.ugc"
    return slug, directory, directory / f"{slug}.vxl", sidecar


def prepare_ugc_project(
    paths: RuntimePaths,
    arguments,
    assets: UGCAssetLayout,
) -> tuple[UGCProject, Path, Path, Path]:
    """Load an existing project or atomically create it from a baseplate."""

    slug, directory, vxl_path, sidecar_path = resolve_project_paths(
        paths, arguments.project, arguments.output_dir
    )
    metadata_path = directory / f"{slug}.txt"
    if sidecar_path.is_file() and not arguments.overwrite:
        if not vxl_path.is_file() or not metadata_path.is_file():
            raise FileNotFoundError(
                f"project sidecar exists but VXL/TXT sibling is missing: {sidecar_path}"
            )
        project = UGCProject.load(
            sidecar_path,
            target_mode=arguments.target_mode,
        )
        if arguments.terrain and project.terrain.key != arguments.terrain:
            raise ValueError(
                "--terrain cannot replace an existing project's baseplate; "
                "use --overwrite to start a new project"
            )
        if arguments.title:
            project.title = str(arguments.title)[:80]
        if arguments.description:
            project.description = str(arguments.description)[:512]
        if arguments.author:
            project.author = str(arguments.author)[:80]
        if arguments.target_mode:
            project.set_target_mode(arguments.target_mode)
        project.save(sidecar_path)
        return project, vxl_path, metadata_path, sidecar_path

    terrain = terrain_spec(arguments.terrain or "grassland")
    source_vxl, source_metadata = assets.terrain_files(terrain)
    skybox, ground_colors = read_baseplate_presentation(source_metadata)
    project = UGCProject(
        title=arguments.title or slug,
        description=arguments.description or arguments.title or slug,
        author=arguments.author or "Unknown",
        baseplate=terrain.stem,
        target_mode=arguments.target_mode or "tdm",
        ground_colors=ground_colors,
        skybox_name=skybox,
        tags=["map", arguments.target_mode or "tdm"],
    )
    # create_project_files owns the actual byte-for-byte baseplate copies.
    create_project_files(
        project,
        directory,
        slug,
        assets,
        overwrite=bool(arguments.overwrite),
    )
    return project, vxl_path, metadata_path, sidecar_path


def configure_ugc_runtime(
    config,
    *,
    paths: RuntimePaths,
    assets: UGCAssetLayout,
    project: UGCProject,
    vxl_path: Path,
    metadata_path: Path,
    sidecar_path: Path,
    port: int | None = None,
):
    """Lock a loaded config to the isolated non-competitive editor contract."""

    if port is not None and not 1 <= int(port) <= 65535:
        raise ValueError("Map Creator port must be between 1 and 65535")
    compatible_prefabs = assets.compatible_prefabs(project.terrain)
    if not compatible_prefabs:
        raise FileNotFoundError(
            f"no retail UGC prefabs match {project.baseplate} in {assets.prefabs}"
        )

    config.ugc_runtime = True
    config.ugc_project = project
    config.ugc_sidecar_path = str(sidecar_path)
    config.ugc_vxl_path = str(vxl_path)
    config.ugc_metadata_path = str(metadata_path)
    config.ugc_preview_path = str(sidecar_path.with_suffix(".png"))
    config.ugc_prefabs = compatible_prefabs
    config.ugc_target_mode = project.target_mode
    config.prefab_search_dirs = (str(paths.prefabs), str(assets.prefabs))

    config.name = f"Map Creator - {project.title}"
    config.default_mode = "ugc"
    config.default_map = vxl_path.stem
    config.maps_path = str(vxl_path.parent)
    config.port = int(config.port if port is None else port)
    config.max_players = 12
    config.max_connections = 12
    config.score_limit = 0
    config.match_length_minutes = None
    config.mode_settings = {"ugc": {"score_limit": 0, "time_limit": 0.0}}
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
    config.max_map_mutation_journal = max(
        65536, int(getattr(config, "max_map_mutation_journal", 8192))
    )
    config.prefab_cell_batch_limit = 512
    config.prefab_validation_batch_limit = 1024
    config.prefab_queue_limit = 64

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
        "RULE_ENABLE_FLARE_BLOCKS": True,
        "RULE_ENABLE_PREFABS": True,
        "RULE_ENABLE_GRAVESTONES": False,
        "RULE_ENABLE_CORPSE_EXPLOSION": False,
        "RULE_ENABLE_DEATH_CAM": False,
        "RULE_ENABLE_MINI_MAP": True,
        "RULE_ENABLE_SPECTATORS": False,
        "RULE_ENABLE_FALL_ON_WATER_DAMAGE": False,
        "RULE_ENABLE_COLOUR_PICKER": True,
        "RULE_RESPAWN_TIMES": 0,
    })
    return config


def _ugc_check(paths: RuntimePaths, retail_root: str | Path | None) -> CheckReport:
    """Append genuine retail editor-asset evidence to the release check."""

    report = run_release_check(paths)
    items = list(report.items)
    try:
        assets = discover_ugc_assets(retail_root)
        details = []
        for terrain in TERRAINS:
            vxl, metadata = assets.terrain_files(terrain)
            details.append(f"{vxl.name}+{metadata.name}")
        prefab_count = len(tuple(assets.prefabs.glob("*.kv6")))
        if prefab_count < 400:
            raise ValueError(
                f"retail UGC prefab catalog is incomplete ({prefab_count}; expected 400+)"
            )
    except (OSError, ValueError) as exc:
        items.append(CheckItem("UGC retail assets", False, str(exc)))
    else:
        items.append(
            CheckItem(
                "UGC retail assets",
                True,
                f"9 terrain triplets, {prefab_count} KV6 models at {assets.root}",
            )
        )
    return CheckReport(tuple(items))


def run(
    argv: Sequence[str] | None = None,
    *,
    paths: RuntimePaths | None = None,
) -> int:
    """Dispatch Map Creator start/version/check without changing run_server."""

    multiprocessing.freeze_support()
    try:
        arguments = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    runtime_paths = paths or RuntimePaths.discover(source_entry=SOURCE_ENTRYPOINT)
    if arguments.version:
        print(f"BattleSpades Map Creator {read_version(runtime_paths.root)}")
        return 0
    try:
        arguments = apply_map_creator_config(arguments, runtime_paths)
    except ValueError as exc:
        print(f"Map Creator startup failed: {exc}", file=sys.stderr)
        return 1
    if arguments.check:
        return _emit_check_report(_ugc_check(runtime_paths, arguments.retail_root))

    try:
        assets = discover_ugc_assets(arguments.retail_root)
        project, vxl_path, metadata_path, sidecar_path = prepare_ugc_project(
            runtime_paths, arguments, assets
        )
    except (OSError, ValueError) as exc:
        print(f"Map Creator startup failed: {exc}", file=sys.stderr)
        return 1

    from modes import register_mode
    from modes.ugc import UGCMode

    register_mode("ugc", UGCMode)
    print(f"Project sidecar: {sidecar_path}")
    print(f"Editable VXL:    {vxl_path}")
    print(f"Map metadata:   {metadata_path}")
    print(
        "Reopen command: "
        f'run_map_creator.py --project "{sidecar_path}"'
    )
    return _run_server(
        runtime_paths,
        config_transform=lambda config: configure_ugc_runtime(
            config,
            paths=runtime_paths,
            assets=assets,
            project=project,
            vxl_path=vxl_path,
            metadata_path=metadata_path,
            sidecar_path=sidecar_path,
            port=arguments.port,
        ),
        banner="BattleSpades Map Creator - reconstructed retail UGC host",
    )


__all__ = [
    "build_parser",
    "apply_map_creator_config",
    "configure_ugc_runtime",
    "prepare_ugc_project",
    "resolve_project_paths",
    "run",
]
