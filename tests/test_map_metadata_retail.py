"""Retail stock-map layouts recovered from the shipped client.

Covers the inert Python 2.7 extractor (``tools/map_metadata``), the committed
sidecars it generated, the per-mode layout overrides found in the retail
``.txtc`` modules, the retail map catalogue, and the evidence-based fallback
team orientation / skyboxes for maps retail never shipped metadata for.
"""

from __future__ import annotations

import json
import logging
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL  # noqa: E402
from server.map_metadata import (  # noqa: E402
    STOCK_MAP_SKYBOXES,
    load_map_metadata,
    retail_map_catalogue,
    retail_mode_pool,
)
from server.world_manager import WorldManager  # noqa: E402
from tools.map_metadata import py27  # noqa: E402
from tools.map_metadata.extract_retail import retail_sidecar  # noqa: E402

MAPS = Path("maps")
RETAIL_SIDECAR_MAPS = ("DragonIsland", "MayanJungle", "SpookyMansion", "Trenches")
STOCK_MAPS = sorted(
    path.stem for path in MAPS.glob("*.vxl") if path.stem.casefold() in STOCK_MAP_SKYBOXES
)


def _sidecar(name: str) -> dict:
    return json.loads((MAPS / f"{name}.json").read_text(encoding="utf-8"))


def _centre(zone) -> tuple[float, float, float]:
    return (zone.x, zone.y, zone.z)


def _world(name: str, mode: str) -> WorldManager:
    wm = WorldManager(ServerConfig(default_mode=mode))
    assert wm.load_map(name)
    return wm


# ---------------------------------------------------------------- extractor


def _code(ops: list[tuple[int, int | None]], consts=(), names=()) -> py27.Code27:
    raw = bytearray()
    for opcode, arg in ops:
        raw.append(opcode)
        if arg is not None:
            raw += int(arg).to_bytes(2, "little")
    return py27.Code27(bytes(raw), tuple(consts), tuple(names), "<test>", "<module>")


def test_evaluator_replays_literal_assignments_in_order():
    # x = (1, -2.5); y = [x, False]; z = 7 / 2 (Python 2 integer division)
    code = _code(
        [
            (100, 0), (100, 1), (11, None), (102, 2), (90, 0),
            (101, 0), (101, 3), (103, 2), (90, 1),
            (100, 2), (100, 3), (21, None), (90, 2),
            (100, 4), (83, None),
        ],
        consts=(1, 2.5, 7, 2, None),
        names=("x", "y", "z", "False"),
    )
    namespace = py27.evaluate_module(code)
    assert list(namespace) == ["x", "y", "z"]
    assert namespace == {"x": (1, -2.5), "y": [(1, -2.5), False], "z": 3}


@pytest.mark.parametrize(
    "ops, names",
    [
        ([(101, 0), (83, None)], ("open",)),              # unknown global
        ([(101, 0), (131, 0), (83, None)], ("True",)),   # CALL_FUNCTION
        ([(101, 0), (106, 1), (83, None)], ("True", "x")),  # LOAD_ATTR
        ([(100, 0), (100, 0), (108, 0)], ("os",)),       # IMPORT_NAME
    ],
)
def test_evaluator_refuses_anything_but_literals(ops, names):
    with pytest.raises(py27.InertError):
        py27.evaluate_module(_code(ops, consts=(None,), names=names))


@pytest.mark.parametrize("name", RETAIL_SIDECAR_MAPS)
def test_committed_sidecar_matches_local_retail_txtc(name):
    """When the retail ``.txtc`` is available, the committed JSON is exact."""

    txtc = MAPS / f"{name}.txtc"
    if not txtc.is_file():
        pytest.skip("retail .txtc not present (gitignored client asset)")
    generated = retail_sidecar(txtc)
    committed = _sidecar(name)
    assert committed == json.loads(json.dumps(generated))


