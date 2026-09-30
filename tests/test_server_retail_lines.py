"""Server-driven retail HUD lines (audit3 server findings, 2026-09-28).

* PLAYER_LEFT "{0} has disconnected" is sent (packet 50) before PlayerLeft.
* PLAYER_JOINED never lets the client translate a player NAME.
* A joiner moved by the join balance gets TEAM_FULL once in the GameScene.
* /me does not repeat the sender name; command replies show team names.
* Over-long server names are capped at MAX_SERVER_NAME_SIZE (31) on the wire.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

from shared.bytes import ByteReader  # noqa: E402
from shared.packet import ChatMessage, LocalisedMessage  # noqa: E402

from server.announcements import (  # noqa: E402
    localisation_safe_name,
    resolve_freeform_variables,
)
from server.game_constants import TEAM1, TEAM2, TEAM_SPECTATOR  # noqa: E402
from server.main import BattleSpadesServer  # noqa: E402


def _localised(data: bytes) -> LocalisedMessage:
    assert data[0] == LocalisedMessage.id
    return LocalisedMessage(ByteReader(data[1:]))


def _server():
    server = SimpleNamespace(sent=[], _stopping=False)
    server.broadcast = lambda data, **_kw: server.sent.append(data)
    return server


def test_player_left_is_announced_for_departing_players():
    server = _server()
    connection = SimpleNamespace(in_game=True)
    player = SimpleNamespace(name="OK", team=TEAM1, is_bot=False)
    BattleSpadesServer._announce_player_left(server, connection, player)
    packet = _localised(server.sent[-1])
    assert packet.string_id == "PLAYER_LEFT"
    assert packet.parameters == ["OK"] and packet.localise_parameters == 0


def test_player_left_skips_bots_spectators_rollovers_and_shutdown():
    connection = SimpleNamespace(in_game=True)
    cases = [
        (SimpleNamespace(name="Bot", team=TEAM1, is_bot=True), connection, False),
        (SimpleNamespace(name="Spec", team=TEAM_SPECTATOR, is_bot=False), connection, False),
        (SimpleNamespace(name="Roll", team=TEAM2, is_bot=False),
         SimpleNamespace(in_game=True, disconnect_reason=18), False),
        (SimpleNamespace(name="Load", team=TEAM2, is_bot=False),
         SimpleNamespace(in_game=False), False),
    ]
    for player, conn, _expected in cases:
        server = _server()
        BattleSpadesServer._announce_player_left(server, conn, player)
        assert server.sent == [], player.name
    server = _server()
    server._stopping = True
    BattleSpadesServer._announce_player_left(
        server, connection, SimpleNamespace(name="X", team=TEAM1, is_bot=False)
    )
    assert server.sent == []


def test_player_names_can_never_be_looked_up_as_string_ids():
    # strings.get_by_id is globals()[id]: every key is an identifier.
    for name in ("OK", "MAP", "TEAM1_COLOR", "os", "get_by_id", "Player_1"):
        safe = localisation_safe_name(name)
        assert safe != name and safe.rstrip() == name and not safe.isidentifier()
    for name in ("[AoS] Kiril", "big guy", "x-y", "日本"):
        assert localisation_safe_name(name) == name


def test_join_rebalance_sends_team_full_once():
    from server.connection import Connection

    sent = []
    connection = SimpleNamespace(_join_rebalanced=True, send=sent.append)
    Connection._send_join_rebalance_notice(connection)
    Connection._send_join_rebalance_notice(connection)
    assert [_localised(data).string_id for data in sent] == ["TEAM_FULL"]


def test_me_does_not_repeat_the_sender_name(monkeypatch):
    import commands.player as player_cmds
    import server.handlers.social as social
    from commands.command_handler import CommandContext

    relayed = []
    monkeypatch.setattr(
        social, "_relay_chat", lambda server, player, data, team: relayed.append(data)
    )
    player = SimpleNamespace(id=3, name="Kiril", muted=False)
    asyncio.run(player_cmds.cmd_me(CommandContext(
        server=SimpleNamespace(), player=player, args=["waves"], raw_args="waves",
    )))
    packet = ChatMessage(ByteReader(relayed[0][1:]))
    assert packet.player_id == 3 and packet.value == "* waves"


def test_command_replies_show_team_display_names():
    from commands.command_handler import send_message

    sent = []
    player = SimpleNamespace(send=sent.append)
    asyncio.run(send_message(None, player, "TEAM1_COLOR: 3 points; TEAM2_COLOR: 1"))
    text = ChatMessage(ByteReader(sent[0][1:])).value
    assert text == "Blue: 3 points; Green: 1"
    assert resolve_freeform_variables("ZOMBIE_TEAM vs SURVIVOR_TEAM") == "Zombie vs Survivor"


def test_server_name_is_capped_at_the_retail_limit():
    from server.config import MAX_SERVER_NAME_SIZE, ServerConfig

    config = ServerConfig()
    config.name = "AoS Revival Official Territory Control"
    assert MAX_SERVER_NAME_SIZE == 31
    assert config.server_name == config.name[:31]
    config.name = "Short"
    assert config.server_name == "Short"


def test_official_fleet_names_fit_the_retail_limit():
    import tomllib
    from pathlib import Path

    for path in sorted(Path("configs").glob("official-*.toml")):
        name = tomllib.loads(path.read_text(encoding="utf-8"))["server"]["name"]
        assert len(name) <= 31, (path, name)


def test_recovered_maps_share_lighting_without_proving_other_map_defaults():
    """Their shared row is evidence for these maps, not missing sidecars."""
    import json
    from pathlib import Path

    rows = [json.loads(Path(f"maps/{name}.json").read_text(encoding="utf-8"))
            for name in ("MayanJungle", "Trenches")]
    for field in ("light_color", "light_direction", "back_light_color",
                  "back_light_direction", "ambient_light_color",
                  "ambient_light_intensity"):
        assert rows[0][field] == rows[1][field]
