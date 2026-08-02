# -*- coding: utf-8 -*-
"""Remove weapon tiles leaked into the retail prefab selection page.

The shipped ``SelectClass.get_class_images`` expands every prefab-list ID into
the intended prefab rows, then accidentally falls through and treats that same
numeric ID as a tool ID.  Small prefab-list IDs therefore display unrelated
weapon icons after each prefab group.  This wrapper keeps the stock expansion
and filters only those leaked numeric rows from the prefab page.
"""
from __future__ import absolute_import


_installed = False
_original_get_class_images = None

try:
    _string_types = (basestring,)
except NameError:  # Python 3 test harness.
    _string_types = (str,)


def _filter_prefab_rows(rows, flareblock_tool):
    """Keep named prefabs and the retail flare-block prefab pseudo-item."""
    filtered = []
    for row in rows:
        if not row:
            continue
        item_id = row[0]
        if isinstance(item_id, _string_types) or item_id == flareblock_tool:
            filtered.append(row)
    return filtered


def install():
    """Patch ``SelectClass.get_class_images`` once when the client starts."""
    global _installed, _original_get_class_images
    if _installed:
        return True

    try:
        from aoslib.scenes.ingame_menus.selectClass import SelectClass
        from shared.constants import CLASS_PREFABS, FLAREBLOCK_TOOL
    except Exception:
        return False

    original = SelectClass.get_class_images
    if getattr(original, '_battlespades_prefab_menu_patch', False):
        _installed = True
        return True

    def prefab_safe_get_class_images(menu, index):
        rows = original(menu, index)
        if index == CLASS_PREFABS:
            return _filter_prefab_rows(rows, FLAREBLOCK_TOOL)
        return rows

    prefab_safe_get_class_images._battlespades_prefab_menu_patch = True
    _original_get_class_images = original
    SelectClass.get_class_images = prefab_safe_get_class_images
    _installed = True
    return True


install()
