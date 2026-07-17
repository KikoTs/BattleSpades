"""Dump retail ``GameClient.start_processing_map`` UGC branches read-only.

Usage::

    idat.exe -A -S"tools/ida_network_ugc_map_dump.py" network.pyd

The generated Cython method is located both by symbol and by its embedded
traceback/name strings.  Related UGC/map-transfer strings are included as
evidence so the server implementation can be tied to native control flow.
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
    "start_processing_map",
    "ugc_data",
    "local ugc data",
    "baseplate",
    "MapDataValidation",
    "MapDataStart",
    "UGCMessage",
    "UGCMapLoadingFromHost",
    "MAP_IS_UGC_HOST",
    "MAP_IS_UGC_CLIENT",
    "map_is_ugc",
    "request_vxl",
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
        evidence.append({
            "address": hex(int(item.ea)),
            "value": value,
            "function_xrefs": sorted(set(xrefs)),
        })

    functions: list[dict[str, object]] = []
    for address in sorted(candidates):
        function = ida_funcs.get_func(address)
        try:
            pseudocode = str(ida_hexrays.decompile(address))
        except Exception as exc:
            pseudocode = f"<decompile failed: {exc}>"
        functions.append({
            "address": hex(address),
            "end": hex(int(function.end_ea)) if function else None,
            "name": ida_name.get_name(address),
            "pseudocode": pseudocode,
        })

    print("NETWORK_UGC_MAP_DUMP_BEGIN")
    print(json.dumps({"evidence": evidence, "functions": functions}, indent=2))
    print("NETWORK_UGC_MAP_DUMP_END")


try:
    main()
finally:
    ida_pro.qexit(0)
