"""Retail-safe player-name allocation.

The native client does not safely tolerate two live ``CreatePlayer`` records
with the same display name.  A later duplicate can steal the first client's
local-player association, after which its no-id movement packets update the
wrong server player.  Names are therefore made unique before any roster packet
is emitted.

Uniqueness is decided on a *confusable skeleton*, not on the raw text, so a
joiner cannot impersonate an online player with homoglyphs ("Κiko" with a
Greek capital kappa, "Kik0", "K​iko" with a zero-width space).  Names that
pose as the server or as staff ("Admin", "[MOD]Kiko", "Console") are replaced
by a neutral fallback.  Everything here is pure and synchronous; it runs on
the gameplay thread during ``NewPlayer``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


MAX_PLAYER_NAME_BYTES = 15
FALLBACK_NAME = "Player"

# Whole-name skeletons nobody may take (compared letters/digits only, after
# confusable folding, so "Adm1n", "A d m i n" and "ADMIN" all match).
RESERVED_NAMES = frozenset({
    "server", "admin", "admins", "administrator", "console", "system",
    "moderator", "mod", "mods", "battlespades", "owner", "staff",
    "developer", "dev", "host", "gm", "gamemaster", "op", "operator",
    "sysop", "root", "anticheat", "vac", "steam",
})
# Bracketed tags ("[Admin]Kiko", "(MOD) x", "<Staff>") claiming a role.
RESERVED_TAGS = frozenset({
    "admin", "adm", "administrator", "mod", "moderator", "staff", "dev",
    "developer", "owner", "server", "console", "system", "gm", "op", "sysop",
    "host", "battlespades",
})
_TAG_PATTERN = re.compile(r"[\[\(\{<|]\s*([^\]\)\}>|]{1,16}?)\s*[\]\)\}>|]")

# Visually identical or near-identical characters folded onto one ASCII
# letter for comparison only (the display name keeps what the player typed).
_CONFUSABLES = {
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h",
    "о": "o", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s",
    "і": "i", "ї": "i", "ј": "j", "һ": "h", "ԁ": "d", "ԛ": "q", "ԝ": "w",
    "ь": "b", "ӏ": "l", "г": "r", "п": "n", "и": "u", "з": "3", "ч": "4",
    # Greek
    "α": "a", "β": "b", "γ": "y", "ε": "e", "ζ": "z", "η": "n", "ι": "i",
    "κ": "k", "μ": "u", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u",
    "χ": "x", "ω": "w", "ς": "c", "σ": "o",
    # Latin lookalikes that survive NFKC/casefold
    "ı": "i", "ł": "l", "ø": "o", "đ": "d", "ħ": "h", "ŀ": "l", "ƅ": "b",
    "ɑ": "a", "ɡ": "g", "ɩ": "i", "ʟ": "l", "ᴏ": "o", "ꞵ": "b",
    # Digits / ASCII punctuation standing in for letters
    "0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b",
    "9": "g", "|": "l", "!": "l", "$": "s", "@": "a",
    # Case-folded lookalikes: I and l are the classic pair.
    "i": "l", "j": "l",
}
_MULTI_CONFUSABLES = (("rn", "m"), ("vv", "w"))


def _truncate_utf8(value: str, limit: int) -> str:
    """Return a valid UTF-8 prefix no longer than ``limit`` wire bytes."""

    encoded = value.encode("utf-8")[: max(0, int(limit))]
    while encoded:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return ""


def _is_invisible(character: str) -> bool:
    """Control, format (zero-width, bidi overrides), and unassigned code points."""

    category = unicodedata.category(character)
    return category in {"Cc", "Cf", "Cs", "Co", "Cn"} or character in {
        "ᅟ", "ᅠ", "ㅤ", "ﾠ", "⠀",  # Hangul/braille blanks
    }


def _safe_base_name(requested: object) -> str:
    """Normalize compatibility forms, drop invisible characters, cap 15 bytes."""

    text = unicodedata.normalize("NFKC", str(requested or ""))
    text = "".join(
        " " if character.isspace() else character
        for character in text
        if character.isspace() or not _is_invisible(character)
    )
    text = re.sub(r"\s+", " ", text).strip()
    # A name made only of combining marks renders as nothing.
    if not any(unicodedata.category(c)[0] != "M" and c != " " for c in text):
        text = ""
    if not text:
        text = FALLBACK_NAME
    return _truncate_utf8(text, MAX_PLAYER_NAME_BYTES) or FALLBACK_NAME


def name_skeleton(name: object) -> str:
    """Return the confusable-folded comparison key for ``name``.

    Two names with the same skeleton look alike on the scoreboard; only one
    of them may be online at a time.
    """

    text = unicodedata.normalize("NFKC", str(name or "")).casefold()
    text = unicodedata.normalize("NFKD", text)
    folded = []
    for character in text:
        if _is_invisible(character) or unicodedata.category(character)[0] == "M":
            continue
        if character.isspace() or character in "_-.'`·":
            continue
        mapped = _CONFUSABLES.get(character, character)
        # Second pass: a Cyrillic "і" folds to "i", which folds to "l".
        folded.append(_CONFUSABLES.get(mapped, mapped))
    skeleton = "".join(folded)
    for source, target in _MULTI_CONFUSABLES:
        skeleton = skeleton.replace(source, target)
    return skeleton


def _alnum_skeleton(name: str) -> str:
    return "".join(c for c in name_skeleton(name) if c.isalnum())


def is_reserved_name(name: object, extra_reserved: Iterable[str] = ()) -> bool:
    """Whether ``name`` poses as the server, console, or staff."""

    text = str(name or "")
    reserved = set(_alnum_skeleton(value) for value in RESERVED_NAMES)
    reserved.update(_alnum_skeleton(str(value)) for value in extra_reserved)
    reserved.discard("")
    if _alnum_skeleton(text) in reserved:
        return True
    tags = set(_alnum_skeleton(value) for value in RESERVED_TAGS) | reserved
    normalized = unicodedata.normalize("NFKC", text)
    for match in _TAG_PATTERN.finditer(normalized):
        if _alnum_skeleton(match.group(1)) in tags:
            return True
    return False


def allocate_unique_player_name(
    requested: object,
    players: Iterable[object],
    *,
    extra_reserved: Iterable[str] = (),
) -> str:
    """Allocate a confusable-unique retail wire name.

    This runs synchronously on the gameplay thread during ``NewPlayer``.  It
    has no persistent state: disconnected names become immediately reusable,
    while live bot and human names share one collision domain (a human can
    never take a bot's name; bots are named elsewhere and never renamed here).

    * reserved/staff names become ``Player`` (then made unique);
    * a name whose skeleton matches a logged-in admin's also falls back to
      ``Player`` instead of receiving a lookalike ``~N`` suffix;
    * any other skeleton collision gets the ``~N`` suffix, within 15 bytes.
    """

    base = _safe_base_name(requested)
    players = tuple(players)
    if is_reserved_name(base, extra_reserved):
        base = FALLBACK_NAME
    else:
        base_skeleton = name_skeleton(base)
        for player in players:
            if (
                getattr(player, "admin", False) is True
                and name_skeleton(getattr(player, "name", "")) == base_skeleton
            ):
                base = FALLBACK_NAME
                break

    used = {
        name_skeleton(getattr(player, "name", ""))
        for player in players
    }
    if name_skeleton(base) not in used:
        return base

    for index in range(2, 10_000):
        suffix = f"~{index}"
        prefix = _truncate_utf8(
            base,
            MAX_PLAYER_NAME_BYTES - len(suffix.encode("ascii")),
        )
        candidate = f"{prefix}{suffix}"
        if name_skeleton(candidate) not in used:
            return candidate

    # The protocol supports far fewer simultaneous players than this branch;
    # keep a deterministic safe fallback instead of returning a duplicate.
    return f"P{len(used):013d}"[-MAX_PLAYER_NAME_BYTES:]
