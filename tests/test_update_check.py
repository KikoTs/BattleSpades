"""Server side of the shared update manifest (server/update_check.py)."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import urllib.error
import zipfile
from pathlib import Path

import pytest

from server import update_check
from server.config import ServerConfig, load_config
from server.launcher import build_parser


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, text in files.items():
            bundle.writestr(name, text)
    return buffer.getvalue()


PACKAGE = _zip({
    "BattleSpades-server-0.3.0/BattleSpades.exe": "server v2",
    "BattleSpades-server-0.3.0/VERSION": "0.3.0\n",
    "BattleSpades-server-0.3.0/config.toml": "shipped",
})


def _manifest(package_bytes: bytes = PACKAGE, mirrors=None, **server_overrides) -> dict:
    server = {
        "version": "0.3.0",
        "protocol": 168,
        "package": "BattleSpades-server-0.3.0.zip",
        "size": len(package_bytes),
        "sha256": hashlib.sha256(package_bytes).hexdigest(),
        "root": "BattleSpades-server-0.3.0",
        "urls": mirrors or ["https://r2.example/server.zip"],
        "preserve": ["config.toml", "bans.json"],
        "mirror_directories": ["_internal"],
    }
    server.update(server_overrides)
    return {"schema": 2, "product": "BattleSpades", "channel": "stable",
            "components": {"server": server, "client": {"version": "0.3.0"}}}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeWeb:
    """URL -> bytes or exception; records every request in order."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[str] = []

    def __call__(self, url: str, timeout: float):
        self.requests.append(url)
        value = self.routes.get(url)
        if value is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if isinstance(value, BaseException):
            raise value
        return _Response(value)


MANIFEST_URL = "https://www.aosplay.net/updates/stable.json"


def test_semver_precedence_matches_the_launcher():
    ordered = ["0.2.0-beta.1", "0.2.0-beta.2", "0.2.0-beta.10", "0.2.0-rc.1", "0.2.0", "0.2.1", "0.10.0", "1.0.0"]
    keys = [update_check.version_key(v) for v in ordered]
    assert keys == sorted(keys)
    assert update_check.is_newer("0.3.0", "0.2.0-beta.2")
    assert not update_check.is_newer("0.2.0-beta.2", "0.2.0-beta.2")
    assert not update_check.is_newer("garbage", "0.1.0")
    assert update_check.version_key("01.0.0") is None


def test_manifest_server_component_is_parsed():
    release = update_check.parse_manifest(_manifest())
    assert release.version == "0.3.0" and release.protocol == 168
    assert release.preserve == ("config.toml", "bans.json")
    assert update_check.parse_manifest({"schema": 2, "product": "BattleSpades", "components": {}}) is None


@pytest.mark.parametrize("change", [
    {"schema": 1},
    {"product": "Other"},
])
def test_manifest_header_is_validated(change):
    document = _manifest()
    document.update(change)
    with pytest.raises(update_check.ManifestError):
        update_check.parse_manifest(document)


@pytest.mark.parametrize("override", [
    {"urls": ["http://r2.example/server.zip"]},
    {"urls": []},
    {"size": 0},
    {"sha256": "xyz"},
    {"package": "../server.zip"},
    {"preserve": ["../config.toml"]},
    {"version": "latest"},
])
def test_manifest_server_entry_is_validated(override):
    with pytest.raises(update_check.ManifestError):
        update_check.parse_manifest(_manifest(**override))


def test_check_for_update_reports_only_newer_versions():
    web = FakeWeb({MANIFEST_URL: json.dumps(_manifest()).encode()})
    assert update_check.check_for_update("0.2.0", MANIFEST_URL, opener=web).version == "0.3.0"
    assert update_check.check_for_update("0.3.0", MANIFEST_URL, opener=web) is None
    with pytest.raises(update_check.ManifestError):
        update_check.check_for_update("0.2.0", "http://example.com/stable.json", opener=web)


def test_startup_notice_logs_new_version(caplog):
    web = FakeWeb({MANIFEST_URL: json.dumps(_manifest(required=True)).encode()})
    log = logging.getLogger("test.update")
    with caplog.at_level(logging.DEBUG, logger="test.update"):
        thread = update_check.start_background_check(ServerConfig(), "0.2.0", log=log, opener=web)
        thread.join(5)
    assert "New BattleSpades server version 0.3.0 available (current 0.2.0)" in caplog.text
    assert "REQUIRED" in caplog.text


def test_startup_notice_is_silent_offline(caplog):
    web = FakeWeb({MANIFEST_URL: urllib.error.URLError("no network")})
    log = logging.getLogger("test.update.offline")
    with caplog.at_level(logging.INFO, logger="test.update.offline"):
        update_check.start_background_check(ServerConfig(), "0.2.0", log=log, opener=web).join(5)
    assert caplog.text == ""


