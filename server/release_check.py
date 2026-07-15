"""Self-contained validation used by source and packaged server builds."""

from __future__ import annotations

import importlib
import multiprocessing
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


def _worker_echo(connection, token: str) -> None:
    """Reply from a spawned interpreter, proving child bootstrap works."""

    try:
        connection.send(token)
    finally:
        connection.close()


def _check_worker_spawn(timeout: float = 10.0) -> str:
    """Start and reap a Windows-safe spawned process."""

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    token = "battlespades-worker-ok"
    process = context.Process(
        target=_worker_echo,
        args=(sender, token),
        name="BattleSpadesReleaseCheck",
    )
    try:
        process.start()
        sender.close()
        if not receiver.poll(timeout):
            raise TimeoutError(f"child did not respond within {timeout:.1f}s")
        response = receiver.recv()
        process.join(timeout)
        if process.is_alive():
            raise TimeoutError(f"child did not exit within {timeout:.1f}s")
        if process.exitcode != 0:
            raise RuntimeError(f"child exited with status {process.exitcode}")
        if response != token:
            raise RuntimeError(f"child returned unexpected token {response!r}")
        return f"spawned child exited {process.exitcode}"
    finally:
        receiver.close()
        sender.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
        process.close()


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
    items.append(_attempt("worker spawn", _check_worker_spawn))
    return CheckReport(tuple(items))
