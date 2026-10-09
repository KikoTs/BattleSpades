"""Bounded map thumbnails; all network and image decoding runs off the Tk thread."""

from __future__ import annotations

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Callable

from PIL import Image, ImageOps, UnidentifiedImageError

from server_gui import workshop_import as wi, workshop_public as wp

MAX_BYTES = 2 * 1024 * 1024
PREVIEW_SIZE = (640, 360)


def normalize_preview(data: bytes) -> bytes | None:
    """Decode only small PNG/JPEG images and letterbox them without stretching."""
    if not data or len(data) > MAX_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(data), formats=("PNG", "JPEG")) as source:
            width, height = source.size
            if not (0 < width <= 4096 and 0 < height <= 4096 and width * height <= 8 * 1024 * 1024):
                return None
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGBA")
            image.thumbnail(PREVIEW_SIZE, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", PREVIEW_SIZE, (25, 26, 20))
            canvas.paste(image, ((640 - image.width) // 2, (360 - image.height) // 2), image)
            output = io.BytesIO()
            canvas.save(output, format="PNG")
            return output.getvalue()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return None


def read_preview(path: Path) -> bytes | None:
    """Treat local client previews and the cache as untrusted, bounded images too."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        with path.open("rb") as stream:
            return normalize_preview(stream.read(MAX_BYTES + 1))
    except OSError:
        return None


def cache_preview(item: wi.WorkshopItem, cache: Path, cancel: Event) -> None:
    """An absent/broken image never prevents browsing or installing its map."""
    wp._check_cancel(cancel)
    if item.preview_path and not item.preview_url:
        data = read_preview(item.preview_path)
        key = hashlib.sha256(data).hexdigest() if data else ""
    elif wp.download_url_allowed(item.preview_url):
        key = hashlib.sha256(item.preview_url.encode("utf-8")).hexdigest()
        path = cache / f"v1-{key}.png"
        if read_preview(path):
            item.preview_path = path
            return
        try:
            data = normalize_preview(wp._fetch(item.preview_url, MAX_BYTES, cancel))
        except wi.WorkshopError:
            wp._check_cancel(cancel)
            return
    else:
        return
    wp._check_cancel(cancel)
    if data:
        try:
            cache.mkdir(parents=True, exist_ok=True)
            path = cache / f"v1-{key}.png"
            wi._atomic_write(path, data)
            item.preview_path = path
        except OSError:
            pass  # An unwritable optional cache must not break the catalog.
    else:
        item.preview_path = None


def load_previews(items: list[wi.WorkshopItem], cache: Path, cancel: Event,
                  ready: Callable[[wi.WorkshopItem], None] | None = None) -> None:
    """Limit concurrent CDN requests while keeping Tk responsive and cancellable."""
    def load(item: wi.WorkshopItem) -> None:
        cache_preview(item, cache, cancel)
        if ready and not cancel.is_set():
            ready(item)
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="map-preview") as pool:
        list(pool.map(load, items))
