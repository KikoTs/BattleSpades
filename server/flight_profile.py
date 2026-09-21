"""Negotiated BattleSpades flight balance; retail arithmetic stays unchanged."""
from __future__ import annotations

from dataclasses import dataclass
import struct

CAPABILITY = b"BSCF\x01"
PROFILE_MAGIC = b"BSFP\x01"


@dataclass(frozen=True)
class FlightProfile:
    drain: tuple[float, float, float] = (75.0, 17.0, 18.0)
    refill: tuple[float, float, float] = (10.0, 9.0, 3.0)
    grounded_refill_only: bool = False
    refill_idle_seconds: float = 0.0
    descending_parachute_only: bool = False

    def encode(self) -> bytes:
        flags = int(self.grounded_refill_only) | (int(self.descending_parachute_only) << 1)
        return PROFILE_MAGIC + struct.pack(
            "<B7H", flags, round(self.refill_idle_seconds * 64),
            *(round(value * 64) for value in self.drain + self.refill),
        )


RETAIL_FLIGHT = FlightProfile()
# Requested local balance: longer finite flights, faster recharge on landing.
# No airborne recharge, including released/tapped thrust or an exhausted hold.
BALANCED_FLIGHT = FlightProfile((30.0, 9.0, 7.5), (20.0, 20.0, 20.0), True, 1.0, True)


def profile_for(player) -> FlightProfile:
    return getattr(getattr(player, "connection", None), "flight_profile", RETAIL_FLIGHT)


def ticket_has_flight_capability(packet: bytes) -> bool:
    """An explicit trailer outside the ticket; never part of the XOR/auth key."""
    if len(packet) < 5 or packet[0] != 105:
        return False
    length = struct.unpack_from("<i", packet, 1)[0]
    return 0 <= length <= 2048 and packet[5 + length:] == CAPABILITY
