"""Behavioral tests for the Python-2-compatible retail scene bridge."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace


PATCH_PATH = (
    Path(__file__).resolve().parents[1]
    / "client_patches"
    / "session_transition_patch.py"
)


def _load_patch(monkeypatch):
    clock = ModuleType("pyglet.clock")
    clock.schedule_interval_soft = lambda callback, interval, *args, **kwargs: callback
    pyglet = ModuleType("pyglet")
    pyglet.clock = clock
    monkeypatch.setitem(sys.modules, "pyglet", pyglet)
    monkeypatch.setitem(sys.modules, "pyglet.clock", clock)

    loading_module = ModuleType("aoslib.scenes.ingame_menus.loadingMenu")

    class LoadingMenu:
        pass

    loading_module.LoadingMenu = LoadingMenu
    monkeypatch.setitem(
        sys.modules,
        "aoslib.scenes.ingame_menus.loadingMenu",
        loading_module,
    )

    packet_module = ModuleType("shared.packet")

    class ClientInMenu:
        in_menu = 0

    packet_module.ClientInMenu = ClientInMenu
    monkeypatch.setitem(sys.modules, "shared.packet", packet_module)

    spec = importlib.util.spec_from_file_location("_session_transition_patch_test", PATCH_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, LoadingMenu


def test_mapended_pause_flags_open_loading_menu_once(monkeypatch) -> None:
    patch, loading_menu = _load_patch(monkeypatch)
    scene = SimpleNamespace(
        pause_players=True,
        pause_entities=True,
        pause_particles=True,
    )
    calls = []
    sent = []

    class GameManager:
        def __init__(self):
            self.game_scene = scene
            self.client = SimpleNamespace(
                disconnected=False,
                send_packet=lambda packet: sent.append(packet),
            )

        def set_menu(self, menu, **kwargs):
            calls.append((menu, kwargs))

    manager = GameManager()
    patch._enter_loading_menu(manager)
    patch._enter_loading_menu(manager)

    assert calls == [(loading_menu, {"from_server_menu": False})]
    assert [packet.in_menu for packet in sent] == [1]

    # The manager reuses its GameScene singleton. Clearing the native pause
    # flags must arm the hook for a later map epoch.
    scene.pause_players = scene.pause_entities = scene.pause_particles = False
    patch._enter_loading_menu(manager)
    scene.pause_players = scene.pause_entities = scene.pause_particles = True
    patch._enter_loading_menu(manager)
    assert len(calls) == 2
    assert [packet.in_menu for packet in sent] == [1, 1]


def test_disconnected_client_never_opens_transition_loader(monkeypatch) -> None:
    patch, _loading_menu = _load_patch(monkeypatch)
    manager = SimpleNamespace(
        game_scene=SimpleNamespace(
            pause_players=True,
            pause_entities=True,
            pause_particles=True,
        ),
        client=SimpleNamespace(disconnected=True),
        set_menu=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("menu must remain untouched")
        ),
    )

    patch._enter_loading_menu(manager)
