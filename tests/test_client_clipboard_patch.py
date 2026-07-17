"""Behavioral tests for the Python-2-compatible retail clipboard hook."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace


PATCH_PATH = (
    Path(__file__).resolve().parents[1]
    / "client_patches"
    / "clipboard_input_patch.py"
)


def _load_patch(monkeypatch):
    key = SimpleNamespace(V=86, MOD_CTRL=2, MOD_COMMAND=4)
    window = ModuleType("pyglet.window")
    window.key = key
    pyglet = ModuleType("pyglet")
    pyglet.window = window
    monkeypatch.setitem(sys.modules, "pyglet", pyglet)
    monkeypatch.setitem(sys.modules, "pyglet.window", window)

    edit_module = ModuleType("aoslib.scenes.gui.editBoxControl")

    class EditBoxControl:
        def on_key_press(self, button, modifiers):
            return ("stock", button, modifiers)

        def set(self, value, _fire=False, **_kwargs):
            self.text = str(value)

    edit_module.EditBoxControl = EditBoxControl
    monkeypatch.setitem(
        sys.modules,
        "aoslib.scenes.gui.editBoxControl",
        edit_module,
    )

    spec = importlib.util.spec_from_file_location(
        "_clipboard_input_patch_test",
        PATCH_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, EditBoxControl, key


def test_ctrl_v_inserts_unicode_clipboard_text_at_caret(monkeypatch) -> None:
    patch, control_type, key = _load_patch(monkeypatch)
    monkeypatch.setattr(
        patch,
        "_read_clipboard_text",
        lambda: "127.0.0.1:\r\n32887",
    )
    control = control_type()
    control.enabled = control.focus = True
    control.text = "server="
    control.caret_index = len(control.text)
    control.max_characters = None

    handled = control.on_key_press(key.V, key.MOD_CTRL)

    assert handled is True
    assert control.text == "server=127.0.0.1: 32887"
    assert control.caret_index == len(control.text)


def test_paste_obeys_control_limit_and_non_paste_keys_use_stock_handler(
    monkeypatch,
) -> None:
    patch, control_type, key = _load_patch(monkeypatch)
    monkeypatch.setattr(patch, "_read_clipboard_text", lambda: "abcdef")
    control = control_type()
    control.enabled = control.focus = True
    control.text = "12"
    control.caret_index = 1
    control.max_characters = 5

    assert control.on_key_press(key.V, key.MOD_COMMAND) is True
    assert control.text == "1abc2"
    assert control.caret_index == 4
    assert control.on_key_press(99, 0) == ("stock", 99, 0)


def test_unfocused_control_does_not_read_clipboard(monkeypatch) -> None:
    patch, control_type, key = _load_patch(monkeypatch)
    reads = []
    monkeypatch.setattr(
        patch,
        "_read_clipboard_text",
        lambda: reads.append(True),
    )
    control = control_type()
    control.enabled = True
    control.focus = False
    control.text = ""
    control.caret_index = 0
    control.max_characters = None

    assert control.on_key_press(key.V, key.MOD_CTRL) == (
        "stock",
        key.V,
        key.MOD_CTRL,
    )
    assert reads == []
