"""A small test court built into the map before anybody joins.

The court gives every scenario the terrain it needs at known coordinates,
on any map (the synthetic flat map of the fast tests or a stock map of the
full run): open ground to run and fight on, a staircase and a raised
platform (climbing, a 10 block drop), a wall (cover, line of sight), a pool
at water level with steps out of it, and a clear building plot.

Coordinates: VXL z grows downward; ``surface`` is the top solid layer of the
court, so a standing player's eye is at ``surface - EYE_ABOVE_GROUND``.
"""

from __future__ import annotations

from dataclasses import dataclass

EYE_ABOVE_GROUND = 2.25
COURT_COLOR = 0x7F6E6E6E
WALL_COLOR = 0x7F884422
WIDTH = 96
DEPTH = 64
WATER_FLOOR = 239


@dataclass(frozen=True)
class Court:
    x: int
    y: int
    surface: int

    def cell(self, u: int, v: int, up: int = 0) -> tuple:
        """Court cell ``(u, v)``; ``up`` layers above the court surface."""

        return (self.x + int(u), self.y + int(v), self.surface - int(up))

    def stand(self, u: float, v: float, up: float = 0.0) -> tuple:
        """Eye position of a player standing on the court at ``(u, v)``."""

        return (
            self.x + float(u) + 0.5,
            self.y + float(v) + 0.5,
            float(self.surface) - float(up) - EYE_ABOVE_GROUND,
        )

    # -- named places --------------------------------------------------
    @property
    def lanes(self) -> list:
        """Eight parallel running lanes (start eye positions), heading +x."""

        return [self.stand(34, 22 + 4 * index) for index in range(8)]

    def lane(self, index: int) -> tuple:
        return self.stand(34, 22 + 4 * (index % 8))

    @property
    def stairs_foot(self) -> tuple:
        return self.stand(47, 12)

    @property
    def platform(self) -> tuple:
        return self.stand(64, 12, up=10)

    @property
    def pool(self) -> tuple:
        return (
            self.x + 14.5, self.y + 50.5,
            float(WATER_FLOOR) - EYE_ABOVE_GROUND,
        )

    @property
    def pool_edge(self) -> tuple:
        """On the court, two blocks from the drop into the pool (heading -x)."""

        return self.stand(31, 52)

    @property
    def plot(self) -> tuple:
        return self.stand(16, 16)

    @property
    def wall_front(self) -> tuple:
        return self.stand(26, 12)

    def sky(self, u: float, v: float, height: float) -> tuple:
        x, y, z = self.stand(u, v)
        return (x, y, z - float(height))


def build_court(world_manager, x: int = 200, y: int = 224, surface: int = 220) -> Court:
    """Carve and fill the court into ``world_manager``'s map."""

    court = Court(int(x), int(y), int(surface))
    vxl = world_manager.map
    set_point = vxl.set_point

    def column(cx: int, cy: int, top: int, color: int = COURT_COLOR) -> None:
        for z in range(0, top):
            set_point(cx, cy, z, False, 0)
        for z in range(top, WATER_FLOOR + 1):
            set_point(cx, cy, z, True, color)

    for u in range(WIDTH):
        for v in range(DEPTH):
            column(court.x + u, court.y + v, court.surface)

    # Pool: open water with steps up to the court on its +x side.
    for u in range(4, 28):
        for v in range(40, 60):
            column(court.x + u, court.y + v, WATER_FLOOR)
    for step, u in enumerate(range(28, 28 + (WATER_FLOOR - court.surface))):
        if u >= 47:
            break
        top = WATER_FLOOR - step - 1
        for v in range(44, 49):
            column(court.x + u, court.y + v, max(court.surface, top))

    # Staircase and raised platform.
    for step, u in enumerate(range(50, 60)):
        for v in range(8, 17):
            column(court.x + u, court.y + v, court.surface - step - 1)
    for u in range(60, 71):
        for v in range(8, 17):
            column(court.x + u, court.y + v, court.surface - 10)

    # Wall, three high.
    for v in range(6, 21):
        for up in range(1, 4):
            set_point(court.x + 30, court.y + v, court.surface - up, True, WALL_COLOR)

    refresh = getattr(world_manager, "_refresh_world", None)
    if callable(refresh):
        refresh()
    for name in ("_spawn_candidates_cache", "_spawn_candidate_cache"):
        if hasattr(world_manager, name):
            try:
                setattr(world_manager, name, {})
            except Exception:  # noqa: BLE001
                pass
    return court
