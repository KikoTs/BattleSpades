"""Importing Steam Workshop maps into the server's maps folder."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from server_gui import catalog, workshop_import as wi

# One span per column (N=0, S=E=238, A=0) plus its colour: a valid 512x512 map.
_COLUMN = bytes([0, 238, 238, 0]) + bytes([40, 160, 40, 127])
TINY_VXL = _COLUMN * (512 * 512)


def sidecar(title="Test Map", tags=("map", "tdm", "ctf"), **extra) -> bytes:
    payload = {"title": title, "author": "Tester", "tags": list(tags), "skybox_name": "User_Grassland.txt",
               "ugc_entities": [], **extra}
    return json.dumps(payload).encode("utf-8")


def chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + struct.pack("<I", len(payload)) + payload


def container(vxl=TINY_VXL, ugc=None, ugc_first=False) -> bytes:
    ugc = sidecar() if ugc is None else ugc
    parts = [chunk(b"VXL\0", vxl), chunk(b"UGC\0", ugc)]
    return b"".join(reversed(parts) if ugc_first else parts)


def workshop_item(root: Path, published_id: str, data: bytes) -> Path:
    folder = root / "steamapps" / "workshop" / "content" / "224540" / published_id
    folder.mkdir(parents=True)
    path = folder / "576750379747887063_legacy.bin"
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ container


def test_container_parses_either_chunk_order() -> None:
    for ugc_first in (False, True):
        parsed = wi.parse_container(container(ugc_first=ugc_first))
        assert parsed.vxl == TINY_VXL and json.loads(parsed.ugc)["title"] == "Test Map"


@pytest.mark.parametrize("data,needle", [
    (b"", "no VXL"),
    (b"VXL\0\x10", "chunk header"),
    (chunk(b"VXL\0", b"abc"), "no UGC"),
    (b"VXL\0" + struct.pack("<I", 10_000) + b"short", "truncated"),
    (chunk(b"PNG\0", b"x") + container(), "unknown chunk"),
    (b"VXL " + struct.pack("<I", 1) + b"x", "unknown chunk"),
    (chunk(b"VXL\0", b"") + chunk(b"UGC\0", sidecar()), "empty"),
], ids=["empty", "short-header", "no-ugc", "truncated", "unknown-first", "unterminated-tag", "empty-vxl"])
def test_malformed_containers_are_rejected(data, needle) -> None:
    with pytest.raises(wi.WorkshopError, match=needle):
        wi.parse_container(data)


def test_huge_items_are_refused_before_parsing(monkeypatch) -> None:
    monkeypatch.setattr(wi, "MAX_ITEM_BYTES", 1024)
    with pytest.raises(wi.WorkshopError, match="larger than"):
        wi.parse_container(b"\0" * 2048)


def test_sidecar_rules() -> None:
    assert wi.parse_sidecar(sidecar())["author"] == "Tester"
    assert wi.parse_sidecar("{\"title\": \"Caf\xe9\"}".encode("latin-1"))["title"] == "Caf\xe9"
    for bad in (b"", b"[1, 2]", b"not json", b"{" + b" " * (wi.MAX_SIDECAR_BYTES + 1) + b"}"):
        with pytest.raises(wi.WorkshopError):
            wi.parse_sidecar(bad)
    assert wi.sidecar_modes({"tags": ["map", "ZOM", "tdm", "bogus"]}) == ["tdm", "zom"]
    assert wi.sidecar_modes({"tags": "tdm"}) == []


def test_vxl_validation_uses_the_server_loader() -> None:
    wi.validate_vxl(TINY_VXL)
    for bad in (b"\x00" * 100, TINY_VXL[: len(TINY_VXL) // 2]):
        with pytest.raises(wi.WorkshopError):
            wi.validate_vxl(bad)


# ---------------------------------------------------------------------- names


@pytest.mark.parametrize("title,expected", [
    ("BF4 - Siege of Shanghai", "BF4_Siege_of_Shanghai"),
    ("de_dust2", "de_dust2"),
    ("Карта Победы", "Karta_Pobedy"),
    ("Щит и Меч 2", "Shtit_i_Mech_2"),
    ("Café Olé", "Cafe_Ole"),
    ("!!!", "Workshop_42"),
    ("火影", "Workshop_42"),
    ("CON", "Workshop_42"),
    ("Subscribed_42", "Workshop_42"),
    ("../../etc/passwd", "etc_passwd"),
    ("x" * 90, "x" * wi.MAX_STEM),
])
def test_safe_stem(title, expected) -> None:
    assert wi.safe_stem(title, "42") == expected


def test_stems_never_clobber_other_maps(tmp_path: Path) -> None:
    (tmp_path / "Paintball.vxl").write_bytes(b"stock")
    (tmp_path / "paintball_2.txt").write_text("{}")
    assert wi.choose_stem(tmp_path, "Paintball", "1") == "Paintball_3"
    assert wi.choose_stem(tmp_path, "Fresh", "1") == "Fresh"


# ------------------------------------------------------------------ discovery


def test_libraryfolders_both_vdf_formats(tmp_path: Path) -> None:
    new = '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"C:\\\\Program Files (x86)\\\\Steam"\n\t\t"apps"\n\t\t{\n\t\t\t"224540"\t\t"482897461"\n\t\t}\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"D:\\\\SteamLibrary"\n\t}\n}\n'
    assert wi.parse_libraryfolders(new) == [Path("C:\\Program Files (x86)\\Steam"), Path("D:\\SteamLibrary")]
    old = '"LibraryFolders"\n{\n\t"TimeNextStatsReport"\t\t"1234"\n\t"1"\t\t"/mnt/games/SteamLibrary"\n}\n'
    assert wi.parse_libraryfolders(old) == [Path("/mnt/games/SteamLibrary")]


def test_discovery_walks_every_library(tmp_path: Path) -> None:
    root = tmp_path / "Steam"
    other = tmp_path / "Games" / "SteamLibrary"
    (root / "steamapps").mkdir(parents=True)
    other.mkdir(parents=True)
    escaped = str(other).replace("\\", "\\\\")
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n\t"1"\n\t{{\n\t\t"path"\t\t"{escaped}"\n\t}}\n}}\n', encoding="utf-8")
    workshop_item(root, "111", container(ugc=sidecar("Alpha")))
    workshop_item(other, "222", container(ugc=sidecar("Bravo", tags=("map", "zom"))))
    workshop_item(other, "333", b"garbage")
    libraries = wi.steam_libraries([root])
    assert [p.resolve() for p in libraries] == [root.resolve(), other.resolve()]
    items = wi.find_items(libraries=libraries, maps_dir=tmp_path / "maps")
    assert [(i.published_id, i.title, i.ok) for i in items] == [("111", "Alpha", True), ("222", "Bravo", True), ("333", "", False)]
    assert items[1].modes == ["zom"] and items[0].author == "Tester" and items[0].size > len(TINY_VXL)


def test_linux_and_macos_roots(tmp_path: Path) -> None:
    (tmp_path / ".local" / "share" / "Steam").mkdir(parents=True)
    assert wi.steam_roots("linux", tmp_path) == [tmp_path / ".local" / "share" / "Steam"]
    (tmp_path / "Library" / "Application Support" / "Steam").mkdir(parents=True)
    assert wi.steam_roots("darwin", tmp_path) == [tmp_path / "Library" / "Application Support" / "Steam"]


def test_client_subscribed_layout_is_accepted(tmp_path: Path) -> None:
    client = tmp_path / "ugc" / "maps"
    client.mkdir(parents=True)
    (client / "Subscribed_697436431.vxl").write_bytes(TINY_VXL)
    (client / "Subscribed_697436431.ugc").write_bytes(sidecar("Urban-1", tags=("map", "zom")))
    (client / "Subscribed_5.vxl").write_bytes(TINY_VXL)  # no sidecar: ignored
    items = wi.find_items([client], maps_dir=tmp_path / "maps", libraries=[])
    assert [(i.published_id, i.title, i.modes) for i in items] == [("697436431", "Urban-1", ["zom"])]


# --------------------------------------------------------------------- import


def test_import_round_trip_and_catalog(tmp_path: Path) -> None:
    root = tmp_path / "Steam"
    workshop_item(root, "185279489", container(ugc=sidecar("Paintball", tags=("map", "tdm"))))
    maps = tmp_path / "maps"
    (maps).mkdir()
    (maps / "Paintball.vxl").write_bytes(b"an operator's own map")
    item = wi.find_items(libraries=[root], maps_dir=maps)[0]
    result = wi.import_item(item, maps)
    assert result.ok, result.message
    assert result.stem == "Paintball_2"
    assert (maps / "Paintball.vxl").read_bytes() == b"an operator's own map"
    assert (maps / "Paintball_2.vxl").read_bytes() == TINY_VXL
    assert (maps / "Paintball_2.ugc").read_bytes() == (maps / "Paintball_2.txt").read_bytes()
    assert not list(maps.glob("*.partial"))
    # The server's own loaders accept it.
    from server.map_metadata import load_map_metadata

    assert load_map_metadata(maps / "Paintball_2.vxl", "tdm") is not None
    # Listed for its modes only, and marked as imported.
    assert "Paintball_2" in catalog.mode_pool(maps, "tdm")
    assert "Paintball_2" not in catalog.mode_pool(maps, "zom")
    again = wi.find_items(libraries=[root], maps_dir=maps)[0]
    assert again.imported_as == "Paintball_2"
    # Re-importing updates in place instead of making Paintball_3.
    assert wi.import_item(again, maps).stem == "Paintball_2"
    assert not (maps / "Paintball_3.vxl").exists()


def test_failed_validation_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "Steam"
    workshop_item(root, "9", container(vxl=b"\x00" * 64))
    maps = tmp_path / "maps"
    item = wi.find_items(libraries=[root], maps_dir=maps)[0]
    result = wi.import_item(item, maps)
    assert not result.ok and "512 x 512" in result.message
    assert not maps.exists() or not any(maps.iterdir())


def test_metadata_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "Steam"
    workshop_item(root, "10", container())
    maps = tmp_path / "maps"
    maps.mkdir()

    def broken(_path, _modes):
        raise wi.WorkshopError("sidecar rejected")

    monkeypatch.setattr(wi, "_check_metadata", broken)
    result = wi.import_item(wi.find_items(libraries=[root], maps_dir=maps)[0], maps)
    assert not result.ok and result.message == "sidecar rejected"
    assert sorted(p.name for p in maps.iterdir()) == []


def test_corrupt_index_is_ignored(tmp_path: Path) -> None:
    (tmp_path / wi.INDEX_NAME).write_text('{"1": "../evil", "x": "Fine", "2": "Good_Map"}', encoding="utf-8")
    assert wi.load_index(tmp_path) == {"2": "Good_Map"}
