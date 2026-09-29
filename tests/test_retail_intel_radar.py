"""Retail CTF intel placement and Scout radar-station values.

Evidence is written up in docs/RETAIL_VALUES.md ("CTF intel placement" and
"Radar station"). These tests pin the decisions so a later edit cannot drift
back to the mislabelled 250-second radar or an invented intel offset.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import shared.constants as C
import shared.constants_gamemode as CG
from server.config import ServerConfig, load_config
from server.game_constants import TEAM1
from server.map_metadata import MapZone

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ radar


def test_radar_constants_carry_the_retail_block_under_the_right_labels():
    # A1899 / A1900 / A1901 in retail order; the client reads only A1900, as
    # the squared-distance limit in RadarStationEntity.can_detect_player.
    assert (C.A1899, C.A1900, C.A1901) == (45, 250, 45)
    assert C.RADAR_STATION_HEALTH == C.A1899 == 45
    assert C.RADAR_STATION_RANGE == C.A1900 == 250
    assert C.RADAR_STATION_LIFETIME == C.A1901 == 45


def test_radar_lifetime_defaults_to_the_retail_45_seconds():
    from server.entities.behaviors import RadarStationBehavior

    assert ServerConfig().radar_station_lifetime_seconds == float(
        C.RADAR_STATION_LIFETIME
    )
    assert RadarStationBehavior(TEAM1).lifetime == float(C.RADAR_STATION_LIFETIME)
    assert RadarStationBehavior(TEAM1).health == float(C.RADAR_STATION_HEALTH)
    shipped = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    assert shipped["game"]["radar_station_lifetime_seconds"] == 45.0


def test_radar_lifetime_toml_is_clamped_but_kept_when_in_range(tmp_path):
    for raw, expected in ((45.0, 45.0), (0.0, 1.0), (900.0, 250.0)):
        path = tmp_path / f"radar_{int(raw)}.toml"
        path.write_text(
            f"[game]\nradar_station_lifetime_seconds = {raw}\n", encoding="utf-8"
        )
        assert load_config(path).radar_station_lifetime_seconds == expected


# ------------------------------------------------------------------ intel


def test_ctf_intel_offsets_follow_the_retail_rule():
    from modes.classic_ctf import ClassicCTFMode
    from modes.ctf import CTFMode

    # Retail CTF: on the authored base point. Classic: the Classic-only
    # minimum radius, which the Classic capture distance still covers.
    assert CTFMode.intel_offset_from_base == 0.0
    assert ClassicCTFMode.intel_offset_from_base == float(
        CG.CLASSIC_CTF_INTEL_MIN_RADIUS_FROM_BASE
    ) == 3.0
    assert ClassicCTFMode.intel_offset_from_base < CG.CLASSIC_CTF_BASE_CAPTURE_DISTANCE
    # Only maps without recovered retail base data use the server fallback.
    assert CTFMode.intel_fallback_offset_from_base == 12.0
    assert ClassicCTFMode.intel_fallback_offset_from_base == 3.0


def test_zero_offset_puts_the_intel_on_the_base_point_even_in_an_authored_box():
    from modes.ctf import _intel_home

    server = SimpleNamespace()  # no world manager: raw coordinates
    box = MapZone("base", TEAM1, 127, 255, 222, (-5, 5, -5, 5, -5, 5), "ctf_base_points[0]")
    assert _intel_home(server, (127.5, 255.5, 0), (384.5, 255.5, 0), 0.0, base_zone=box)[:2] == (
        127.5,
        255.5,
    )
    x, y, _ = _intel_home(server, (127.5, 255.5, 0), (384.5, 255.5, 0), 3.0, base_zone=box)
    assert (x, y) == (130.5, 255.5)
