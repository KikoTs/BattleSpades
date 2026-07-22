"""Retail owner movement guard and explicit parachute-key bridge.

Install this module inside the client's ``aoslib`` package and call
``install(manager)`` immediately after GameManager construction.  It is
compatible with the bundled Python 2.7 runtime.
"""

from __future__ import division


MAX_LAUNCH_RESTORE_DISTANCE = 0.25
MAX_LAUNCH_RESTORE_DISTANCE_SQ = MAX_LAUNCH_RESTORE_DISTANCE ** 2
PARACHUTE_NORMAL = 72
DEFAULT_HOVER_KEY = 122


def _position3(value):
    """Copy a native vector before the wrapped update mutates it."""

    return (float(value[0]), float(value[1]), float(value[2]))


def _install_parachute_key(manager):
    """Route the configured Z/hover key into the existing ClientData bit."""

    if manager is None or getattr(manager, '_parachute_key_installed', False):
        return

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
        if int(symbol) != hover_key:
            return
        world_object = local_parachute_world_object(require_airborne=True)
        if world_object is not None:
            # Character.set_hover rejects this action for non-jetpack classes,
            # but ClientData already has a general hover bit.  Setting the
            # native world property feeds that stock serializer directly.
            world_object.hover = True

    def parachute_key_release(symbol, modifiers):
        if int(symbol) != hover_key:
            return
        world_object = local_parachute_world_object(require_airborne=False)
        if world_object is not None:
            world_object.hover = False

    manager.window.push_handlers(
        on_key_press=parachute_key_press,
        on_key_release=parachute_key_release,
    )
    # This bundled pyglet dispatches the event stack from its oldest frame.
    # GameManager consumes keyboard events, so a normal late push would never
    # reach us. Move only our frame behind the manager frame.
    event_stack = getattr(manager.window, '_event_stack', None)
    if event_stack is not None:
        for index, frame in enumerate(event_stack):
            if frame.get('on_key_press') is parachute_key_press:
                event_stack.append(event_stack.pop(index))
                break
    manager._parachute_key_handlers = (
        parachute_key_press, parachute_key_release)
    manager._parachute_key_installed = True


def install(manager=None):
    """Install the Character guard and parachute key route on the game thread."""

    from aoslib.character import Character

    if getattr(Character, '_jump_anchor_smoothing_installed', False):
        _install_parachute_key(manager)
        return True

    original_update = Character.update

    def smoothed_update(self, *args, **kwargs):
        world_object = getattr(self, 'world_object', None)
        if world_object is None:
            return original_update(self, *args, **kwargs)

        pre_position = _position3(world_object.position)
        was_airborne = bool(world_object.airborne)
        result = original_update(self, *args, **kwargs)

        if not was_airborne and bool(world_object.jump_this_frame):
            post_position = _position3(world_object.position)
            distance_sq = sum(
                (post_position[index] - pre_position[index]) ** 2
                for index in range(3)
            )
            if distance_sq > MAX_LAUNCH_RESTORE_DISTANCE_SQ:
                world_object.set_position(*pre_position)

        return result

    Character._jump_anchor_smoothing_original_update = original_update
    Character.update = smoothed_update
    Character._jump_anchor_smoothing_installed = True
    _install_parachute_key(manager)
    return True
