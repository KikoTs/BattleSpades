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


# --- impersonation hardening ------------------------------------------------

from server.player_names import is_reserved_name, name_skeleton  # noqa: E402


def test_homoglyph_names_cannot_impersonate_an_online_player():
    players = [SimpleNamespace(name="Kiko")]

    for spoof in (
        "Κiko",        # Greek capital kappa
        "Kіko",        # Cyrillic i
        "Kikо",        # Cyrillic o
        "Kik0",             # zero for o
        "K​iko",       # zero-width space
        "Ｋｉｋｏ",  # fullwidth
        "KIKO",
        "Kíko",       # combining acute
        " Kiko ",
    ):
        allocated = allocate_unique_player_name(spoof, players)
        assert allocated.endswith("~2"), (spoof, allocated)
        assert name_skeleton(allocated) != name_skeleton("Kiko")


def test_lookalike_letter_pairs_share_a_skeleton():
    assert name_skeleton("Iван") != name_skeleton("Ivan")  # real Cyrillic stays distinct
    assert name_skeleton("lnfo") == name_skeleton("Info") == name_skeleton("1nfo")
    assert name_skeleton("rnike") == name_skeleton("mike")
    assert name_skeleton("Саt") == name_skeleton("Cat")


def test_invisible_and_bidi_characters_are_removed_from_display_names():
    assert allocate_unique_player_name("Ki​ko‮", []) == "Kiko"
    assert allocate_unique_player_name("​‌", []) == "Player"
    assert allocate_unique_player_name("́́", []) == "Player"


def test_reserved_and_staff_names_fall_back_to_player():
    for name in (
        "Server", "admin", "ADM1N", "Console", "System", "Moderator",
        "BattleSpades", "[Admin]Kiko", "(MOD) Kiko", "<Staff>x", "A d m i n",
        "Аdmin",  # Cyrillic A
    ):
        assert is_reserved_name(name), name
        assert allocate_unique_player_name(name, []) == "Player", name
    assert allocate_unique_player_name(
        "Kiril", [], extra_reserved=["kiril"]
    ) == "Player"
    for ordinary in ("Kiko", "[ABC]Kiko", "Adminton", "Modest", "Sysadmin99"):
        assert not is_reserved_name(ordinary), ordinary


def test_reserved_fallback_is_still_unique():
    players = [SimpleNamespace(name="Player")]
    assert allocate_unique_player_name("Admin", players) == "Player~2"


def test_names_impersonating_a_logged_in_admin_are_blocked():
    admin = SimpleNamespace(name="Kiko", admin=True)
    assert allocate_unique_player_name("K1ko", [admin]) == "Player"
    # A non-admin owner of the name only forces a suffix.
    plain = SimpleNamespace(name="Kiko", admin=False)
    assert allocate_unique_player_name("K1ko", [plain]) == "K1ko~2"


def test_humans_cannot_take_a_bot_name():
    bot = SimpleNamespace(name="Bot Alpha", is_bot=True)
    assert allocate_unique_player_name("B0t Alpha", [bot]) == "B0t Alpha~2"


def test_skeleton_suffix_stays_within_wire_limit():
    players = [SimpleNamespace(name="FifteenByteName")]
    allocated = allocate_unique_player_name("F1fteenByteName", players)
    assert len(allocated.encode("utf-8")) <= MAX_PLAYER_NAME_BYTES
    assert allocated.endswith("~2")