@pytest.mark.parametrize("name", RETAIL_SIDECAR_MAPS)
def test_committed_sidecars_record_their_retail_provenance(name):
    source = _sidecar(name)["_retail_source"]
    assert source["file"] == f"{name}.txtc"
    assert len(source["sha256"]) == 64
    assert source["compiled_from"].endswith(f"common/maps/{name}.txt")


# ------------------------------------------------------------ retail layouts


def test_trenches_ctf_uses_retail_capture_bases_not_spawn_fallback():
    ctf = load_map_metadata(MAPS / "Trenches.vxl", "ctf")
    tdm = load_map_metadata(MAPS / "Trenches.vxl", "tdm")

    assert [_centre(z) for z in ctf.base_zones[TEAM1]] == [(127.0, 255.0, 222.0)]
    assert [_centre(z) for z in ctf.base_zones[TEAM2]] == [(384.0, 255.0, 222.0)]
    assert ctf.base_zones[TEAM1][0].xy_bounds() == (122, 132, 250, 260)
    assert ctf.layout_sources[f"base{TEAM1}"] == "ctf_base_points"
    assert ctf.layout_sources[f"base{TEAM2}"] == "ctf_base_points"
    # CTF capture boxes are CTF-only; other modes keep the team layout.
    assert tdm.base_zones == {TEAM1: [], TEAM2: []}
    # Blue base sits inside Blue's half, beside Blue's retail spawn band.
    assert ctf.spawn_zones[TEAM1][0].xy_bounds() == (16, 91, 32, 482)


def test_classic_ctf_reads_the_same_retail_capture_bases():
    cctf = load_map_metadata(MAPS / "Trenches.vxl", "classic_ctf")
    assert [_centre(z) for z in cctf.base_zones[TEAM1]] == [(127.0, 255.0, 222.0)]
    assert [_centre(z) for z in cctf.base_zones[TEAM2]] == [(384.0, 255.0, 222.0)]


def test_trenches_territory_control_uses_retail_territories_not_hills():
    tc = load_map_metadata(MAPS / "Trenches.vxl", "tc")
    mh = load_map_metadata(MAPS / "Trenches.vxl", "mh")
    retail = _sidecar("Trenches")

    assert [_centre(z) for z in tc.neutral_base_zones] == [
        tuple(float(v) for v in point) for point in retail["tc_base_points"]
    ]
    assert len(tc.neutral_base_zones) == 10
    assert all(zone.team == TEAM_NEUTRAL for zone in tc.neutral_base_zones)
    assert [_centre(z) for z in mh.neutral_base_zones] == [
        tuple(float(v) for v in point) for point in retail["mh_base_points"]
    ]
    assert tc.layout_sources["neutral"] == "tc_base_points"
    assert mh.layout_sources["neutral"] == "mh_base_points"


def test_trenches_retail_spawn_points_lie_inside_retail_spawn_areas():
    metadata = load_map_metadata(MAPS / "Trenches.vxl", "ctf")
    for team in (TEAM1, TEAM2):
        (area,) = metadata.spawn_zones[team]
        x0, x1, y0, y1 = area.xy_bounds()
        assert len(metadata.spawn_points[team]) == 14
        for x, y, _z in metadata.spawn_points[team]:
            assert x0 <= x <= x1 and y0 <= y <= y1


