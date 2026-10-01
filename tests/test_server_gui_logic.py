"""Desktop host logic, tested without a display or a toolkit."""

from __future__ import annotations

import socket
import struct
import subprocess
import sys
from pathlib import Path

import pytest
import tomlkit

from server_gui import catalog, config_doc, console, firewall, host_settings, logparse, network
from server_gui.config_doc import ConfigDocument, validate_text
from server_gui.gui_state import GuiState, SingleInstance

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHIPPED = (PROJECT_ROOT / "config.toml").read_text(encoding="utf-8")


def _comment_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith("#")]


# ---------------------------------------------------------------- config doc


def test_untouched_document_round_trips_byte_for_byte() -> None:
    doc = ConfigDocument(SHIPPED)
    assert doc.text() == SHIPPED
    assert not doc.dirty


def test_edits_keep_every_comment_and_trailing_note() -> None:
    doc = ConfigDocument(SHIPPED)
    doc.set("server", "max_players", 16)
    doc.set("bots", "difficulty", "hard")
    doc.set("game_rules", "RULE_BLOCK_HEALTH", "200%")
    doc.set("lobby", "map_rotation", ["London", "Atlantis"])
    text = doc.text()
    assert doc.dirty
    assert _comment_lines(text) == _comment_lines(SHIPPED)
    assert "max_players = 16 # stock Match Lobby maximum" in text
    assert 'RULE_BLOCK_HEALTH = "200%"' in text and "# 50%, 100%, 200%" in text
    data = tomlkit.parse(text)
    assert data["lobby"]["map_rotation"] == ["London", "Atlantis"]
    # Only the edited lines differ.
    changed = [a for a, b in zip(SHIPPED.splitlines(), text.splitlines()) if a != b]
    assert len(changed) == 4


def test_describe_covers_every_key_with_help_and_kinds() -> None:
    doc = ConfigDocument(SHIPPED)
    sections = {section.name: section for section in doc.describe()}
    for name in ("server", "steam", "revival", "game", "lobby", "game_rules", "bots", "modes.tdm", "admin", "debug"):
        assert name in sections
    shipped = tomlkit.parse(SHIPPED)
    for name, section in sections.items():
        node = shipped
        for part in name.split("."):
            node = node[part]
        present = {spec.key for spec in section.keys if not spec.optional}
        expected = {k for k, v in node.items() if not isinstance(v, dict) or isinstance(v, tomlkit.items.InlineTable)}
        assert present == expected, name
    server = {spec.key: spec for spec in sections["server"].keys}
    assert "Empty = open server" in server["password"].description
    assert "Private welcome" not in server["password"].description
    assert server["join_greeting"].optional and server["motd"].optional and server["motd"].kind == "list"
    lobby = {spec.key: spec for spec in sections["lobby"].keys}
    assert lobby["match_length_minutes"].optional and lobby["match_length_minutes"].example == 15
    assert lobby["map_size_overrides"].kind == "table"
    bots = {spec.key: spec for spec in sections["bots"].keys}
    assert bots["difficulty"].kind == "enum"
    assert [v for _l, v in bots["difficulty"].choices] == ["casual", "normal", "hard", "mixed"]
    rules = {spec.key: spec for spec in sections["game_rules"].keys}
    assert rules["RULE_ENABLE_BLOCKS"].kind == "bool"
    assert rules["RULE_SPAWN_PROTECTION_TIME"].choices[0] == ("OFF", "OFF")
    assert ("OFF", False) in rules["RULE_TDM_SCORE_TARGET"].choices


def test_rule_choices_are_all_accepted_by_the_server() -> None:
    doc = ConfigDocument(SHIPPED)
    rules = next(s for s in doc.describe() if s.name == "game_rules")
    for spec in rules.keys:
        if spec.kind != "enum":
            continue
        for _label, value in spec.choices:
            candidate = ConfigDocument(SHIPPED)
            candidate.set("game_rules", spec.key, value)
            result = validate_text(candidate.text())
            assert result.ok, (spec.key, value, result.error)


