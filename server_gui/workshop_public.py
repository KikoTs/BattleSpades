"""Public Steam map discovery and downloads, shared by the GUI and console.

Legacy Ace of Spades items expose their .aos URL through Valve's public
GetPublishedFileDetails API. No Steam installation, credentials or API key is
used. Discovery reads public browse links; pasting an item ID works independently.
"""

from __future__ import annotations

import http.client
import json
import re
import html
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from threading import Event
from typing import Callable

from server_gui import workshop_import as wi

DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
BROWSE_URL = "https://steamcommunity.com/workshop/browse/"
PAGE_SIZE = 30  # Steam's public catalog supports 30 results per page.
MAX_RESPONSE = 4 * 1024 * 1024
Progress = Callable[[str], None]
SORTS = {"Most popular": "trend", "Top rated": "toprated", "Most subscribed": "totaluniquesubscribers",
         "Newest": "mostrecent", "Recently updated": "lastupdated"}
PERIODS = {"This week": 7, "This month": 30, "Three months": 90, "This year": 365, "All time": -1}
MODES = ("CTF", "dem", "dia", "MH", "oc", "TDM", "TC", "vip", "zom")


@dataclass
class WorkshopPage:
    """A bounded public catalog page and its continuation flag."""

    items: list[wi.WorkshopItem]
    more: bool = False


def published_id(value: str) -> str:
    """Accept a canonical uint64 ID or an exact Steam item URL."""
    value = value.strip()
    if value.startswith("https://"):
        try:
            url = urllib.parse.urlsplit(value)
        except ValueError as exc:
            raise wi.WorkshopError("Paste a valid Steam Workshop item link.") from exc
        if url.netloc != "steamcommunity.com" or url.path not in (
            "/sharedfiles/filedetails/", "/workshop/filedetails/"
        ):
            raise wi.WorkshopError("Paste an Ace of Spades Workshop item link or ID.")
        ids = urllib.parse.parse_qs(url.query).get("id", [])
        value = ids[0] if len(ids) == 1 else ""
    if (not value.isascii() or not value.isdecimal() or len(value) > 20
            or value.startswith("0") or not 0 < int(value) <= 2**64 - 1):
        raise wi.WorkshopError("Paste a valid Steam Workshop item link or ID.")
    return value


def download_url_allowed(value: str) -> bool:
    """Only public HTTPS Steam CDN URLs; credentials and redirects are rejected."""
    try:
        url = urllib.parse.urlsplit(value)
        host = url.hostname or ""
        return (url.scheme == "https" and url.port in (None, 443)
                and url.username is None and url.password is None and not url.fragment
                and (host.endswith(".steamusercontent.com") or host in (
                    "steamuserimages-a.akamaihd.net", "steamusercontent-a.akamaihd.net")))
    except ValueError:
        return False


def _check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise wi.WorkshopError("Workshop operation cancelled.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: object, code: int,
                         msg: str, headers: object, newurl: str) -> None:
        raise wi.WorkshopError("Steam returned a redirect; the download was not followed.")


def _fetch(url: str, limit: int, cancel: Event | None = None,
           form: dict[str, str] | None = None, progress: Progress | None = None) -> bytes:
    """Bound bytes and time, with cancellation between reads and no redirects."""
    _check_cancel(cancel)
    data = urllib.parse.urlencode(form).encode("ascii") if form is not None else None
    request = urllib.request.Request(url, data=data, headers={
        "User-Agent": "BattleSpadesServerWorkshop/1.0", "Accept-Encoding": "identity",
    })
    deadline = time.monotonic() + (180 if limit > MAX_RESPONSE else 30)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
            if response.status != 200:
                raise wi.WorkshopError(f"Steam request failed (HTTP {response.status}).")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > limit):
                raise wi.WorkshopError("Steam returned an oversized file.")
            result = bytearray()
            while True:
                _check_cancel(cancel)
                if time.monotonic() >= deadline:
                    raise wi.WorkshopError("Steam download timed out. Please retry.")
                chunk = response.read1(min(65536, limit + 1 - len(result)))
                if not chunk:
                    break
                result.extend(chunk)
                if len(result) > limit:
                    raise wi.WorkshopError("Steam returned an oversized file.")
                if progress:
                    progress(f"Downloading: {len(result) / (1024 * 1024):.1f} / {limit / (1024 * 1024):.1f} MiB")
            _check_cancel(cancel)
            return bytes(result)
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise wi.WorkshopError(f"Steam request failed: {exc}") from exc


