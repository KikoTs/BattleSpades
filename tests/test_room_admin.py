"""Room-creator admin, auto-admin identities and bot name labels.

* /claimhost: the client-generated one-time creator token grants admin once;
  a wrong token or a replay is refused and counts as a failed admin login;
  rooms without a token (dedicated servers) have nothing to claim;
* the generated room password is shown to the claimant and /roompassword
  repeats it only in player-hosted rooms;
* loopback is kicked but never banned (the host and both relays use it);
* ``[admin] auto_admin`` matches only verified identities, never names;
* bots wear the ``[bots] name_prefix`` label within 15 bytes, stay unique,
  and humans cannot wear it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

import shared.constants as C
import commands.admin as admin_cmds
from commands.command_handler import get_all_commands, handle_command
from server.bans import BanManager
from server.config import (
    MAX_BOT_NAME_PREFIX,
    ServerConfig,
    load_config,
    normalize_admin_identity,
    valid_creator_token,
)
from server.game_constants import TEAM1
from server.player_names import (
    MAX_PLAYER_NAME_BYTES,
    allocate_bot_display_name,
    allocate_unique_player_name,
    poses_as_bot,
)
from tests.test_anticheat_commands import (  # noqa: F401 (fixture)
    STRONG_PASSWORD,
    _ctx,
    _player,
    _server,
    captured,
)

TOKEN = "Zq3vK8pW2mX9rT4yB7nC6dF5gH1jL0sA"
ROOM_PASSWORD = "h7Kp2QmX9vRt4Wza"


def _room(tmp_path=None):
    server = _server()
    server.config.admin_password = ROOM_PASSWORD
    server.config.admin_creator_token = TOKEN
    if tmp_path is not None:
        server.ban_manager = BanManager(str(tmp_path / "bans.json"))
    return server


def _claim(server, player, token):
    asyncio.run(admin_cmds.cmd_claim_host(_ctx(server, player, token)))


# --- /claimhost -------------------------------------------------------------


def test_creator_token_grants_admin_once_and_shows_the_room_password(captured):
    server = _room()
    creator = _player(server, 1, TEAM1, address="127.0.0.1:50000")

    _claim(server, creator, TOKEN)

    assert creator.admin is True
    assert creator.room_host is True
    assert server._creator_token_used is True
    lines = [text for _name, text in captured]
    assert any("you are its admin" in text for text in lines)
    assert any(ROOM_PASSWORD in text for text in lines)


def test_replayed_creator_token_is_refused_and_counted(captured, tmp_path):
    server = _room(tmp_path)
    creator = _player(server, 1, TEAM1, address="127.0.0.1:50000")
    _claim(server, creator, TOKEN)
    sniffer = _player(server, 2, TEAM1, address="203.0.113.7:4000")

    _claim(server, sniffer, TOKEN)

    assert not sniffer.admin
    assert captured[-1] == (sniffer.name, "The room host was already claimed.")
    assert admin_cmds._login_failures(server)["ip:203.0.113.7"][0] == 1


def test_wrong_creator_token_is_refused_and_bans_like_admin(captured, tmp_path):
    server = _room(tmp_path)
    limit = int(server.config.anticheat.admin_login_attempts)
    guesser = _player(server, 2, TEAM1, address="203.0.113.8:4000")

    for attempt in range(limit):
        _claim(server, guesser, f"{TOKEN[:-1]}{attempt}")

    assert not guesser.admin
    assert guesser.connection.disconnected == int(C.DISCONNECT.ERROR_KICKED)
    assert server.ban_manager.is_banned("203.0.113.8") is not None
    # The real creator can still claim: failures never consume the token.
    creator = _player(server, 1, TEAM1, address="127.0.0.1:50000")
    _claim(server, creator, TOKEN)
    assert creator.admin


def test_loopback_guessers_are_kicked_but_loopback_is_never_banned(captured, tmp_path):
    server = _room(tmp_path)
    limit = int(server.config.anticheat.admin_login_attempts)
    relayed = _player(server, 2, TEAM1, address="127.0.0.1:61000")

    for _ in range(limit):
        _claim(server, relayed, "x" * 32)

    assert relayed.connection.disconnected == int(C.DISCONNECT.ERROR_KICKED)
    assert server.ban_manager.is_banned("127.0.0.1") is None


def test_dedicated_server_has_no_room_host_to_claim(captured):
    server = _server()
    assert server.config.admin_creator_token == ""
    player = _player(server, 1, TEAM1, address="198.51.100.2:1")

    _claim(server, player, TOKEN)

    assert not player.admin
    assert captured[-1] == (player.name, "This server has no room host to claim.")
    assert admin_cmds._login_failures(server) == {}


def test_claimhost_compares_in_constant_time(captured, monkeypatch):
    calls = []
    real = admin_cmds.hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr(admin_cmds.hmac, "compare_digest", spy)
    server = _room()
    _claim(server, _player(server, 1, TEAM1), "guess")
    assert calls == [(b"guess", TOKEN.encode())]


def test_claimhost_is_hidden_from_help_and_never_logged(monkeypatch, caplog):
    assert "claimhost" not in {c.name for c in get_all_commands() if not c.hidden}
    server = _room()
    server.config.log_commands = True
    player = _player(server, 1, TEAM1, address="127.0.0.1:50000")
    sent = []

    async def fake_send(_server, _player, message):
        sent.append(message)

    monkeypatch.setattr(admin_cmds, "send_message", fake_send)
    with caplog.at_level(logging.INFO):
        asyncio.run(handle_command(server, player, f"claimhost {TOKEN}"))
    assert player.admin
    assert TOKEN not in caplog.text
    assert "<redacted>" in caplog.text


def test_room_password_is_repeated_only_in_player_hosted_rooms(captured):
    room = _room()
    admin = _player(room, 1, TEAM1)
    admin.admin = True
    asyncio.run(admin_cmds.cmd_room_password(_ctx(room, admin)))
    assert ROOM_PASSWORD in captured[-1][1]

    dedicated = _server()
    operator = _player(dedicated, 1, TEAM1)
    operator.admin = True
    asyncio.run(admin_cmds.cmd_room_password(_ctx(dedicated, operator)))
    assert STRONG_PASSWORD not in captured[-1][1]


# --- config -----------------------------------------------------------------


def test_creator_token_config_rejects_weak_values(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[admin]\ncreator_token = "short"\n', encoding="utf-8")
    assert load_config(path).admin_creator_token == ""
    path.write_text(f'[admin]\ncreator_token = "{TOKEN}"\n', encoding="utf-8")
    assert load_config(path).admin_creator_token == TOKEN
    assert valid_creator_token(TOKEN)
    assert not valid_creator_token(TOKEN[:20])
    assert not valid_creator_token(TOKEN[:-1] + '"')


def test_admin_identity_normalisation_never_accepts_names():
    assert normalize_admin_identity("76561198000000001") == "steam:76561198000000001"
    assert normalize_admin_identity("STEAM:76561198000000001") == "steam:76561198000000001"
    assert normalize_admin_identity("aosplay:PLY_abc") == "aosplay:ply_abc"
    assert normalize_admin_identity("KikoTs") is None
    assert normalize_admin_identity("steam:KikoTs") is None
    assert normalize_admin_identity("name:KikoTs") is None


@pytest.mark.parametrize("prefix", ['"[ROBOT]"', '"\\u00e9b"'])
def test_invalid_bot_name_prefix_is_rejected(tmp_path, prefix):
    path = tmp_path / "config.toml"
    path.write_text(f"[bots]\nname_prefix = {prefix}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)
    assert MAX_BOT_NAME_PREFIX == 6


# --- auto-admin ------------------------------------------------------------


def test_auto_admin_matches_verified_identities_only():
    server = _server()
    server.config.admin_auto_ids = ["steam:76561198000000001", "aosplay:ply_kiko"]
    identity = SimpleNamespace(public_id="ply_kiko", legacy_id="42", steam_id=None)
    verified = _player(server, 1, TEAM1)
    assert admin_cmds.grant_auto_admin(server, verified, identity)
    assert verified.admin

    # A legacy (unverified) joiner typing a listed id as a name gets nothing.
    impostor = _player(server, 2, TEAM1)
    impostor.name = "ply_kiko"
    assert not admin_cmds.grant_auto_admin(server, impostor, None)
    assert not impostor.admin


def test_auto_admin_uses_the_steam_relay_identity():
    server = _server()
    server.config.admin_auto_ids = ["steam:76561198000000001"]
    server.steam_p2p = SimpleNamespace(
        identity_for=lambda peer: "steam:76561198000000001"
    )
    player = _player(server, 1, TEAM1, address="127.0.0.1:41000")
    assert admin_cmds.grant_auto_admin(server, player, None)
    assert player.admin


def test_no_auto_admin_list_grants_nothing():
    server = _server()
    player = _player(server, 1, TEAM1)
    assert not admin_cmds.grant_auto_admin(
        server, player, SimpleNamespace(public_id="ply_x", legacy_id="1", steam_id="7")
    )
    assert not player.admin


# --- bot name labels --------------------------------------------------------


def test_bot_label_fits_the_retail_name_field_and_is_never_cut():
    players = []
    for base in ("Pancake", "SpadewalkerXXL", "Ж" * 10):
        name = allocate_bot_display_name(base, "[BOT]", players)
        assert name.startswith("[BOT]")
        assert len(name.encode("utf-8")) <= MAX_PLAYER_NAME_BYTES
        players.append(SimpleNamespace(name=name))
    assert players[0].name == "[BOT]Pancake"


def test_bot_labels_stay_unique_against_every_player():
    players = [SimpleNamespace(name="[BOT]Pancake"), SimpleNamespace(name="[B0T]Pancake~2")]
    name = allocate_bot_display_name("Pancake", "[BOT]", players)
    assert name == "[BOT]Pancake~3"
    long_name = allocate_bot_display_name(
        "Spadewalker", "[BOT]", [SimpleNamespace(name="[BOT]Spadewalke")]
    )
    assert long_name.startswith("[BOT]") and long_name.endswith("~2")
    assert len(long_name.encode("utf-8")) <= MAX_PLAYER_NAME_BYTES


def test_empty_prefix_keeps_bare_bot_names():
    assert allocate_bot_display_name("Pancake", "", []) == "Pancake"


def test_humans_cannot_wear_the_bot_label():
    for requested in ("[BOT]Kiko", "(b0t) Kiko", "Kiko <BOT>", "[ B O T ]Kiko"):
        assert poses_as_bot(requested, "[BOT]"), requested
        assert allocate_unique_player_name(requested, [], bot_label="[BOT]") == "Player"
    assert not poses_as_bot("Bottle", "[BOT]")
    assert allocate_unique_player_name("Bottle", [], bot_label="[BOT]") == "Bottle"
    # A plain prefix only blocks names that start with it.
    assert poses_as_bot("bot_Kiko", "BOT_")
    assert poses_as_bot("B0T_Kiko", "BOT_")
    assert not poses_as_bot("Bottle", "BOT_")
    # Without a label nothing is reserved.
    assert allocate_unique_player_name("[BOT]Kiko", []) == "[BOT]Kiko"


def test_profile_names_leave_room_for_the_label():
    from server.bot_ai.profiles import ProfileFactory

    factory = ProfileFactory(seed=4, max_name_length=15 - len("[BOT]"))
    names = [factory.create("normal").name for _ in range(40)]
    assert all(3 <= len(name) <= 10 for name in names)
    assert len({name.casefold() for name in names}) == len(names)
    # The default budget keeps the seeded catalogue exactly as before.
    assert ProfileFactory(seed=4).create("normal").name == ProfileFactory(seed=4).create("normal").name


def test_shipped_config_labels_bots_by_default():
    from pathlib import Path

    config = load_config(Path(__file__).resolve().parents[1] / "config.toml")
    assert config.bots.name_prefix == "[BOT]"
    assert ServerConfig().bots.name_prefix == "[BOT]"


def test_spawned_bots_wear_the_label_on_the_wire():
    from server.bot_ai.director import BotDirector
    from server.game_constants import TEAM2
    from server.main import BattleSpadesServer
    from shared.bytes import ByteReader
    from shared.packet import CreatePlayer

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    broadcasts: list[bytes] = []
    server.broadcast = lambda data, *_args, **_kwargs: broadcasts.append(bytes(data))
    director = BotDirector(server, supervisor=SimpleNamespace())

    named = asyncio.run(director.add_bot(team=TEAM1, name="Pancake",
                                         class_id=int(C.CLASS_SOLDIER)))
    drawn = asyncio.run(director.add_bot(team=TEAM2, class_id=int(C.CLASS_SOLDIER)))

    assert named.name == "[BOT]Pancake"
    assert drawn.name.startswith("[BOT]")
    assert len(drawn.name.encode("utf-8")) <= MAX_PLAYER_NAME_BYTES
    wire_names = {
        CreatePlayer(ByteReader(data[1:])).name
        for data in broadcasts if data[0] == CreatePlayer.id
    }
    assert {named.name, drawn.name} <= wire_names


# --- desktop host (server GUI) ---------------------------------------------


def _shipped_doc():
    from pathlib import Path

    from server_gui.config_doc import ConfigDocument

    root = Path(__file__).resolve().parents[1]
    return ConfigDocument((root / "config.toml").read_text(encoding="utf-8"))


def test_gui_replaces_the_shipped_admin_password_once():
    from server.config import admin_password_problem
    from server_gui import host_settings
    from server_gui.config_doc import validate_text

    doc = _shipped_doc()
    assert host_settings.admin_password_is_shipped_default(doc)
    generated = host_settings.ensure_admin_password(doc)
    assert generated and admin_password_problem(generated) is None
    assert doc.get("admin", "password") == generated
    assert not host_settings.admin_password_is_shipped_default(doc)
    assert host_settings.ensure_admin_password(doc) is None  # never rotates a real one
    assert validate_text(doc.text()).ok
    # A deliberately emptied password (login disabled) is respected.
    doc.set("admin", "password", "")
    assert host_settings.ensure_admin_password(doc) is None


def test_gui_generated_passwords_are_random_and_unambiguous():
    from server_gui import host_settings

    passwords = {host_settings.generate_admin_password() for _ in range(50)}
    assert len(passwords) == 50
    assert all(len(p) == 16 and not set(p) & set("0O1lI") for p in passwords)


def test_gui_auto_admin_round_trips_and_rejects_names():
    from server_gui import host_settings

    doc = _shipped_doc()
    settings = host_settings.read(doc)
    assert settings.auto_admin == []
    settings.auto_admin = host_settings.parse_auto_admin(
        "steam:76561198000000001, aosplay:ply_kiko"
    )
    host_settings.apply(doc, settings)
    assert doc.get("admin", "auto_admin") == ["steam:76561198000000001", "aosplay:ply_kiko"]
    assert host_settings.read(doc).auto_admin == settings.auto_admin
    settings.auto_admin = ["KikoTs"]
    assert any("not names" in problem for problem in host_settings.problems(settings))
