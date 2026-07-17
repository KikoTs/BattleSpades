import shared.constants as C

from server.mode_data import get


def test_tdm_only_exposes_normal_battle_builder_classes():
    allowed = set(get("tdm").allowed_classes)
    assert allowed == {
        int(C.CLASS_SOLDIER),
        int(C.CLASS_SCOUT),
        int(C.CLASS_ROCKETEER),
        int(C.CLASS_MINER),
        int(C.CLASS_ENGINEER),
        int(C.CLASS_SPECIALIST),
        int(C.CLASS_MEDIC),
    }
    assert int(C.CLASS_ZOMBIE) not in allowed
    assert int(C.CLASS_GANGSTER_1) not in allowed
    assert int(C.CLASS_CLASSIC_SOLDIER) not in allowed
    assert int(C.CLASS_UGCBUILDER) not in allowed
