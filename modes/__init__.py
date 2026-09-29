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
from .multi_hill import MultiHillMode
from .demolition import DemolitionMode
from .diamond_mine import DiamondMineMode
from .occupation import OccupationMode
from .territory_control import TerritoryControlMode


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
    return _modes.get(str(name).strip().lower())


def canonical_mode_code(name: str) -> Optional[str]:
    """Return the one registry code for a mode name or alias.

    ``occupation``/``diamond``/``zombie``/``classic_ctf`` resolve to the
    retail short codes (``oc``/``dia``/``zom``/``cctf``) that map metadata,
    mode_data and config overlays key on. Returns ``None`` for an unknown
    mode. A registered name without a retail short code (``arena``) is
    returned as-is.
    """
    normalized = str(name).strip().lower()
    mode_class = _modes.get(normalized)
    if mode_class is None:
        return None
    from server import mode_data

    code = mode_data.get(normalized).code
    if code != "nor" and _modes.get(code) is mode_class:
        return code
    return normalized


def registered_mode_codes() -> tuple[str, ...]:
    """Every accepted mode name, sorted (for operator error messages)."""
    return tuple(sorted(_modes))


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
    "canonical_mode_code",
    "get_mode_class",
    "register_mode",
    "registered_mode_codes",
]
