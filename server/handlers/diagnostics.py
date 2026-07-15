"""Explicit opt-in reverse-engineering diagnostic packet handlers."""

from __future__ import annotations

from protocol.handler_registry import register_handler


@register_handler(241)
async def handle_debug_parity_toggle(server, player, packet) -> None:
    """Start or stop one authenticated, opt-in parity capture session."""

    manager = getattr(server, "debug_parity", None)
    if manager is not None:
        manager.handle_toggle(player, packet)


@register_handler(242)
async def handle_debug_client_sample(server, player, packet) -> None:
    """Queue one rate-limited client parity sample without blocking the tick."""

    manager = getattr(server, "debug_parity", None)
    if manager is not None:
        manager.handle_client_sample(player, packet)


@register_handler(243)
async def handle_debug_client_event(server, player, packet) -> None:
    """Queue one bounded client diagnostic event for the writer thread."""

    manager = getattr(server, "debug_parity", None)
    if manager is not None:
        manager.handle_client_event(player, packet)
