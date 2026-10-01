"""A server installed below a Cyrillic folder (C:\\Users\\Тодор\\Игры\\...).

The client bundles this server in <install>\\server and players install
BattleSpades wherever they like, so config, maps, prefabs, logs and the
staged-update paths must all work from a non-ASCII root.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from server.config import load_config
from server.runtime_paths import RuntimePaths, apply_runtime_paths, read_version

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def cyrillic_install(tmp_path: Path) -> Path:
    root = tmp_path / "Пользователи" / "Тодор" / "Игры" / "BattleSpades" / "server"
    (root / "maps").mkdir(parents=True)
    (root / "prefabs").mkdir()
    shutil.copy(ROOT / "config.toml", root / "config.toml")
    shutil.copy(ROOT / "VERSION", root / "VERSION")
    for name in ("Training.vxl", "Training.botnav"):
        source = ROOT / "maps" / name
        if source.exists():
            shutil.copy(source, root / "maps" / name)
    shutil.copy(ROOT / "prefabs" / "prefab_barricade.kv6", root / "prefabs" / "prefab_barricade.kv6")
    return root


def test_runtime_paths_and_config_from_a_cyrillic_root(cyrillic_install: Path):
    paths = RuntimePaths.from_root(cyrillic_install)
    assert paths.root == cyrillic_install.resolve()
    config = apply_runtime_paths(load_config(paths.config), paths)
    assert Path(config.maps_path) == cyrillic_install.resolve() / "maps"
    assert read_version(paths.root) == (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_map_loads_from_a_cyrillic_folder(cyrillic_install: Path):
    from server.world_manager import WorldManager

    paths = RuntimePaths.from_root(cyrillic_install)
    config = apply_runtime_paths(load_config(paths.config), paths)
    world = WorldManager(config)
    assert world.load_map("Training")
    # A failed open falls back to a generated flat map; that would be the bug.
    assert world.map_name == "Training"
    assert world.map_raw_bytes == (cyrillic_install / "maps" / "Training.vxl").read_bytes()


def test_prefab_kv6_loads_from_a_cyrillic_folder(cyrillic_install: Path):
    from server.prefabs import PrefabRegistry

    registry = PrefabRegistry(search_dirs=(str(cyrillic_install / "prefabs"),))
    model = registry.get("prefab_barricade")
    assert model is not None and len(model.get_points()) > 0


def test_logs_are_written_below_a_cyrillic_folder(cyrillic_install: Path):
    from server.config import ServerConfig
    from server.logging_runtime import configure_logging

    config = ServerConfig()
    config.log_console = False
    config.log_file = "сервер.log"
    runtime = configure_logging(config, cyrillic_install / "logs")
    try:
        logging.getLogger("BattleSpades").warning("проверка: путь %s", cyrillic_install)
    finally:
        runtime.stop()
        # Leave the process-wide logging configuration as other tests expect it.
        for handler in list(logging.getLogger().handlers):
            if handler not in runtime.sinks:
                continue
            logging.getLogger().removeHandler(handler)
    written = (cyrillic_install / "logs" / "сервер.log").read_text(encoding="utf-8")
    assert "проверка" in written and str(cyrillic_install) in written


def test_update_staging_below_a_cyrillic_folder(cyrillic_install: Path):
    import hashlib
    import io
    import json
    import zipfile

    from server import update_check

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("BattleSpades-server-9.0.0/VERSION", "9.0.0\n")
        bundle.writestr("BattleSpades-server-9.0.0/карты/Крепость.txt", "карта")
    package = buffer.getvalue()
    manifest = {
        "schema": 2, "product": "BattleSpades",
        "components": {"server": {
            "version": "9.0.0", "package": "server.zip", "size": len(package),
            "sha256": hashlib.sha256(package).hexdigest(), "urls": ["https://mirror.example/server.zip"],
        }},
    }

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def opener(url, timeout):
        return Response(json.dumps(manifest).encode() if url.endswith(".json") else package)

    assert update_check.run_update(cyrillic_install, "0.1.0", url="https://www.aosplay.net/updates/stable.json",
                                   opener=opener, out=lambda _m: None) == 0
    staged = cyrillic_install / "update" / "staging" / "server-9.0.0" / "payload" / "BattleSpades-server-9.0.0"
    assert (staged / "карты" / "Крепость.txt").read_text(encoding="utf-8") == "карта"
