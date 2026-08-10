"""Shared retail objective-zone packet helpers.

Packet 43 stores a world-space AABB as raw signed voxel coordinates.  The
first byte is ``visible_team`` (TEAM_NEUTRAL means shared), while the colour is
independent and identifies the current owner.  Keeping construction here
prevents CTF, Multi-Hill, and Demolition from drifting into different field
orders or late-join behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import shared.constants as C

from server.game_constants import TEAM_NEUTRAL


Bounds = tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class ObjectiveZone:
    """One authoritative objective volume and its stable packet identity."""

    index: int
    bounds: Bounds
    center: tuple[float, float, float]

    def contains(self, position) -> bool:
        x0, x1, y0, y1, z0, z1 = self.bounds
        x, y, z = (float(value) for value in position[:3])
        return x0 <= x <= x1 and y0 <= y <= y1 and z0 <= z <= z1


def clamp_bounds(values: Iterable[float]) -> Bounds:
    """Normalize and clamp an x0,x1,y0,y1,z0,z1 tuple to the VXL."""

    raw = tuple(float(value) for value in values)
    if len(raw) != 6:
        raise ValueError("objective bounds require six coordinates")
    x0, x1 = sorted((raw[0], raw[1]))
    y0, y1 = sorted((raw[2], raw[3]))
    z0, z1 = sorted((raw[4], raw[5]))
    return (
        max(0, min(int(C.MAP_X) - 1, int(round(x0)))),
        max(0, min(int(C.MAP_X) - 1, int(round(x1)))),
        max(0, min(int(C.MAP_Y) - 1, int(round(y0)))),
        max(0, min(int(C.MAP_Y) - 1, int(round(y1)))),
        max(0, min(int(C.MAP_Z) - 1, int(round(z0)))),
        max(0, min(int(C.MAP_Z) - 1, int(round(z1)))),
    )


def from_map_zone(index: int, zone, *, z_shift: int = 0) -> ObjectiveZone:
    """Convert a :class:`server.map_metadata.MapZone` to packet coordinates."""

    x0, x1, y0, y1, z0, z1 = zone.extents
    bounds = clamp_bounds((
        zone.x + x0,
        zone.x + x1,
        zone.y + y0,
        zone.y + y1,
        zone.z + z0 + int(z_shift),
        zone.z + z1 + int(z_shift),
    ))
    return ObjectiveZone(
        int(index),
        bounds,
        (
            (bounds[0] + bounds[1]) * 0.5,
            (bounds[2] + bounds[3]) * 0.5,
            (bounds[4] + bounds[5]) * 0.5,
        ),
    )


def around(
    index: int,
    center,
    *,
    radius_xy: float,
    height_above: float = 8.0,
    depth_below: float = 8.0,
) -> ObjectiveZone:
    """Create a deterministic fallback objective volume around one anchor."""

    x, y, z = (float(value) for value in center[:3])
    bounds = clamp_bounds((
        x - radius_xy,
        x + radius_xy,
        y - radius_xy,
        y + radius_xy,
        z - height_above,
        z + depth_below,
    ))
    return ObjectiveZone(int(index), bounds, (x, y, z))


def minimap_zone_packet(
    zone: ObjectiveZone,
    *,
    color,
    icon_id: int,
    visible_team: int = TEAM_NEUTRAL,
    icon_scale: float = 1.0,
    locked_in_zone: bool = False,
):
    """Build packet 43 in the retail x1,y1,z1,x2,y2,z2 wire order."""

    from shared.packet import MinimapZone

    x0, x1, y0, y1, z0, z1 = zone.bounds
    packet = MinimapZone()
    packet.key = int(visible_team)
    packet.color = tuple(int(value) & 0xFF for value in color[:3])
    packet.A2018, packet.A2019 = x0, x1
    packet.A2020, packet.A2021 = y0, y1
    packet.A2022, packet.A2023 = z0, z1
    packet.icon_scale = float(icon_scale)
    packet.icon_id = int(icon_id)
    packet.locked_in_zone = int(bool(locked_in_zone))
    return packet


def minimap_zone_clear_packet(zone: ObjectiveZone):
    """Build packet 44 using the exact six-coordinate identity of packet 43."""

    from shared.packet import MinimapZoneClear

    x0, x1, y0, y1, z0, z1 = zone.bounds
    packet = MinimapZoneClear()
    packet.A2018, packet.A2019 = x0, x1
    packet.A2020, packet.A2021 = y0, y1
    packet.A2022, packet.A2023 = z0, z1
    return packet


def lock_to_zone_packet(zone: ObjectiveZone):
    """Build the native packet-108 movement clamp for a build phase."""

    from shared.packet import LockToZone

    x0, x1, y0, y1, z0, z1 = zone.bounds
    packet = LockToZone()
    packet.A2018, packet.A2019 = x0, x1
    packet.A2020, packet.A2021 = y0, y1
    packet.A2022, packet.A2023 = z0, z1
    return packet


__all__ = [
    "Bounds",
    "ObjectiveZone",
    "around",
    "clamp_bounds",
    "from_map_zone",
    "lock_to_zone_packet",
    "minimap_zone_clear_packet",
    "minimap_zone_packet",
]
