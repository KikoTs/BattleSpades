"""Dump native Map Creator handlers from the retail GameScene IDB.

Run with IDA's console executable::

    idat.exe -A -S"tools/ida_ugc_handler_dump.py" gameScene.pyd.i64

The script is intentionally read-only.  It locates both named Cython methods
and functions referencing their generated method-name strings, decompiles the
results, prints JSON, and exits without saving the database.
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
    "InitialUGCBatch",
    "ReqestUGCEntities",
    "PlaceUGC",
    "UGCMapInfo",
    "UGCMapLoadingFromHost",
    "UGCMessage",
    "UGCObjectives",
    "SetUGCEditMode",
    "ugc_objectives",
    "ugc_entities",
    "ugc_mode",
    "process_packet_place_ugc",
    "process_packet_initial_ugc_batch",
    "process_packet_request_ugc_entities",
    "process_packet_ugc_message",
    "process_packet_ugc_map_loading_from_host",
    "process_packet_ugc_map_info",
    "process_packet_ugc_objectives",
    "process_packet_set_ugc_edit_mode",
    "send_place_ugc",
    "send_ugc_edit_mode",
    "send_build_prefab",
    "send_erase_prefab",
    "process_packet_build_prefab",
    "process_packet_erase_prefab",
    "process_packet_build_prefab_action",
    "process_packet_erase_prefab_action",
    "process_packet_prefab_complete",
    "prefab_action",
    "building_prefab",
    "PrefabComplete",
    "request_ugc",
    "is_ugc_host",
    "is_in_ugc_mode",
    "convert_ugc",
    "save_ugc_file",
    "save_vxl_file",
    "save_png_file",
    "set_water_color",
    "get_water_color",
    "set_skybox_name",
    "get_skybox_name",
    "process_packet_skybox_data",
    "process_packet_set_ground_colors",
    "send_ugc_message",
    "request_ugc_entities",
)


def _matches(value: str) -> bool:
    lowered = value.casefold()
    return any(pattern in lowered for pattern in PATTERNS)


def main() -> None:
    """Print matching native functions and their pseudocode, then exit."""

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
            if function is not None:
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

    print("UGC_HANDLER_DUMP_BEGIN")
    print(json.dumps({"evidence": evidence, "functions": functions}, indent=2))
    print("UGC_HANDLER_DUMP_END")


try:
    main()
finally:
    ida_pro.qexit(0)