def test_spooky_mansion_occupation_and_zombie_spawns_are_mode_owned():
    tdm = load_map_metadata(MAPS / "SpookyMansion.vxl", "tdm")
    oc = load_map_metadata(MAPS / "SpookyMansion.vxl", "occupation")
    zom = load_map_metadata(MAPS / "SpookyMansion.vxl", "zom")
    retail = _sidecar("SpookyMansion")

    assert [_centre(z) for z in tdm.spawn_zones[TEAM1]] == [(277.0, 147.0, 222.0)]
    assert len(tdm.spawn_zones[TEAM2]) == 2
    assert [_centre(z) for z in oc.spawn_zones[TEAM1]] == [
        tuple(float(v) for v in row[0]) for row in retail["oc_team_one_spawn_area"]
    ]
    assert [_centre(z) for z in oc.spawn_zones[TEAM2]] == [
        tuple(float(v) for v in row[0]) for row in retail["oc_team_two_spawn_area"]
    ]
    # Occupation defenders (Green) spawn in the occupation base itself.
    base = oc.occupation_base_zone
    bx0, bx1, by0, by1 = base.xy_bounds()
    assert base.team == TEAM2
    assert bx0 <= oc.spawn_zones[TEAM2][0].x <= bx1
    assert by0 <= oc.spawn_zones[TEAM2][0].y <= by1
    # Zombie: TEAM1 is the zombie role, TEAM2 the survivors.  The retail
    # zombie areas lead; the team-one shore follows as the dry complement.
    assert [_centre(z) for z in zom.spawn_zones[TEAM1][:8]] == [
        tuple(float(v) for v in row[0]) for row in retail["zombie_spawn_area"]
    ]
    assert [z.item for z in zom.spawn_zones[TEAM1][8:]] == ["team_one_spawn_area"]
    assert [_centre(z) for z in zom.spawn_zones[TEAM2]] == [
        tuple(float(v) for v in row[0]) for row in retail["survivor_spawn_area"]
    ]
    assert zom.layout_sources["spawn%d" % TEAM1] == "zombie_spawn_area+team_one_spawn_area"


def test_spooky_mansion_territories_are_the_retail_tc_list():
    tc = load_map_metadata(MAPS / "SpookyMansion.vxl", "tc")
    assert [_centre(z) for z in tc.neutral_base_zones] == [
        tuple(float(v) for v in point) for point in _sidecar("SpookyMansion")["tc_base_points"]
    ]


def test_dragon_island_demolition_bases_limits_and_palette_are_retail():
    metadata = load_map_metadata(MAPS / "DragonIsland.vxl", "dem")

    assert [_centre(z) for z in metadata.base_zones[TEAM1]] == [(210.0, 174.0, 168.0)]
    assert [_centre(z) for z in metadata.base_zones[TEAM2]] == [(179.0, 403.0, 168.0)]
    assert metadata.base_min_destruction == {TEAM1: 40, TEAM2: 40}
    assert metadata.ground_colors == [
        (85, 60, 30, 32), (31, 22, 11, 238), (37, 78, 172, 239),
    ]
    assert metadata.fog_color == (55, 99, 199)
    assert metadata.cap_limit == 100
    assert metadata.time_limit == 480.0
    assert metadata.display_name == "Dragon Island"


def test_retail_objective_layout_drives_territory_control_mode():
    from modes.territory_control import TerritoryControlMode
    from tests.test_recovered_objective_modes import _Server

    server = _Server()
    server.world_manager.map_metadata = load_map_metadata(MAPS / "Trenches.vxl", "tc")
    zones = TerritoryControlMode(server)._build_zones()

    centres = sorted((z.center[0], z.center[1]) for z in zones)
    expected = sorted(
        (float(x), float(y)) for x, y, _z in _sidecar("Trenches")["tc_base_points"]
    )
    assert centres == expected


@pytest.mark.parametrize(
    "name, mode, area_key, team",
    [
        ("Trenches", "ctf", "team_one_spawn_area", TEAM1),
        ("Trenches", "ctf", "team_two_spawn_area", TEAM2),
        ("DragonIsland", "dem", "team_one_spawn_area", TEAM1),
        ("MayanJungle", "tdm", "team_two_spawn_area", TEAM2),
        # Retail zombie areas are all open water; the dry complement is the
        # retail team-one shore.
        ("SpookyMansion", "zom", "zombie_spawn_area+team_one_spawn_area", TEAM1),
        ("SpookyMansion", "zom", "survivor_spawn_area", TEAM2),
        ("SpookyMansion", "oc", "oc_team_two_spawn_area", TEAM2),
        ("SpookyMansion", "oc", "oc_team_one_spawn_area", TEAM1),
    ],
)
def test_live_spawns_land_inside_retail_spawn_areas(name, mode, area_key, team):
    wm = _world(name, mode)
    areas = []
    for key in area_key.split("+"):
        for (cx, cy, _cz), (w, h, _d) in _sidecar(name)[key]:
            areas.append((cx - w / 2 - 1, cx + w / 2 + 1, cy - h / 2 - 1, cy + h / 2 + 1))
    for _ in range(25):
        x, y, z = wm.get_spawn_point(team)
        assert wm.spawn_position_is_safe((x, y, z), team=team)
        assert any(x0 <= x <= x1 and y0 <= y <= y1 for x0, x1, y0, y1 in areas), (x, y)


