"""Atomic class/loadout selection and deployable authorization.

The retail client sends class identity and its loadout in two independent
packets.  Keeping those fields independently mutable lets a new class inherit
the previous class's equipment (for example Miner plus Medic medpack).  This
module turns the wire fragments into one immutable, validated value that can
be committed only at a life boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

import shared.constants as C


# The native SelectClass menu injects FLAREBLOCK_TOOL as a fake first entry on
# the prefab page unless InitialInfo disables it.  Keep this shared with the
# spawn normalizer so the hidden tile cannot reappear in CreatePlayer.loadout.
DEFAULT_DISABLED_TOOLS: tuple[int, ...] = (int(C.FLAREBLOCK_TOOL),)


@dataclass(frozen=True)
class ClassSelection:
    """A complete class choice ready to be applied as one transaction."""

    class_id: int
    loadout: tuple[int, ...]
    prefabs: tuple[str, ...] = ()
    ugc_tools: tuple[int, ...] = ()


class _DeployablePlayer(Protocol):
    alive: bool
    spawned: bool
    tool: int
    class_id: int
    loadout: list[int]


_DEPLOYABLE_CLASSES: dict[int, frozenset[int]] = {
    int(C.DYNAMITE_TOOL): frozenset((int(C.CLASS_MINER),)),
    int(C.C4_TOOL): frozenset((int(C.CLASS_MINER),)),
    int(C.BLOCK_SUCKER_TOOL): frozenset((int(C.CLASS_MINER),)),
    int(C.LANDMINE_TOOL): frozenset((int(C.CLASS_SCOUT),)),
    int(C.RADAR_STATION_TOOL): frozenset((int(C.CLASS_SCOUT),)),
    int(C.MEDPACK_TOOL): frozenset((int(C.CLASS_MEDIC),)),
    int(C.ROCKET_TURRET_TOOL): frozenset(
        (int(C.CLASS_ENGINEER), int(C.CLASS_ROCKETEER))
    ),
    int(C.DISGUISE_TOOL): frozenset((int(C.CLASS_ENGINEER),)),
    # The mounted MG is carried by mgWeapon but is absent from the recovered
    # default CLASS_ITEMS table.  Stock mode ownership is Soldier; a mode may
    # still explicitly grant MG_TOOL by putting it in the active loadout.
    int(C.MG_TOOL): frozenset((int(C.CLASS_SOLDIER),)),
}


def _unique_ints(values: Iterable[int]) -> tuple[int, ...]:
    """Return integer values in input order with duplicates removed."""

    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        item = int(value)
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _normalize_class_id(requested: int, fallback: int | None) -> int:
    candidate = int(requested)
    if candidate in C.CLASS_ITEMS:
        return candidate
    if fallback is not None and int(fallback) in C.CLASS_ITEMS:
        return int(fallback)
    return int(C.DEFAULT_CLASS)


def _allowed_prefabs(class_id: int) -> set[str]:
    allowed: set[str] = set()
    class_items = C.CLASS_ITEMS[class_id]
    for list_id in class_items.get(int(C.CLASS_PREFABS), ()):
        allowed.update(str(name).lower() for name in C.PREFAB_LISTS.get(int(list_id), ()))
    return allowed


def _allowed_ugc_tools(class_id: int) -> set[int]:
    allowed: set[int] = set()
    class_items = C.CLASS_ITEMS[class_id]
    for list_id in class_items.get(int(C.CLASS_UGC_TOOLS), ()):
        # The recovered UGC Builder catalog uses ``range(0, 1)`` itself as a
        # dictionary key. Coercing it to int crashes an otherwise valid class
        # selection; preserve the hashable wire-table key exactly.
        allowed.update(int(item) for item in C.UGCTOOL_LISTS.get(list_id, ()))
    return allowed


def normalize_class_selection(
    class_id: int,
    loadout: Iterable[int] = (),
    prefabs: Iterable[str] = (),
    ugc_tools: Iterable[int] = (),
    *,
    fallback_class_id: int | None = None,
    disabled_tools: Iterable[int] = DEFAULT_DISABLED_TOOLS,
) -> ClassSelection:
    """Validate untrusted client selection data and fill required slots.

    Exactly one enabled tool is retained for every selectable class slot.  A
    missing or cross-class choice is replaced by that slot's first enabled
    default. Common tools are then included exactly once, preventing both
    empty-loadout spawns and inherited equipment. Jetpacks remain ordinary
    equipment-slot choices; adding a second class-default pack here would make
    Engineer's mutually exclusive Disguise selection carry both states.
    """

    normalized_class = _normalize_class_id(class_id, fallback_class_id)
    class_items = C.CLASS_ITEMS[normalized_class]
    disabled = {int(tool) for tool in disabled_tools}
    requested = _unique_ints(loadout)
    requested_set = set(requested)
    chosen: list[int] = []

    for slot in range(int(C.CLASS_NOOF_SELECTABLE_ITEMS)):
        if slot == int(C.CLASS_PREFABS):
            continue
        options = [
            int(tool) for tool in class_items.get(slot, ())
            if int(tool) not in disabled
        ]
        if not options:
            continue
        selected = next((tool for tool in options if tool in requested_set), options[0])
        chosen.append(selected)

    common = [
        int(tool) for tool in class_items.get(int(C.CLASS_COMMON), ())
        if int(tool) not in disabled
    ]
    # Retail places BLOCK_TOOL first.  Preserve that stable wire ordering so
    # the native client's initial selected tool does not move between spawns.
    block_tool = int(C.BLOCK_TOOL)
    flare_tool = int(C.FLAREBLOCK_TOOL)
    prefab_tool = int(C.PREFAB_TOOL)
    if block_tool in common:
        chosen.insert(0, block_tool)
    # GameClass.set_common_loadout_items skips flare here. build_class_loadout
    # appends it after pickups/common tools, which keeps the near-identical
    # normal and glowing block tools in their stock carousel positions.
    chosen.extend(
        tool for tool in common
        if tool not in (block_tool, flare_tool)
    )

    # Retail adds these two non-mafia tools after the class/common pass. The
    # final ordered de-duplication keeps PREFAB in its earlier common slot when
    # present while still restoring both tools for Classic Soldier.
    if flare_tool not in disabled:
        chosen.append(flare_tool)
    if prefab_tool not in disabled:
        chosen.append(prefab_tool)

    allowed_prefabs = _allowed_prefabs(normalized_class)
    normalized_prefabs = tuple(
        name for name in dict.fromkeys(str(value) for value in prefabs)
        if name.lower() in allowed_prefabs
    )
    allowed_ugc = _allowed_ugc_tools(normalized_class)
    normalized_ugc = tuple(
        tool for tool in _unique_ints(ugc_tools) if tool in allowed_ugc
    )

    return ClassSelection(
        class_id=normalized_class,
        loadout=_unique_ints(chosen),
        prefabs=normalized_prefabs,
        ugc_tools=normalized_ugc,
    )


def active_tool_authorized(player: _DeployablePlayer, tool_id: int) -> bool:
    """Return whether an alive player may act with ``tool_id`` right now.

    The tool byte in a client packet is untrusted.  It must agree with both
    the held tool replicated by ChangeTool and the normalized loadout committed
    for the current life.  This common gate is used by deployables and oriented
    projectiles so a delayed or forged packet cannot act as another class.
    """

    tool_id = int(tool_id)
    return (
        bool(getattr(player, "alive", False))
        and bool(getattr(player, "spawned", False))
        and int(getattr(player, "tool", -1)) == tool_id
        and tool_id in {int(tool) for tool in (getattr(player, "loadout", ()) or ())}
    )


def deployable_authorized(player: _DeployablePlayer, tool_id: int) -> bool:
    """Return whether the active life may perform a deployable action.

    Packet identity is not sufficient authorization: the held tool, committed
    loadout, and committed class must all agree.  This invariant prevents a
    delayed Medic packet from creating a health pack after switching to Miner.
    """

    tool_id = int(tool_id)
    permitted_classes = _DEPLOYABLE_CLASSES.get(tool_id)
    if permitted_classes is None:
        return False
    return active_tool_authorized(player, tool_id) and (
        int(getattr(player, "class_id", -1)) in permitted_classes
    )
