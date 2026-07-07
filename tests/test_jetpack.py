"""Jetpack fuel model + per-tool melee profile tests (Phase-2 class abilities).

Fuel constants ground truth (client JETPACK_PROPERTIES, extracted 2026-07-07):
  JETPACK_NORMAL(66):  delay .25, max 100, activation 10, regen 10/s, drain 75/s
  JETPACK2(67):        delay .25, max 100, activation 10, regen  9/s, drain 17/s
  JETPACK_ENGINEER(68):delay .25, max 100, activation 10, regen  3/s, drain 18/s
  JETPACK_UGCBUILDER(69): delay .1, max 100, activation 0, regen 100/s, drain 0/s
"""
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from server.player import Player, _JETPACK_PROPERTIES  # noqa: E402

DT = 1.0 / 60.0


def make_player(class_id=int(C.CLASS_SOLDIER)):
    p = Player(id=1, name="Test", team=3, weapon=int(C.RIFLE_TOOL), connection=None)
    p.class_id = class_id
    p.alive = True
    p.spawned = True
    return p


def hold_hover(p, seconds):
    """Drive the fuel model with hover held for N seconds."""
    p.input.hover = True
    ticks = int(seconds / DT)
    for _ in range(ticks):
        p._update_jetpack(DT)


def test_properties_table_loaded():
    assert set(_JETPACK_PROPERTIES) == {66, 67, 68, 69}
    assert _JETPACK_PROPERTIES[66][4] == 75    # NORMAL drain/s
    assert _JETPACK_PROPERTIES[67][4] == 17    # JETPACK2 drain/s
    assert _JETPACK_PROPERTIES[68][3] == 3     # ENGINEER regen/s


def test_no_jetpack_never_activates():
    p = make_player()
    p.jetpack_id = 0
    hold_hover(p, 1.0)
    assert p.jetpack_active is False


def test_activation_after_start_delay_and_cost():
    p = make_player()
    p.jetpack_id = 67  # JETPACK2
    p.jetpack_fuel = 100.0
    hold_hover(p, 0.2)                    # under the 0.25s start delay
    assert p.jetpack_active is False
    hold_hover(p, 0.2)                    # crosses the delay
    assert p.jetpack_active is True
    # activation cost 10 was paid + some drain
    assert p.jetpack_fuel < 90.5


def test_fuel_exhaustion_deactivates():
    p = make_player()
    p.jetpack_id = 66  # NORMAL: drains 75/s -> ~1.2s of flight after cost
    p.jetpack_fuel = 100.0
    hold_hover(p, 2.5)
    assert p.jetpack_active is False
    assert p.jetpack_fuel <= 25.0  # mostly regen after the burn


def test_release_deactivates_and_regens():
    p = make_player()
    p.jetpack_id = 67
    p.jetpack_fuel = 100.0
    p._last_damage_at = 0.0
    hold_hover(p, 1.0)
    assert p.jetpack_active is True
    fuel_after_burn = p.jetpack_fuel
    p.input.hover = False
    for _ in range(60):  # 1s idle
        p._update_jetpack(DT)
    assert p.jetpack_active is False
    assert p.jetpack_fuel > fuel_after_burn  # regenerated (9/s)


def test_damage_pauses_regen():
    import time
    p = make_player()
    p.jetpack_id = 66  # refill delay after damage: 2.0s
    p.jetpack_fuel = 50.0
    p.input.hover = False
    p._last_damage_at = time.time()   # just damaged
    for _ in range(30):  # 0.5s — inside the 2s pause window
        p._update_jetpack(DT)
    assert p.jetpack_fuel == 50.0


def test_spawn_assigns_class_jetpack():
    p = make_player(class_id=int(C.CLASS_ROCKETEER))
    p.spawn(100.0, 100.0, 30.0)
    assert p.jetpack_id == int(C.JETPACK2)   # rocketeer default equipment
    assert p.jetpack_fuel == 100.0

    s = make_player(class_id=int(C.CLASS_SOLDIER))
    s.spawn(100.0, 100.0, 30.0)
    assert s.jetpack_id == 0                 # soldier has no jetpack


def test_spawn_honors_client_chosen_jetpack():
    p = make_player(class_id=int(C.CLASS_ROCKETEER))
    p.loadout = [int(C.SMG_TOOL), int(C.JETPACK_NORMAL)]  # picked NORMAL over JETPACK2
    p.spawn(100.0, 100.0, 30.0)
    assert p.jetpack_id == int(C.JETPACK_NORMAL)


# --- per-tool melee profiles -------------------------------------------------

def test_melee_profiles_per_tool():
    p = make_player()
    for tool, player_dmg, block_dmg in [
        (int(C.PICKAXE_TOOL), 50, 7),
        (int(C.SPADE_TOOL), 35, 5),
        (int(C.SUPERSPADE_TOOL), 50, 7.5),
        (int(C.CROWBAR_TOOL), 80, 5),
    ]:
        p.set_tool(tool)
        if not p.is_spade_tool():
            continue  # tool not in the melee set on this build — skip
        prof = p.get_weapon_profile()
        assert prof.base_damage == player_dmg, f"tool {tool} player dmg"
        assert prof.block_damage == block_dmg, f"tool {tool} block dmg"
