"""Small immutable facts for the unofficial pack; no native objects cross IPC."""
from __future__ import annotations

from dataclasses import dataclass

from .messages import Vector3, VoxelCoordinate


@dataclass(frozen=True, slots=True)
class FunPlayer:
    player_id: int
    active: bool
    disguised: bool = False
    checkpoint: int = 0
    prisoner: bool = False


@dataclass(frozen=True, slots=True)
class FunCargo:
    position: Vector3
    team: int
    holder: int = -1
    value: int = 1
    hidden: bool = False
    ready: bool = True


@dataclass(frozen=True, slots=True)
class FunState:
    round_number: int
    attackers: int
    center: Vector3
    homes: tuple[tuple[int, Vector3], ...]
    players: tuple[FunPlayer, ...]
    cargo: tuple[FunCargo, ...] = ()
    exit: Vector3 = (0.0, 0.0, 0.0)
    exit_open: bool = False
    stage: int = 0
    red: bool = False
    light_remaining: float = 0.0
    checkpoints: tuple[Vector3, ...] = ()
    route: tuple[Vector3, ...] = ()
    cores: tuple[tuple[int, VoxelCoordinate], ...] = ()
    repair_cells: tuple[VoxelCoordinate, ...] = ()
    paint_bounds: tuple[int, ...] = ()
    # Local samples only, capped at nine per bot; never the entire painted floor.
    paint: tuple[tuple[VoxelCoordinate, int], ...] = ()
    champions: tuple[tuple[int, int], ...] = ()
    holder: int = -1
    clue_until: float = 0.0
    clues: tuple[Vector3, ...] = ()
    hunt_points: tuple[Vector3, ...] = ()
    stashed: tuple[int, ...] = ()
    paused: bool = False
