"""Retail compatibility tests for the dedicated Classic CTF ruleset."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants as C
from modes import get_mode_class
from modes.classic_ctf import ClassicCTFMode
from server.builders.initial_info import build_initial_info
from server.builders.state_data import build_state_data
from server.class_selection import normalize_class_selection
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer

def _native_server() -> BattleSpadesServer:
    config = ServerConfig(default_mode="cctf")
    server = BattleSpadesServer(config)
    server.mode = ClassicCTFMode(server)
    return server


def test_classic_ctf_registry_uses_ctf_scene_with_classic_switches() -> None:
    server = _native_server()

    state = build_state_data(server, player_id=3)
    info = build_initial_info(server)

    assert get_mode_class("cctf") is ClassicCTFMode
    assert get_mode_class("classic_ctf") is ClassicCTFMode
    assert state.mode_type == 8  # native MODE_CTF; classic is a feature bit
    assert info.mode_key == 8
    assert info.classic == 1
    assert info.enable_minimap == 0
    assert info.allow_shooting_holding_intel == 1
    assert state.team1_classes == [int(C.CLASS_CLASSIC_SOLDIER)]
    assert state.team2_classes == [int(C.CLASS_CLASSIC_SOLDIER)]
    assert state.team1_locked_class is True
    assert state.team2_locked_class is True
    assert int(C.CLASSIC_SMG_TOOL) in info.disabled_tools
    assert int(C.CLASSIC_SHOTGUN_TOOL) in info.disabled_tools


def test_classic_selection_forces_deuce_rifle_grenade_and_spade() -> None:
    mode = _native_server().mode
    untrusted = normalize_class_selection(
        int(C.CLASS_SOLDIER),
        (int(C.MINIGUN_TOOL), int(C.GRENADE_TOOL)),
    )

    selected = mode.prepare_join_selection(TEAM1, untrusted)

    assert selected.class_id == int(C.CLASS_CLASSIC_SOLDIER)
    assert int(C.RIFLE_TOOL) in selected.loadout
    assert int(C.CLASSIC_GRENADE_TOOL) in selected.loadout
    assert int(C.CLASSIC_SPADE_TOOL) in selected.loadout
    assert int(C.CLASSIC_SMG_TOOL) not in selected.loadout
    assert int(C.CLASSIC_SHOTGUN_TOOL) not in selected.loadout
    assert mode.allows_class_selection(
        SimpleNamespace(team=TEAM1), selected
    )


def test_classic_dropped_intel_does_not_auto_return(monkeypatch) -> None:
    server = SimpleNamespace(
        config=SimpleNamespace(mode_settings={}),
        players={},
    )
    mode = ClassicCTFMode(server)
    server.mode = mode
    dropped_position = (210.0, 220.0, 50.0)
    mode.intel_positions[TEAM2] = dropped_position
    mode.intel_drop_time[TEAM2] = 100.0
    mode.start_time = 100.0
    monkeypatch.setattr("modes.ctf.time.time", lambda: 161.0)

    asyncio.run(mode.on_tick(1))

    assert mode.intel_auto_return is False
    assert mode.intel_positions[TEAM2] == dropped_position
    assert mode.intel_drop_time[TEAM2] == 100.0