def test_shipped_config_validates_with_admin_warning_only() -> None:
    result = validate_text(SHIPPED, maps_dir=PROJECT_ROOT / "maps")
    assert result.ok, result.error
    assert any("/admin login stays disabled" in w for w in result.warnings)


@pytest.mark.parametrize("section,key,value,needle", [
    ("steam", "app_id", 480, "224540"),
    ("server", "port", 70000, "server.port"),
    ("server", "port", "abc", "server.port"),
    ("game", "default_mode", "bogus", "not a registered game mode"),
    ("lobby", "match_length_minutes", 7, "match_length_minutes"),
    ("server", "password", "x" * 65, "64 bytes"),
    ("updates", "update_manifest_url", "ftp://example", "https"),
    ("game_rules", "RULE_BLOCK_HEALTH", "300%", "RULE_BLOCK_HEALTH"),
])
def test_invalid_values_are_rejected_like_the_server_does(section, key, value, needle) -> None:
    doc = ConfigDocument(SHIPPED)
    doc.set(section, key, value)
    result = validate_text(doc.text())
    assert not result.ok
    assert needle in result.error


def test_syntax_errors_are_caught_before_the_lenient_loader() -> None:
    result = validate_text(SHIPPED + "\n[broken\n")
    assert not result.ok and "TOML syntax error" in result.error


def test_missing_start_map_is_rejected_when_maps_dir_known(tmp_path: Path) -> None:
    doc = ConfigDocument(SHIPPED)
    doc.set("game", "default_map", "NoSuchMap")
    assert not validate_text(doc.text(), maps_dir=PROJECT_ROOT / "maps").ok


