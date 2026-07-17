"""Dump the retail UGC prefab-catalog loader read-only with IDA.

Usage::

    idat.exe -A -S"tools/ida_ugc_prefab_dump.py" \
        aoslib/scenes/main/gameScene.pyd.i64

The stock client loads UGC models and decides which of them appear in the
Construct Library through separate code paths.  This script records both so a
server change cannot confuse "model is in the palette" with "model is allowed
on this map" again.
"""

from __future__ import annotations

import json

import ida_auto
import ida_funcs
import ida_hexrays
import ida_name
import ida_pro
import idautils


PATTERNS = (
    "load_ugc_prefabs",
    "load_prefabs",
    "load_next_ugc_prefab",
    "map_prefabs",
    "ugc_prefab_sets",
    "prefabs_to_load",
    "process_packet_state_data",
    "process_packet_initial_info",
)


def _matches(value: str) -> bool:
    lowered = value.casefold()
    return any(pattern.casefold() in lowered for pattern in PATTERNS)


def main() -> None:
    ida_auto.auto_wait()
    candidates: set[int] = set()
    evidence: list[dict[str, object]] = []

    for address in idautils.Functions():
        name = ida_name.get_name(int(address))
        if _matches(name):
            candidates.add(int(address))

    for item in idautils.Strings():
        value = str(item)
        if not _matches(value):
            continue
        xrefs: list[str] = []
        for xref in idautils.XrefsTo(int(item.ea)):
            function = ida_funcs.get_func(int(xref.frm))
            if function is None:
                continue
            candidates.add(int(function.start_ea))
            xrefs.append(hex(int(function.start_ea)))
        evidence.append(
            {
                "address": hex(int(item.ea)),
                "value": value,
                "function_xrefs": sorted(set(xrefs)),
            }
        )

    functions: list[dict[str, object]] = []
    for address in sorted(candidates):
        function = ida_funcs.get_func(address)
        try:
            pseudocode = str(ida_hexrays.decompile(address))
        except Exception as exc:
            pseudocode = f"<decompile failed: {exc}>"
        functions.append(
            {
                "address": hex(address),
                "end": hex(int(function.end_ea)) if function else None,
                "name": ida_name.get_name(address),
                "pseudocode": pseudocode,
            }
        )

    print("UGC_PREFAB_DUMP_BEGIN")
    print(json.dumps({"evidence": evidence, "functions": functions}, indent=2))
    print("UGC_PREFAB_DUMP_END")


try:
    main()
finally:
    ida_pro.qexit(0)
