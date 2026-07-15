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
from .lobby_skeletons import (
    DemolitionMode,
    DiamondMineMode,
    MultiHillMode,
    OccupationMode,
    TerritoryControlMode,
)


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
    "mh": MultiHillMode,
    "multihill": MultiHillMode,
    "multi-hill": MultiHillMode,
    "tc": TerritoryControlMode,
    "territory_control": TerritoryControlMode,
    "territory-control": TerritoryControlMode,
    "dia": DiamondMineMode,
    "diamond": DiamondMineMode,
    "diamond_mine": DiamondMineMode,
    "dem": DemolitionMode,
    "demolition": DemolitionMode,
    "oc": OccupationMode,
    "occupation": OccupationMode,
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
    "MultiHillMode",
    "TerritoryControlMode",
    "DiamondMineMode",
    "DemolitionMode",
    "OccupationMode",
    "get_mode_class",
    "register_mode",
]
