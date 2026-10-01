"""Bring Steam Workshop maps (Ace of Spades, app 224540) into the server.

Workshop maps are legacy items. Steam keeps each downloaded one as
``<library>/steamapps/workshop/content/224540/<published id>/<handle>_legacy.bin``,
which is the retail ``.aos`` container: chunks of a 4-byte NUL-terminated tag
(``VXL\\0`` or ``UGC\\0``), a 4-byte little-endian length and the payload. The
UGC chunk is the map's JSON sidecar (title, author, tags = valid modes...).
The BattleSpades client also installs synced items as
``ugc/maps/Subscribed_<id>.{vxl,ugc,txt}``; both layouts are accepted.

Importing writes ``<stem>.vxl``, ``<stem>.txt`` and ``<stem>.ugc`` into the
server's maps folder, the layout Map Creator projects use and
``server.map_metadata`` reads, after checking the VXL with the server's own
parser and the sidecar with its metadata loader. Steam does not need to run:
this only reads files.
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

APP_ID = "224540"
MAX_ITEM_BYTES = 64 * 1024 * 1024        # whole container
MAX_SIDECAR_BYTES = 1024 * 1024          # UGC chunk (same cap as the client)
VXL_COLUMNS = 512 * 512
MAX_STEM = 40
INDEX_NAME = ".workshop_imports.json"
_TAGS = {b"VXL\0": "vxl", b"UGC\0": "ugc"}
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_SUBSCRIBED = re.compile(r"^Subscribed_([1-9][0-9]{0,19})$")
#: Mode tags the server understands for hosting (the stock Match Lobby modes).
HOSTABLE_MODES = ("tdm", "ctf", "cctf", "zom", "vip", "mh", "tc", "dia", "dem", "oc")

_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht",
    "ъ": "a", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", "є": "ye", "і": "i", "ї": "yi",
    "ґ": "g", "ў": "u",
}


class WorkshopError(ValueError):
    """A Workshop item cannot be read or imported."""


@dataclass
class AosContainer:
    vxl: bytes
    ugc: bytes


@dataclass
class WorkshopItem:
    """One map found on disk, described from its own sidecar."""

    published_id: str
    source: Path                 # .bin/.aos container, or the Subscribed_<id>.vxl
    sidecar: Path | None = None  # only for the client's Subscribed_* layout
    title: str = ""
    author: str = ""
    modes: list[str] = field(default_factory=list)
    size: int = 0
    error: str = ""
    imported_as: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def display_title(self) -> str:
        return self.title or f"Workshop {self.published_id}"


# --------------------------------------------------------------------------
# Container and sidecar


def parse_container(data: bytes) -> AosContainer:
    """Split an ``.aos`` container; strict bounds, retail chunk semantics."""

    if len(data) > MAX_ITEM_BYTES:
        raise WorkshopError(f"the item is larger than {MAX_ITEM_BYTES // (1024 * 1024)} MiB")
    chunks: dict[str, bytes] = {}
    offset = 0
    while not ("vxl" in chunks and "ugc" in chunks):
        left = len(data) - offset
        if left < 8:
            if left == 0:
                raise WorkshopError("the item has no UGC sidecar" if "vxl" in chunks else "the item has no VXL map data")
            raise WorkshopError("the item ends inside a chunk header")
        tag = data[offset:offset + 4]
        kind = _TAGS.get(tag)
        if kind is None:
            raise WorkshopError(f"unknown chunk at byte {offset}")
        (length,) = struct.unpack_from("<I", data, offset + 4)
        if length > left - 8:
            raise WorkshopError(f"the {kind.upper()} chunk is truncated")
        # A repeated chunk replaces the earlier one, as retail did.
        chunks[kind] = bytes(data[offset + 8:offset + 8 + length])
        offset += 8 + length
    if not chunks["vxl"]:
        raise WorkshopError("the VXL map data is empty")
    return AosContainer(chunks["vxl"], chunks["ugc"])


def parse_sidecar(raw: bytes) -> dict:
    """The UGC sidecar as a JSON object (UTF-8, Latin-1 fallback)."""

    if not raw or len(raw) > MAX_SIDECAR_BYTES:
        raise WorkshopError("the UGC sidecar is empty or larger than 1 MiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise WorkshopError(f"the UGC sidecar is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise WorkshopError("the UGC sidecar is not a JSON object")
    return data


def sidecar_modes(sidecar: dict) -> list[str]:
    """Valid modes from ``tags`` (``map`` and unknown tags dropped), lobby order."""

    tags = sidecar.get("tags", ())
    if not isinstance(tags, list):
        return []
    found = {str(tag).strip().lower() for tag in tags}
    return [mode for mode in HOSTABLE_MODES if mode in found]


def _text(value: object, limit: int = 96) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()
    return text[:limit]


def validate_vxl(vxl: bytes) -> None:
    """Reject anything the server's VXL loader would not load as a map."""

    from aoslib.vxl import VXL, raw_vxl_size

    columns, _max_ref = raw_vxl_size(vxl)
    if columns != VXL_COLUMNS:
        raise WorkshopError("the VXL data is not a complete 512 x 512 map")
    if not VXL(1, vxl, 0).ready:
        raise WorkshopError("the server's VXL loader rejected the map data")


