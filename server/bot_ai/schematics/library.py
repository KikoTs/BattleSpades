"""The authored bot schematic catalogue plus small parametric generators.

Layers are listed bottom first; rows FRONT (toward the threat) to BACK. See
``model.py`` for the legend. Every entry is validated by
``tests/test_bot_schematics.py`` against the server's build rules in all
four rotations: face support in build order, ground connection, keep-clear
cells, door/occupant escape and block-line decomposition.
"""

from __future__ import annotations

from functools import lru_cache

from .model import Schematic


def _rows(*rows: str) -> tuple[str, ...]:
    return tuple(rows)


def make_stair(height: int, width: int = 1, *, name: str = "", color: str = "stone") -> Schematic:
    """A straight stair climbing ``height`` layers forward (toward ``f`` > 0).

    Step ``k`` (``f = k``) is a solid column ``h = 0..k``; three layers of
    headroom above each tread stay clear so a player can walk it. Used for
    library stairs and by strategy code that needs to reach a ledge.
    """

    if not 1 <= height <= 12 or not 1 <= width <= 3:
        raise ValueError("stair dimensions out of range")
    total = height + 3
    rows_by_layer: list[list[str]] = []
    for h in range(total):
        rows = []
        for k in range(height - 1, -1, -1):  # front row first (tallest)
            if h <= k:
                char = "#"
            elif h <= k + 3:
                char = "_"
            else:
                char = "."
            rows.append(char * width)
        rows_by_layer.append(rows)
    layers = tuple(tuple(rows) for rows in rows_by_layer)
    return Schematic(
        name or f"stairs_{height}x{width}", layers, anchor=(width // 2, height - 1),
        palette={"#": color}, tags=("traversal", "climb", "stairs", "zombie"),
        purpose="traversal", description=f"{height}-step straight stair",
        max_builders=2,
    )


def make_bridge(length: int, *, name: str = "", color: str = "wood") -> Schematic:
    """A one-wide deck extending ``length`` cells forward at walking level.

    The anchor is the last solid bank column; deck cells replace the missing
    terrain top (``h = -1``), so the walking surface stays level.
    """

    if not 1 <= length <= 10:
        raise ValueError("bridge length out of range")
    rows = tuple("#" for _ in range(length)) + (".",)
    clear = tuple("_" for _ in range(length)) + (".",)
    return Schematic(
        name or f"bridge_{length}", (rows, clear, clear, clear), anchor=(0, length),
        palette={"#": color}, tags=("traversal", "bridge"), purpose="traversal",
        description=f"{length}-cell walking deck", ground_mode="deck", base_level=-1,
        max_builders=1,
    )


def make_ring(radius: int, height: int = 2, *, name: str = "", color: str = "sand",
              gaps: bool = True) -> Schematic:
    """A square defensive ring of ``radius`` around the anchor.

    Two one-wide entrances (left and right side middles) keep the ring from
    trapping defenders and keep the server's sole-exit rule satisfied.
    """

    if not 2 <= radius <= 8 or not 1 <= height <= 3:
        raise ValueError("ring dimensions out of range")
    size = radius * 2 + 1
    rows = []
    for row in range(size):
        if row in (0, size - 1):
            rows.append("#" * size)
        elif gaps and row == radius:
            rows.append("_" + "." * (size - 2) + "_")
        else:
            rows.append("#" + "." * (size - 2) + "#")
    layers = tuple(tuple(rows) for _ in range(height))
    return Schematic(
        name or f"ring_{radius}", layers, anchor=(radius, radius), palette={"#": color},
        tags=("objective", "defend", "ring", "barrier"), purpose="objective",
        description=f"defensive ring radius {radius}", max_builders=4,
    )


_HUT_PALETTE = {"#": "wood", "=": "dark", "+": "brick"}
_CONCRETE = {"#": "concrete", "=": "dark", "+": "olive"}


def _catalogue() -> tuple[Schematic, ...]:
    sandbag = Schematic(
        "sandbag_wall",
        (_rows("#####"), _rows("#####")),
        anchor=(2, 0), palette={"#": "sand"},
        tags=("cover", "barrier", "any"), purpose="cover",
        description="waist-high 5-wide sandbag line", max_builders=2,
    )
    cover_wall = Schematic(
        "cover_wall",
        (_rows("#######"), _rows("#_###_#"), _rows("#######")),
        anchor=(3, 0), palette={"#": "concrete"},
        tags=("cover", "barrier", "any"), purpose="cover",
        description="head-high 7-wide wall with two firing slits", max_builders=2,
    )
    corner = Schematic(
        "corner_cover",
        (_rows("#####", "#....", "#...."), _rows("#####", "#....", "#....")),
        anchor=(2, 1), palette={"#": "sand"},
        tags=("cover", "barrier", "any"), purpose="cover",
        description="L-shaped waist-high cover", max_builders=2,
    )
    hut = Schematic(
        "small_hut",
        (
            _rows("#####", "#___#", "#___#", "#___#", "##_##", ".._.."),
            _rows("##_##", "#___#", "_____", "#___#", "##_##", ".._.."),
            _rows("#####", "#___#", "#___#", "#___#", "##_##", ".._.."),
            _rows("=====", "=====", "=====", "=====", "=====", "....."),
        ),
        anchor=(2, 2), palette=_HUT_PALETTE,
        tags=("shelter", "house", "any"), purpose="shelter",
        description="5x5 hut: back door, three windows, flat roof", occupant=(0, 0, 0),
        max_builders=3,
    )
    bunker = Schematic(
        "bunker",
        (
            _rows("#######", "#_____#", "#_____#", "#_____#", "###_###", "..._..."),
            _rows("#_____#", "#_____#", "#_____#", "#_____#", "###_###", "..._..."),
            _rows("#######", "#_____#", "#_____#", "#_____#", "###_###", "..._..."),
            _rows("=======", "=======", "=======", "=======", "======="),
        ),
        anchor=(3, 2), palette=_CONCRETE,
        tags=("shelter", "bunker", "defend", "any"), purpose="defend",
        description="7x5 bunker: wide front firing slit, back door, roof", occupant=(0, 0, 0),
        max_builders=3,
    )
    pillbox = Schematic(
        "pillbox",
        (
            _rows("#####", "#___#", "#___#", "#___#", "##_##", ".._.."),
            _rows("#___#", "_____", "#___#", "#___#", "##_##", ".._.."),
            _rows("#####", "#___#", "#___#", "#___#", "##_##", ".._.."),
            _rows("=====", "=====", "=====", "=====", "====="),
        ),
        anchor=(2, 2), palette=_CONCRETE,
        tags=("shelter", "pillbox", "defend", "any"), purpose="defend",
        description="5x5 pillbox: front and side slits, back door", occupant=(0, 0, 0),
        max_builders=3,
    )
    vip = Schematic(
        "vip_shelter",
        (
            _rows("#####", "#___#", "#___#", "#___#", "##_##", ".._.."),
            _rows("##_##", "#___#", "_____", "#___#", "##_##", ".._.."),
            _rows("+++++", "+___+", "+___+", "+___+", "++_++", ".._.."),
            _rows("=====", "=====", "=====", "=====", "====="),
        ),
        anchor=(2, 2), palette={"#": "concrete", "+": "gold", "=": "dark"},
        tags=("vip", "shelter", "defend"), purpose="vip_shelter",
        description="VIP box around its occupant: slits, back door, roof",
        occupant=(0, 0, 0), max_builders=4,
    )
    watchtower = Schematic(
        "watchtower",
        (
            _rows("#.#", "...", "#.#", ".#.", ".#.", ".#.", ".#."),
            _rows("#.#", "...", "#.#", ".#.", ".#.", ".#.", "._."),
            _rows("#.#", "...", "#.#", ".#.", ".#.", "._.", "._."),
            _rows("#.#", "...", "#.#", ".#.", "._.", "._.", "._."),
            _rows("###", "###", "###", "._.", "._.", "._.", "..."),
            _rows("###", "#_#", "___", "._.", "._.", "...", "..."),
            _rows("...", "___", "___", "._.", "...", "...", "..."),
            _rows("...", "___", "___", "...", "...", "...", "..."),
        ),
        anchor=(1, 1), palette={"#": "wood"},
        tags=("overwatch", "tower", "climb", "any"), purpose="overwatch",
        description="4-high leg tower, 3x3 platform, parapet and back stair",
        occupant=(0, 0, 5), max_builders=2,
    )
    nest = Schematic(
        "sniper_nest",
        (
            _rows("###", "###", "###", ".#."),
            _rows("###", "###", "###", "._."),
            _rows("###", "#_#", "___", "._."),
            _rows("...", "___", "___", "._."),
            _rows("...", "___", "___", "..."),
        ),
        anchor=(1, 1), palette={"#": "olive"},
        tags=("sniper", "overwatch", "any"), purpose="overwatch",
        description="2-high raised firing step with parapet and back step",
        occupant=(0, 0, 2), max_builders=2,
    )
    return (
        sandbag, cover_wall, corner, hut, bunker, pillbox, vip, watchtower, nest,
        make_stair(4, 2, name="stairs"),
        make_ring(4, name="objective_ring"),
        make_ring(7, name="base_ring", color="concrete"),
        make_bridge(4, name="bridge_4"),
        make_bridge(6, name="bridge_6"),
    )


@lru_cache(maxsize=1)
def library() -> dict[str, Schematic]:
    """Name -> schematic for the whole catalogue (built once per process)."""

    return {schematic.name: schematic for schematic in _catalogue()}


def get(name: str) -> Schematic | None:
    return library().get(str(name).lower())


def by_tag(tag: str) -> tuple[Schematic, ...]:
    return tuple(s for s in library().values() if tag in s.tags)
