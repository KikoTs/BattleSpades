"""Enable bundled DLC choices in the retail equipment menu (Python 2.7).

Only the menu's tool predicate and GameClass's DLC-only predicate are changed.
Steam ownership, item data, team/class restrictions and networking stay stock.
Written against the retail call sites; no AGEX implementation is retained.
"""
from aosfix_runtime import method


def install(runtime):
    def patch_tools(menu):
        from shared.constants_DLC import get_tool_dlc_Name
        original = method(menu, 'is_tool_selectable')

        def menu_tool_selectable(tool_id, dlc_manager):
            if get_tool_dlc_Name(tool_id) is not None:
                return True
            return original(tool_id, dlc_manager)

        runtime.replace('equipment.tools', [
            (menu, 'is_tool_selectable', original, menu_tool_selectable)])

    def patch_characters(module):
        cls = module.GameClass
        original = method(cls, 'is_selectable')
        dlc_for_character = method(module, 'get_character_dlc_Name')

        def character_selectable(self):
            if dlc_for_character(self.id) is not None:
                return True
            return original(self)

        runtime.replace('equipment.characters', [
            (cls, 'is_selectable', original, character_selectable)])

    runtime.watch('aoslib.scenes.ingame_menus.selectClass', 'equipment.tools', patch_tools)
    runtime.watch('aoslib.scenes.main.gameClass', 'equipment.characters', patch_characters)
