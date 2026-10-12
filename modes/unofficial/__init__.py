"""Unofficial, retail-compatible community modes; never selected implicitly."""

from .catalog import ALIASES, CATALOG


def register(registry: dict) -> None:
    """Install concrete rulesets and their command/config aliases."""
    from .combat import BlockHunt, FloorTax, HotPotato, OneInTheSpade, BorrowedArsenal, Kingmaker, Lockdown
    from .races import CrumblingRun, RedLightRaid
    from .objectives import FalseWall, LastExit, TunnelRats, BridgeChicken, Towerfall, PaintRaid, SmugglersRun, ReverseSiege, ScrapyardEngineers, Spadeball, NightShift

    classes = (BlockHunt, FalseWall, CrumblingRun, LastExit, NightShift, TunnelRats,
               BridgeChicken, Towerfall, FloorTax, HotPotato, Lockdown, PaintRaid,
               SmugglersRun, OneInTheSpade, BorrowedArsenal, ReverseSiege, Kingmaker,
               RedLightRaid, ScrapyardEngineers, Spadeball)
    for cls in classes:
        registry[cls.mode_code] = cls
        registry[cls.mode_code.replace("_", "-")] = cls
    for alias, code in ALIASES.items():
        registry[alias] = registry[code]
