"""Packet-id to coroutine-handler registry.

Keeping registration independent from decoding lets domain modules own their
handlers without importing the dispatcher and creating circular dependencies.
"""

from __future__ import annotations

from collections.abc import Callable


HANDLERS: dict[int, Callable] = {}


def register_handler(packet_id: int):
    """Register one async gameplay handler for a retail packet id."""

    def decorator(func: Callable) -> Callable:
        HANDLERS[int(packet_id)] = func
        return func

    return decorator
