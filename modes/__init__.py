"""
modes - Game Mode Modules
Different game modes like CTF, TDM, Arena.
"""

from typing import Optional, Type

from .base_mode import BaseMode
from .ctf import CTFMode
from .tdm import TDMMode
from .arena import ArenaMode


# Mode registry
_modes = {
    "ctf": CTFMode,
    "tdm": TDMMode,
    "arena": ArenaMode,
}


def get_mode_class(name: str) -> Optional[Type[BaseMode]]:
    """Get mode class by name."""
    return _modes.get(name.lower())


def register_mode(name: str, mode_class: Type[BaseMode]):
    """Register a custom game mode."""
    _modes[name.lower()] = mode_class


__all__ = [
    "BaseMode",
    "CTFMode",
    "TDMMode", 
    "ArenaMode",
    "get_mode_class",
    "register_mode",
]
