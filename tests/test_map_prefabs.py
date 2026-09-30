"""StateData.prefabs: the constructs a map adds to every class.

Retail: the list becomes the client's ``prefab_manager.map_prefabs`` and the
class menu appends it for every class whose construct lists hold
MAP_PREFABS. Which constructs a retail map listed is not recovered; the
shipped constants leave the list empty. The defaults here are the six
map-themed constructs the game ships, on the map each is named after.
"""
from __future__ import annotations

import asyncio
import textwrap
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import StateData

from server import map_prefabs as mp
from server.class_selection import normalize_server_selection
from server.config import ServerConfig, load_config
from server.connection import Connection
from server.main import BattleSpadesServer
from server.prefabs import get_registry, prefab_allowed

ROOT = Path(__file__).resolve().parents[1]
LONDON = ("prefab_london_taxi", "prefab_london_postbox", "prefab_london_duck")
LUNAR = ("prefab_lunarcargo", "prefab_lunarshield", "prefab_lunarsteps")


class Peer:
    address = ("127.0.0.1", 40300)

    def disconnect(self, reason=0):
        return None


def _config(map_name, **changes):
    config = ServerConfig(default_map=map_name)
    for key, value in changes.items():
        setattr(config, key, value)
    return config


def test_the_six_shipped_constructs_exist_on_the_server():
    for name in (*LONDON, *LUNAR):
        assert get_registry().get(name) is not None, name


def test_defaults_are_the_constructs_named_after_the_map():
    assert mp.map_prefabs(_config("London")) == LONDON
    assert mp.map_prefabs(_config("LunarBase")) == LUNAR
    assert mp.map_prefabs(_config("lunarbase")) == LUNAR
    for other in ("MayanJungle", "ArcticBase", "DragonIsland", "Trenches", ""):
        assert mp.map_prefabs(_config(other)) == ()


def test_every_class_but_zombies_and_classic_soldier_offers_them():
    offering = {
        class_id for class_id in C.CLASS_ITEMS
        if mp.class_offers_map_prefabs(class_id)
    }
    for class_id in (C.CLASS_SOLDIER, C.CLASS_SCOUT, C.CLASS_ROCKETEER,
                     C.CLASS_MINER, C.CLASS_ENGINEER, C.CLASS_SPECIALIST,
                     C.CLASS_MEDIC, C.CLASS_GANGSTER_1):
        assert int(class_id) in offering
    for class_id in (C.CLASS_ZOMBIE, C.CLASS_CLASSIC_SOLDIER):
        assert int(class_id) not in offering
    assert not mp.class_offers_map_prefabs(9999)


def test_operator_table_replaces_the_defaults_and_empty_offers_none():
    config = _config("MayanJungle", map_prefabs={
        "MayanJungle": ("prefab_ladder", "prefab_does_not_exist"),
    })
    assert mp.map_prefabs(config) == ("prefab_ladder",)
    assert mp.map_prefabs(_config("London", map_prefabs={})) == ()


def test_map_creator_is_left_alone():
    assert mp.map_prefabs(_config("London", ugc_runtime=True)) == ()


def _state_prefabs(server, *, native):
    connection = Connection(Peer(), server)
    connection.flight_profile_capable = native
    sent = []
    connection.send = lambda data, *args, **kwargs: sent.append(bytes(data))
    asyncio.run(connection.send_state_data(0))
    state = next(data for data in sent if data[0] == StateData.id)
    return list(StateData(ByteReader(state[1:])).prefabs)


def test_state_data_lists_them_for_the_native_client_only():
    server = BattleSpadesServer(ServerConfig(default_map="London"))

    assert _state_prefabs(server, native=True) == list(LONDON)
    assert _state_prefabs(server, native=False) == []

    server.config.default_map = "MayanJungle"
    assert _state_prefabs(server, native=True) == []


def test_a_map_construct_can_be_chosen_kept_in_order_and_built():
    config = _config("London")
    chosen = ("prefab_london_taxi", "prefab_ultrabarrier", "prefab_LONDON_duck",
              "prefab_zombiehand", "prefab_lunarcargo")

    selection = normalize_server_selection(config, C.CLASS_SOLDIER, (), chosen)

    assert selection.prefabs == (
        "prefab_london_taxi", "prefab_ultrabarrier", "prefab_london_duck",
    )
    player = SimpleNamespace(class_id=selection.class_id, prefabs=selection.prefabs)
    assert prefab_allowed(player, "prefab_london_taxi")
    assert not prefab_allowed(player, "prefab_london_postbox")   # not chosen


def test_other_maps_and_other_classes_cannot_carry_them():
    chosen = ("prefab_london_taxi", "prefab_ultrabarrier")
    elsewhere = normalize_server_selection(
        _config("MayanJungle"), C.CLASS_SOLDIER, (), chosen
    )
    assert elsewhere.prefabs == ("prefab_ultrabarrier",)

    zombie = normalize_server_selection(
        _config("London"), C.CLASS_ZOMBIE, (), ("prefab_london_taxi", "prefab_zombiehand")
    )
    assert zombie.prefabs == ("prefab_zombiehand",)


def test_generator_selection_preserves_map_constructs_and_order():
    chosen = (name for name in ("prefab_london_taxi", "prefab_ultrabarrier"))

    selection = normalize_server_selection(
        _config("London"), C.CLASS_SOLDIER, (), chosen
    )

    assert selection.prefabs == ("prefab_london_taxi", "prefab_ultrabarrier")


def test_config_table_is_read_and_checked(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""
        [lobby]
        map_prefabs = { MayanJungle = ["prefab_ladder", "prefab_ladder"] }
    """), encoding="utf-8")
    assert load_config(path).map_prefabs == {"MayanJungle": ("prefab_ladder",)}

    assert ServerConfig().map_prefabs is None
    for bad in ('map_prefabs = { London = "prefab_ladder" }',
                'map_prefabs = { London = ["../evil"] }',
                'map_prefabs = { London = [1] }',
                'map_prefabs = ["prefab_ladder"]'):
        path.write_text("[lobby]\n" + bad + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(path)


def test_shipped_config_lists_the_defaults():
    with (ROOT / "config.toml").open("rb") as stream:
        table = tomllib.load(stream)["lobby"]["map_prefabs"]
    assert {key: tuple(value) for key, value in table.items()} == mp.DEFAULT_MAP_PREFABS
    assert load_config(ROOT / "config.toml").map_prefabs == mp.DEFAULT_MAP_PREFABS
