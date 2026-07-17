# -*- coding: utf-8 -*-
"""Add Ctrl+V support to the retail client's text edit controls.

The shipped Python 2 ``EditBoxControl`` handles navigation and typed text but
never reads the operating-system clipboard.  This hook uses the Unicode Win32
clipboard format and inserts text at the current caret.  It is deliberately
small and synchronous: clipboard access only occurs in direct response to the
user pressing Ctrl+V, never from the render or network update loop.
"""
from __future__ import absolute_import

import ctypes
import sys

from pyglet.window import key


CF_UNICODETEXT = 13
_installed = False
_original_on_key_press = None

try:
    _text_type = unicode
except NameError:  # Python 3 test harness.
    _text_type = str


def _read_clipboard_text():
    """Return Unicode clipboard text on Windows, or ``None`` on failure."""
    if sys.platform != 'win32':
        return None

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    handle = None
    pointer = None
    try:
        if not user32.OpenClipboard(None):
            return None
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        return ctypes.wstring_at(pointer)
    except Exception:
        # Clipboard ownership can change between OpenClipboard and the read.
        # Falling back to the stock key handler is safer than breaking input.
        return None
    finally:
        if pointer and handle:
            try:
                kernel32.GlobalUnlock(handle)
            except Exception:
                pass
        try:
            user32.CloseClipboard()
        except Exception:
            pass


def _normalise_paste(value):
    """Collapse clipboard line separators into safe single-line text."""
    if value is None:
        return u''
    try:
        value = _text_type(value)
    except Exception:
        return u''
    value = value.replace(u'\x00', u'').replace(u'\t', u' ')
    return u' '.join(value.splitlines()).strip()


def _insert_at_caret(control, value):
    """Insert a bounded paste using the control's native ``set`` method."""
    value = _normalise_paste(value)
    if not value:
        return False

    current = _text_type(getattr(control, 'text', u''))
    caret = max(0, min(int(getattr(control, 'caret_index', len(current))),
                       len(current)))
    limit = 200
    configured_limit = getattr(control, 'max_characters', None)
    if configured_limit:
        limit = min(limit, max(0, int(configured_limit)))
    available = max(0, limit - len(current))
    value = value[:available]
    if not value:
        return True

    new_text = current[:caret] + value + current[caret:]
    new_caret = caret + len(value)
    control.set(new_text, False, max_visible_index=new_caret)
    control.caret_index = min(new_caret, len(control.text))
    return True


def install():
    """Patch ``EditBoxControl`` once after the retail GUI is initialized."""
    global _installed, _original_on_key_press
    if _installed:
        return True

    try:
        from aoslib.scenes.gui.editBoxControl import EditBoxControl
    except Exception:
        return False

    original = EditBoxControl.on_key_press
    if getattr(original, '_battlespades_clipboard_patch', False):
        _installed = True
        return True

    paste_modifiers = int(getattr(key, 'MOD_CTRL', 0))
    paste_modifiers |= int(getattr(key, 'MOD_COMMAND', 0))

    def clipboard_on_key_press(control, button, modifiers):
        if (
            getattr(control, 'enabled', False)
            and getattr(control, 'focus', False)
            and button == key.V
            and paste_modifiers
            and modifiers & paste_modifiers
        ):
            value = _read_clipboard_text()
            if value is not None and _insert_at_caret(control, value):
                return True
        return original(control, button, modifiers)

    clipboard_on_key_press._battlespades_clipboard_patch = True
    _original_on_key_press = original
    EditBoxControl.on_key_press = clipboard_on_key_press
    _installed = True
    return True


install()
