"""Behavioral tests for the retail prefab-menu compatibility hook."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import shared.constants as C


PATCH_PATH = (
    Path(__file__).resolve().parents[1]
    / "client_patches"
    / "prefab_menu_patch.py"
)


def _load_patch(monkeypatch, original):
    select_class_module = ModuleType("aoslib.scenes.ingame_menus.selectClass")

    class SelectClass:
        get_class_images = original

    select_class_module.SelectClass = SelectClass
    monkeypatch.setitem(
        sys.modules,
        "aoslib.scenes.ingame_menus.selectClass",
        select_class_module,
    )

    spec = importlib.util.spec_from_file_location("_prefab_menu_patch_test", PATCH_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, SelectClass


def test_prefab_page_keeps_prefabs_and_removes_leaked_weapon_ids(monkeypatch) -> None:
    expected_prefab = ["prefab_ultrabarrier", "prefab.png", True]
    expected_map_prefab = [u"custom_bunker", "custom.png", True]
    expected_flare = [C.FLAREBLOCK_TOOL, "flare.png", True]
    leaked_rows = [
        expected_flare,
        expected_prefab,
        [C.CLASS_PREFABS_SOLDIER, "pickaxe.png", True],
        expected_map_prefab,
        [C.DEFAULT_PREFABS, "minigun.png", True],
        [C.MAP_PREFABS, "smg.png", True],
    ]

    _patch, select_class = _load_patch(
        monkeypatch,
        lambda _menu, _index: list(leaked_rows),
    )

    assert select_class().get_class_images(C.CLASS_PREFABS) == [
        expected_flare,
        expected_prefab,
        expected_map_prefab,
    ]


def test_non_prefab_pages_are_unchanged(monkeypatch) -> None:
    weapon_rows = [[C.RIFLE_TOOL, "rifle.png", True]]
    _patch, select_class = _load_patch(
        monkeypatch,
        lambda _menu, _index: weapon_rows,
    )

    assert select_class().get_class_images(C.CLASS_PRIMARY_WEAPONS) is weapon_rows
