"""Print tutorial-related strings, xrefs, and functions from an IDA database.

Run with IDA's console executable, for example::

    idat.exe -A -S"tools/ida_tutorial_inventory.py" gameScene.pyd.i64

The script is intentionally read-only and exits without saving the database.
Its output is suitable for preserving primary reverse-engineering evidence in
an ordinary text log.
"""

from __future__ import annotations

import json

import ida_auto
import ida_funcs
import ida_name
import ida_pro
import idautils


PATTERNS = ("tutorial", "training", "boot camp")


def _matches(value: str) -> bool:
    lowered = value.casefold()
    return any(pattern in lowered for pattern in PATTERNS)


def main() -> None:
    """Wait for analysis, emit matching evidence, and close without saving."""

    ida_auto.auto_wait()
    rows: list[dict[str, object]] = []
    candidate_functions: set[int] = set()

    for item in idautils.Strings():
        value = str(item)
        if not _matches(value):
            continue
        xrefs: list[dict[str, object]] = []
        for xref in idautils.XrefsTo(int(item.ea)):
            function = ida_funcs.get_func(int(xref.frm))
            function_start = int(function.start_ea) if function else None
            if function_start is not None:
                candidate_functions.add(function_start)
            xrefs.append(
                {
                    "from": hex(int(xref.frm)),
                    "function": (
                        ida_name.get_name(function_start)
                        if function_start is not None
                        else None
                    ),
                    "function_start": (
                        hex(function_start) if function_start is not None else None
                    ),
                }
            )
        rows.append(
            {
                "kind": "string",
                "address": hex(int(item.ea)),
                "value": value,
                "xrefs": xrefs,
            }
        )

    for function_start in idautils.Functions():
        name = ida_name.get_name(int(function_start))
        if _matches(name):
            candidate_functions.add(int(function_start))

    functions: list[dict[str, object]] = []
    for function_start in sorted(candidate_functions):
        function = ida_funcs.get_func(function_start)
        functions.append(
            {
                "kind": "function",
                "address": hex(function_start),
                "end": hex(int(function.end_ea)) if function else None,
                "size": (
                    int(function.end_ea - function.start_ea) if function else None
                ),
                "name": ida_name.get_name(function_start),
            }
        )

    print("TUTORIAL_INVENTORY_BEGIN")
    print(json.dumps({"strings": rows, "functions": functions}, indent=2))
    print("TUTORIAL_INVENTORY_END")


try:
    main()
finally:
    # qexit does not invoke save_database; the inspected retail IDB stays
    # byte-for-byte untouched.
    ida_pro.qexit(0)
