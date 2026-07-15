from types import SimpleNamespace

from server.player_names import (
    MAX_PLAYER_NAME_BYTES,
    allocate_unique_player_name,
)


def test_duplicate_retail_names_receive_stable_unique_suffixes():
    players = [
        SimpleNamespace(name="KikoTs"),
        SimpleNamespace(name="KikoTs~2"),
    ]

    assert allocate_unique_player_name("KikoTs", players) == "KikoTs~3"
    assert allocate_unique_player_name("kikots", players) == "kikots~3"


def test_duplicate_suffix_stays_within_retail_wire_limit():
    players = [SimpleNamespace(name="FifteenByteName")]

    allocated = allocate_unique_player_name("FifteenByteName", players)

    assert allocated == "FifteenByteNa~2"
    assert len(allocated.encode("utf-8")) <= MAX_PLAYER_NAME_BYTES


def test_utf8_truncation_never_splits_a_codepoint():
    allocated = allocate_unique_player_name("Ж" * 20, [])

    assert allocated
    assert len(allocated.encode("utf-8")) <= MAX_PLAYER_NAME_BYTES


def test_empty_or_control_only_name_gets_safe_fallback():
    assert allocate_unique_player_name("\x00\x01", []) == "Player"
