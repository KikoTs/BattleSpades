"""Negotiated BattleSpades flight balance; stock owners keep retail arithmetic.

Two negotiated versions exist. Version 1 (``BSCF\\x01``/``BSFP\\x01``) only
changes fuel policy. Version 2 (``BSCF\\x02``/``BSFP\\x02``) additionally
carries two native-mover tunings that the native client predicts exactly:

* ``engineer_flight_accel``: the air-acceleration factor of an *active*
  Engineer pack. Stock ``world.pyd`` uses 0.1 (0x10012DC8), which keeps a
  thrusting Engineer at 40% of its walking speed (2.24 blocks/s walking,
  4 blocks/s sprinting). Version 2 uses 0.25: the Engineer flies at its
  own ground walk/sprint speed (5.6 / 10 blocks/s).
* ``canopy_gravity_scale`` / ``canopy_free_fall_floor``: stock canopy gravity
  is 0.05 (0x10012EFD), a 1.6 blocks/s terminal that a chute opened near the
  top of a fall approaches from rest over seconds. Version 2 uses 0.15625
  (5 blocks/s terminal) and lets a slow body fall with ordinary gravity until
  it reaches that terminal; a fast body brakes exactly as the stock canopy.

Stock clients, bots and version-1 native clients keep the stock values.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import struct

CAPABILITY = b"BSCF\x01"
CAPABILITY_V2 = b"BSCF\x02"
PROFILE_MAGIC = b"BSFP\x01"
PROFILE_MAGIC_V2 = b"BSFP\x02"

# Stock world.pyd literals (float32 values, as the mover stores them).
RETAIL_ENGINEER_FLIGHT_ACCEL = 0.1
RETAIL_CANOPY_GRAVITY_SCALE = 0.05


@dataclass(frozen=True)
class FlightProfile:
    drain: tuple[float, float, float] = (75.0, 17.0, 18.0)
    refill: tuple[float, float, float] = (10.0, 9.0, 3.0)
    grounded_refill_only: bool = False
    refill_idle_seconds: float = 0.0
    descending_parachute_only: bool = False
    version: int = 1
    engineer_flight_accel: float = RETAIL_ENGINEER_FLIGHT_ACCEL
    canopy_gravity_scale: float = RETAIL_CANOPY_GRAVITY_SCALE
    canopy_free_fall_floor: bool = False

    def encode(self) -> bytes:
        flags = int(self.grounded_refill_only) | (int(self.descending_parachute_only) << 1)
        rates = tuple(round(value * 64) for value in self.drain + self.refill)
        idle = round(self.refill_idle_seconds * 64)
        if self.version < 2:
            return PROFILE_MAGIC + struct.pack("<B7H", flags, idle, *rates)
        flags |= int(self.canopy_free_fall_floor) << 2
        # Mover tunings in exact 1/1024 units (0.25 = 256, 0.15625 = 160).
        return PROFILE_MAGIC_V2 + struct.pack(
            "<B9H", flags, idle, *rates,
            round(self.engineer_flight_accel * 1024),
            round(self.canopy_gravity_scale * 1024),
        )


RETAIL_FLIGHT = FlightProfile()
# Requested local balance: longer finite flights, faster recharge on landing.
# No airborne recharge, including released/tapped thrust or an exhausted hold.
BALANCED_FLIGHT = FlightProfile((30.0, 9.0, 7.5), (20.0, 20.0, 20.0), True, 1.0, True)
# Version 2 adds Engineer flight speed and a canopy that does not stall a
# slow fall (2026-10-01 request). Both values are exact in float32 and in the
# 1/1024 wire unit, so server and native prediction use identical numbers.
BALANCED_FLIGHT_V2 = replace(
    BALANCED_FLIGHT,
    version=2,
    engineer_flight_accel=0.25,
    canopy_gravity_scale=0.15625,
    canopy_free_fall_floor=True,
)


def profile_for(player) -> FlightProfile:
    return getattr(getattr(player, "connection", None), "flight_profile", RETAIL_FLIGHT)


def apply_mover_tuning(world_object, profile: FlightProfile) -> None:
    """Copy the profile's native-mover tunings onto one world.Player."""
    world_object.engineer_flight_accel = float(profile.engineer_flight_accel)
    world_object.parachute_gravity_scale = float(profile.canopy_gravity_scale)
    world_object.parachute_free_fall_floor = bool(profile.canopy_free_fall_floor)


def canopy_vz_step(vz: float, dt: float, gravity: float, profile: FlightProfile) -> float:
    """One canopy frame of vertical speed (float64 model of the native step)."""
    divisor = 1.0 + dt
    stepped = (vz + dt * gravity * float(profile.canopy_gravity_scale)) / divisor
    if profile.canopy_free_fall_floor:
        terminal = float(profile.canopy_gravity_scale) * gravity
        stepped = max(stepped, min((vz + dt * gravity) / divisor, terminal))
    return stepped


def ticket_flight_capability(packet: bytes) -> int:
    """Negotiated flight version from the trailer outside the ticket (0 = stock)."""
    if len(packet) < 5 or packet[0] != 105:
        return 0
    length = struct.unpack_from("<i", packet, 1)[0]
    if not 0 <= length <= 2048:
        return 0
    trailer = packet[5 + length:]
    if trailer == CAPABILITY_V2:
        return 2
    if trailer == CAPABILITY:
        return 1
    return 0


def ticket_has_flight_capability(packet: bytes) -> bool:
    """An explicit trailer outside the ticket; never part of the XOR/auth key."""
    return ticket_flight_capability(packet) > 0


def profile_for_capability(version: int) -> FlightProfile:
    if version >= 2:
        return BALANCED_FLIGHT_V2
    if version == 1:
        return BALANCED_FLIGHT
    return RETAIL_FLIGHT