# --------------------------------------------------------------------------
# Names


def safe_stem(title: str, published_id: str) -> str:
    """A portable ASCII map name; ``Workshop_<id>`` when nothing usable remains."""

    def latin(ch: str) -> str:
        mapped = _CYRILLIC.get(ch.lower())
        if mapped is None:
            return ch
        return mapped.capitalize() if ch.isupper() else mapped

    text = "".join(latin(ch) for ch in str(title))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[A-Za-z0-9]+", text)
    stem = "_".join(words)[:MAX_STEM].strip("_")
    if len(stem) < 2 or stem.lower() in _RESERVED or stem.lower().startswith("subscribed_"):
        return f"Workshop_{published_id}"
    return stem


def _existing_stems(maps_dir: Path) -> set[str]:
    try:
        return {p.stem.casefold() for p in Path(maps_dir).iterdir() if p.suffix.lower() in (".vxl", ".ugc", ".txt", ".json")}
    except OSError:
        return set()


def load_index(maps_dir: Path) -> dict[str, str]:
    """``published id -> imported stem`` for maps this tool imported."""

    try:
        data = json.loads((Path(maps_dir) / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(k).isdigit() and re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", str(v))}


def _save_index(maps_dir: Path, index: dict[str, str]) -> None:
    _atomic_write(Path(maps_dir) / INDEX_NAME, json.dumps(index, indent=2, sort_keys=True).encode("utf-8"))


def choose_stem(maps_dir: Path, title: str, published_id: str, index: dict[str, str] | None = None) -> str:
    """Reuse this item's earlier name; otherwise never clobber another map."""

    index = load_index(maps_dir) if index is None else index
    previous = index.get(published_id)
    if previous and (Path(maps_dir) / f"{previous}.vxl").is_file():
        return previous
    taken = _existing_stems(maps_dir)
    base = safe_stem(title, published_id)
    stem, number = base, 2
    while stem.casefold() in taken:
        stem = f"{base[:MAX_STEM - 4]}_{number}"
        number += 1
    return stem


# --------------------------------------------------------------------------
# Discovery


def steam_roots(platform: str = sys.platform, home: Path | None = None) -> list[Path]:
    """Steam installation folders, found the way the client finds them."""

    home = Path.home() if home is None else Path(home)
    roots: list[Path] = []
    if platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                value, _kind = winreg.QueryValueEx(key, "SteamPath")
                roots.append(Path(value))
        except OSError:
            pass
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
            if os.environ.get(env):
                roots.append(Path(os.environ[env]) / "Steam")
    elif platform == "darwin":
        roots.append(home / "Library" / "Application Support" / "Steam")
    else:
        roots += [home / ".steam" / "steam", home / ".local" / "share" / "Steam",
                  home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"]
    unique: list[Path] = []
    for root in roots:
        if root.is_dir() and all(not _same(root, seen) for seen in unique):
            unique.append(root)
    return unique


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a).casefold() == str(b).casefold()


def parse_libraryfolders(text: str) -> list[Path]:
    """Library paths from ``steamapps/libraryfolders.vdf`` (old and new format)."""

    paths = []
    for match in re.finditer(r'^[ \t]*"(?:path|\d+)"[ \t]+"((?:[^"\\]|\\.)*)"', text, re.MULTILINE):
        value = re.sub(r"\\(.)", r"\1", match.group(1))
        if value and not value.isdigit():
            paths.append(Path(value))
    return paths


def steam_libraries(roots: Iterable[Path] | None = None) -> list[Path]:
    libraries: list[Path] = []
    for root in (steam_roots() if roots is None else roots):
        candidates = [Path(root)]
        try:
            vdf = (Path(root) / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
            candidates += parse_libraryfolders(vdf)
        except OSError:
            pass
        for library in candidates:
            if library.is_dir() and all(not _same(library, seen) for seen in libraries):
                libraries.append(library)
    return libraries


def workshop_folders(libraries: Iterable[Path] | None = None) -> list[Path]:
    folders = []
    for library in (steam_libraries() if libraries is None else libraries):
        folder = Path(library) / "steamapps" / "workshop" / "content" / APP_ID
        if folder.is_dir():
            folders.append(folder)
    return folders


def _container_in(item_dir: Path) -> Path | None:
    files = sorted(p for p in item_dir.iterdir() if p.is_file() and p.suffix.lower() in (".bin", ".aos"))
    legacy = [p for p in files if p.name.endswith("_legacy.bin")]
    return (legacy or files or [None])[0]


def scan_folder(folder: Path) -> list[WorkshopItem]:
    """Items in a Workshop content folder, an item folder, or a ugc/maps folder."""

    folder = Path(folder)
    items: list[WorkshopItem] = []
    if not folder.is_dir():
        return items
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return items
    for entry in entries:
        if entry.is_dir() and entry.name.isdigit():
            container = _container_in(entry)
            if container is not None:
                items.append(WorkshopItem(entry.name, container))
        elif entry.is_file() and entry.suffix.lower() == ".vxl":
            match = _SUBSCRIBED.match(entry.stem)
            sidecar = entry.with_suffix(".ugc")
            if match and sidecar.is_file():
                items.append(WorkshopItem(match.group(1), entry, sidecar))
    if not items and folder.name.isdigit():
        container = _container_in(folder)
        if container is not None:
            items.append(WorkshopItem(folder.name, container))
    loose = [p for p in entries if p.is_file() and p.suffix.lower() == ".aos"]
    for path in loose:
        items.append(WorkshopItem(_digits(path.stem) or path.stem, path))
    return items


def _digits(text: str) -> str:
    match = re.search(r"\d{5,20}", text)
    return match.group(0) if match else ""


def read_item(item: WorkshopItem) -> AosContainer:
    """Load the map bytes and sidecar of one item (bounded)."""

    try:
        size = item.source.stat().st_size + (item.sidecar.stat().st_size if item.sidecar else 0)
        if size > MAX_ITEM_BYTES:
            raise WorkshopError(f"the item is larger than {MAX_ITEM_BYTES // (1024 * 1024)} MiB")
        if item.sidecar is not None:
            return AosContainer(item.source.read_bytes(), item.sidecar.read_bytes())
        return parse_container(item.source.read_bytes())
    except OSError as exc:
        raise WorkshopError(f"cannot read {item.source.name}: {exc.strerror or exc}") from None


def describe(item: WorkshopItem, maps_dir: Path | None = None, index: dict[str, str] | None = None) -> WorkshopItem:
    """Fill title/author/modes/size (without validating the VXL)."""

    try:
        container = read_item(item)
        sidecar = parse_sidecar(container.ugc)
        item.title = _text(sidecar.get("title")) or _text(sidecar.get("description"))
        item.author = _text(sidecar.get("author"), 64)
        item.modes = sidecar_modes(sidecar)
        item.size = len(container.vxl) + len(container.ugc)
        item.error = ""
    except WorkshopError as exc:
        item.error = str(exc)
    if maps_dir is not None:
        index = load_index(maps_dir) if index is None else index
        stem = index.get(item.published_id, "")
        item.imported_as = stem if stem and (Path(maps_dir) / f"{stem}.vxl").is_file() else ""
    return item


def find_items(extra_folders: Iterable[Path] = (), maps_dir: Path | None = None,
               libraries: Iterable[Path] | None = None) -> list[WorkshopItem]:
    """Every Workshop map on this computer, one entry per published id."""

    seen: dict[str, WorkshopItem] = {}
    index = load_index(maps_dir) if maps_dir is not None else {}
    folders = list(workshop_folders(libraries)) + [Path(f) for f in extra_folders]
    for folder in folders:
        for item in scan_folder(folder):
            if item.published_id in seen and seen[item.published_id].ok:
                continue
            seen[item.published_id] = describe(item, maps_dir, index)
    return sorted(seen.values(), key=lambda i: (not i.ok, i.display_title.casefold()))


# --------------------------------------------------------------------------
# Import


def _atomic_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@dataclass
class ImportResult:
    item: WorkshopItem
    ok: bool
    stem: str = ""
    message: str = ""


def import_item(item: WorkshopItem, maps_dir: Path) -> ImportResult:
    """Validate and install one item as ``<stem>.vxl/.txt/.ugc``."""

    maps_dir = Path(maps_dir)
    try:
        container = read_item(item)
        sidecar = parse_sidecar(container.ugc)
        validate_vxl(container.vxl)
    except WorkshopError as exc:
        return ImportResult(item, False, message=str(exc))
    title = _text(sidecar.get("title")) or _text(sidecar.get("description"))
    modes = sidecar_modes(sidecar)
    maps_dir.mkdir(parents=True, exist_ok=True)
    index = load_index(maps_dir)
    stem = choose_stem(maps_dir, title, item.published_id, index)
    targets = [maps_dir / f"{stem}{suffix}" for suffix in (".vxl", ".txt", ".ugc")]
    existed = {path: path.read_bytes() if path.is_file() else None for path in targets}
    try:
        # .ugc last, like the client, so a scanner never sees half a map.
        _atomic_write(targets[0], container.vxl)
        _atomic_write(targets[1], container.ugc)
        _atomic_write(targets[2], container.ugc)
        _check_metadata(targets[0], modes)
    except (OSError, WorkshopError) as exc:
        for path, previous in existed.items():
            try:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, previous)
            except OSError:
                pass
        return ImportResult(item, False, message=str(exc))
    index[item.published_id] = stem
    try:
        _save_index(maps_dir, index)
    except OSError:
        pass
    item.title, item.modes, item.imported_as = title, modes, stem
    return ImportResult(item, True, stem, f"Imported as {stem}")


def _check_metadata(vxl_path: Path, modes: list[str]) -> None:
    """The server's metadata loader must accept the sidecar for its modes."""

    from server.map_metadata import load_map_metadata

    for mode in modes or ["tdm"]:
        try:
            metadata = load_map_metadata(vxl_path, mode)
        except Exception as exc:  # the loader is strict about operator files
            raise WorkshopError(f"the server could not read the map's sidecar for {mode}: {exc}") from None
        if metadata is None:
            raise WorkshopError("the server found no metadata for the map")


def custom_map_modes(maps_dir: Path) -> dict[str, list[str]]:
    """``map stem -> modes`` from ``.ugc`` tags of custom maps in a folder."""

    result: dict[str, list[str]] = {}
    try:
        sidecars = list(Path(maps_dir).glob("*.ugc"))
    except OSError:
        return result
    for sidecar in sidecars:
        if not sidecar.with_suffix(".vxl").is_file():
            continue
        try:
            if sidecar.stat().st_size > MAX_SIDECAR_BYTES:
                continue
            modes = sidecar_modes(parse_sidecar(sidecar.read_bytes()))
        except (OSError, WorkshopError):
            continue
        if modes:
            result[sidecar.stem] = modes
    return result
