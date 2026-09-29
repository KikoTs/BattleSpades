"""Stock-map chroma markers versus team-coloured art (docs/VXL_MARKERS.md).

The retail ``aoslib.vxl.pyd`` cleanup (``sub_10029FD0``) compares each voxel
masked with ``0x00F0F0F0`` against a two-entry table at ``0x1003D0D4``:
``0x000000FF`` and ``0x0000FF00``. Nothing else is special. In particular the
saturated team colours ``#0028BE`` / ``#00BE28`` / ``#00BE2A`` painted on
Atlantis, BlockNess, DoubleDragon, TokyoNeon and WW1 are ordinary authored
voxels that the stock client renders solid with their own colour.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from aoslib.vxl import find_marker_voxels
from server.runtime_vxl import _retail_marker_family
from server.world_manager import WorldManager


MAPS_DIR = Path(__file__).resolve().parents[1] / "maps"

# Raw marker-family words per stock VXL, from ``find_marker_voxels``.
STOCK_MARKER_WORDS = {
    "20thCenturyTown": 2534,
    "AncientEgypt": 16,
    "ArcticBase": 42,
    "Frontier": 24,
    "GreatWall": 30,
    "MayanJungle": 4,
    "SpookyMansion": 1,
    "TheColosseum": 32,
    "TokyoNeon": 6,
    "Training": 24,
}

# Exposed markers the native cleanup removes (two air cells above). Every
# green-family word on the stock maps is exposed; only 20thCenturyTown keeps
# embedded blue-family words, exactly as a hash-identical stock client does.
STOCK_REMOVED_MARKERS = {
    "20thCenturyTown": 524,
    "AncientEgypt": 16,
    "ArcticBase": 42,
    "Frontier": 24,
    "GreatWall": 30,
    "MayanJungle": 4,
    "SpookyMansion": 1,
    "TheColosseum": 32,
    "TokyoNeon": 6,
    "Training": 24,
}


def _load(name: str) -> WorldManager:
    path = MAPS_DIR / f"{name}.vxl"
    if not path.exists():
        pytest.skip(f"{name}.vxl not present")
    wm = WorldManager(SimpleNamespace(maps_path=str(MAPS_DIR), game_mode="tdm"))
    assert wm.load_map(name)
    return wm


def test_native_marker_table_is_exactly_pure_blue_and_pure_green() -> None:
    assert _retail_marker_family(0xFF00FF00) == 0
    assert _retail_marker_family(0xFF0000FF) == 1
    # The mask keeps near-pure variants (ArcticBase ships several).
    assert _retail_marker_family(0xFF05FF05) == 0
    assert _retail_marker_family(0xFF0000FA) == 1
    # Team-coloured art and other saturated greens are not markers.
    for color in (0x0028BE, 0x00BE28, 0x00BE2A, 0xA7F35C, 0x25E12B, 0x5AF56A):
        assert _retail_marker_family(0xFF000000 | color) is None, hex(color)


def test_stock_marker_inventory_is_pinned() -> None:
    found = {}
    for path in sorted(MAPS_DIR.glob("*.vxl")):
        count = len(find_marker_voxels(path.read_bytes()))
        if count:
            found[path.stem] = count
    if not found:
        pytest.skip("stock maps not present")
    assert found == STOCK_MARKER_WORDS


@pytest.mark.parametrize("name", sorted(STOCK_REMOVED_MARKERS))
def test_no_green_marker_survives_server_cleanup(name: str) -> None:
    logging.disable(logging.WARNING)
    try:
        wm = _load(name)
    finally:
        logging.disable(logging.NOTSET)
    removed = set(wm.map.retail_marker_positions)
    assert len(removed) == STOCK_REMOVED_MARKERS[name]
    shift = wm.map.source_z_shift
    raw = (MAPS_DIR / f"{name}.vxl").read_bytes()
    for x, y, z, color in find_marker_voxels(raw):
        if _retail_marker_family(color) == 0:
            # Green family: always exposed on stock maps, so always removed
            # (then optionally re-created as a palette-coloured flare block).
            assert (x, y, z + shift) in removed


@pytest.mark.parametrize(
    ("name", "cell", "rgb"),
    [
        # Atlantis: 7-tall team posts at the north-west / south-east jetties.
        ("Atlantis", (117, 97, 192), 0x0028BE),
        ("Atlantis", (406, 381, 192), 0x00BE28),
    ],
)
def test_team_coloured_art_stays_solid_with_authored_colour(name, cell, rgb) -> None:
    """Live stock client on Atlantis reads (0,40,190)/(0,190,40), solid."""
    wm = _load(name)
    assert wm.map.retail_marker_positions == ()
    assert wm.get_solid(*cell) is True
    assert wm.get_color(*cell) & 0xFFFFFF == rgb


def test_restored_flare_cells_reach_the_bot_worker_map():
    """Bots load the raw VXL (markers stripped); restored flares must follow."""
    from types import SimpleNamespace

    from server.bot_ai.director import BotDirector
    from server.config import ServerConfig
    from server.main import BattleSpadesServer

    server = BattleSpadesServer(ServerConfig())
    assert server.world_manager.load_map("Training")
    published = []
    supervisor = SimpleNamespace(
        publish_world_change=lambda change, **_kw: published.append(change),
    )
    director = BotDirector(server, supervisor=supervisor)
    director._bind_world_mutations()
    server.map_resources.rebuild()
    cells = server.world_manager.static_light_cells
    assert cells, "Training has 24 retail light markers"
    live = {(c.x, c.y, c.z) for c in published if c.solid}
    assert set(cells) <= live
    published.clear()
    director._publish_static_lights()
    assert {(c.x, c.y, c.z) for c in published} == set(cells)
