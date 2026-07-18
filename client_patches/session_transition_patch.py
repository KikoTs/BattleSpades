# -*- coding: utf-8 -*-
"""Retain the GameClient while a BattleSpades server replaces the map.

The retail ``MapEnded`` handler only freezes the compiled ``GameScene``; it
does not open ``LoadingMenu`` and ENet disconnect reason 18 does not reconnect.
BattleSpades sends a fresh loader handshake over the existing authenticated
peer, so this compatibility hook moves the client to ``LoadingMenu`` after the
three native MapEnded pause flags become true.

The hook wraps the GameManager's already-scheduled update callback.  It does no
polling thread, network I/O, or filesystem I/O and remains compatible with the
optional physics tracer's schedule wrapper.
"""
from __future__ import absolute_import, print_function

import sys


_installed = False
_transition_scene = None
_transition_ready_sent = False


def _manager_from_callback(callback):
    owner = getattr(callback, 'im_self', None)
    if owner is None:
        owner = getattr(callback, '__self__', None)
    if owner is None:
        owner = getattr(sys, '_aos_manager', None)
    if owner is None or type(owner).__name__ != 'GameManager':
        return None
    return owner


def _enter_loading_menu(manager):
    """Open the same-connection loader once for one frozen GameScene."""
    global _transition_scene, _transition_ready_sent

    scene = getattr(manager, 'game_scene', None)
    client = getattr(manager, 'client', None)
    if scene is None or client is None or getattr(client, 'disconnected', True):
        _transition_scene = None
        _transition_ready_sent = False
        return

    ended = (
        bool(getattr(scene, 'pause_players', False))
        and bool(getattr(scene, 'pause_entities', False))
        and bool(getattr(scene, 'pause_particles', False))
    )
    if not ended:
        # GameScene is a manager-owned singleton and is reinitialized for the
        # next map.  Its pause flags, not object identity, delimit epochs.
        _transition_scene = None
        _transition_ready_sent = False
        return

    if _transition_scene is not scene:
        try:
            from aoslib.scenes.ingame_menus.loadingMenu import LoadingMenu
            # identifier=None is intentional: LoadingMenu must reuse the
            # current GameClient, not create a second ENet connection.
            manager.set_menu(LoadingMenu, from_server_menu=False)
            _transition_scene = scene
            _transition_ready_sent = False
        except Exception:
            _transition_scene = None
            try:
                import traceback
                traceback.print_exc()
            except Exception:
                pass
            return

    if _transition_ready_sent:
        return
    try:
        from shared.packet import ClientInMenu
        ready = ClientInMenu()
        ready.in_menu = 1
        # This existing reliable packet is the server's proof that InitialInfo
        # will be consumed by LoadingMenu rather than the retired GameScene.
        client.send_packet(ready)
        _transition_ready_sent = True
    except Exception:
        # Keep the loader installed and retry the acknowledgement next frame.
        try:
            import traceback
            traceback.print_exc()
        except Exception:
            pass


def install():
    """Install before ``aoslib.run`` schedules ``GameManager.update``."""
    global _installed
    if _installed:
        return True

    try:
        import pyglet.clock as clock
    except Exception:
        return False

    original = getattr(clock, 'schedule_interval_soft', None)
    if original is None:
        return False

    def schedule_interval_soft(callback, interval, *args, **kwargs):
        manager = _manager_from_callback(callback)
        if manager is None:
            return original(callback, interval, *args, **kwargs)

        def transition_aware_update(dt, *update_args, **update_kwargs):
            result = callback(dt, *update_args, **update_kwargs)
            _enter_loading_menu(manager)
            return result

        return original(
            transition_aware_update,
            interval,
            *args,
            **kwargs
        )

    clock.schedule_interval_soft = schedule_interval_soft
    _installed = True
    return True


install()
