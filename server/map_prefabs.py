"""Constructs a map adds to every class: ``StateData.prefabs``.

Retail mechanism (client and shared code):

* ``StateData.prefabs`` becomes ``prefab_manager.map_prefabs``
  (gameScene.pyd ``process_packet_state_data``).
* The class menu lists, for every class whose construct lists contain
  ``MAP_PREFABS`` (all but the zombies and the Classic Soldier), its own
  constructs without the names that are map prefabs, then every map prefab
  (selectClass.py ``get_class_images``).
* The retail server authorised a construct through the same lists
  (shared/prefabManager.py ``find_prefab``: ``PREFAB_LISTS[item]`` for the
  class's lists, ``MAP_PREFABS`` among them).

What a retail map listed is not recovered. ``PREFAB_LISTS[MAP_PREFABS]`` is
empty in the shipped constants, the four retail map sidecars that survive
carry no construct list, and no playlist does. The game ships six map-themed
constructs named after two maps (``prefab_london_*``, ``prefab_lunar*``)
without a display string for any of them. The defaults below are those six,
on the map each is named after; ``[lobby] map_prefabs`` replaces them.

The list goes to BattleSpades clients only. The stock client keeps the empty
list it has always received.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

import shared.constants as C

DEFAULT_MAP_PREFABS: dict[str, tuple[str, ...]] = {
    "London": ("prefab_london_taxi", "prefab_london_postbox", "prefab_london_duck"),
    "LunarBase": ("prefab_lunarcargo", "prefab_lunarshield", "prefab_lunarsteps"),
}
# A class shows at most three chosen constructs; a map offering more than a
# page of tiles helps nobody.
MAX_MAP_PREFABS = 8
_NAME = re.compile(r"[A-Za-z0-9_]{1,64}\Z")


def valid_name(name: object) -> bool:
    return isinstance(name, str) and _NAME.match(name) is not None


def normalize_table(table: Mapping[str, Iterable[str]]) -> dict[str, tuple[str, ...]]:
    """Validate a ``{map: [construct, ...]}`` table (config loader)."""
    normalized: dict[str, tuple[str, ...]] = {}
    for map_name, names in table.items():
        if isinstance(names, str) or not isinstance(names, (list, tuple)):
            raise ValueError(f"lobby.map_prefabs.{map_name} must be an array of names")
        chosen: list[str] = []
        for name in names:
            if not valid_name(name):
                raise ValueError(
                    f"lobby.map_prefabs.{map_name}: {name!r} is not a construct name"
                )
            if name.lower() not in (other.lower() for other in chosen):
                chosen.append(name)
        if len(chosen) > MAX_MAP_PREFABS:
            raise ValueError(
                f"lobby.map_prefabs.{map_name} lists more than {MAX_MAP_PREFABS} names"
            )
        normalized[str(map_name)] = tuple(chosen)
    return normalized


def class_offers_map_prefabs(class_id: int) -> bool:
    try:
        lists = C.CLASS_ITEMS[int(class_id)][int(C.CLASS_PREFABS)]
    except (KeyError, TypeError, ValueError):
        return False
    return int(C.MAP_PREFABS) in {int(item) for item in lists}


def map_prefabs(config, map_name: str | None = None) -> tuple[str, ...]:
    """The constructs the current map adds, in menu order."""
    if config is None or bool(getattr(config, "ugc_runtime", False)):
        return ()
    table = getattr(config, "map_prefabs", None)
    if not isinstance(table, Mapping):
        table = DEFAULT_MAP_PREFABS
    wanted = str(map_name if map_name is not None
                 else getattr(config, "default_map", "")).casefold()
    names = next(
        (tuple(value) for key, value in table.items() if str(key).casefold() == wanted),
        (),
    )
    if not names:
        return ()
    from server.prefabs import get_registry

    registry = get_registry()
    return tuple(
        name for name in names[:MAX_MAP_PREFABS]
        if valid_name(name) and registry.get(name) is not None
    )


def selected_map_prefabs(config, class_id: int, requested: Iterable[str]) -> tuple[str, ...]:
    """The requested names that are map constructs this class may carry."""
    if not class_offers_map_prefabs(class_id):
        return ()
    offered = {name.lower(): name for name in map_prefabs(config)}
    if not offered:
        return ()
    return tuple(
        offered[key]
        for key in dict.fromkeys(str(value).lower() for value in requested)
        if key in offered
    )
