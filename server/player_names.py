"""Retail-safe player-name allocation.

The native client does not safely tolerate two live ``CreatePlayer`` records
with the same display name.  A later duplicate can steal the first client's
local-player association, after which its no-id movement packets update the
wrong server player.  Names are therefore made unique before any roster packet
is emitted.
"""

from __future__ import annotations

from collections.abc import Iterable


MAX_PLAYER_NAME_BYTES = 15


def _truncate_utf8(value: str, limit: int) -> str:
    """Return a valid UTF-8 prefix no longer than ``limit`` wire bytes."""

    encoded = value.encode("utf-8")[: max(0, int(limit))]
    while encoded:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return ""


def _safe_base_name(requested: object) -> str:
    """Normalize control characters and enforce the retail 15-byte field."""

    text = "".join(
        character
        for character in str(requested or "")
        if character >= " " and character != "\x7f"
    ).strip()
    if not text:
        text = "Player"
    return _truncate_utf8(text, MAX_PLAYER_NAME_BYTES) or "Player"


def allocate_unique_player_name(
    requested: object,
    players: Iterable[object],
) -> str:
    """Allocate a case-insensitively unique retail wire name.

    This runs synchronously on the gameplay thread during ``NewPlayer``.  It
    has no persistent state: disconnected names become immediately reusable,
    while live bot and human names share one collision domain.
    """

    base = _safe_base_name(requested)
    used = {
        str(getattr(player, "name", "")).casefold()
        for player in players
    }
    if base.casefold() not in used:
        return base

    for index in range(2, 10_000):
        suffix = f"~{index}"
        prefix = _truncate_utf8(
            base,
            MAX_PLAYER_NAME_BYTES - len(suffix.encode("ascii")),
        )
        candidate = f"{prefix}{suffix}"
        if candidate.casefold() not in used:
            return candidate

    # The protocol supports far fewer simultaneous players than this branch;
    # keep a deterministic safe fallback instead of returning a duplicate.
    return f"P{len(used):013d}"[-MAX_PLAYER_NAME_BYTES:]
