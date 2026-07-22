"""Route the configured Z/hover key through retail ClientData for parachutes.

Install this module inside the client's ``aoslib`` package and call
``install(manager)`` immediately after GameManager construction.  It is
compatible with the bundled Python 2.7 runtime and does not replace any
movement or Character update method.
"""


PARACHUTE_NORMAL = 72
DEFAULT_HOVER_KEY = 122


def install(manager):
    """Install one idempotent parachute key handler on the game window."""

    if manager is None or getattr(manager, '_parachute_key_installed', False):
        return False
    hover_key = int(getattr(manager.config, 'hover', DEFAULT_HOVER_KEY))

    def local_parachute_world_object(require_airborne):
        scene = getattr(manager, 'scene', None)
        player = getattr(scene, 'player', None)
        if player is None or int(getattr(player, 'parachute', 0)) != PARACHUTE_NORMAL:
            return None
        character = getattr(player, 'character', None)
        world_object = getattr(character, 'world_object', None)
        if world_object is None:
            return None
        if require_airborne and not bool(world_object.airborne):
            return None
        return world_object

    def parachute_key_press(symbol, modifiers):
        del modifiers
        if int(symbol) != hover_key:
            return
        world_object = local_parachute_world_object(require_airborne=True)
        if world_object is not None:
            world_object.hover = True

    def parachute_key_release(symbol, modifiers):
        del modifiers
        if int(symbol) != hover_key:
            return
        world_object = local_parachute_world_object(require_airborne=False)
        if world_object is not None:
            world_object.hover = False

    manager.window.push_handlers(
        on_key_press=parachute_key_press,
        on_key_release=parachute_key_release,
    )
    event_stack = getattr(manager.window, '_event_stack', None)
    if event_stack is not None:
        for index, frame in enumerate(event_stack):
            if frame.get('on_key_press') is parachute_key_press:
                event_stack.append(event_stack.pop(index))
                break
    manager._parachute_key_handlers = (
        parachute_key_press, parachute_key_release)
    manager._parachute_key_installed = True
    return True
