"""Colours and fonts: an olive field-kit palette, drawn by us.

No artwork, images or fonts from the original game are used. Headings use
Barlow Condensed (SIL Open Font License 1.1, bundled with its licence in
``server_gui/fonts``); body text uses the platform's UI font.
"""

from __future__ import annotations

import sys
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "fonts"
HEADING_FAMILY = "Barlow Condensed"

# Palette ------------------------------------------------------------------
BG = "#16180f"            # window
PANEL = "#20231a"         # cards
PANEL_ALT = "#2a2e22"     # inputs / raised
BORDER = "#3b412f"
OLIVE = "#5f7a2e"         # primary action
OLIVE_HOVER = "#6f8d37"
OLIVE_BRIGHT = "#9db35a"  # accents, selected tab
KHAKI = "#e4dcc0"         # headings
TEXT = "#d9d7cb"
MUTED = "#9a9a86"
FAINT = "#6d6f5f"
DANGER = "#a3402c"
DANGER_HOVER = "#b84b34"
WARN = "#d9a032"
OK = "#86b84f"
INFO = "#7fa7c9"

LEVEL_COLOURS = {
    "DEBUG": FAINT,
    "INFO": TEXT,
    "OUTPUT": MUTED,
    "WARNING": WARN,
    "ERROR": "#e06a50",
    "CRITICAL": "#ff7b5c",
    "COMMAND": OLIVE_BRIGHT,
}
FINDING_COLOURS = {"ok": OK, "warn": WARN, "bad": "#e06a50", "info": MUTED}
FINDING_MARKS = {"ok": "✔", "warn": "⚠", "bad": "✖", "info": "•"}


def body_family() -> str:
    if sys.platform == "win32":
        return "Segoe UI"
    if sys.platform == "darwin":
        return "Helvetica Neue"
    return "DejaVu Sans"


def mono_family() -> str:
    if sys.platform == "win32":
        return "Consolas"
    if sys.platform == "darwin":
        return "Menlo"
    return "DejaVu Sans Mono"


def register_fonts() -> bool:
    """Make the bundled heading font available to this process only."""

    files = sorted(FONT_DIR.glob("*.ttf"))
    if not files:
        return False
    try:
        if sys.platform == "win32":
            import ctypes

            gdi32 = ctypes.WinDLL("gdi32")
            fr_private = 0x10
            return all(gdi32.AddFontResourceExW(str(path), fr_private, 0) > 0 for path in files)
        if sys.platform == "darwin":
            return _register_macos(files)
        return _register_fontconfig()
    except Exception:
        return False


def _register_fontconfig() -> bool:
    import ctypes
    import ctypes.util

    name = ctypes.util.find_library("fontconfig") or "libfontconfig.so.1"
    library = ctypes.CDLL(name)
    library.FcConfigAppFontAddDir.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    library.FcConfigAppFontAddDir.restype = ctypes.c_int
    return bool(library.FcConfigAppFontAddDir(None, str(FONT_DIR).encode()))


def _register_macos(files) -> bool:
    import ctypes
    import ctypes.util

    core_text = ctypes.CDLL(ctypes.util.find_library("CoreText"))
    core_foundation = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    core_foundation.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
    core_foundation.CFURLCreateFromFileSystemRepresentation.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool,
    ]
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    core_text.CTFontManagerRegisterFontsForURL.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    core_text.CTFontManagerRegisterFontsForURL.restype = ctypes.c_bool
    scope_process = 1
    ok = True
    for path in files:
        raw = str(path).encode("utf-8")
        url = core_foundation.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False)
        if not url:
            ok = False
            continue
        ok = bool(core_text.CTFontManagerRegisterFontsForURL(url, scope_process, None)) and ok
        core_foundation.CFRelease(url)
    return ok
