"""
modes - Game Mode Modules
Different game modes like CTF, TDM, Arena.
"""

from typing import Optional, Type

from .base_mode import BaseMode
from .ctf import CTFMode
from .classic_ctf import ClassicCTFMode
from .tdm import TDMMode
from .arena import ArenaMode
from .vip import VIPMode
from .zombie import ZombieMode


# Mode registry
_modes = {
    "ctf": CTFMode,
    "cctf": ClassicCTFMode,
    "classic_ctf": ClassicCTFMode,
    "classic-ctf": ClassicCTFMode,
    "tdm": TDMMode,
    "arena": ArenaMode,
    "vip": VIPMode,
    "zom": ZombieMode,
    "zombie": ZombieMode,
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
    "ClassicCTFMode",
    "TDMMode", 
    "ArenaMode",
    "VIPMode",
    "ZombieMode",
    "get_mode_class",
    "register_mode",
]
