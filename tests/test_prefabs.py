"""Prefab system tests.

Rotation helpers are pinned to the ground truth extracted from the game's own
compiled shared/common.pyd (2026-07-07):
  rotate_z_axis(1,2,3,n) -> (1,2,3) (2,-1,3) (-1,-2,3) (-2,1,3)
  rotate_x_axis(1,2,3,n) -> (1,2,3) (1,3,-2) (1,-2,-3) (1,-3,2)
  rotate_y_axis(1,2,3,n) -> (1,2,3) (-3,2,1) (-1,2,-3) (3,2,-1)
  blend_color((0,0,255),(100,200,50),0.5) -> (50,100,152)   (int truncation)
"""
import os
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from server import prefabs as P  # noqa: E402


# --- rotation ground truth ---------------------------------------------------

def test_rotate_z_axis_matches_client():
    assert [P.rotate_z_axis(1, 2, 3, n) for n in range(4)] == \
        [(1, 2, 3), (2, -1, 3), (-1, -2, 3), (-2, 1, 3)]


def test_rotate_x_axis_matches_client():
    assert [P.rotate_x_axis(1, 2, 3, n) for n in range(4)] == \
        [(1, 2, 3), (1, 3, -2), (1, -2, -3), (1, -3, 2)]


def test_rotate_y_axis_matches_client():
    assert [P.rotate_y_axis(1, 2, 3, n) for n in range(4)] == \
        [(1, 2, 3), (-3, 2, 1), (-1, 2, -3), (3, 2, -1)]


def test_blend_color_matches_client():
    assert P.blend_color((0, 0, 255), (100, 200, 50), 0.5) == (50, 100, 152)
    assert P.blend_color((255, 0, 0), (0, 0, 0), 0.5) == (127, 0, 0)


def test_rotate_point_order_roll_pitch_yaw():
    # rotate_point must equal z(x(y(p, roll), pitch), yaw) — original order.
    x, y, z = 1, 2, 3
    expect = P.rotate_z_axis(*P.rotate_x_axis(*P.rotate_y_axis(x, y, z, 1), 1), 1)
    assert P.rotate_point(x, y, z, yaw=1, pitch=1, roll=1) == expect


# --- registry + geometry ------------------------------------------------------

def _registry():
    return P.get_registry()


def test_registry_loads_class_prefabs():
    reg = _registry()
    model = reg.get("prefab_fort_wall")
    assert model is not None
    pts = model.get_points()
    assert len(pts) == 39           # true geometry (invscale=1), not display scale
    assert model.get_sizes() == (2, 9, 4)


def test_registry_unknown_prefab_returns_none():
    assert _registry().get("prefab_does_not_exist") is None


def test_all_class_prefab_lists_resolve_to_models():
    """Every prefab any class can select must load from the shipped assets."""
    reg = _registry()
    missing = []
    for class_id in range(len(getattr(C, "CLASS_NAMES", range(18)))):
        for name in P.allowed_prefabs_for_class(class_id):
            if reg.get(name) is None:
                missing.append((class_id, name))
    assert missing == [], f"unresolvable class prefabs: {missing}"


def test_expand_prefab_translates_and_blends():
    reg = _registry()
    model = reg.get("prefab_fort_wall")
    cells = P.expand_prefab(model, (100, 200, 50), 0, 0, 0, base_color=(0, 0, 255))
    assert len(cells) == 39
    (x, y, z), color = cells[0]
    assert x >= 100 and y >= 200 and z >= 50      # anchored, unrotated offsets >= 0
    # 50/50 blend of (0,0,255) with the model color (176,180,180) -> (88,90,217)
    assert color == (88, 90, 217)


def test_expand_prefab_rotation_changes_footprint():
    reg = _registry()
    model = reg.get("prefab_fort_wall")   # 2 x 9 x 4
    flat = P.expand_prefab(model, (0, 0, 0), 0, 0, 0)
    rot = P.expand_prefab(model, (0, 0, 0), 1, 0, 0)   # quarter-turn yaw
    xs = lambda cs: {c[0][0] for c in cs}
    ys = lambda cs: {c[0][1] for c in cs}
    # after a yaw quarter turn the 9-long axis moves from y onto x
    assert max(ys(flat)) - min(ys(flat)) == 8
    assert max(xs(rot)) - min(xs(rot)) == 8


# --- allow-list ---------------------------------------------------------------

def test_soldier_allowed_prefabs():
    allowed = P.allowed_prefabs_for_class(int(C.CLASS_SOLDIER))
    assert "prefab_fort_wall" in allowed
    assert "prefab_ultrabarrier" in allowed
    assert "prefab_supertower" not in allowed     # scout/engineer prefab


def test_prefab_allowed_honors_player_choice():
    player = SimpleNamespace(class_id=int(C.CLASS_SOLDIER), prefabs=["prefab_supertower"])
    assert P.prefab_allowed(player, "prefab_fort_wall")      # class list
    assert P.prefab_allowed(player, "prefab_supertower")     # chosen loadout
    assert not P.prefab_allowed(player, "prefab_zombiehand")


# --- placement rules ----------------------------------------------------------

class FakeWorld:
    def __init__(self, solids=()):
        self.solids = set(solids)

    def get_solid(self, x, y, z):
        return (x, y, z) in self.solids


def test_touches_world():
    cells = [((10, 10, 10), (0, 0, 0))]
    assert not P.touches_world(FakeWorld(), cells)
    assert P.touches_world(FakeWorld(solids={(10, 10, 11)}), cells)   # resting on ground


def test_collides_with_player():
    p = SimpleNamespace(alive=True, spawned=True, x=10.5, y=10.5, z=10.0)
    hit = [((10, 10, 11), (0, 0, 0))]
    miss = [((50, 50, 50), (0, 0, 0))]
    assert P.collides_with_player(hit, [p])
    assert not P.collides_with_player(miss, [p])
    dead = SimpleNamespace(alive=False, spawned=True, x=10.5, y=10.5, z=10.0)
    assert not P.collides_with_player(hit, [dead])
