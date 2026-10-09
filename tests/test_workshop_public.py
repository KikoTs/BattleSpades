"""Public downloads must survive untrusted replies without damaging server maps."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from server_gui import catalog, workshop_import as wi, workshop_public as wp
from tests.test_workshop_import import TINY_VXL, container, sidecar


def test_catalog_filters_apply_to_steam_request() -> None:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(wp.browse_url("snow & city", 2, "mostrecent", 30, "CTF")).query)
    assert query["browsesort"] == ["mostrecent"] and query["actualsort"] == ["mostrecent"]
    assert query["days"] == ["30"] and query["p"] == ["3"]
    assert query["searchtext"] == ["snow & city"] and query["requiredtags[]"] == ["CTF"]
    for sort, days, tag in [("bad", 7, ""), ("trend", 0, ""), ("trend", 7, "CTF&appid=480")]:
        with pytest.raises(wi.WorkshopError):
            wp.browse_url("", 0, sort, days, tag)


def test_gallery_only_accepts_bounded_steam_screenshot_table() -> None:
    a, b = "https://images.steamusercontent.com/ugc/a?x=1&y=2", "https://images.steamusercontent.com/ugc/b"
    html = f'''<img src="https://images.steamusercontent.com/avatar">
    var rgScreenshotURLs = {{ '123': '{a.replace('&', '&amp;')}', '124': '{b}',
      '125': 'https://127.0.0.1/private', '126': '{b}', '' : '' }};
    var unrelated = {{'123': 'https://images.steamusercontent.com/unrelated'}};'''
    assert wp.parse_gallery(html) == [a, b]
    assert wp.parse_gallery("<script>alert('no images')</script>") == []


def test_catalog_keeps_steam_ranking_when_api_reorders(monkeypatch) -> None:
    def fetch(url, *_args):
        if url.startswith(wp.BROWSE_URL):
            return ''.join(f'<a href="https://steamcommunity.com/sharedfiles/filedetails/?id={item}"></a>' for item in (20, 10)).encode()
        return json.dumps({"response": {"publishedfiledetails": [details(publishedfileid="10"), details(publishedfileid="20")]}}).encode()
    monkeypatch.setattr(wp, "_fetch", fetch)
    assert [item.published_id for item in wp.browse().items] == ["20", "10"]


def details(**overrides) -> dict:
    return {"publishedfileid": "185279489", "result": 1, "consumer_app_id": 224540,
            "visibility": 0, "banned": 0, "file_type": 0, "title": "Paintball",
            "creator": "76561190000000000", "file_size": str(len(container())),
            "file_url": "https://cdn.steamusercontent.com/ugc/map", **overrides}


@pytest.mark.parametrize("value", ["", "0", "01", "-1", "１２３", "18446744073709551616", "1e9",
    "https://steamcommunity.com.evil.test/sharedfiles/filedetails/?id=123",
    "https://steamcommunity.com/sharedfiles/filedetails/?id=1&id=2", "https://[broken", "../123"])
def test_invalid_references(value: str) -> None:
    with pytest.raises(wi.WorkshopError):
        wp.published_id(value)


def test_uint64_and_real_link() -> None:
    assert wp.published_id("18446744073709551615") == "18446744073709551615"
    assert wp.published_id("https://steamcommunity.com/sharedfiles/filedetails/?id=185279489&searchtext=") == "185279489"


@pytest.mark.parametrize("value", ["http://cdn.steamusercontent.com/map", "file:///etc/passwd",
    "https://127.0.0.1/map", "https://cdn.steamusercontent.com.evil.test/map",
    "https://user@cdn.steamusercontent.com/map", "https://cdn.steamusercontent.com:444/map",
    "https://cdn.steamusercontent.com/map#fragment", "https://[broken"])
def test_cdn_boundaries(value: str) -> None:
    assert not wp.download_url_allowed(value)


@pytest.mark.parametrize("change", [{"consumer_app_id": 480}, {"visibility": 1}, {"banned": True},
    {"banned": 1}, {"file_type": 2}, {"result": 9}, {"file_size": str(wi.MAX_ITEM_BYTES + 1)},
    {"file_url": "https://evil.test/file"}, {"file_size": "9" * 5000}])
def test_non_downloadable_details(change: dict) -> None:
    with pytest.raises(wi.WorkshopError):
        wp.parse_details(details(**change))


def fake_network(monkeypatch, *, data: bytes | None = None) -> list[str]:
    content = container() if data is None else data
    calls = []

    def fetch(url, limit, cancel=None, form=None, progress=None):
        wp._check_cancel(cancel)
        calls.append(url)
        if url == wp.DETAILS_URL:
            assert form == {"itemcount": "1", "publishedfileids[0]": "185279489"}
            return json.dumps({"response": {"publishedfiledetails": [details(file_size=str(len(content)))]}}).encode()
        assert wp.download_url_allowed(url)
        return content

    monkeypatch.setattr(wp, "_fetch", fetch)
    return calls


def test_download_uses_server_parser_and_catalog(monkeypatch, tmp_path: Path) -> None:
    fake_network(monkeypatch)
    item = wp.get_item("185279489")
    result = wp.download_item(item, tmp_path)
    assert result.ok, result.message
    assert (tmp_path / f"{result.stem}.vxl").read_bytes() == TINY_VXL
    assert (tmp_path / f"{result.stem}.txt").read_bytes() == (tmp_path / f"{result.stem}.ugc").read_bytes()
    assert result.stem in catalog.mode_pool(tmp_path, "tdm")
    assert result.stem not in catalog.mode_pool(tmp_path, "zom")
    assert wp.download_item(item, tmp_path).stem == result.stem
    assert len(list(tmp_path.glob("*.vxl"))) == 1


def test_bad_download_preserves_installed_revision(monkeypatch, tmp_path: Path) -> None:
    fake_network(monkeypatch)
    item = wp.get_item("185279489")
    assert wp.download_item(item, tmp_path).ok
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    fake_network(monkeypatch, data=container(vxl=b"broken"))
    result = wp.download_item(item, tmp_path)
    assert not result.ok
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_cancellation_before_and_after_validation(monkeypatch, tmp_path: Path) -> None:
    calls = fake_network(monkeypatch)
    item = wp.parse_details(details())
    cancel = Event()
    cancel.set()
    assert not wp.download_item(item, tmp_path, cancel).ok
    assert not calls and not list(tmp_path.iterdir())
    cancel.clear()
    check = wi._check_metadata

    def cancel_after_validation(path, modes):
        check(path, modes)
        cancel.set()

    monkeypatch.setattr(wi, "_check_metadata", cancel_after_validation)
    result = wp.download_item(item, tmp_path, cancel)
    assert not result.ok and "cancelled" in result.message
    assert not list(tmp_path.iterdir())


def test_receipt_write_failure_rolls_back_update(monkeypatch, tmp_path: Path) -> None:
    fake_network(monkeypatch)
    item = wp.get_item("185279489")
    assert wp.download_item(item, tmp_path).ok
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    fake_network(monkeypatch, data=container(ugc=sidecar("Changed Title")))

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(wi, "_save_index", fail)
    result = wp.download_item(item, tmp_path)
    assert not result.ok and "disk full" in result.message
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_browse_deduplicates_and_checks_returned_ids(monkeypatch) -> None:
    html = '<a href="https://steamcommunity.com/sharedfiles/filedetails/?id=185279489">Map</a>'

    def fetch(url, limit, cancel=None, form=None):
        if url.startswith(wp.BROWSE_URL):
            return (html + html).encode()
        assert form["itemcount"] == "1"
        return json.dumps({"response": {"publishedfiledetails": [details(), details(publishedfileid="123")]}}).encode()

    monkeypatch.setattr(wp, "_fetch", fetch)
    page = wp.browse("paintball")
    assert [item.published_id for item in page.items] == ["185279489"] and not page.more
    with pytest.raises(wi.WorkshopError, match="requested map"):
        wp.get_item("185279489")


def test_transport_bounds_and_redirects(monkeypatch) -> None:
    class Response:
        status = 200
        headers = {}
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read1(self, size):
            return b"x" * size

    monkeypatch.setattr(wp.urllib.request, "build_opener", lambda *_args:
        SimpleNamespace(open=lambda *_args, **_kwargs: Response()))
    with pytest.raises(wi.WorkshopError, match="oversized"):
        wp._fetch(wp.DETAILS_URL, 10)
    with pytest.raises(wi.WorkshopError, match="redirect"):
        wp._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test")


def test_console_uses_selected_config_and_does_not_start_server(monkeypatch, tmp_path: Path, capsys) -> None:
    from server import launcher
    from server.runtime_paths import RuntimePaths

    fake_network(monkeypatch)
    selected = tmp_path / "alternate.toml"
    selected.write_text('[world]\nmaps_path = "host-maps"\n', encoding="utf-8")
    monkeypatch.setattr(launcher, "_run_server", lambda *_a, **_k: pytest.fail("server started"))
    args = ["--config", str(selected), "--workshop-download", "185279489"]
    assert launcher.run(args, paths=RuntimePaths.from_root(tmp_path)) == 0
    assert list((tmp_path / "host-maps").glob("*.vxl"))
    assert "Start map:" in capsys.readouterr().out
    assert selected.read_text(encoding="utf-8") == '[world]\nmaps_path = "host-maps"\n'
    assert launcher.run(["--workshop-download", "invalid"], paths=RuntimePaths.from_root(tmp_path)) == 1


def test_new_client_download_layout_imports(tmp_path: Path) -> None:
    client, maps = tmp_path / "client", tmp_path / "maps"
    client.mkdir()
    (client / "Subscribed_Web_steam_185279489.vxl").write_bytes(TINY_VXL)
    (client / "Subscribed_Web_steam_185279489.ugc").write_bytes(sidecar())
    items = wi.find_items([client], maps_dir=maps, libraries=[])
    assert len(items) == 1 and items[0].published_id == "185279489"
    assert wi.import_item(items[0], maps).ok


def test_aosplay_client_copy_keeps_import_identity(tmp_path: Path) -> None:
    client, maps = tmp_path / "client", tmp_path / "maps"
    client.mkdir()
    stem = "Subscribed_Web_aosplay_12345678-1234-1234-1234-123456789abc"
    (client / f"{stem}.vxl").write_bytes(TINY_VXL)
    (client / f"{stem}.ugc").write_bytes(sidecar("Archive Map"))
    item = wi.find_items([client], maps_dir=maps, libraries=[])[0]
    result = wi.import_item(item, maps)
    assert result.ok and result.stem == "Archive_Map"
    assert wi.import_item(item, maps).stem == result.stem
    assert wi.find_items([client], maps_dir=maps, libraries=[])[0].imported_as == result.stem
