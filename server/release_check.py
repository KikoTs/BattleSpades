"""Self-contained validation used by source and packaged server builds."""

from __future__ import annotations

import importlib
import multiprocessing
import queue
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import toml

from server.config import load_config
from server.runtime_paths import RuntimePaths, apply_runtime_paths, read_version


_NATIVE_MODULES = (
    "enet",
    "shared.bytes",
    "shared.glm",
    "shared.packet",
    "aoslib.vxl",
    "aoslib.kv6",
    "aoslib.world",
    "server.bot_ai.recast",
)


@dataclass(frozen=True, slots=True)
class CheckItem:
    """One named health-check outcome."""

    name: str
    ok: bool
    detail: str

    @property
    def line(self) -> str:
        """Render a stable human- and CI-readable status line."""

        status = "OK" if self.ok else "FAIL"
        return f"{status} {self.name}: {self.detail}"


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Complete bounded release validation report."""

    items: tuple[CheckItem, ...]

    @property
    def ok(self) -> bool:
        """Return true only when every recorded check succeeded."""

        return bool(self.items) and all(item.ok for item in self.items)

    @property
    def exit_code(self) -> int:
        """Return the process status corresponding to this report."""

        return 0 if self.ok else 1

    @property
    def lines(self) -> tuple[str, ...]:
        """Return rendered lines in execution order."""

        return tuple(item.line for item in self.items)


def _check_worker_spawn(
    default_map: Path,
    *,
    cold_timeout: float = 30.0,
    intent_timeout: float = 10.0,
) -> str:
    """Prove cold full-map bootstrap and a separate real AI decision.

    The first phase reproduces production ordering with the staged default
    VXL as the worker's first message. The second empty-map phase is separate
    so a heartbeat cannot substitute for proving :class:`BotBrain` emitted a
    genuine intention. No gameplay listener is opened.
    """

    from server.bot_ai.messages import (
        BotIntent,
        MapSnapshot,
        PerceptionFrame,
        PlayerSnapshot,
        WorkerHeartbeat,
        WorkerShutdown,
    )
    from server.bot_ai.profiles import ProfileFactory
    from server.bot_ai.snapshot_transport import encode_map_snapshot
    from server.bot_ai.worker import run_worker
    from server.game_constants import DEFAULT_WEAPON_TOOL

    map_path = Path(default_map)
    if not map_path.is_file():
        raise OSError(f"default map is missing: {map_path}")
    raw_vxl = map_path.read_bytes()
    if not raw_vxl:
        raise ValueError(f"default map is empty: {map_path}")

    context = multiprocessing.get_context("spawn")
    worker_input = context.Queue(maxsize=8)
    worker_output = context.Queue(maxsize=8)
    process = context.Process(
        target=run_worker,
        args=(worker_input, worker_output, 11, 8.0, 24.0),
        name="BattleSpadesReleaseCheck",
    )
    try:
        process.start()

        def send(message, phase: str) -> None:
            try:
                worker_input.put(message, timeout=2.0)
            except queue.Full as exc:
                raise TimeoutError(
                    f"AI child input queue blocked during {phase}"
                ) from exc

        def send_snapshot(snapshot: MapSnapshot, transfer_id: int) -> None:
            encoded = encode_map_snapshot(
                snapshot,
                transfer_id=transfer_id,
            )
            for message in encoded.messages:
                send(message, f"snapshot transfer {transfer_id}")

        observer = PlayerSnapshot(
            player_id=1,
            generation=1,
            team=2,
            class_id=0,
            alive=True,
            spawned=True,
            position=(0.0, 0.0, 0.0),
            eye=(0.0, 0.0, 0.0),
            orientation=(1.0, 0.0, 0.0),
            velocity=(0.0, 0.0, 0.0),
            health=100,
            tool=DEFAULT_WEAPON_TOOL,
            blocks=50,
            ammo_clip=10,
            ammo_reserve=50,
            is_bot=True,
        )
        enemy = PlayerSnapshot(
            player_id=2,
            generation=1,
            team=3,
            class_id=0,
            alive=True,
            spawned=True,
            position=(10.0, 0.0, 0.0),
            eye=(10.0, 0.0, 0.0),
            orientation=(-1.0, 0.0, 0.0),
            velocity=(0.0, 0.0, 0.0),
            health=100,
            tool=DEFAULT_WEAPON_TOOL,
            blocks=50,
            ammo_clip=10,
            ammo_reserve=50,
            is_bot=False,
        )
        profile = ProfileFactory(seed=11).create("normal")

        def frame(frame_id: int, map_epoch: int) -> PerceptionFrame:
            return PerceptionFrame(
                frame_id=frame_id,
                map_epoch=map_epoch,
                mode_epoch=1,
                topology_version=0,
                observer_id=1,
                observer_generation=1,
                created_at=time.monotonic(),
                mode_id="tdm",
                players=(observer, enemy),
                profile=profile,
            )

        # Phase one: this must be the cold child's first navigation snapshot.
        send_snapshot(
            MapSnapshot(1, 0, raw_vxl, "tdm", map_path.stem),
            1,
        )
        send(
            frame(1, 1),
            "full-map frame",
        )
        full_map_heartbeat = None
        deadline = time.monotonic() + max(0.1, float(cold_timeout))
        while time.monotonic() < deadline and full_map_heartbeat is None:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                message = worker_output.get(timeout=remaining)
            except queue.Empty:
                break
            if (
                isinstance(message, WorkerHeartbeat)
                and int(message.map_epoch) == 1
                and int(message.processed_frame_id) >= 1
                and int(message.snapshot_transfer_id) == 1
            ):
                full_map_heartbeat = message
        if full_map_heartbeat is None:
            raise TimeoutError(
                "AI child returned no full-map frame heartbeat for "
                f"{map_path.name} within {cold_timeout:.1f}s"
            )

        # Phase two: reset to a collision-less map and require a real intent,
        # independent from the full-map heartbeat accepted above.
        send_snapshot(
            MapSnapshot(2, 0, b"", "tdm", "release-check-intent"),
            2,
        )
        send(frame(2, 2), "intent-phase frame")
        intent_heartbeat = None
        intent = None
        deadline = time.monotonic() + max(0.1, float(intent_timeout))
        while time.monotonic() < deadline and (
            intent_heartbeat is None or intent is None
        ):
            remaining = max(0.01, deadline - time.monotonic())
            try:
                message = worker_output.get(timeout=remaining)
            except queue.Empty:
                break
            if (
                isinstance(message, WorkerHeartbeat)
                and int(message.map_epoch) == 2
                and int(message.processed_frame_id) >= 2
                and int(message.snapshot_transfer_id) == 2
            ):
                intent_heartbeat = message
            elif isinstance(message, BotIntent) and int(message.frame_id) == 2:
                intent = message
        if intent_heartbeat is None or intent is None:
            missing = []
            if intent_heartbeat is None:
                missing.append("intent-phase heartbeat")
            if intent is None:
                missing.append("bot intent")
            raise TimeoutError(
                f"AI child returned no {' or '.join(missing)} during separate "
                f"intent phase within {intent_timeout:.1f}s"
            )

        send(WorkerShutdown(), "shutdown")
        process.join(max(2.0, float(intent_timeout)))
        if process.is_alive():
            raise TimeoutError(
                f"AI child did not exit within {intent_timeout:.1f}s"
            )
        if process.exitcode != 0:
            raise RuntimeError(f"child exited with status {process.exitcode}")
        return (
            f"AI child processed full map {map_path.name} "
            f"(heartbeat batch={full_map_heartbeat.batch_id}); "
            f"intent frame={intent.frame_id}; exited {process.exitcode}"
        )
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
        process.close()
        for process_queue in (worker_input, worker_output):
            process_queue.cancel_join_thread()
            process_queue.close()


def _attempt(name: str, operation: Callable[[], str]) -> CheckItem:
    """Run one check and turn specific failures into a report item."""

    try:
        return CheckItem(name=name, ok=True, detail=operation())
    except (ImportError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return CheckItem(name=name, ok=False, detail=str(exc))


def run_release_check(paths: RuntimePaths) -> CheckReport:
    """Validate a staged server without opening a gameplay listener.

    Checks execute synchronously during operator startup or CI and never touch
    the authoritative gameplay loop. A missing prerequisite stops dependent
    checks, keeping error output concise and avoiding misleading follow-on
    failures.
    """

    items: list[CheckItem] = []
    items.append(_attempt("version", lambda: read_version(paths.root)))

    def check_config() -> str:
        if not paths.config.is_file():
            raise OSError(f"missing {paths.config}")
        parsed = toml.load(paths.config)
        if not isinstance(parsed, dict):
            raise ValueError(f"invalid TOML root in {paths.config}")
        return str(paths.config)

    items.append(_attempt("config.toml", check_config))
    if not items[-1].ok:
        return CheckReport(tuple(items))

    config = apply_runtime_paths(load_config(paths.config), paths)

    def check_maps() -> str:
        maps_root = Path(config.maps_path)
        maps = sorted(maps_root.glob("*.vxl"))
        if not maps:
            raise OSError(f"no VXL maps in {maps_root}")
        default_name = str(config.default_map)
        default_file = maps_root / (
            default_name if default_name.lower().endswith(".vxl") else f"{default_name}.vxl"
        )
        if not default_file.is_file():
            raise OSError(f"default map is missing: {default_file}")
        return f"{len(maps)} VXL files; default={default_file.name}"

    items.append(_attempt("maps", check_maps))

    def check_native_imports() -> str:
        for module_name in _NATIVE_MODULES:
            importlib.import_module(module_name)
        return f"{len(_NATIVE_MODULES)} modules"

    items.append(_attempt("native imports", check_native_imports))
    if not items[-1].ok:
        return CheckReport(tuple(items))

    def check_steam_discovery() -> str:
        if not config.steam.enabled:
            return "disabled"
        from server.steam_master import (
            inspect_runtime,
            resolve_helper_path,
            resolve_runtime_dir,
            resolve_steamclient_dir,
        )

        inspection = inspect_runtime(resolve_runtime_dir(config.steam))
        helper = resolve_helper_path(config.steam)
        if not helper.is_file():
            raise OSError(f"missing Steam bridge {helper}")
        client_dir = resolve_steamclient_dir(
            config.steam,
            inspection.runtime_dir,
        )
        client_detail = str(client_dir) if client_dir is not None else "desktop Steam"
        return f"x86 API + bridge; client={client_detail}"

    items.append(_attempt("Steam discovery", check_steam_discovery))

    def check_prefabs() -> str:
        from server.prefabs import PrefabRegistry

        prefab_root = Path(config.prefabs_path)
        files = sorted(prefab_root.glob("*.kv6"))
        if not files:
            raise OSError(f"no KV6 prefabs in {prefab_root}")
        registry = PrefabRegistry((str(prefab_root),))
        missing = [path.name for path in files if registry.get(path.stem) is None]
        if missing:
            raise RuntimeError(f"unreadable KV6 prefabs: {', '.join(missing)}")
        return f"{len(files)} KV6 files"

    items.append(_attempt("prefabs", check_prefabs))

    maps_root = Path(config.maps_path)
    default_name = str(config.default_map)
    default_map = maps_root / (
        default_name
        if default_name.lower().endswith(".vxl")
        else f"{default_name}.vxl"
    )
    items.append(
        _attempt(
            "worker spawn",
            lambda: _check_worker_spawn(default_map),
        )
    )
    return CheckReport(tuple(items))
