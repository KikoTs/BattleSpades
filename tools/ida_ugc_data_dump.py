"""Dump the retail ``aoslib.ugc_data`` baseplate/VXL paths read-only.

Usage::

    idat.exe -A -S"tools/ida_ugc_data_dump.py" aoslib/ugc_data.pyd

The editor's terrain transfer contract is split between ``network.pyd`` and
this module.  Keeping the extraction script in-tree makes each wire invariant
repeatable against an untouched retail binary.
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
    "check_baseplate",
    "baseplate",
    "get_ugc_data",
    "get_map_baseplate",
    "ugc_data.__init__",
    "load_vxl",
    "save_vxl",
    "local_vxl",
    "vxl_data",
    "hosted_ugc",
    "subscribed_ugc",
    "ugc/maps",
    "does not exist",
)


def _matches(value: str) -> bool:
    lowered = value.casefold()
    return any(pattern.casefold() in lowered for pattern in PATTERNS)


def main() -> None:
    ida_auto.auto_wait()
    candidates: set[int] = set()
    evidence: list[dict[str, object]] = []

    for address in idautils.Functions():
        if _matches(ida_name.get_name(int(address))):
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

    print("UGC_DATA_DUMP_BEGIN")
    print(json.dumps({"evidence": evidence, "functions": functions}, indent=2))
    print("UGC_DATA_DUMP_END")


try:
    main()
finally:
    ida_pro.qexit(0)
