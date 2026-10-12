"""Publish bounded mode facts once per perception batch, without pathfinding."""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from server.bot_ai.fun_messages import FunCargo, FunPlayer, FunState
from .base import TEAMS

if TYPE_CHECKING:
    from .base import FunMode


def snapshot(mode: FunMode) -> FunState:
    now = mode.clock()
    roster = mode.players()
    checkpoints = getattr(mode, "checkpoint", {})
    prisoners = getattr(mode, "prisoners", ())
    owners = getattr(mode, "owners", {})
    bounds = tuple(getattr(mode, "paint_bounds", ()))
    paint = {}
    if len(bounds) == 5:
        x0, x1, y0, y1, z = bounds
        for player in roster:
            if not player.is_bot or not mode.active(player):
                continue
            x, y = math.floor(player.x), math.floor(player.y)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    cell = x + dx, y + dy, z
                    if x0 <= cell[0] <= x1 and y0 <= cell[1] <= y1:
                        paint[cell] = owners.get(cell, -1)
    return FunState(
        round_number=mode.round_number, attackers=mode.attackers,
        center=mode.anchor("center"), homes=tuple((t, mode.home(t)) for t in TEAMS),
        players=tuple(FunPlayer(
            p.id, mode.active(p) or (mode.phase == "prepare" and p in mode.participants),
            p.disguised, checkpoints.get(p, 0), p in prisoners,
        ) for p in roster),
        cargo=tuple(FunCargo(
            # A hidden carrier's current location is never published here.
            c.home if c.hidden and c.holder is not None else
            c.holder.position if c.holder is not None else c.position,
            c.team, c.holder.id if c.holder is not None else -1, c.value,
            c.hidden, now >= c.available_at,
        ) for c in getattr(mode, "cargo", ())[:32]),
        exit=mode.anchor("exit"), exit_open=getattr(mode, "exit_open", False),
        stage=getattr(mode, "stage", 0), red=getattr(mode, "red", False),
        light_remaining=max(0.0, getattr(mode, "change_at", now) - now),
        checkpoints=tuple(getattr(mode, "course", ())),
        route=tuple(tuple(p) for p in mode.data.get("bot_route", ())[:128]),
        cores=tuple(getattr(mode, "cores", {}).items()),
        repair_cells=tuple(c for c in getattr(mode, "repair_cells", ())[:64]
                           if not mode.server.world_manager.get_solid(*c)),
        paint_bounds=bounds, paint=tuple(paint.items()),
        champions=tuple((team, p.id) for team, p in getattr(mode, "champions", {}).items()
                        if p is not None and mode.active(p)),
        holder=getattr(getattr(mode, "holder", None), "id", -1),
        clue_until=getattr(mode, "bot_clue_until", 0.0),
        clues=tuple(getattr(mode, "bot_clues", ())),
        hunt_points=tuple(getattr(mode, "hunt_points", ())),
        stashed=tuple(getattr(mode, "stashed", ())),
        paused=now < getattr(mode, "goal_pause", 0),
    )
