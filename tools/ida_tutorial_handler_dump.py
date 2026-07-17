"""Read-only IDA dump of native tutorial/audio/loadout packet handlers."""

from __future__ import annotations

import json

import ida_auto
import ida_funcs
import ida_hexrays
import ida_name
import ida_pro
import idautils


PATTERNS = (
    "process_packet_help_message",
    "process_packet_play_music",
    "process_packet_stop_music",
    "process_packet_change_class_loadout",
)


def main() -> None:
    """Print matching symbol metadata and pseudocode, then exit unsaved."""

    ida_auto.auto_wait()
    candidates: set[int] = set()
    for address in idautils.Functions():
        name = ida_name.get_name(address)
        if any(pattern in name for pattern in PATTERNS):
            candidates.add(int(address))
    for item in idautils.Strings():
        value = str(item)
        if not any(pattern in value for pattern in PATTERNS):
            continue
        for xref in idautils.XrefsTo(int(item.ea)):
            function = ida_funcs.get_func(int(xref.frm))
            if function is not None:
                candidates.add(int(function.start_ea))

    rows = []
    for address in sorted(candidates):
        name = ida_name.get_name(address)
        function = ida_funcs.get_func(address)
        try:
            pseudocode = str(ida_hexrays.decompile(address))
        except Exception as exc:
            pseudocode = f"<decompile failed: {exc}>"
        rows.append({
            "address": hex(int(address)),
            "end": hex(int(function.end_ea)) if function else None,
            "name": name,
            "pseudocode": pseudocode,
        })
    print("TUTORIAL_HANDLER_DUMP_BEGIN")
    print(json.dumps(rows, indent=2))
    print("TUTORIAL_HANDLER_DUMP_END")


try:
    main()
finally:
    ida_pro.qexit(0)