def test_trenches_ctf_team_anchors_are_the_retail_capture_bases():
    wm = _world("Trenches", "ctf")
    for team, (bx, by) in ((TEAM1, (127, 255)), (TEAM2, (384, 255))):
        x, y, _z = wm.team_base_anchor(team)
        assert math.dist((x, y), (bx, by)) <= 8.0


# --------------------------------------------------------- retail catalogue


def test_retail_catalogue_covers_every_stock_map():
    catalogue = retail_map_catalogue(MAPS)
    retail_stock = [name for name in STOCK_MAPS if name != "20thCenturyTown"]
    assert retail_stock
    for name in retail_stock:
        assert name.casefold() in catalogue, name
    assert catalogue["trenches"]["valid_modes"] == ["ctf", "tdm", "dia", "mh", "tc", "vip", "zom"]
    assert catalogue["alcatraz"]["playlist_modes"] == ["tc", "vip"]
    assert catalogue["training"]["valid_modes"] == ["tut"]


def test_every_stock_map_and_retail_mode_loads_a_complete_environment(caplog):
    catalogue = retail_map_catalogue(MAPS)
    caplog.set_level(logging.WARNING, logger="server.map_metadata")
    checked = 0
    for name in STOCK_MAPS:
        entry = catalogue.get(name.casefold())
        modes = entry["valid_modes"] if entry else ["tdm"]
        for mode in modes:
            metadata = load_map_metadata(MAPS / f"{name}.vxl", mode)
            assert metadata.official_map
            assert metadata.skybox_name == STOCK_MAP_SKYBOXES[name.casefold()] or (
                metadata.source is not None
            )
            assert metadata.fog_color is not None
            assert metadata.ambient_sounds
            if entry:
                assert metadata.retail_modes == tuple(entry["valid_modes"])
            if name in RETAIL_SIDECAR_MAPS and mode not in ("tut", "ugc"):
                assert metadata.spawn_zones[TEAM1] and metadata.spawn_zones[TEAM2]
            checked += 1
    assert checked > 150
    assert not [r for r in caplog.records if "invalid for mode" in r.getMessage()]


