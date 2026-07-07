"""Server-side prefab system — faithful port of the original game's
shared/prefabManager.py (readable in the client install; logic transcribed
1:1, helpers verified empirically against the compiled shared/common.pyd).

Flow (measured from the original):
  - Client sends BuildPrefabAction(30) with prefab NAME + anchor position +
    quarter-turn rotations (yaw/pitch/roll 0-3). The SERVER expands the
    prefab's KV6 model into blocks (the client sends no block list).
  - Each model voxel is rotated roll -> pitch -> yaw (exact quarter-turn
    matrices below), translated to the anchor, colored with a 50/50 blend of
    the team/packet color and the voxel's model color, and placed.
  - ErasePrefabAction(31) removes the same expanded cell set.

Rotation ground truth (extracted 2026-07-07 by calling the game's own
shared.common in its bundled Python):
  rotate_z_axis(x,y,z,1) == ( y, -x,  z)
  rotate_x_axis(x,y,z,1) == ( x,  z, -y)
  rotate_y_axis(x,y,z,1) == (-z,  y,  x)
  blend_color(a,b,f)      == int(a*f + b*(1-f)) per channel (truncated)

Prefab geometry lives in KV6 model files (kv6/prefab_*.kv6 in the client
install; the class-prefab set is mirrored in this repo's prefabs/ dir).
IMPORTANT: load with invscale=1 — the display default (3) shrinks the model.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import shared.constants as C

logger = logging.getLogger(__name__)

DEFAULT_PREFAB_HEALTH = float(getattr(C, "DEFAULT_PREFAB_HEALTH", 9))

# Directories searched for <name>.kv6, in order.
PREFAB_SEARCH_DIRS = (
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prefabs"),
    r"G:\AoSRevival\aceofspades_nonsteam\kv6",  # dev fallback: full client install
)


# ---------------------------------------------------------------------------
# Ground-truth helpers (verified against the compiled shared/common.pyd)
# ---------------------------------------------------------------------------

def rotate_z_axis(x, y, z, n):
    n &= 3
    for _ in range(n):
        x, y = y, -x
    return (x, y, z)


def rotate_x_axis(x, y, z, n):
    n &= 3
    for _ in range(n):
        y, z = z, -y
    return (x, y, z)


def rotate_y_axis(x, y, z, n):
    n &= 3
    for _ in range(n):
        x, z = -z, x
    return (x, y, z)


def rotate_point(x, y, z, yaw, pitch, roll):
    """Original rotation order: roll (Y) -> pitch (X) -> yaw (Z)."""
    p = rotate_y_axis(int(x), int(y), int(z), int(roll))
    p = rotate_x_axis(p[0], p[1], p[2], int(pitch))
    return rotate_z_axis(p[0], p[1], p[2], int(yaw))


def blend_color(a, b, factor=0.5):
    """int-truncated per-channel blend (a*f + b*(1-f)) — matches common.pyd."""
    inv = 1.0 - factor
    return (
        int(a[0] * factor + b[0] * inv),
        int(a[1] * factor + b[1] * inv),
        int(a[2] * factor + b[2] * inv),
    )


# ---------------------------------------------------------------------------
# Registry: prefab name -> lazily loaded KV6 model
# ---------------------------------------------------------------------------

class PrefabRegistry:
    def __init__(self, search_dirs=PREFAB_SEARCH_DIRS):
        self.search_dirs = tuple(search_dirs)
        self._models: dict[str, object] = {}
        self._missing: set[str] = set()

    def get(self, name: str):
        """Return the KV6 model for a prefab name (lazy load), or None."""
        key = str(name).lower()
        if key in self._models:
            return self._models[key]
        if key in self._missing:
            return None
        for base in self.search_dirs:
            path = os.path.join(base, key + ".kv6")
            if os.path.isfile(path):
                try:
                    from aoslib.kv6 import KV6
                    # invscale=1: true block geometry (display default 3 shrinks)
                    model = KV6(path, False, load_display=False, invscale=1)
                    self._models[key] = model
                    logger.info("Prefab loaded: %s (%d blocks) from %s",
                                key, len(model.get_points()), base)
                    return model
                except Exception:
                    logger.warning("Prefab load FAILED: %s", path, exc_info=True)
                    break
        self._missing.add(key)
        logger.warning("Prefab model not found: %s (searched %s)", key, self.search_dirs)
        return None


_registry: Optional[PrefabRegistry] = None


def get_registry() -> PrefabRegistry:
    global _registry
    if _registry is None:
        _registry = PrefabRegistry()
    return _registry


# ---------------------------------------------------------------------------
# Class allow-list (original find_prefab: prefab must be in the player's
# class prefab lists)
# ---------------------------------------------------------------------------

def allowed_prefabs_for_class(class_id: int) -> set[str]:
    names: set[str] = set()
    try:
        class_items = C.CLASS_ITEMS[int(class_id)]
        for list_index in class_items[int(C.CLASS_PREFABS)]:
            for prefab in C.PREFAB_LISTS.get(int(list_index), []):
                names.add(str(prefab).lower())
    except Exception:
        pass
    return names


def prefab_allowed(player, name: str) -> bool:
    key = str(name).lower()
    if key in allowed_prefabs_for_class(getattr(player, "class_id", 0)):
        return True
    # The player's chosen loadout prefabs (SetClassLoadout) also count.
    chosen = getattr(player, "prefabs", None) or []
    return key in {str(p).lower() for p in chosen}


# ---------------------------------------------------------------------------
# Expansion (the core of the original build_prefab)
# ---------------------------------------------------------------------------

def expand_prefab(model, position, yaw, pitch, roll, base_color=None):
    """Rotate + translate every model voxel; returns list of
    ((x, y, z), (r, g, b)) world cells. base_color=None keeps raw model
    colors; otherwise each block is a 50/50 blend (original behavior)."""
    px, py, pz = int(position[0]), int(position[1]), int(position[2])
    out = []
    for x, y, z, r, g, b in model.get_points():
        wx, wy, wz = rotate_point(x, y, z, yaw, pitch, roll)
        if base_color is not None:
            color = blend_color(base_color, (int(r), int(g), int(b)), 0.5)
        else:
            color = (int(r), int(g), int(b))
        out.append(((wx + px, wy + py, wz + pz), color))
    return out


def touches_world(world_manager, cells) -> bool:
    """Original placement rule: the prefab must touch the existing world —
    any cell adjacent to (or resting on) a solid block qualifies."""
    for (x, y, z), _color in cells:
        for nx, ny, nz in ((x + 1, y, z), (x - 1, y, z), (x, y + 1, z),
                           (x, y - 1, z), (x, y, z + 1), (x, y, z - 1)):
            try:
                if world_manager.get_solid(int(nx), int(ny), int(nz)):
                    return True
            except Exception:
                continue
    return False


def collides_with_player(cells, players) -> bool:
    """Reject placement that would entomb a player (original
    prefab_collide_with_player, simplified to the player's occupied cells)."""
    for p in players:
        if not getattr(p, "alive", False) or not getattr(p, "spawned", False):
            continue
        pxl, pyl = int(p.x), int(p.y)
        # z grows downward; the body spans roughly [z, z+2.5] blocks from eye.
        occupied = {(pxl + dx, pyl + dy, int(p.z) + dz)
                    for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (0, 1, 2)}
        for (x, y, z), _c in cells:
            if (x, y, z) in occupied:
                return True
    return False