def test_save_refuses_invalid_and_writes_valid_atomically(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(SHIPPED, encoding="utf-8")
    doc = ConfigDocument.load(path)
    doc.set("steam", "app_id", 480)
    assert not doc.save().ok
    assert path.read_text(encoding="utf-8") == SHIPPED
    doc.set("steam", "app_id", 224540)
    doc.set("server", "name", "Field HQ")
    result = doc.save()
    assert result.ok
    written = path.read_text(encoding="utf-8")
    assert 'name = "Field HQ"' in written
    assert _comment_lines(written) == _comment_lines(SHIPPED)
    assert not doc.dirty
    assert not list(tmp_path.glob(".config-*"))


def test_restore_section_defaults() -> None:
    defaults = ConfigDocument(SHIPPED)
    doc = ConfigDocument(SHIPPED)
    doc.set("bots", "difficulty", "hard")
    doc.set("bots", "fill_target", 3)
    doc.set("lobby", "match_length_minutes", 30)
    assert set(doc.restore_section("bots", defaults)) == {"difficulty", "fill_target"}
    assert doc.restore_section("lobby", defaults) == ["match_length_minutes"]
    assert doc.text() == SHIPPED


def test_coerce_converts_and_rejects() -> None:
    spec = config_doc.KeySpec("server", "max_players", "int")
    assert config_doc.coerce(spec, " 12 ") == 12
    with pytest.raises(ValueError):
        config_doc.coerce(spec, "twelve")
    assert config_doc.coerce(config_doc.KeySpec("a", "b", "float"), "1.5") == 1.5
    listing = config_doc.KeySpec("lobby", "map_rotation", "list")
    assert config_doc.coerce(listing, '["A", "B"]') == ["A", "B"]
    with pytest.raises(ValueError):
        config_doc.coerce(listing, '{ a = 1 }')
    enum = config_doc.KeySpec("bots", "difficulty", "enum", choices=(("Hard", "hard"),))
    assert config_doc.coerce(enum, "Hard") == "hard"
    with pytest.raises(ValueError):
        config_doc.coerce(enum, "Impossible")


# ------------------------------------------------------------------- catalog


def test_registered_mode_codes_match_the_server_registry() -> None:
    from modes import canonical_mode_code, registered_mode_codes

    canonical = {canonical_mode_code(name) for name in registered_mode_codes()}
    assert canonical == set(catalog.REGISTERED_MODE_CODES)


def test_mode_choices_and_pools_use_installed_maps() -> None:
    choices = catalog.mode_choices()
    assert [c.code for c in choices] == ["tdm", "ctf", "cctf", "zom", "vip", "mh", "tc", "dia", "dem", "oc"]
    installed = set(catalog.available_maps(PROJECT_ROOT / "maps"))
    for choice in choices:
        pool = catalog.mode_pool(PROJECT_ROOT / "maps", choice.code)
        assert pool, choice.code
        assert set(pool) <= installed
    assert catalog.mode_pool(PROJECT_ROOT / "maps", "vip") == ["Alcatraz", "CityOfChicago"]
    assert catalog.map_title("MayanJungle") == "Mayan Jungle"
    assert catalog.map_title("WW1") == "WW1"


def test_mode_pool_falls_back_to_lobby_preset(tmp_path: Path) -> None:
    for name in ("Alcatraz", "CityOfChicago", "London"):
        (tmp_path / f"{name}.vxl").write_bytes(b"")
    assert catalog.mode_pool(tmp_path, "tc") == ["Alcatraz", "CityOfChicago"]


# ------------------------------------------------------------- host settings


def test_host_settings_round_trip_through_real_keys() -> None:
    doc = ConfigDocument(SHIPPED)
    settings = host_settings.read(doc)
    assert settings.rotation == "retail" and settings.mode == "tdm"
    settings.name = "Olive Drab"
    settings.mode = "ctf"
    settings.start_map = "Atlantis"
    settings.rotation = "single"
    settings.bot_mode = "fixed"
    settings.bot_target = 6
    settings.difficulty = "hard"
    settings.password = "letmein"
    settings.port = 32887
    host_settings.apply(doc, settings)
    assert doc.get("lobby", "map_rotation") == ["Atlantis"]
    assert doc.get("bots", "population_mode") == "fixed" and doc.get("bots", "max_bots") == 6
    assert doc.get("server", "password") == "letmein"
    assert doc.get("admin", "password") == "changeme"  # empty field leaves it alone
    again = host_settings.read(doc)
    assert again.rotation == "single" and again.bot_mode == "fixed" and again.bot_target == 6
    assert validate_text(doc.text()).ok


def test_backfill_raises_max_bots_to_the_target() -> None:
    doc = ConfigDocument(SHIPPED)
    settings = host_settings.read(doc)
    settings.bot_target = 20
    host_settings.apply(doc, settings)
    assert doc.get("bots", "fill_target") == 20 and doc.get("bots", "max_bots") == 20


def test_problems_and_ports() -> None:
    bad = host_settings.HostSettings(name=" ", port=0, rotation="custom", custom_rotation=[], password="x" * 70)
    assert len(host_settings.problems(bad)) == 4
    doc = ConfigDocument(SHIPPED)
    doc.set("server", "port", 32887)
    assert host_settings.server_ports(doc) == [32887]
    doc.set("steam", "enabled", True)
    assert host_settings.server_ports(doc) == [8766, 32887, 32888]
    doc.set("steam", "query_port", 40000)
    assert host_settings.query_port(doc) == 40000


def test_steam_p2p_readiness_mirrors_server_checks(tmp_path: Path) -> None:
    doc = ConfigDocument(SHIPPED)
    assert not host_settings.steam_p2p_readiness(tmp_path, doc, platform="linux", helper=None).available
    assert not host_settings.steam_p2p_readiness(tmp_path, doc, platform="win32", helper=None).available
    helper = tmp_path / "aos-retail-relay.exe"
    ready = host_settings.steam_p2p_readiness(tmp_path, doc, platform="win32", helper=helper, steam_running=False)
    assert ready.available and "Steam is not running" in ready.warning
    doc.set("revival", "require_identity", True)
    assert not host_settings.steam_p2p_readiness(tmp_path, doc, platform="win32", helper=helper).available


def test_join_info_has_addresses_and_no_password() -> None:
    text = host_settings.join_info(name="HQ", public_ip="203.0.113.7", lan_ip="192.168.1.4", port=32887,
                                   password=True, steam_hosted=True, steam_lobby="1099")
    assert "203.0.113.7:32887" in text and "192.168.1.4:32887" in text
    assert "[Steam] HQ" in text and "1099" in text
    assert "Password required" in text


def test_steam_browser_readiness_flags_runtime_and_port(tmp_path: Path) -> None:
    doc = ConfigDocument(SHIPPED)
    doc.set("server", "port", 27015)
    notes = host_settings.steam_browser_readiness(tmp_path, doc, platform="win32")
    assert any(level == "warn" and "steam_api.dll" in text for level, text in notes)
    assert any("32887" in text for _level, text in notes)


# ------------------------------------------------------------------- network


IGD_XML = b"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device><deviceList><device><deviceList><device>
<serviceList><service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
<controlURL>/ctl/IPConn</controlURL></service></serviceList></device></deviceList></device></deviceList></device></root>"""


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, _limit=-1) -> bytes:
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


def soap_reply(action: str, **values) -> bytes:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in values.items())
    return (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action}Response xmlns:u="x">{body}</u:{action}Response></s:Body></s:Envelope>').encode()


def test_ssdp_and_device_description_parsing() -> None:
    reply = b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=120\r\nLOCATION: http://192.168.1.1:5000/rootDesc.xml\r\n\r\n"
    assert network.parse_ssdp_location(reply) == "http://192.168.1.1:5000/rootDesc.xml"
    assert network.parse_ssdp_location(b"HTTP/1.1 200 OK\r\nLOCATION: file:///etc/passwd\r\n\r\n") is None
    gateway = network.parse_device_description(IGD_XML, "http://192.168.1.1:5000/rootDesc.xml")
    assert gateway.control_url == "http://192.168.1.1:5000/ctl/IPConn"
    assert gateway.host == "192.168.1.1"


def test_soap_envelope_escapes_values() -> None:
    body = network.soap_envelope("urn:x", "AddPortMapping", [("NewPortMappingDescription", "<a&b>")])
    assert b"&lt;a&amp;b&gt;" in body


def test_upnp_client_add_delete_external_with_mocked_router() -> None:
    requests = []

    def opener(request, timeout=0):
        if isinstance(request, str):
            return FakeResponse(IGD_XML)
        requests.append((request.headers.get("Soapaction"), request.data))
        action = request.headers["Soapaction"].split("#")[1].strip('"')
        if action == "GetExternalIPAddress":
            return FakeResponse(soap_reply(action, NewExternalIPAddress="203.0.113.7"))
        return FakeResponse(soap_reply(action))

    client = network.UPnPClient.discover(opener=opener, search=lambda _t: ["http://192.168.1.1:5000/rootDesc.xml"])
    client.add_mapping(32887, "192.168.1.4")
    assert client.external_ip() == "203.0.113.7"
    client.delete_mapping(32887)
    actions = [a for a, _d in requests]
    assert actions[0].endswith('#AddPortMapping"') and actions[-1].endswith('#DeletePortMapping"')
    assert b"<NewInternalClient>192.168.1.4</NewInternalClient>" in requests[0][1]
    assert b"<NewProtocol>UDP</NewProtocol>" in requests[0][1]


def test_upnp_discovery_reports_no_router() -> None:
    with pytest.raises(network.UPnPError, match="No UPnP router"):
        network.UPnPClient.discover(search=lambda _t: [])


class FakeUPnP:
    def __init__(self) -> None:
        self.gateway = network.Gateway("http://192.168.1.1/x", "http://192.168.1.1/c", "urn:x")
        self.added, self.deleted = [], []

    def add_mapping(self, port, client):
        self.added.append((port, client))

    def delete_mapping(self, port):
        self.deleted.append(port)

    def external_ip(self):
        return "203.0.113.7"


def test_forwarder_maps_and_unmaps_exactly_its_ports() -> None:
    fake = FakeUPnP()
    forwarder = network.PortForwarder(upnp_factory=lambda: fake, lan_ip=lambda: "192.168.1.4")
    forwarder.lan_ip_for = lambda _host: "192.168.1.4"
    result = forwarder.open([32887, 32888, 32887])
    assert result.ok and result.method == "UPnP" and result.ports == [32887, 32888]
    assert result.external_ip == "203.0.113.7"
    closed = forwarder.close()
    assert closed.ok and fake.deleted == [32887, 32888]
    assert forwarder.mapped == []


def test_forwarder_falls_back_to_natpmp_then_reports_failure() -> None:
    def no_upnp():
        raise network.UPnPError("No UPnP router answered.")

    calls = []

    def natpmp(gateway, port, lifetime=network.NATPMP_LIFETIME):
        calls.append((gateway, port, lifetime))
        return port, 7200

    forwarder = network.PortForwarder(upnp_factory=no_upnp, gateway_finder=lambda: "192.168.1.1", natpmp=natpmp)
    result = forwarder.open([32887])
    assert result.ok and result.method == "NAT-PMP"
    forwarder.close()
    assert calls == [("192.168.1.1", 32887, 7200), ("192.168.1.1", 32887, 0)]

    def refused(*_args, **_kwargs):
        raise network.NatPmpError("the router does not answer NAT-PMP")

    failing = network.PortForwarder(upnp_factory=no_upnp, gateway_finder=lambda: "192.168.1.1", natpmp=refused)
    result = failing.open([32887])
    assert not result.ok and "UPnP" in result.message and "NAT-PMP" in result.message


def test_natpmp_wire_format() -> None:
    assert network.natpmp_request(32887) == struct.pack("!BBHHHI", 0, 1, 0, 32887, 32887, 7200)
    assert network.natpmp_request(32887, 0) == struct.pack("!BBHHHI", 0, 1, 0, 32887, 0, 0)
    answer = struct.pack("!BBHIHHI", 0, 129, 0, 5, 32887, 32887, 3600)
    assert network.natpmp_parse(answer) == (32887, 32887, 3600)
    with pytest.raises(network.NatPmpError, match="refused"):
        network.natpmp_parse(struct.pack("!BBHIHHI", 0, 129, 2, 5, 0, 0, 0))


def test_classify_and_honest_assessment() -> None:
    assert network.classify("100.72.1.2") == "cgnat"
    assert network.classify("192.168.1.1") == "private"
    assert network.classify("8.8.8.8") == "public"
    findings = network.assess(port=32887, server_running=True, local_answer=True, lan_ip="192.168.1.4",
                              public="203.0.113.7", router_external="100.72.1.2", forwarding=None, hairpin_answer=False)
    texts = " ".join(f.text for f in findings)
    assert "carrier-grade NAT" in texts
    assert "does not prove the port is closed" in texts
    assert "ask a friend" in texts
    assert not any(f.level == "ok" and "public address 203.0.113.7:32887" in f.text for f in findings)
    stopped = network.assess(port=32887, server_running=False, local_answer=None, lan_ip=None, public=None,
                             router_external=None, forwarding=None, hairpin_answer=None)
    assert stopped[1].level == "warn"


def test_a2s_probe_against_a_local_responder() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    import threading

    def answer():
        data, address = server.recvfrom(1024)
        if data == network.A2S_INFO:
            server.sendto(b"\xff\xff\xff\xffA\x01\x02\x03\x04", address)

    thread = threading.Thread(target=answer)
    thread.start()
    try:
        assert network.a2s_probe("127.0.0.1", port, timeout=2)
    finally:
        thread.join()
        server.close()
    assert not network.a2s_probe("127.0.0.1", port, timeout=0.2)


# ------------------------------------------------------------ firewall, misc


def test_firewall_rule_and_instructions() -> None:
    program = Path(r"C:\Games\Battle Spades\BattleSpades.exe")
    arguments = firewall.netsh_add_arguments(program, [32888, 32887])
    assert "protocol=UDP" in arguments and "localport=32887,32888" in arguments and "dir=in" in arguments
    command = firewall.windows_command_line(program, [32887])
    assert 'program="C:\\Games\\Battle Spades\\BattleSpades.exe"' in command
    assert 'name="BattleSpades Server (UDP 32887)"' in command
    assert "sudo ufw allow 32887/udp" in firewall.instructions("linux", program, [32887])
    assert "socketfilterfw" in firewall.instructions("darwin", program, [32887])


def test_gui_state_round_trip_and_bad_types(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = GuiState(geometry="1000x700+10+10", steam_p2p=False, tab="Console")
    assert state.save(path)
    loaded = GuiState.load(path)
    assert loaded.geometry == "1000x700+10+10" and loaded.steam_p2p is False and loaded.tab == "Console"
    path.write_text('{"steam_p2p": "yes", "tab": 3, "unknown": 1}', encoding="utf-8")
    loaded = GuiState.load(path)
    assert loaded.steam_p2p is True and loaded.tab == "Host"
    path.write_text("not json", encoding="utf-8")
    assert GuiState.load(path) == GuiState()


def test_single_instance_lock(tmp_path: Path) -> None:
    first = SingleInstance(tmp_path / "gui.lock")
    second = SingleInstance(tmp_path / "gui.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_log_parsing_and_session_events() -> None:
    line = logparse.parse("2026-10-01 18:27:45 [WARNING] server.main: Something odd")
    assert line.level == "WARNING" and line.logger == "server.main" and line.message == "Something odd"
    trace = logparse.parse("  File \"x.py\", line 1", previous_level="ERROR")
    assert trace.level == "ERROR"
    events = logparse.SessionEvents()
    events.feed(logparse.parse("2026-10-01 18:27:45 [INFO] server.main: Server started: HQ"))
    events.feed(logparse.parse("2026-10-01 18:27:46 [INFO] server.steam_p2p: Steam relay hosting ready: lobby=109775 virtual_port=168"))
    assert events.started and events.steam_lobby == "109775" and events.steam_virtual_port == 168
    assert logparse.matches(line, min_level="INFO", search="odd")
    assert not logparse.matches(line, min_level="ERROR")


def test_palette_only_uses_real_console_commands() -> None:
    from server.control_channel import BUILTIN_COMMANDS, CONSOLE_COMMANDS
    from commands.command_handler import get_command
    import commands  # noqa: F401 - registers handlers

    for entry in console.PALETTE:
        name = entry.command
        assert name in CONSOLE_COMMANDS | BUILTIN_COMMANDS, name
        if name in CONSOLE_COMMANDS:
            assert get_command(name) is not None, name
    for name in CONSOLE_COMMANDS:
        assert get_command(name) is not None and get_command(name).admin_only or name in {"help", "players", "score"}
    assert console.fill(console.PALETTE[10], {"player": "Bob", "reason": ""}) == "kick Bob"
    assert console.validate_command("say hi") is None
    assert console.validate_command("tp 1 2 3") is not None
    assert console.validate_command("say a\nshutdown") is not None


def test_gui_modules_import_without_creating_a_window() -> None:
    script = (
        "import tkinter, server_gui.app, server_gui.theme;"
        "assert tkinter._default_root is None;"
        "print('ok')"
    )
    completed = subprocess.run([sys.executable, "-c", script], cwd=PROJECT_ROOT, capture_output=True,
                               text=True, timeout=120, env={**__import__("os").environ, "DISPLAY": ""})
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"
