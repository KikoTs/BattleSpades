# -*- coding: utf-8 -*-
"""Scenario registry for testbot.

Each scenario is a module exporting:
  NAME    : str
  TIMEOUT : float                 # overall scenario timeout in seconds
  script(client) : callable       # raises on failure, returns None on success

Add a new scenario by creating testbot/scenarios/<name>.py and importing it
below.
"""

from . import (  # noqa: F401
    block_build,
    connect_only,
    full_handshake,
    idle_keepalive,
    multi_bot,
    reconnect,
    spawn_chat,
    spawn_walk,
    walk_speed,
)

REGISTRY = {
    connect_only.NAME:    connect_only,
    full_handshake.NAME:  full_handshake,
    idle_keepalive.NAME:  idle_keepalive,
    multi_bot.NAME:       multi_bot,
    reconnect.NAME:       reconnect,
    spawn_chat.NAME:      spawn_chat,
    spawn_walk.NAME:      spawn_walk,
    block_build.NAME:     block_build,
    walk_speed.NAME:      walk_speed,
}


def get(name):
    return REGISTRY.get(name)


def names():
    return sorted(REGISTRY.keys())
