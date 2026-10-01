import shared.constants as C
import pytest

from server.mode_data import get


@pytest.mark.parametrize("alias, canonical", [
    ("zombie", "zom"), ("classic_ctf", "cctf"), ("classic-ctf", "cctf"),
    ("multihill", "mh"), ("multi-hill", "mh"),
    ("territory_control", "tc"), ("territory-control", "tc"),
    ("diamond", "dia"), ("diamond_mine", "dia"),
    ("demolition", "dem"), ("occupation", "oc"), ("tutorial", "tut"),
])
def test_hosted_mode_aliases_share_wire_discovery_and_progression_metadata(alias, canonical):
    assert get(alias) is get(canonical)


def test_tdm_offers_the_battle_builder_classes_with_the_rocketeer():
    # Stock DEFAULT_TEAM_CLASSES (alias A93) card order plus the Rocketeer
    # (Glide/Jump packs), which BattleSpades TDM offers again since 2026-10-01.
    assert tuple(get("tdm").allowed_classes) == (
        int(C.CLASS_SOLDIER),
        int(C.CLASS_SCOUT),
        int(C.CLASS_ENGINEER),
        int(C.CLASS_MINER),
        int(C.CLASS_ROCKETEER),
        int(C.CLASS_SPECIALIST),
        int(C.CLASS_MEDIC),
    )
    allowed = set(get("tdm").allowed_classes)
    assert int(C.CLASS_ZOMBIE) not in allowed
    assert int(C.CLASS_GANGSTER_1) not in allowed
    assert int(C.CLASS_CLASSIC_SOLDIER) not in allowed
    assert int(C.CLASS_UGCBUILDER) not in allowed


def test_zombie_survivors_include_the_rocketeer():
    allowed = tuple(get("zom").allowed_classes)
    assert int(C.CLASS_ROCKETEER) in allowed
    assert int(C.CLASS_ZOMBIE) in allowed
    assert int(C.CLASS_FAST_ZOMBIE) not in allowed


def test_ctf_keeps_the_stock_six():
    # Only TDM and Zombie offered the Rocketeer before 68c36ca.
    assert int(C.CLASS_ROCKETEER) not in set(get("ctf").allowed_classes)
