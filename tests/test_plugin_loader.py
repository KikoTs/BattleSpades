"""External plugin discovery for portable frozen releases."""

import asyncio
from pathlib import Path

from plugins.base_plugin import PluginManager
from server.plugin_loader import load_external_plugins


PLUGIN_SOURCE = """
from plugins.base_plugin import BasePlugin


class HelloPlugin(BasePlugin):
    name = "Hello"

    async def on_load(self):
        self.server.loaded.append(self.name)
"""


class FakeServer:
    """Minimal plugin owner used by lifecycle tests."""

    def __init__(self) -> None:
        self.loaded: list[str] = []


def test_external_plugin_loads_from_runtime_directory(tmp_path: Path) -> None:
    """A top-level Python plugin is imported and receives its lifecycle hook."""

    (tmp_path / "hello.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    server = FakeServer()
    manager = PluginManager(server)

    assert asyncio.run(load_external_plugins(manager, tmp_path)) == 1
    assert server.loaded == ["Hello"]
    assert "Hello" in manager.plugins


def test_disabled_and_private_plugins_are_ignored(tmp_path: Path) -> None:
    """The shipped example remains inert until an operator renames it."""

    (tmp_path / "_private.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    (tmp_path / "sample.py.disabled").write_text(
        PLUGIN_SOURCE,
        encoding="utf-8",
    )

    assert asyncio.run(
        load_external_plugins(PluginManager(FakeServer()), tmp_path)
    ) == 0


def test_broken_plugin_does_not_block_valid_neighbor(tmp_path: Path) -> None:
    """One import failure is logged and discovery continues deterministically."""

    (tmp_path / "broken.py").write_text(
        "raise RuntimeError('broken plugin')\n",
        encoding="utf-8",
    )
    (tmp_path / "hello.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    server = FakeServer()

    loaded = asyncio.run(load_external_plugins(PluginManager(server), tmp_path))

    assert loaded == 1
    assert server.loaded == ["Hello"]


def test_missing_plugin_directory_is_empty_not_fatal(tmp_path: Path) -> None:
    """Operators may delete the optional plugin directory."""

    missing = tmp_path / "plugins"

    assert asyncio.run(
        load_external_plugins(PluginManager(FakeServer()), missing)
    ) == 0
