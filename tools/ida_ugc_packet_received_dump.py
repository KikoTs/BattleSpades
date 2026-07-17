"""Read-only dump of the retail GameScene packet dispatcher.

This complements ``ida_ugc_handler_dump.py``: several Map Creator packets are
handled directly inside the giant Cython ``packet_received`` body and therefore
have no standalone method-name string to find.
"""

from __future__ import annotations

import json

import ida_auto
import ida_funcs
import ida_hexrays
import ida_name
import ida_pro
import idautils


def main() -> None:
    ida_auto.auto_wait()
    candidates: set[int] = set()
    evidence: list[dict[str, object]] = []
    for item in idautils.Strings():
        value = str(item)
        if "GameScene.packet_received" not in value:
            continue
        xrefs: list[str] = []
        for xref in idautils.XrefsTo(int(item.ea)):
            function = ida_funcs.get_func(int(xref.frm))
            if function is not None:
                candidates.add(int(function.start_ea))
                xrefs.append(hex(int(function.start_ea)))
        evidence.append({
            "address": hex(int(item.ea)),
            "value": value,
            "function_xrefs": sorted(set(xrefs)),
        })

    functions = []
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

    print("UGC_PACKET_DISPATCH_BEGIN")
    print(json.dumps({"evidence": evidence, "functions": functions}, indent=2))
    print("UGC_PACKET_DISPATCH_END")


try:
    main()
finally:
    ida_pro.qexit(0)
