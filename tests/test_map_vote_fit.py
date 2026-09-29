"""Map-vote candidates fit the lobby: size, never current, avoid recent."""

from __future__ import annotations

import ast
import struct
import sys
import zlib
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

from shared.bytes import ByteReader  # noqa: E402
from shared.packet import GenericVoteMessage  # noqa: E402

from server import voting  # noqa: E402

_HEADER = struct.Struct("<8sHHHH16sII")


def _write_botnav(directory, name, dry_columns, *, width=64, height=64):
    """A minimal nav cache whose flags plane marks ``dry_columns`` as DRY."""
    area = width * height
    flags = bytes([2] * dry_columns + [1] * (area - dry_columns))
    payload = bytes(area) + bytes(area) + flags + bytes(area * 17)
    compressed = zlib.compress(payload)
    header = _HEADER.pack(b"BSNAV01\0", 1, width, height, 63, bytes(16),
                          len(payload), len(compressed))
    (directory / f"{name}.botnav").write_bytes(header + compressed)
    (directory / f"{name}.vxl").write_bytes(b"")


class _Server:
    def __init__(self, maps_path, *, players=0, bots=0, default_map="", **config):
        self.config = SimpleNamespace(
            maps_path=str(maps_path), map_rotation=[], default_map=default_map,
            **config,
        )
        self.players = {}
        for index in range(players):
            self.players[index] = SimpleNamespace(
                id=index, is_bot=False, connection=SimpleNamespace(in_game=True)
            )
        for index in range(bots):
            self.players[100 + index] = SimpleNamespace(
                id=100 + index, is_bot=True, connection=None
            )
        self.connections = {}
        self.sent = []

    def broadcast(self, data):
        self.sent.append(data)


def _catalog(tmp_path):
    # Areas are scaled down (64x64 map); per-player area is configured to
    # match: 100 columns per player.
    sizes = {"Tiny": 250, "Small": 450, "Medium": 1000, "Large": 2400, "Huge": 3900}
    for name, dry in sizes.items():
        _write_botnav(tmp_path, name, dry)
    return sizes


def _manager(tmp_path, **kwargs):
    kwargs.setdefault("map_vote_area_per_player", 100.0)
    kwargs.setdefault("map_vote_min_area", 200.0)
    server = _Server(tmp_path, **kwargs)
    return server, voting.VoteManager(server)


def test_playable_area_is_read_from_the_nav_cache(tmp_path):
    _write_botnav(tmp_path, "Island", 1234)
    assert voting.map_playable_area(tmp_path, "Island") == 1234
    assert voting.map_playable_area(tmp_path, "Missing") is None
    (tmp_path / "Broken.botnav").write_bytes(b"junk")
    assert voting.map_playable_area(tmp_path, "Broken") is None


def test_small_lobby_is_offered_small_maps(tmp_path):
    _catalog(tmp_path)
    server, manager = _manager(tmp_path, players=4, default_map="Huge")
    assert manager.ensure_map_vote(now=1.0)
    assert set(manager.candidates) == {"Tiny", "Small", "Medium"}
    assert "Huge" not in manager.candidates


def test_big_lobby_is_offered_big_maps_and_bots_count(tmp_path):
    _catalog(tmp_path)
    server, manager = _manager(tmp_path, players=6, bots=18, default_map="Tiny")
    assert manager.ensure_map_vote(now=1.0)
    assert set(manager.candidates[:2]) == {"Large", "Huge"}
    assert "Tiny" not in manager.candidates


def test_bot_weight_shrinks_a_bot_heavy_lobby(tmp_path):
    _catalog(tmp_path)
    server, manager = _manager(
        tmp_path, players=2, bots=18, default_map="Huge", map_vote_bot_weight=0.0
    )
    assert manager.ensure_map_vote(now=1.0)
    assert manager.candidates[0] in {"Tiny", "Small"}


def test_recent_maps_are_avoided_but_current_is_never_offered(tmp_path):
    _catalog(tmp_path)
    server, manager = _manager(tmp_path, players=4, default_map="Medium")
    manager.note_map_played("Tiny")
    manager.note_map_played("Small")
    ranked = manager.rank_map_candidates(manager._mode_available_maps(), "Medium")
    assert "Medium" not in ranked
    # Tiny and Small fit best but were the last two maps: they go last,
    # the least recently played (Tiny) before Small.
    assert ranked[-2:] == ["Tiny", "Small"]
    assert manager.ensure_map_vote(now=1.0)
    assert "Small" not in manager.candidates
    assert "Medium" not in manager.candidates


def test_recent_maps_refill_a_short_catalog(tmp_path):
    for name in ("A", "B", "C"):
        _write_botnav(tmp_path, name, 1000)
    server, manager = _manager(tmp_path, players=10, default_map="C")
    manager.note_map_played("A")
    manager.note_map_played("B")
    assert manager.ensure_map_vote(now=1.0)
    assert manager.candidates == ("A", "B")