def test_startup_notice_can_be_disabled():
    config = ServerConfig()
    config.update_check_enabled = False
    web = FakeWeb({})
    assert update_check.start_background_check(config, "0.2.0", opener=web) is None
    assert web.requests == []


def test_download_falls_back_through_mirrors_in_order(tmp_path):
    corrupted = bytearray(PACKAGE)
    corrupted[40] ^= 0xFF
    urls = ["https://github.example/server.zip", "https://bad.example/server.zip",
            "https://slow.example/server.zip", "https://r2.example/server.zip"]
    web = FakeWeb({
        urls[1]: bytes(corrupted),
        urls[2]: TimeoutError("timed out"),
        urls[3]: PACKAGE,
    })
    release = update_check.parse_manifest(_manifest(mirrors=urls))
    messages = []
    archive = update_check.download_verified(release, tmp_path, opener=web, out=messages.append)
    assert web.requests == urls
    assert archive.read_bytes() == PACKAGE
    assert sum("failed" in m for m in messages) == 3
    assert not list(tmp_path.glob("*.partial"))


def test_download_fails_when_no_mirror_verifies(tmp_path):
    web = FakeWeb({"https://r2.example/server.zip": PACKAGE[:-1]})
    release = update_check.parse_manifest(_manifest())
    with pytest.raises(update_check.UpdateError, match="every mirror failed"):
        update_check.download_verified(release, tmp_path, opener=web, out=lambda _m: None)
    assert not list(tmp_path.iterdir())


def test_update_command_stages_without_touching_the_installation(tmp_path):
    (tmp_path / "BattleSpades.exe").write_text("server v1")
    (tmp_path / "VERSION").write_text("0.2.0\n")
    (tmp_path / "config.toml").write_text("operator config")
    before = {p.name: p.read_text() for p in tmp_path.iterdir() if p.is_file()}
    web = FakeWeb({
        MANIFEST_URL: json.dumps(_manifest()).encode(),
        "https://r2.example/server.zip": PACKAGE,
    })
    messages = []
    assert update_check.run_update(tmp_path, "0.2.0", url=MANIFEST_URL, opener=web, out=messages.append) == 0
    staged = tmp_path / "update" / "staging" / "server-0.3.0"
    marker = json.loads((staged / "staged.json").read_text())
    assert marker["component"] == "server" and marker["version"] == "0.3.0"
    assert (staged / "payload" / marker["root"] / "BattleSpades.exe").read_text() == "server v2"
    assert {p.name: p.read_text() for p in tmp_path.iterdir() if p.is_file()} == before
    assert any("Nothing was replaced" in m for m in messages)
    assert not (tmp_path / "update" / "download" / "BattleSpades-server-0.3.0.zip").exists()


def test_update_command_up_to_date_and_failures(tmp_path):
    web = FakeWeb({MANIFEST_URL: json.dumps(_manifest()).encode()})
    assert update_check.run_update(tmp_path, "0.3.0", url=MANIFEST_URL, opener=web, out=lambda _m: None) == 0
    assert not (tmp_path / "update").exists()
    offline = FakeWeb({})
    assert update_check.run_update(tmp_path, "0.2.0", url=MANIFEST_URL, opener=offline, out=lambda _m: None) == 1


def test_staging_refuses_zip_slip(tmp_path):
    evil = _zip({"../escaped.txt": "x", "VERSION": "0.3.0"})
    release = update_check.parse_manifest(_manifest(package_bytes=evil))
    archive = tmp_path / "evil.zip"
    archive.write_bytes(evil)
    with pytest.raises(update_check.UpdateError, match="escapes"):
        update_check.stage_update(tmp_path, release, archive)
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert not list((tmp_path / "update" / "staging").iterdir())


def test_update_flag_is_an_exclusive_action():
    parser = build_parser()
    assert parser.parse_args(["--update"]).update
    assert parser.parse_args(["--update", "--config", "x.toml"]).update
    with pytest.raises(SystemExit):
        parser.parse_args(["--update", "--check"])


def test_update_manifest_url_must_be_https(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[updates]\nupdate_manifest_url = "http://example.com/s.json"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="update_manifest_url"):
        load_config(path)
    path.write_text('[updates]\nupdate_check_enabled = false\n'
                    'update_manifest_url = "https://mirror.example/beta.json"\n', encoding="utf-8")
    config = load_config(path)
    assert config.update_check_enabled is False
    assert config.update_manifest_url == "https://mirror.example/beta.json"
