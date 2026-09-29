"""Authoritative oriented-projectile action shared by packets and bots."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

import shared.constants as C
from server.class_selection import active_tool_authorized

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player


# The client spawns thrown/launched projectiles at its own eye plus a small
# muzzle offset (bots use 0.6). The launch point must lie within this many
# blocks of a server-simulated eye (plus the movement allowance when only the
# current eye is known) and in voxel line of sight of it: a forged launch
# point used to be accepted 10 blocks away, through walls.
LAUNCH_ORIGIN_TOLERANCE = 3.0
# A launch point this close to an eye needs no line of sight: the muzzle
# offset may legitimately poke into a wall the thrower hugs, and a point
# within 1 block cannot lie beyond a full 1-block wall.
LAUNCH_MUZZLE_RADIUS = 1.0
# The segment end is shortened by this much (vs 0.3 for shots) so a muzzle
# point just inside the hugged wall face is not occluded by it.
LAUNCH_LOS_END_SHRINK = 0.6

# Stock launch speeds (client constants). The client adds some of the
# thrower's own velocity, so the cap below leaves generous headroom; it only
# stops teleport-speed projectiles.
_LAUNCH_SPEEDS = {
    int(C.GRENADE_TOOL): float(getattr(C, "GRENADE_THROW_SPEED", 50.0)),
    int(getattr(C, "CLASSIC_GRENADE_TOOL", 31)): float(
        getattr(C, "CLASSIC_GRENADE_THROW_SPEED", 35.0)
    ),
    int(getattr(C, "ANTIPERSONNEL_GRENADE_TOOL", 32)): float(
        getattr(C, "ANTIPERSONNEL_GRENADE_THROW_SPEED", 50.0)
    ),
    int(getattr(C, "MOLOTOV_TOOL", 33)): float(getattr(C, "MOLOTOV_THROW_SPEED", 40.0)),
    int(C.RPG_TOOL): float(getattr(C, "ROCKET_SPEED", 75.0)),
    int(C.RPG2_TOOL): float(getattr(C, "ROCKET2_SPEED", 150.0)),
    int(C.DRILLGUN_TOOL): float(getattr(C, "DRILL_FLYING_SPEED", 40.0)),
    int(getattr(C, "UGC_DRILLGUN_TOOL", 47)): float(
        getattr(C, "UGC_DRILL_FLYING_SPEED", 40.0)
    ),
    int(getattr(C, "SNOWBLOWER_TOOL", 29)): float(getattr(C, "SNOWBALL_SPEED", 50.0)),
    int(getattr(C, "UGC_SNOWBLOWER_TOOL", 48)): float(
        getattr(C, "UGC_SNOWBALL_SPEED", 50.0)
    ),
    # Stock ChemicalBombWeapon/Character.throw_chemicalbomb: A1663 = 50
    # (full charge), A1664 = 25 (minimum).
    int(getattr(C, "CHEMICALBOMB_TOOL", 54)): 50.0,
    int(getattr(C, "GRENADE_LAUNCHER_WEAPON_TOOL", 55)): float(
        getattr(C, "GRENADE_LAUNCHER_PROJECTILE_SPEED", 75.0)
    ),
    int(getattr(C, "STICKY_GRENADE_TOOL", 57)): 50.0,
    int(getattr(C, "MINE_LAUNCHER_TOOL", 58)): float(
        getattr(C, "MINE_LAUNCHER_PROJECTILE_SPEED", 75.0)
    ),
}
_DEFAULT_LAUNCH_SPEED = 150.0
_SPEED_HEADROOM_SCALE = 1.5
_SPEED_HEADROOM_ADD = 20.0

# Longest legal client fuse per timed (bounce) tool. A fully cooked grenade
# legitimately arrives with fuse 0 (it explodes in the hand), so the floor is
# zero; the origin check above keeps that blast at the thrower.
_MAX_FUSES = {
    int(C.GRENADE_TOOL): float(getattr(C, "GRENADE_EXPLOSION_FUSE", 2.5)),
    int(getattr(C, "CLASSIC_GRENADE_TOOL", 31)): float(
        getattr(C, "CLASSIC_GRENADE_EXPLOSION_FUSE", 3.0)
    ),
    int(getattr(C, "ANTIPERSONNEL_GRENADE_TOOL", 32)): float(
        getattr(C, "ANTIPERSONNEL_GRENADE_EXPLOSION_FUSE", 2.5)
    ),
    int(getattr(C, "STICKY_GRENADE_TOOL", 57)): float(
        getattr(C, "STICKY_GRENADE_STICK_FUSE", 5.0)
    ),
}
_DEFAULT_MAX_FUSE = 10.0


def max_launch_speed(tool_id: int) -> float:
    """Largest accepted launch speed for ``tool_id`` (blocks per second)."""

    base = _LAUNCH_SPEEDS.get(int(tool_id), _DEFAULT_LAUNCH_SPEED)
    return base * _SPEED_HEADROOM_SCALE + _SPEED_HEADROOM_ADD


def max_fuse(tool_id: int) -> float:
    """Longest accepted client fuse for ``tool_id`` in seconds."""

    return _MAX_FUSES.get(int(tool_id), _DEFAULT_MAX_FUSE)


class OrientedActionService:
    """Validate, spawn, consume, and replicate one oriented item use.

    This method runs on the gameplay thread and delegates projectile creation
    to the existing authoritative engine. Malformed primitive values, invalid
    class/loadout/tool state, cadence, or empty stock fail without consuming
    inventory.
    """

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server

    def use(
        self,
        player: "Player",
        *,
        tool_id: int,
        position: tuple[float, float, float],
        velocity: tuple[float, float, float],
        fuse: float,
        loop: int | None = None,
    ) -> bool:
        """Submit one packet-equivalent oriented projectile action."""

        tool_id = int(tool_id)
        if not active_tool_authorized(player, tool_id):
            return False
        launch = self._validated_launch(
            player, tool_id, position, velocity, fuse,
            world=getattr(self.server, "world_manager", None),
            loop=loop,
        )
        if launch is None:
            from server import anticheat

            anticheat.report(
                self.server, player, "launch_rejected", tool=tool_id,
            )
            return False
        position, velocity, fuse = launch
        now = time.monotonic()
        can_use = getattr(player, "can_use_oriented_item", None)
        if callable(can_use) and not can_use(tool_id, now):
            return False

        from shared.packet import UseOrientedItem

        packet = UseOrientedItem()
        packet.loop_count = int(getattr(self.server, "loop_count", 0))
        packet.player_id = int(getattr(player, "id", 0))
        packet.tool = tool_id
        packet.value = float(fuse)
        packet.position = tuple(float(value) for value in position)
        packet.velocity = tuple(float(value) for value in velocity)
        if self.server.spawn_grenade(player, packet) is False:
            return False
        consume = getattr(player, "consume_oriented_item", None)
        if callable(consume) and not consume(tool_id, now):
            return False
        player.disguised = False
        # Throwing or launching anything ends this life's spawn protection.
        end_protection = getattr(player, "end_spawn_protection", None)
        if callable(end_protection):
            end_protection()
        return True

    @staticmethod
    def _validated_launch(
        player, tool_id, position, velocity, fuse, *, world=None, loop=None
    ):
        """Return a sanitized (position, velocity, fuse) or ``None``.

        The client's spawn point must be near a server-simulated eye of the
        thrower (hard LAUNCH_ORIGIN_TOLERANCE + movement slack) and, beyond
        the muzzle radius, in voxel line of sight of it -- a forged packet
        could otherwise detonate anywhere, or behind a wall. The speed is
        clamped to the tool's stock launch speed plus headroom, and the fuse
        to the tool's legal range.
        """

        from server.combat_runtime import lag_slack, reference_eyes, segment_clear

        try:
            position = tuple(float(value) for value in position)
            velocity = tuple(float(value) for value in velocity)
            fuse = float(fuse)
            eye = tuple(float(value) for value in player.eye)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
        if len(position) != 3 or len(velocity) != 3 or len(eye) != 3:
            return None
        if not all(
            math.isfinite(value) for value in (*position, *velocity, *eye, fuse)
        ):
            return None
        at_loop, eyes = reference_eyes(player, loop)
        if not eyes:
            eyes = [eye]
        slack = 0.0 if at_loop is not None else lag_slack(player)
        nearest = min(math.dist(position, candidate) for candidate in eyes)
        if nearest > LAUNCH_ORIGIN_TOLERANCE + slack:
            return None
        if nearest > LAUNCH_MUZZLE_RADIUS and not any(
            segment_clear(
                world, candidate, position, shrink_end=LAUNCH_LOS_END_SHRINK
            )
            for candidate in eyes
        ):
            return None
        speed = math.sqrt(sum(value * value for value in velocity))
        limit = max_launch_speed(tool_id)
        if speed > limit:
            scale = limit / speed
            velocity = tuple(value * scale for value in velocity)
        fuse = max(0.0, min(fuse, max_fuse(tool_id)))
        return position, velocity, fuse