def test_size_fit_can_be_disabled_to_keep_pure_rotation(tmp_path):
    _catalog(tmp_path)
    server, manager = _manager(
        tmp_path, players=4, default_map="Large", map_vote_size_fit=False,
        map_vote_recent_exclude=0,
    )
    assert manager.ensure_map_vote(now=1.0)
    # Alphabetical catalog rotated after the current map.
    assert manager.candidates == ("Medium", "Small", "Tiny")


def test_unknown_size_is_a_neutral_fit_and_overrides_win(tmp_path):
    _catalog(tmp_path)
    (tmp_path / "Custom.vxl").write_bytes(b"")
    server, manager = _manager(
        tmp_path, players=4, default_map="Tiny",
        map_size_overrides={"Huge": 400},
    )
    assert manager.map_area("Custom") is None
    assert manager.map_area("Huge") == 400
    ranked = manager.rank_map_candidates(manager._mode_available_maps(), "Tiny")
    assert ranked.index("Huge") < ranked.index("Custom") < ranked.index("Large")


def test_map_transitions_record_history_and_vote_packets_stay_retail(tmp_path):
    _catalog(tmp_path)
    server, manager = _manager(tmp_path, players=4, default_map="Small")
    manager.note_map_played("Small")
    manager.note_map_played("Small")
    assert list(manager.map_history) == ["Small"]
    assert manager.ensure_map_vote(now=1.0)
    start = GenericVoteMessage(ByteReader(server.sent[-1][1:]))
    assert start.message_type == voting.VOTE_START
    assert len(start.candidates) == 3
    for candidate in start.candidates:
        name, args = ast.literal_eval(candidate["name"])
        assert args == () and name in manager.candidates


# ------------------------------------------- retail playlist pool (2026-09-28)


def _retail_catalog(tmp_path, names):
    import json
    import shutil

    shutil.copy("maps/retail_map_info.json", tmp_path / "retail_map_info.json")
    for name in names:
        (tmp_path / f"{name}.vxl").write_bytes(b"")
    json.loads((tmp_path / "retail_map_info.json").read_text(encoding="utf-8"))


def test_empty_rotation_uses_the_filtered_retail_playlist(tmp_path):
    _retail_catalog(tmp_path, ["Alcatraz", "CityOfChicago", "Atlantis",
                               "GreatWall", "Training", "Trenches"])
    server = _Server(tmp_path, game_mode="tc")
    manager = voting.VoteManager(server)
    assert set(manager._mode_available_maps()) == {"Alcatraz", "CityOfChicago"}
    server.config.game_mode = "dem"
    # GreatWall is in demolition.txt but invalid for dem; Training/Trenches
    # are tutorial/classic maps.
    assert set(manager._mode_available_maps()) == {"Atlantis"}


def test_explicit_rotation_drops_retail_invalid_stock_pairs(tmp_path, caplog):
    _retail_catalog(tmp_path, ["Atlantis", "GreatWall", "MyCustomMap"])
    server = _Server(tmp_path, game_mode="dem")
    server.config.map_rotation = ["Atlantis", "GreatWall", "MyCustomMap"]
    manager = voting.VoteManager(server)
    assert manager._mode_available_maps() == ("Atlantis", "MyCustomMap")
    assert any("GreatWall" in r.getMessage() for r in caplog.records)


def test_maps_over_their_retail_player_cap_are_offered_last(tmp_path):
    _retail_catalog(tmp_path, ["DragonIsland", "Atlantis", "BlockNess", "London"])
    server = _Server(tmp_path, players=18, game_mode="tdm",
                     map_vote_retail_max_players=True, map_vote_size_fit=False,
                     map_vote_recent_exclude=0)
    manager = voting.VoteManager(server)
    ranked = manager.rank_map_candidates(
        ["DragonIsland", "Atlantis", "BlockNess", "London"], "")
    # DragonIsland caps at 16, London at 20, BlockNess at 24 (retail mapinfo).
    assert ranked[-1] == "DragonIsland"
    server.config.map_vote_retail_max_players = False
    assert manager.rank_map_candidates(
        ["DragonIsland", "Atlantis"], "")[0] == "DragonIsland"


def test_rotation_is_shuffled_once_like_the_retail_lobby(tmp_path, monkeypatch):
    names = [f"Map{i:02d}" for i in range(12)]
    for name in names:
        (tmp_path / f"{name}.vxl").write_bytes(b"")
    monkeypatch.setattr(voting.random, "shuffle", lambda items: items.reverse())
    shuffled = voting.VoteManager(_Server(tmp_path, map_rotation_shuffle=True))
    assert shuffled._available_maps == tuple(reversed(names))
    plain = voting.VoteManager(_Server(tmp_path, map_rotation_shuffle=False))
    assert plain._available_maps == tuple(names)
