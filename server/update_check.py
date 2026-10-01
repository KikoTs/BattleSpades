"""Server-side view of the BattleSpades update manifest.

The client launcher and dedicated servers read the same ``stable.json``
(schema 2, published at https://www.aosplay.net/updates/stable.json). For a
dedicated server this module only ever:

* logs "new server version X available (current Y)" at startup, from a
  daemon thread that never delays or affects the listener, and
* downloads, verifies (size + SHA-256, mirror by mirror) and stages the
  ``server`` component when the operator explicitly runs
  ``BattleSpades --update``.

It never replaces files of a server installation, running or not: applying
a staged update is the operator's decision (see docs/ADMIN_GUIDE.md).

Config (``[updates]`` in config.toml): ``update_check_enabled`` and
``update_manifest_url``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

DEFAULT_MANIFEST_URL = "https://www.aosplay.net/updates/stable.json"
DEFAULT_CHECK_ENABLED = True
COMPONENT = "server"
CHECK_TIMEOUT_SECONDS = 6.0
DOWNLOAD_TIMEOUT_SECONDS = 30.0
MAX_MANIFEST_BYTES = 1024 * 1024
_CHUNK = 1 << 16

_VERSION = re.compile(
    r"v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)(?:\.(?P<patch>0|[1-9]\d*))?"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?"
)
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")

logger = logging.getLogger("BattleSpades.update")


class ManifestError(ValueError):
    """The manifest is unusable (a publishing error, not an outage)."""


class UpdateError(RuntimeError):
    """A download, verification or staging step failed."""


# ---------------------------------------------------------------------------
# Versions (semver precedence, identical to the launcher's rules)
# ---------------------------------------------------------------------------


def version_key(text: str):
    """Sortable semver precedence key, or ``None`` for an invalid version."""

    match = _VERSION.fullmatch(text.strip())
    if match is None:
        return None
    core = (int(match["major"]), int(match["minor"]), int(match["patch"] or 0))
    pre = match["pre"]
    if pre is None:
        return core + ((1,),)
    identifiers = []
    for part in pre.split("."):
        if part.isdigit():
            if len(part) > 1 and part.startswith("0"):
                return None
            identifiers.append((0, int(part), ""))
        else:
            identifiers.append((1, 0, part))
    return core + ((0, tuple(identifiers)),)


def is_newer(candidate: str, current: str) -> bool:
    """True only when both parse and ``candidate`` has higher precedence."""

    left, right = version_key(candidate), version_key(current)
    return left is not None and right is not None and left > right


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def acceptable_url(url: str) -> bool:
    """https, or http to a localhost test mirror only."""

    lowered = url.lower()
    return lowered.startswith(("https://", "http://127.0.0.1", "http://localhost"))


def _safe_relative(path: str) -> bool:
    if not path:
        return True
    pure = PurePosixPath(path.replace("\\", "/"))
    return not pure.is_absolute() and ":" not in path and ".." not in pure.parts


@dataclass(frozen=True, slots=True)
class ServerRelease:
    """The ``server`` component of a schema-2 manifest."""

    version: str
    package: str
    urls: tuple[str, ...]
    size: int
    sha256: str
    root: str = ""
    required: bool = False
    protocol: int | None = None
    preserve: tuple[str, ...] = field(default_factory=tuple)
    mirror_directories: tuple[str, ...] = field(default_factory=tuple)
    remove: tuple[str, ...] = field(default_factory=tuple)


def parse_manifest(document: object) -> ServerRelease | None:
    """Return the server component, ``None`` when the manifest has none."""

    if not isinstance(document, dict):
        raise ManifestError("manifest root must be an object")
    if document.get("schema") != 2:
        raise ManifestError(f"unsupported manifest schema {document.get('schema')!r}")
    if document.get("product") != "BattleSpades":
        raise ManifestError(f"manifest is for {document.get('product')!r}, not BattleSpades")
    components = document.get("components")
    if not isinstance(components, dict):
        raise ManifestError("components must be an object keyed by component name")
    entry = components.get(COMPONENT)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise ManifestError("server component must be an object")
    version = str(entry.get("version", ""))
    if version_key(version) is None:
        raise ManifestError(f"server version is not semver: {version!r}")
    package = str(entry.get("package", ""))
    if not package or any(c in package for c in "/\\:"):
        raise ManifestError("server package must be a plain file name")
    urls = entry.get("urls")
    if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
        raise ManifestError("server urls must list at least one mirror")
    for url in urls:
        if not acceptable_url(url):
            raise ManifestError(f"mirror URL must use https: {url}")
    size = entry.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ManifestError("server size must be the package size in bytes")
    sha256 = str(entry.get("sha256", ""))
    if not _SHA256.fullmatch(sha256):
        raise ManifestError("server sha256 must be 64 hex digits")
    root = str(entry.get("root", ""))
    if not _safe_relative(root):
        raise ManifestError("server root must be a relative path inside the archive")
    lists = {}
    for key in ("preserve", "mirror_directories", "remove"):
        values = entry.get(key, [])
        if not isinstance(values, list) or not all(
            isinstance(v, str) and v and _safe_relative(v) for v in values
        ):
            raise ManifestError(f"server {key} must list safe relative paths")
        lists[key] = tuple(values)
    protocol = entry.get("protocol")
    return ServerRelease(
        version=version,
        package=package,
        urls=tuple(urls),
        size=size,
        sha256=sha256.lower(),
        root=root,
        required=bool(entry.get("required", False) or document.get("required", False)),
        protocol=protocol if isinstance(protocol, int) else None,
        **lists,
    )


# ---------------------------------------------------------------------------
# Transport (injectable for tests)
# ---------------------------------------------------------------------------

Opener = Callable[[str, float], object]


def _open(url: str, timeout: float):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "BattleSpades-Server-Update", "Cache-Control": "no-cache"},
    )
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - scheme validated


def fetch_manifest(url: str, *, timeout: float = CHECK_TIMEOUT_SECONDS, opener: Opener = _open) -> object:
    """Download and decode the manifest (bounded)."""

    if not acceptable_url(url):
        raise ManifestError(f"manifest URL must use https: {url}")
    with opener(url, timeout) as response:
        body = response.read(MAX_MANIFEST_BYTES + 1)
    if len(body) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest is larger than 1 MiB")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest is not JSON: {exc}") from exc


def check_for_update(
    current: str,
    url: str = DEFAULT_MANIFEST_URL,
    *,
    timeout: float = CHECK_TIMEOUT_SECONDS,
    opener: Opener = _open,
) -> ServerRelease | None:
    """The newer server release, or ``None`` when up to date."""

    release = parse_manifest(fetch_manifest(url, timeout=timeout, opener=opener))
    if release is None or not is_newer(release.version, current):
        return None
    return release


def _setting(config: object, name: str, default):
    return getattr(config, name, default)


def start_background_check(
    config: object,
    current: str,
    *,
    log: logging.Logger | None = None,
    opener: Opener = _open,
) -> threading.Thread | None:
    """Log an available update from a daemon thread; silent when offline."""

    if not _setting(config, "update_check_enabled", DEFAULT_CHECK_ENABLED):
        return None
    url = str(_setting(config, "update_manifest_url", DEFAULT_MANIFEST_URL) or DEFAULT_MANIFEST_URL)
    target = log or logger

    def run() -> None:
        try:
            release = check_for_update(current, url, opener=opener)
        except ManifestError as exc:
            target.warning("Update manifest %s is invalid: %s", url, exc)
            return
        except Exception as exc:  # offline, DNS, TLS, HTTP errors: never noisy
            target.debug("Update check skipped: %s", exc)
            return
        if release is not None:
            target.warning(
                "New BattleSpades server version %s available (current %s)%s. "
                "Run 'BattleSpades --update' to download and stage it.",
                release.version,
                current,
                " - REQUIRED: clients of this release need it" if release.required else "",
            )
        else:
            target.info("BattleSpades server %s is up to date", current)

    thread = threading.Thread(target=run, name="BattleSpades-update-check", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# --update: download, verify, stage
# ---------------------------------------------------------------------------


def _download_one(url: str, destination: Path, release: ServerRelease, opener: Opener) -> None:
    partial = destination.with_name(destination.name + ".partial")
    digest = hashlib.sha256()
    received = 0
    try:
        with opener(url, DOWNLOAD_TIMEOUT_SECONDS) as response, partial.open("wb") as output:
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                received += len(chunk)
                if received > release.size:
                    raise UpdateError(f"more than the declared {release.size} bytes")
                digest.update(chunk)
                output.write(chunk)
        if received != release.size:
            raise UpdateError(f"size {received} instead of {release.size}")
        if digest.hexdigest() != release.sha256:
            raise UpdateError(f"SHA-256 mismatch ({digest.hexdigest()})")
        partial.replace(destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def download_verified(
    release: ServerRelease,
    download_dir: Path,
    *,
    opener: Opener = _open,
    out: Callable[[str], None] = print,
) -> Path:
    """Try each mirror in order; return the verified archive path."""

    download_dir.mkdir(parents=True, exist_ok=True)
    destination = download_dir / release.package
    if destination.is_file() and destination.stat().st_size == release.size:
        if _file_sha256(destination) == release.sha256:
            out(f"Using the already verified {destination.name}")
            return destination
    destination.unlink(missing_ok=True)
    failures = []
    for url in release.urls:
        out(f"Downloading {release.package} from {url}")
        try:
            _download_one(url, destination, release, opener)
            return destination
        except (OSError, UpdateError, urllib.error.URLError, ValueError) as exc:
            failures.append(f"{url}: {exc}")
            out(f"  failed: {exc}")
    raise UpdateError("every mirror failed:\n  " + "\n  ".join(failures))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = member.filename.replace("\\", "/")
            target = (root / name).resolve()
            if name.startswith("/") or ":" in name or (target != root and root not in target.parents):
                raise UpdateError(f"archive member escapes the staging folder: {member.filename}")
        bundle.extractall(root)


def _package_root(payload: Path, hint: str) -> Path:
    if hint:
        candidate = payload / hint
        if (candidate / "BattleSpades.exe").is_file() or (candidate / "VERSION").is_file():
            return candidate
    queue = [(payload, 0)]
    while queue:
        directory, depth = queue.pop(0)
        if (directory / "VERSION").is_file() or (directory / "BattleSpades.exe").is_file():
            return directory
        if depth < 3:
            queue.extend((child, depth + 1) for child in sorted(directory.iterdir()) if child.is_dir())
    raise UpdateError("the server package has no VERSION or BattleSpades.exe")


def stage_update(root: Path, release: ServerRelease, archive: Path) -> Path:
    """Extract into <root>/update/staging/server-<version>; return the new tree."""

    staging = root / "update" / "staging"
    final = staging / f"{COMPONENT}-{release.version}"
    extracting = staging / f"{COMPONENT}-{release.version}.extracting"
    shutil.rmtree(extracting, ignore_errors=True)
    payload = extracting / "payload"
    payload.mkdir(parents=True)
    try:
        _safe_extract(archive, payload)
        package_root = _package_root(payload, release.root)
        marker = {
            "schema": 2,
            "component": COMPONENT,
            "version": release.version,
            "sha256": release.sha256,
            "root": package_root.relative_to(payload).as_posix(),
            "preserve": list(release.preserve),
            "mirror_directories": list(release.mirror_directories),
            "remove": list(release.remove),
        }
        (extracting / "staged.json").write_text(json.dumps(marker, indent=2), encoding="utf-8")
    except BaseException:
        shutil.rmtree(extracting, ignore_errors=True)
        raise
    shutil.rmtree(final, ignore_errors=True)
    extracting.replace(final)
    return final / "payload" / marker["root"]


def run_update(
    root: Path,
    current: str,
    *,
    url: str = DEFAULT_MANIFEST_URL,
    opener: Opener = _open,
    out: Callable[[str], None] = print,
) -> int:
    """``BattleSpades --update``: 0 staged or up to date, 1 failure."""

    try:
        release = check_for_update(current, url, opener=opener)
    except (ManifestError, OSError, urllib.error.URLError, ValueError) as exc:
        out(f"Update check failed: {exc}")
        return 1
    if release is None:
        out(f"BattleSpades server {current} is up to date.")
        return 0
    out(f"New server version {release.version} available (current {current}).")
    try:
        archive = download_verified(release, root / "update" / "download", opener=opener, out=out)
        staged = stage_update(root, release, archive)
    except (UpdateError, OSError, zipfile.BadZipFile) as exc:
        out(f"Update failed: {exc}")
        return 1
    archive.unlink(missing_ok=True)
    out(f"Verified and staged at: {staged}")
    out("Nothing was replaced. To install it, stop the server, then copy that folder over this one,")
    out("keeping your " + ", ".join(release.preserve or ("config.toml",)) + " (see docs/ADMIN_GUIDE.md).")
    return 0