def test_loading_a_map_for_a_retail_invalid_mode_is_reported(caplog):
    caplog.set_level(logging.WARNING, logger="server.map_metadata")
    load_map_metadata(MAPS / "DragonIsland.vxl", "ctf")
    assert any("invalid for mode ctf" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("name, mode", [("GreatWall", "dem"), ("BranCastle", "oc")])
def test_retail_playlists_skip_catalogue_invalid_pairs(caplog, name, mode):
    """Retail PlayList skips a mode in the map's invalid_modes even when the
    raw playlist .txt names the map (demolition.txt/GreatWall,
    occupation.txt/BranCastle), so retail never served either pair."""

    caplog.set_level(logging.WARNING, logger="server.map_metadata")
    metadata = load_map_metadata(MAPS / f"{name}.vxl", mode)
    assert mode not in metadata.retail_modes
    assert mode not in metadata.retail_playlist_modes
    assert any(f"invalid for mode {mode}" in r.getMessage() for r in caplog.records)


def test_retail_mode_pools_follow_the_filtered_playlists():
    assert retail_mode_pool(MAPS, "tc") == ("Alcatraz", "CityOfChicago")
    assert retail_mode_pool(MAPS, "vip") == ("Alcatraz", "CityOfChicago")
    assert retail_mode_pool(MAPS, "territory_control") == ("Alcatraz", "CityOfChicago")
    dem = retail_mode_pool(MAPS, "dem")
    assert "GreatWall" not in dem and "Atlantis" in dem and len(dem) == 8
    oc = retail_mode_pool(MAPS, "oc")
    assert "BranCastle" not in oc and len(oc) == 13
    classic = retail_mode_pool(MAPS, "cctf")
    assert classic == ("Crossroads", "Hiesville", "ToTheBridge", "Trenches",
                       "WinterValley", "WW1", "Classic")
    ctf = retail_mode_pool(MAPS, "ctf")
    assert ctf == ("Atlantis", "BlockNess", "CastleWars", "DoubleDragon",
                   "Invasion", "TokyoNeon")
    assert "LunarBase" not in retail_mode_pool(MAPS, "zom")
    assert retail_mode_pool(MAPS, "nor") == ()


def test_official_rotations_hold_only_retail_served_pairs():
    import tomllib

    for path in sorted(Path("configs").glob("official-*.toml")):
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        mode = document["game"]["default_mode"]
        rotation = document["lobby"]["map_rotation"]
        pool = retail_mode_pool(MAPS, mode)
        assert pool, path
        assert set(rotation) <= set(pool), (path, set(rotation) - set(pool))


# ---------------------------------------- evidence-based stock-map fallbacks


@pytest.mark.parametrize(
    "name, blue_side, green_side",
    [
        # (predicate on TEAM1 anchor, predicate on TEAM2 anchor)
        ("CastleWars", lambda x, y: x > 330 and y > 330, lambda x, y: x < 180 and y < 180),
        ("Atlantis", lambda x, y: x < 180 and y < 180, lambda x, y: x > 330 and y > 330),
        ("DoubleDragon", lambda x, y: y > 330, lambda x, y: y < 180),
        ("WW1", lambda x, y: y > 350, lambda x, y: y < 165),
        ("ToTheBridge", lambda x, y: y > 330, lambda x, y: y < 180),
        ("Crossroads", lambda x, y: y < 210, lambda x, y: y > 300),
        ("TokyoNeon", lambda x, y: x > 300, lambda x, y: x < 210),
    ],
)
def test_voxel_only_maps_put_each_team_on_its_coloured_side(name, blue_side, green_side):
    wm = _world(name, "tdm")
    blue = wm.team_base_anchor(TEAM1)
    green = wm.team_base_anchor(TEAM2)
    assert blue_side(blue[0], blue[1]), blue
    assert green_side(green[0], green[1]), green
    for team, side in ((TEAM1, blue_side), (TEAM2, green_side)):
        for _ in range(10):
            x, y, z = wm.get_spawn_point(team)
            assert wm.spawn_position_is_safe((x, y, z))
            assert side(x, y), (name, team, x, y)


@pytest.mark.parametrize(
    "name, skybox, ambience",
    [
        ("CastleWars", "Invasion.txt", "amb_castlewars"),
        ("DoubleDragon", "SecretBase_Night.txt", "amb_area51"),
        ("Crossroads", "WW1.txt", "amb_ww_lighter"),
        ("Hiesville", "WW2.txt", "amb_ww_coastalcold"),
        ("DragonIsland", "SecretBase.txt", "amb_doomwind"),
        ("MayanJungle", "MayanJungle.txt", "amb_jungle"),
    ],
)
def test_stock_atmosphere_follows_retail_evidence(name, skybox, ambience):
    metadata = load_map_metadata(MAPS / f"{name}.vxl", "tdm")
    assert metadata.skybox_name == skybox
    assert metadata.ambient_sounds[0].name == ambience
    if name not in RETAIL_SIDECAR_MAPS:
        assert metadata.fog_color == tuple(C.FOG_COLORS[skybox])
