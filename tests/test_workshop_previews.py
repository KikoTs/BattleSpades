"""Real image decoding, cache failures and preview preservation during imports."""

import io
from pathlib import Path
from threading import Event

import pytest
from PIL import Image

from server_gui import workshop_previews as previews, workshop_public as wp, workshop_import as wi
from tests.test_workshop_public import details
from tests.test_workshop_import import container


def encoded(format: str = "PNG", size: tuple[int, int] = (160, 90)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "#ff0000").save(output, format=format)
    return output.getvalue()


@pytest.mark.parametrize("format", ["PNG", "JPEG"])
def test_portrait_preview_is_letterboxed_without_distortion(format: str) -> None:
    result = previews.normalize_preview(encoded(format, (90, 160)))
    assert result
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (640, 360)
        assert image.getpixel((0, 0)) == (25, 26, 20)
        assert image.getpixel((320, 180))[0] > 250
        assert image.getpixel((270, 180)) == (25, 26, 20)


@pytest.mark.parametrize("data", [b"broken", b"x" * (previews.MAX_BYTES + 1),
                                     encoded(size=(4097, 1)), encoded("GIF")],
                         ids=["invalid", "too-many-bytes", "too-wide", "gif"])
def test_invalid_oversized_and_unsupported_images_are_optional(data: bytes) -> None:
    assert previews.normalize_preview(data) is None


def test_remote_cache_and_invalid_url(monkeypatch, tmp_path: Path) -> None:
    item = wp.parse_details(details(preview_url="https://cdn.steamusercontent.com/preview.jpg"))
    calls = []
    monkeypatch.setattr(wp, "_fetch", lambda *args: calls.append(args) or encoded("JPEG"))
    previews.load_previews([item], tmp_path, Event())
    assert item.preview_path and item.preview_path.is_file()
    previews.load_previews([item], tmp_path, Event())
    assert len(calls) == 1
    item.preview_url = "https://127.0.0.1/preview"
    item.preview_path = None
    previews.cache_preview(item, tmp_path, Event())
    assert len(calls) == 1 and item.preview_path is None


def test_missing_preview_and_cancellation(monkeypatch, tmp_path: Path) -> None:
    item = wp.parse_details(details(preview_url="https://cdn.steamusercontent.com/preview.jpg"))
    def fail(*_args):
        raise wi.WorkshopError("Unavailable")
    monkeypatch.setattr(wp, "_fetch", fail)
    previews.cache_preview(item, tmp_path, Event())
    assert item.preview_path is None
    cancel = Event()
    cancel.set()
    with pytest.raises(wi.WorkshopError, match="cancelled"):
        previews.load_previews([item], tmp_path, cancel)


def test_import_preview_and_receipt_failure_rollback(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.aos"
    source.write_bytes(container())
    image = tmp_path / "source.png"
    image.write_bytes(encoded())
    item = wi.describe(wi.WorkshopItem("185279489", source))
    maps = tmp_path / "maps"
    result = wi.import_item(item, maps)
    assert result.ok, result.message
    assert previews.read_preview(maps / f"{result.stem}.png")
    before = {p.name: p.read_bytes() for p in maps.iterdir()}
    image.write_bytes(encoded("JPEG", (90, 160)))
    def fail(*_args):
        raise OSError("disk full")
    monkeypatch.setattr(wi, "_save_index", fail)
    assert not wi.import_item(item, maps).ok
    assert {p.name: p.read_bytes() for p in maps.iterdir()} == before