def _number(value: object) -> int:
    text = str(value)
    if isinstance(value, (int, str)) and len(text) <= 20 and text.isascii() and text.isdecimal():
        return int(text)
    return -1


def parse_details(row: dict) -> wi.WorkshopItem:
    """Validate the app, visibility and direct-download manifest from Valve."""
    item_id = published_id(str(row.get("publishedfileid", "")))
    if (row.get("result") != 1 or row.get("consumer_app_id") != int(wi.APP_ID)
            or row.get("visibility") != 0 or row.get("banned", False) not in (False, 0)
            or row.get("file_type", 0) != 0):
        raise wi.WorkshopError("Choose a public Ace of Spades map (app 224540).")
    url, size = row.get("file_url", ""), _number(row.get("file_size"))
    if not isinstance(url, str) or not download_url_allowed(url) or not 0 < size <= wi.MAX_ITEM_BYTES:
        raise wi.WorkshopError("This map has no supported public download (maximum 64 MiB).")
    tags = row.get("tags", [])
    modes = wi.sidecar_modes({"tags": [tag.get("tag", "") for tag in tags if isinstance(tag, dict)]}
                             ) if isinstance(tags, list) else []
    preview = row.get("preview_url", "")
    if not isinstance(preview, str) or not download_url_allowed(preview):
        preview = ""
    return wi.WorkshopItem(item_id, Path(), title=wi._text(row.get("title"), 160),
                           author=wi._text(row.get("creator"), 64), modes=modes,
                           size=size, download_url=url, preview_url=preview,
                           description=str(row.get("description") or "")[:8192],
                           created=max(0, _number(row.get("time_created"))), updated=max(0, _number(row.get("time_updated"))),
                           subscribers=max(0, _number(row.get("subscriptions"))), favorites=max(0, _number(row.get("favorited"))),
                           views=max(0, _number(row.get("views"))))


def _details(ids: list[str], cancel: Event | None = None) -> list[dict]:
    form = {"itemcount": str(len(ids)), **{f"publishedfileids[{i}]": item for i, item in enumerate(ids)}}
    try:
        result = json.loads(_fetch(DETAILS_URL, MAX_RESPONSE, cancel, form))
        rows = result["response"]["publishedfiledetails"]
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or not all(isinstance(row, dict) for row in rows):
            raise ValueError("invalid item list")
        return rows
    except (ValueError, TypeError, KeyError) as exc:
        raise wi.WorkshopError(f"Steam returned invalid item details: {exc}") from exc


def get_item(value: str, cancel: Event | None = None) -> wi.WorkshopItem:
    """Resolve one public legacy map without a Steam account."""
    item_id = published_id(value)
    rows = _details([item_id], cancel)
    if len(rows) != 1:
        raise wi.WorkshopError("Steam did not return the requested map.")
    item = parse_details(rows[0])
    if item.published_id != item_id:
        raise wi.WorkshopError("Steam returned a different map.")
    return item


class _BrowseLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or len(self.ids) >= PAGE_SIZE:
            return
        href = dict(attrs).get("href", "") or ""
        if not href.startswith("https://steamcommunity.com/sharedfiles/filedetails/?"):
            return
        try:
            item_id = published_id(href)
        except wi.WorkshopError:
            return
        if item_id not in self.ids:
            self.ids.append(item_id)


