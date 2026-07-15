"""Load trusted operator plugins from the portable runtime directory."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

from plugins.base_plugin import BasePlugin, PluginManager


logger = logging.getLogger(__name__)


def _is_within_directory(path: Path, directory: Path) -> bool:
    """Return whether a resolved candidate remains inside its plugin root."""

    try:
        return os.path.commonpath((str(path), str(directory))) == str(directory)
    except ValueError:
        return False


def discover_plugin_classes(directory: Path) -> Iterator[type[BasePlugin]]:
    """Yield concrete plugin classes from safe top-level Python files.

    Import failures are logged and skipped so a malformed optional plugin
    cannot prevent the dedicated server from starting.
    """

    plugin_root = Path(directory).resolve()
    if not plugin_root.is_dir():
        return

    for candidate in sorted(plugin_root.glob("*.py"), key=lambda path: path.name):
        if candidate.name.startswith("_") or candidate.stem == "base_plugin":
            continue
        resolved = candidate.resolve()
        if not _is_within_directory(resolved, plugin_root):
            logger.error("Ignoring plugin outside configured root: %s", candidate)
            continue

        identity = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
        module_name = f"battlespades_user_plugin_{candidate.stem}_{identity}"
        spec = importlib.util.spec_from_file_location(module_name, resolved)
        if spec is None or spec.loader is None:
            logger.error("Unable to create import specification for %s", resolved)
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            logger.error("Failed to import plugin module %s", resolved, exc_info=True)
            continue

        for _name, value in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(value, BasePlugin)
                and value is not BasePlugin
                and value.__module__ == module.__name__
            ):
                yield value


async def load_external_plugins(
    manager: PluginManager,
    directory: Path,
    *,
    allowlist=(),
    denylist=(),
) -> int:
    """Discover and initialize configured plugins.

    Filters use case-insensitive filename stems.  An empty allowlist permits
    every discovered plugin; a denylist entry always wins.  Imports remain
    isolated and failures remain non-fatal to the authoritative server.
    """

    loaded = 0
    allowed = {str(value).strip().casefold() for value in allowlist if str(value).strip()}
    denied = {str(value).strip().casefold() for value in denylist if str(value).strip()}
    for plugin_class in discover_plugin_classes(directory):
        module_file = Path(inspect.getfile(plugin_class)).stem.casefold()
        plugin_name = str(getattr(plugin_class, "name", "")).strip().casefold()
        identities = {module_file, plugin_name}
        if identities & denied:
            logger.info("Plugin %s disabled by denylist", module_file)
            continue
        if allowed and not identities & allowed:
            continue
        if await manager.load_plugin(plugin_class):
            loaded += 1
    return loaded