def browse_url(query: str, page: int, sort: str = "trend", days: int = 7, tag: str = "") -> str:
    """Steam's catalog-wide ordering and game-mode filters (never page-local sorting)."""
    if sort not in SORTS.values() or days not in PERIODS.values() or tag not in ("", *MODES):
        raise wi.WorkshopError("Unsupported Workshop filter.")
    params = {"appid": wi.APP_ID, "browsesort": sort, "actualsort": sort,
              "section": "readytouseitems", "numperpage": PAGE_SIZE,
              "p": min(max(page, 0), 833) + 1, "searchtext": query[:96], "days": days}
    if tag:
        params["requiredtags[]"] = tag
    return BROWSE_URL + "?" + urllib.parse.urlencode(params)


def parse_gallery(text: str) -> list[str]:
    """Read only Steam's bounded screenshot table, without executing page scripts."""
    match = re.search(r"var\s+rgScreenshotURLs\s*=\s*\{([^}]{0,65536})\};", text)
    if not match:
        return []
    result = []
    for url in re.findall(r'''['"][0-9]+['"]\s*:\s*['"](https://[^'"\s<>]+)['"]''', match[1]):
        url = html.unescape(url)
        if download_url_allowed(url) and url not in result:
            result.append(url)
        if len(result) == 10:
            break
    return result


def gallery(item_id: str, cancel: Event | None = None) -> list[str]:
    url = "https://steamcommunity.com/sharedfiles/filedetails/?id=" + published_id(item_id)
    return parse_gallery(_fetch(url, MAX_RESPONSE, cancel).decode("utf-8", "replace"))


def browse(query: str = "", page: int = 0, cancel: Event | None = None, *,
           sort: str = "trend", days: int = 7, tag: str = "") -> WorkshopPage:
    """Search public browse links, then resolve their authoritative API metadata."""
    query = query.strip()
    if query.startswith("https://") or query.isdecimal():
        return WorkshopPage([get_item(query, cancel)])
    parser = _BrowseLinks()
    parser.feed(_fetch(browse_url(query, page, sort, days, tag), MAX_RESPONSE, cancel).decode("utf-8", "replace"))
    items = []
    for row in _details(parser.ids, cancel) if parser.ids else []:
        try:
            item = parse_details(row)
            if item.published_id in parser.ids and all(i.published_id != item.published_id for i in items):
                items.append(item)
        except wi.WorkshopError:
            continue  # Private, deleted, wrong-app and non-downloadable items.
    items.sort(key=lambda item: parser.ids.index(item.published_id))
    return WorkshopPage(items, len(parser.ids) == PAGE_SIZE and page < 833)


def download_item(item: wi.WorkshopItem, maps_dir: Path, cancel: Event | None = None,
                  progress: Progress | None = None) -> wi.ImportResult:
    """Download to temporary storage, then use the server's validated map importer."""
    try:
        # Resolve again: a map may have changed since its catalog page was loaded.
        current = get_item(item.published_id, cancel)
        if current.preview_url == item.preview_url:
            current.preview_path = item.preview_path
        data = _fetch(current.download_url, current.size, cancel, progress=progress)
        if len(data) != current.size:
            raise wi.WorkshopError("Steam file size verification failed.")
        _check_cancel(cancel)
        with tempfile.TemporaryDirectory(prefix="battlespades-workshop-") as directory:
            source = Path(directory) / f"{current.published_id}.aos"
            source.write_bytes(data)
            if progress:
                progress(f"Checking and installing {current.display_title}...")
            _check_cancel(cancel)
            result = wi.import_item(replace(current, source=source, download_url=""), maps_dir, cancel=cancel)
        if result.ok:
            item.imported_as = result.stem
        return replace(result, item=item)
    except (OSError, wi.WorkshopError) as exc:
        return wi.ImportResult(item, False, message=str(exc))
